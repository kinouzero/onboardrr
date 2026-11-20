"""Verify a recorded Linux onboarding run against the host and central services."""

import json

from .inventory import NAS_PROFILE
from .pangolin import enrollment_site, validate_site
from .primary_storage import storage_endpoint
from .runs import Failure, Ownership, ResourceKind
from .stacks import BASELINE
from .storage import device_lock


STEP = "verify"


def host_snapshot(device, known_hosts, public_key_path, mounts, projects, *, storage_name="goku"):
    from . import ssh

    public_key = ssh.load_public_key(public_key_path)
    with ssh.agent_identity(public_key) as key, ssh.connect(device, known_hosts, key=key) as client:
        nas = device.get("profile") == NAS_PROFILE
        payload = json.dumps({"mounts": mounts, "projects": projects, **(
            {"nas_device": device["name"], "local_storage": device["name"] == storage_name} if nas else {}
        )}).encode()
        return json.loads(ssh.script(client, "verify.py", payload, timeout=50, **({"entware": True} if nas else {})))


def verify_run(device, runs, komodo, pangolin, *, known_hosts, public_key_path, apply=False, monitoring=None):
    nas = device.get("profile") == NAS_PROFILE
    name = device["name"]
    with device_lock(runs.database, device):
        history = runs.list(name)
        if not history:
            raise ValueError("The device has no onboarding run to verify.")
        run = runs.show(history[0]["id"])
        step = run["next_step"]
        resources = run["resources"]
        checks = {}
        projects = {}
        existing_storage = False
        mounts = sorted(item["reference"] for item in resources if item["kind"] == ResourceKind.MOUNT)
        try:
            servers = komodo._request("/read", "ListServers", {"limit": 0})
            stack_list = komodo._request("/read", "ListStacks", {"limit": 0})
            server_ids = {item["reference"] for item in resources if item["kind"] == ResourceKind.KOMODO_SERVER}
            stack_ids = {item["reference"] for item in resources if item["kind"] == ResourceKind.KOMODO_STACK}
            matched = [item for item in servers if item["name"] == name and item["id"] in server_ids]
            server = matched[0] if len(matched) == 1 else None
            checks["periphery"] = bool(server and server["info"].get("state") == "Ok")
            existing_storage = bool(server and (nas or (server["info"].get("address") or "").startswith("https://")) and any(
                item["kind"] == ResourceKind.KOMODO_SERVER and item["reference"] == server["id"]
                and item["ownership"] == Ownership.REUSED for item in resources
            ))
            for kind in BASELINE:
                stack_name = f"{name}-{kind}"
                matches = [stack for stack in stack_list if stack["name"] == stack_name and stack["id"] in stack_ids]
                stack = matches[0] if len(matches) == 1 else None
                active = bool(server and stack and stack["info"].get("server_id") == server["id"]
                              and stack["info"].get("state", "").casefold() == "running")
                checks[f"stack:{kind}"] = active
                if active:
                    config = komodo._request("/read", "GetStack", {"stack": stack["id"]})["config"]
                    project = config.get("project_name")
                    if project:
                        projects[kind] = project
        except (OSError, ValueError):
            checks["komodo-api"] = False

        try:
            agent = nas and runs.component_completed(run["id"], "openbao-agent")
            project_names = set(projects.values())
            if agent:
                project_names.add("secrets")
            storage = {"storage_name": storage_endpoint(runs.database).name} if nas else {}
            host = host_snapshot(device, known_hosts, public_key_path, mounts, sorted(project_names), **storage)
            checks["ssh"] = True
            checks["docker"] = bool(host.get("docker") and host.get("compose"))
            types = set(host["mounts"].values())
            expected_mounts = all(host["mounts"].get(path) in {"cifs", "smb3", "nfs", "nfs4"} for path in mounts)
            goku_mounts = bool(types & {"cifs", "smb3"} and types & {"nfs", "nfs4"})
            if nas:
                checks["goku-storage"] = existing_storage or host.get("nas_storage") is True
                checks["nas-shares"] = host.get("nas_shares") is True
                if agent:
                    checks["openbao-agent"] = host["projects"].get("secrets") is True
            else:
                checks["mounts"] = expected_mounts and (existing_storage or goku_mounts)
            for kind in BASELINE:
                checks[f"containers:{kind}"] = host["projects"].get(projects.get(kind)) is True
        except (OSError, ValueError, KeyError, TypeError):
            host = {}
            checks["ssh"] = False

        try:
            site_ids = {item["reference"]: Ownership(item["ownership"]) for item in resources
                        if item["kind"] == ResourceKind.PANGOLIN_SITE}
            site = enrollment_site(pangolin, name, site_ids) if site_ids else None
            if site:
                validate_site(site, name, allow_display_name=site_ids.get(str(site["siteId"])) == Ownership.REUSED)
            checks["pangolin"] = bool(site and str(site["siteId"]) in site_ids and site.get("online") is True)
        except (OSError, ValueError):
            checks["pangolin"] = False

        ready = all(checks.values()) and step in {STEP, None}
        monitoring_result = {}
        if ready and monitoring:
            try:
                monitoring_result = monitoring(device, apply=apply)
                checks["monitoring"] = all(monitoring_result["checks"].values())
            except (OSError, ValueError, KeyError, TypeError) as error:
                checks["monitoring"] = False
                detail = str(error) if type(error) is ValueError else "Monitoring could not be verified."
                monitoring_result = {"error": detail}
            ready = checks["monitoring"]
        if apply and step == STEP:
            if ready:
                runs.complete_step(run["id"], STEP)
                step = None
            else:
                runs.mark_failed(run["id"], STEP, Failure.VERIFICATION)
        return {
            "device": name, "run_id": run["id"], "next_step": step,
            "status": "ready" if ready else "incomplete", "checks": checks,
            "mounts": host.get("mounts", {}), "docker_version": host.get("docker"),
            "compose_version": host.get("compose"),
            **({"monitoring": monitoring_result} if monitoring_result else {}),
        }
