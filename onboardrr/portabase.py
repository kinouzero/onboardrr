"""Synchronize a local Portabase agent and its dashboard projects."""

import json
import os
import re
from contextlib import nullcontext
from pathlib import Path
from time import monotonic, sleep
from uuid import UUID

from . import ssh
from .http_json import HttpStatusError, JsonService
from .inventory import NAS_PROFILE
from .secrets import configured_openbao, token_file
from .storage import connect, device_lock, provisioning_lock


COMPOSE_PROJECT = re.compile(r"[a-z0-9][a-z0-9_-]{0,127}")
GFS_DEFAULTS = {"hourly": 0, "daily": 7, "weekly": 4, "monthly": 12, "yearly": 3}


def uuid_string(value):
    if not isinstance(value, str):
        raise ValueError("Portabase returned an invalid resource ID.")
    return str(UUID(value))


class Portabase:
    def __init__(self, origin, token):
        self.service = JsonService("Portabase", origin)
        self.headers = {"x-api-key": token, "Content-Type": "application/json"}

    def request(self, method, path, data=None):
        return self.service.request(method, "/api/v1" + path, headers=self.headers, data=data,
                                    operation="synchronize backup sources")

    def rows(self, path):
        result = self.request("GET", path)
        rows = result.get("data") if isinstance(result, dict) else None
        if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
            raise ValueError("Portabase returned an invalid resource list.")
        return rows

    def resource(self, method, path, data=None):
        result = self.request(method, path, data)
        row = result.get("data") if isinstance(result, dict) else None
        if not isinstance(row, dict):
            raise ValueError("Portabase returned an invalid resource.")
        return row


def configured_portabase(origin, *, required=True):
    token = os.environ.get("ONBOARDRR_PORTABASE_API_TOKEN") or os.environ.get("PORTABASE_API_KEY")
    token_path = os.environ.get("ONBOARDRR_PORTABASE_API_TOKEN_FILE")
    if not token and token_path:
        token = token_file(Path(token_path).expanduser(), service="Portabase")
    if not token:
        bao = configured_openbao(mount="services")
        if bao is None:
            if not required:
                return None
            raise ValueError("Configure an API token for Portabase project assignment or backup cleanup.")
        try:
            fields, _ = bao.fields_with_version("portabase/onboardrr", "api_token")
        except HttpStatusError as error:
            if not required and error.status in {403, 404}:
                return None
            if error.status != 404:
                raise
            raise ValueError("Store the Portabase API token in services/portabase/onboardrr.") from None
        token = fields.get("api_token")
    if not isinstance(token, str) or not token or not all(33 <= ord(char) <= 126 for char in token):
        raise ValueError("Provide a valid Portabase API token.")
    return Portabase(origin, token)


def dashboard_sources(database, device):
    # Sources owned by the former browser workflow remain dashboard-managed.
    with connect(database) as connection:
        rows = connection.execute(
            "SELECT agent_id, source FROM portabase_dashboard_sources WHERE device_id = ?", (device["id"],)
        )
        sources = {}
        for row in rows:
            sources.setdefault(row["agent_id"], []).append(row["source"])
        return sources


def agent_report(client, nas, sources, *, apply=False):
    payload = json.dumps({"apply": apply, "dashboard_sources": sources}).encode()
    report = json.loads(ssh.script(client, "portabase_sync.py", payload, entware=nas, timeout=130))
    if report.get("error"):
        raise ValueError(report["error"])
    return report


def agent_catalog(api, agent_id, needed):
    catalog = {}
    for row in api.rows("/databases"):
        identifier = row.get("agentDatabaseId")
        if identifier not in needed or row.get("agentId") != agent_id:
            continue
        if identifier in catalog:
            raise ValueError("Portabase returned conflicting entries for a managed database.")
        catalog[identifier] = row
    return catalog


def project_catalog(api):
    projects = {}
    for organization in api.rows("/organizations"):
        organization_id = uuid_string(organization["id"])
        for row in api.rows(f"/organizations/{organization_id}/projects"):
            if row.get("isArchived") or row.get("deletedAt"):
                continue
            name = row.get("name")
            if isinstance(name, str) and name.strip():
                projects[uuid_string(row["id"])] = (name.strip(), organization_id)
    return projects


def available_projects(api):
    return {identifier: name for identifier, (name, _) in project_catalog(api).items()}


def channel_policy(api, path, channel_id_key, *, alerts=False):
    policies = []
    for row in api.rows(path):
        fields = {channel_id_key: uuid_string(row.get(channel_id_key)), "enabled": row["enabled"]}
        if alerts:
            fields["eventKinds"] = sorted(row["eventKinds"])
        policies.append(fields)
    return {"policies": sorted(policies, key=lambda policy: policy[channel_id_key])}


def project_policies(api, identifier):
    path = f"/projects/{uuid_string(identifier)}"
    row = api.resource("GET", path)
    retention = row.get("retentionPolicy")
    retention_policy = None
    if retention:
        retention_policy = {key: retention[key] for key in ("type", "count", "days")}
        retention_policy["gfs"] = {
            field: default if (value := retention.get(f"gfs{field.title()}")) is None else value
            for field, default in GFS_DEFAULTS.items()
        }
    return {
        "storage": channel_policy(api, path + "/storage-policy", "storageChannelId"),
        "alert": channel_policy(api, path + "/alert-policy", "notificationChannelId", alerts=True),
        "backup": {"backupPolicy": row.get("backupPolicy")},
        "retention": retention_policy,
    }


def backup_template(api, projects, name, agent_id):
    references = [(identifier, org) for identifier, (label, org) in projects.items() if label.casefold() == name.casefold()]
    if len(references) != 1:
        raise ValueError("Set portabase_policy_project to the unique name of an active Portabase project.")
    reference, organization = references[0]
    if not any(row.get("id") == agent_id and not row.get("isArchived")
               for row in api.rows(f"/organizations/{organization}/agents")):
        raise ValueError("The Portabase agent must belong to the backup template project's organization.")
    policies = project_policies(api, reference)
    if not policies["backup"]["backupPolicy"] or not policies["retention"]:
        raise ValueError("Set a backup schedule and retention policy on the Portabase template project.")
    for policy, channel_kind, key in (("storage", "storage", "storageChannelId"),
                                      ("alert", "notification", "notificationChannelId")):
        required = {row[key] for row in policies[policy]["policies"]}
        if required:
            channels = api.rows(f"/organizations/{organization}/{channel_kind}-channels")
            if required - {row.get("id") for row in channels}:
                raise ValueError(f"Make the Portabase template's {channel_kind} channels available to its organization.")
    return organization, policies


def create_project(api, organization, name, policies):
    project = api.resource("POST", f"/organizations/{organization}/projects", {"name": name})
    identifier = uuid_string(project.get("id"))
    if project.get("name") != name or project.get("organizationId") != organization:
        raise ValueError("Portabase did not confirm the new project's name and organization.")
    path = f"/projects/{identifier}"
    try:
        for policy, payload in policies.items():
            api.request("PUT", f"{path}/{policy}-policy", payload)
        if project_policies(api, identifier) != policies:
            raise ValueError("Portabase did not save the complete backup policy.")
    except (OSError, ValueError) as error:
        # Archive only a project created by this call and still empty.
        try:
            if not any(row.get("projectId") == identifier for row in api.rows("/databases")):
                api.request("DELETE", path)
        except (OSError, ValueError) as cleanup_error:
            raise ValueError(
                f"Could not verify cleanup for Portabase project {name} after policy setup failed. Check the project before retrying."
            ) from cleanup_error
        raise ValueError(f"Could not configure backup policies for new Portabase project {name}: {error}") from error
    return identifier


def project_choices(hints, catalog, names, *, create_missing=False):
    projects_by_name, projects_by_application = {}, {}
    for identifier, name in names.items():
        projects_by_name.setdefault(name.casefold(), set()).add(identifier)
    for identifier, hint in hints.items():
        project = catalog.get(identifier, {}).get("projectId")
        if project in names:
            application = (hint["compose"] or hint["stack"], hint["application"])
            projects_by_application.setdefault(application, set()).add(project)
    for identifier, hint in hints.items():
        application = (hint["compose"] or hint["stack"], hint["application"])
        matches = projects_by_name.get(hint["compose"]) or projects_by_application.get(application)
        if not matches and not (create_missing and COMPOSE_PROJECT.fullmatch(hint["compose"])):
            matches = projects_by_name.get(hint["stack"])
        yield identifier, matches or set()


def catalog_actions(api, report, *, project, purge, apply, policy_project=None):
    hints = report.get("project_hints", {})
    managed = set(report["managed"])
    needed = managed | hints.keys() | set(report["retired"].values())
    catalog = agent_catalog(api, report["agent_id"], needed)
    deleted = pending = 0
    busy, unavailable = [], []
    names, assignments, unmatched, new_projects = {}, [], [], {}
    candidates = {identifier for identifier in (managed if project else hints)
                  if not catalog.get(identifier, {}).get("projectId")}
    if project:
        choices = dict.fromkeys(candidates, project)
    elif candidates:
        projects = project_catalog(api)
        names = {identifier: name for identifier, (name, _) in projects.items()}
        choices = {}
        for identifier, matches in project_choices(hints, catalog, names, create_missing=bool(policy_project)):
            if identifier not in candidates:
                continue
            if len(matches) == 1:
                choices[identifier] = next(iter(matches))
            elif not matches and policy_project and COMPOSE_PROJECT.fullmatch(hints[identifier]["compose"]):
                new_projects[identifier] = hints[identifier]["compose"]
    else:
        choices = {}
    missing = (choices.keys() | new_projects.keys()) - catalog.keys()
    if apply and missing and report.get("added"):
        deadline = monotonic() + 20
        while missing and monotonic() < deadline:
            sleep(2)
            catalog = agent_catalog(api, report["agent_id"], needed)
            missing -= catalog.keys()

    project_plan = sorted({name for identifier, name in new_projects.items()
                           if not apply or (identifier in catalog and not catalog[identifier].get("projectId"))})
    created = {}
    if project_plan:
        organization, policies = backup_template(api, projects, policy_project, report["agent_id"])
        if apply:
            created = {name: create_project(api, organization, name, policies) for name in project_plan}
    for identifier in sorted(candidates, key=lambda item: (hints.get(item, {}).get("container", ""), item)):
        row = catalog.get(identifier, {})
        if row.get("projectId"):
            continue
        source = hints.get(identifier, {}).get("container") or row.get("name") or identifier
        new_project = new_projects.get(identifier)
        destination = choices.get(identifier) or created.get(new_project)
        if not destination and not new_project:
            unmatched.append(source)
            continue
        if not row:
            pending += 1
            if apply:
                continue
        elif apply:
            api.request("PATCH", f"/databases/{uuid_string(row['id'])}", {"projectId": destination})
        assignments.append({"source": source, "project": new_project or names.get(destination, destination)})

    if purge:
        for source, identifier in report["retired"].items():
            row = catalog.get(identifier)
            if not row:
                continue
            if not row.get("projectId"):
                unavailable.append(source)
                continue
            path = f"/databases/{uuid_string(row['id'])}/backup"
            backups = api.rows(path)
            if any(backup.get("status") in {"waiting", "ongoing"} for backup in backups):
                busy.append(source)
                continue
            for backup_id in [uuid_string(backup["id"]) for backup in backups]:
                if apply:
                    try:
                        api.request("DELETE", f"{path}/{backup_id}")
                    except HttpStatusError as error:
                        if error.status != 404:
                            raise
                        continue
                deleted += 1

    return {"projects_linked" if apply else "projects_to_link": len(assignments),
            "projects_created" if apply else "projects_to_create": project_plan,
            "project_assignments": assignments, "projects_unmatched": unmatched,
            "backups_deleted" if apply else "backups_to_delete": deleted,
            "awaiting_agent_report": pending, "backup_jobs_active": busy, "backup_cleanup_unavailable": unavailable}


def sync(database, device, public_key, known_hosts, *, apply, purge, project=None):
    project = project or os.environ.get("ONBOARDRR_PORTABASE_PROJECT_ID")
    project = uuid_string(project) if project else None
    policy_project = os.environ.get("ONBOARDRR_PORTABASE_POLICY_PROJECT")
    key = ssh.load_public_key(public_key.expanduser())
    nas = device["profile"] == NAS_PROFILE
    with device_lock(database, device), ssh.agent_identity(key) as identity:
        sources = dashboard_sources(database, device)
        with ssh.connect(device, known_hosts.expanduser(), key=identity) as client:
            report = agent_report(client, nas, sources)
            requires_api = purge or bool(project)
            api = (configured_portabase(os.environ.get("ONBOARDRR_PORTABASE_URL") or report["origin"],
                                        required=requires_api)
                   if requires_api or report["project_hints"] else None)
            if apply:
                if project:
                    api.request("GET", f"/projects/{project}")
                if purge:
                    api.rows("/databases")
                report = agent_report(client, nas, sources, apply=True)
            if apply and purge and report["retired"]:
                current = agent_report(client, nas, sources)
                if (current["agent_id"] != report["agent_id"] or
                        any(current["retired"].get(source) != identifier for source, identifier in report["retired"].items())):
                    raise ValueError("A retired source or its agent changed before backup cleanup. Run another preview.")
        if api:
            lock = provisioning_lock(database, "portabase-projects") if apply and policy_project else nullcontext()
            with lock:
                report.update(catalog_actions(api, report, project=project, purge=purge, apply=apply,
                                              policy_project=policy_project))
        elif report["project_hints"]:
            report["project_assignment_status"] = "Portabase API token unavailable."

    report.pop("managed")
    report.pop("project_hints")
    report.pop("origin")
    report.pop("agent_id")
    report["retired"] = sorted(report["retired"])
    return {"device": device["name"], "mode": "apply" if apply else "check", **report}
