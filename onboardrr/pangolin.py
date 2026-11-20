"""Inspect and enroll device-specific Newt sites."""

import json
import re
import shlex
import subprocess
from hmac import compare_digest
from hashlib import sha256
from pathlib import Path

from .http_json import HttpStatusError, JsonService
from .inventory import HOST_LABEL, normalize_host
from .pangolin_network import configure_private_network, detach_private_host_routes, list_records, note_remaining_private_hosts
from .runs import Ownership, Resource, ResourceKind
from .runtime import system_environment
from .storage import device_lock


IDENTIFIER = re.compile(r"[a-zA-Z0-9_-]+")
VARIABLES = "variables"
STACK_STEP = "komodo-stacks"
SITE_STEP = "pangolin"


def newt_reference(identifier, secret):
    return sha256(json.dumps([identifier, secret], separators=(",", ":")).encode()).hexdigest()


def newt_record(bao, name):
    if bao.mount == "services":
        if not HOST_LABEL.fullmatch(name):
            raise ValueError("Use a valid device name for the Newt record.")
        return f"pangolin/newt/{name}", ("ID", "SECRET")
    prefix = name.upper().replace("-", "_")
    return VARIABLES, (f"{prefix}_NEWT_ID", f"{prefix}_NEWT_SECRET")


def read_newt_record(bao, path, fields):
    try:
        return bao.fields_with_version(path, *fields)
    except HttpStatusError as error:
        if bao.mount == "services" and error.status == 404:
            return {}, 0
        raise


class SshIntegration:
    def __init__(self, target):
        self.target = normalize_host(target)

    def request(self, method, path, *, headers, operation, data=None):
        script = Path(__file__).with_name("remote").joinpath("pangolin_api.py").read_text()
        try:
            reply = subprocess.run(
                [
                    "ssh", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes",
                    "-o", "ConnectTimeout=7", self.target, "python3 -c " + shlex.quote(script),
                ],
                input=json.dumps({"method": method, "path": path, "headers": headers, "data": data}),
                capture_output=True, text=True, timeout=25, check=True, env=system_environment(),
            )
            payload = json.loads(reply.stdout)
            status = payload["status"]
            if type(status) is not int:
                raise TypeError
        except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError):
            raise ValueError(f"Pangolin {operation} could not complete over SSH.") from None
        if not 200 <= status < 300:
            raise HttpStatusError("Pangolin", operation, status)
        return payload.get("response")


class Pangolin:
    def __init__(self, service, token, organization):
        if not token or any(not 33 <= ord(character) <= 126 for character in token):
            raise ValueError("A valid Pangolin API token is required.")
        if not IDENTIFIER.fullmatch(organization):
            raise ValueError("Use a valid Pangolin organization identifier.")
        self.service = service
        self.organization = organization
        self.prefix = f"/v1/org/{organization}"
        self.headers = {"Authorization": f"Bearer {token}"}

    def request(self, method, path, operation, data=None):
        return self.request_path(method, f"{self.prefix}/{path}", operation, data)

    def request_path(self, method, path, operation, data=None):
        arguments = {"headers": self.headers, "operation": operation}
        if data is not None:
            arguments.update(headers={**self.headers, "Content-Type": "application/json"}, data=data)
        response = self.service.request(method, path, **arguments)
        if not isinstance(response, dict) or not isinstance(response.get("data"), dict):
            raise ValueError(f"Pangolin did not return a valid response for {operation}.")
        return response["data"]

    def site(self, name, *, optional=False, operation="read site"):
        if not IDENTIFIER.fullmatch(name):
            raise ValueError("Use a valid Pangolin site identifier.")
        try:
            data = self.request("GET", f"site/{name}", operation)
        except HttpStatusError as error:
            if optional and error.status == 404:
                return None
            raise
        if type(data.get("siteId")) is not int or data["siteId"] < 1:
            raise ValueError("Pangolin did not return a valid site identifier.")
        return data

    def site_by_id(self, identifier):
        if not identifier.isascii() or not identifier.isdecimal() or int(identifier) < 1:
            raise ValueError("Use a valid numeric Pangolin site identifier.")
        try:
            result = self.service.request(
                "GET", f"/v1/site/{identifier}", headers=self.headers, operation="read site by ID",
            )
        except HttpStatusError as error:
            if error.status == 404:
                return None
            raise
        data = result.get("data")
        if not isinstance(data, dict) or str(data.get("siteId")) != identifier:
            raise ValueError("Pangolin did not return the expected site.")
        return data

    def site_resource_count(self, name, identifier):
        listing = self.request("GET", f"sites?query={name}&pageSize=25", "check site resource count")
        sites = listing.get("sites")
        if not isinstance(sites, list) or any(not isinstance(item, dict) for item in sites):
            return None
        matches = [item for item in sites if str(item.get("siteId")) == identifier]
        if len(matches) != 1:
            return None
        count = matches[0].get("resourceCount")
        if isinstance(count, str) and re.fullmatch(r"[0-9]{1,12}", count):
            count = int(count)
        return count if type(count) is int and count >= 0 else None

    def retire_site(self, name, identifier, *, detach_private_routes=False, owned_routes=None):
        site = self.site(name, optional=True)
        if not site or str(site["siteId"]) != identifier:
            if self.site_by_id(identifier):
                return "identity-mismatch"
            note_remaining_private_hosts(self, owned_routes)
            return "absent"
        if site.get("niceId") != name or site.get("type") != "newt":
            return "identity-mismatch"
        if site.get("online") is not False:
            return "site-online"
        count = self.site_resource_count(name, identifier)
        if count is None:
            return "site-dependencies-unknown"
        if count:
            if not detach_private_routes or not detach_private_host_routes(
                self, name, site["siteId"], count, owned_routes=owned_routes,
            ):
                return "site-has-dependencies"
            current = self.site(name, optional=True)
            if not current or current["siteId"] != site["siteId"] or current.get("type") != "newt":
                return "identity-mismatch"
            if current.get("online") is not False:
                return "site-online"
            count = self.site_resource_count(name, identifier)
            if count is None:
                return "site-dependencies-unknown"
            if count:
                return "site-has-dependencies"
        else:
            note_remaining_private_hosts(self, owned_routes)
        result = self.service.request(
            "DELETE", f"/v1/site/{identifier}?deleteResources=false",
            headers=self.headers, operation="retire Newt site",
        )
        if result.get("success") is not True:
            raise ValueError("Pangolin did not confirm site removal.")
        return "removed"


def safe_site(data):
    return {
        field: data[field]
        for field in ("siteId", "niceId", "name", "type", "online", "address", "exitNodeId")
        if field in data
    }


def reference_site(url, token, organization, site):
    api = Pangolin(JsonService("Pangolin", url), token, organization)
    return safe_site(api.site(site, operation="read reference site"))


def enroll_site(api, device, *, runs, bao=None, apply=False, ipv4=None, site_only=False, sync_routes=False):
    name = device["name"]
    if not apply:
        site = enrollment_site(api, name, {})
        if site:
            validate_site(site, name, allow_display_name=True)
        return {"action": "reuse" if site else "create", "site": safe_site(site) if site else None}

    with device_lock(runs.database, device):
        history = runs.list(name)
        if not history or history[0]["next_step"] == "ssh":
            raise ValueError("Complete SSH enrollment before enrolling in Pangolin.")
        if not site_only and history[0]["next_step"] not in (STACK_STEP, SITE_STEP, "verify", None):
            raise ValueError("Complete Periphery preparation before enrolling in Pangolin.")
        run = history[0]
        step = run["next_step"]
        owned = {
            entry["reference"]: Ownership(entry["ownership"])
            for entry in runs.show(run["id"])["resources"] if entry["kind"] == ResourceKind.PANGOLIN_SITE
        }
        site = enrollment_site(api, name, owned)
        if site:
            validate_site(site, name, allow_display_name=True)
        can_record = not sync_routes and step not in (SITE_STEP, "verify", None) and (site_only or step == STACK_STEP)
        if (owned or not can_record) and (not site or str(site["siteId"]) not in owned):
            raise ValueError("The Pangolin site does not match the site recorded by the onboarding run.")
        if bao is None:
            raise ValueError("Configure the OpenBao token for Komodo's Newt secret record.")
        path, keys = newt_record(bao, name)
        id_field, secret_field = keys
        fields, version = read_newt_record(bao, path, keys)
        if fields and (len(fields) != 2 or not all(isinstance(value, str) and value for value in fields.values())):
            raise ValueError("The device's Newt credential pair in OpenBao is incomplete or invalid.")

        action = "reuse"
        if not site:
            if not fields:
                defaults = api.request("GET", "pick-site-defaults", "pick Newt credentials")
                fields = {id_field: defaults.get("newtId"), secret_field: defaults.get("newtSecret")}
                if not all(isinstance(value, str) and value for value in fields.values()):
                    raise ValueError("Pangolin did not return Newt credentials.")
                if version == 0:
                    bao.create(path, fields)
                else:
                    bao.patch(path, fields, version=version)
                saved = bao.read(path, *keys)
                if saved != (fields[id_field], fields[secret_field]):
                    raise ValueError("The saved Newt credentials changed before site creation.")
                runs.record_resource(
                    run["id"], step,
                    Resource(ResourceKind.NEWT_CREDENTIALS, newt_reference(*saved), Ownership.CREATED),
                )
            api.request("PUT", "site", "create Newt site", {
                "name": name, "niceId": name, "type": "newt",
                "newtId": fields[id_field], "secret": fields[secret_field],
            })
            site = api.site(name)
            validate_site(site, name)
            action = "create"
        identifier = site.get("newtId")
        if not fields or not isinstance(identifier, str) or not compare_digest(
            identifier.encode(), fields[id_field].encode()
        ):
            raise ValueError("The existing Pangolin site does not match the Newt identity stored in OpenBao.")
        reference = str(site["siteId"])
        if can_record:
            ownership = owned.get(reference, Ownership.CREATED if action == "create" else Ownership.REUSED)
            runs.record_resource(run["id"], step, Resource(ResourceKind.PANGOLIN_SITE, reference, ownership))
        result = {"action": action, "site": safe_site(site)}
        if sync_routes and site.get("online") is not True:
            return {**result, "status": "incomplete", "blockers": ["Newt is not connected to Pangolin yet."]}
        if not site_only and (step == SITE_STEP or sync_routes) and site.get("online") is True:
            def record_private_host(reference):
                runs.record_resource(run["id"], step, Resource(
                    ResourceKind.PANGOLIN_PRIVATE_ROUTE, reference, Ownership.CREATED,
                ))

            result["private_network"] = configure_private_network(
                api, device, site, database=runs.database, ipv4=ipv4, apply=True,
                record_created=record_private_host,
            )
            if step == SITE_STEP:
                runs.complete_step(run["id"], step)
        return result


def enrollment_site(api, name, recorded):
    site = api.site(name, optional=True)
    if site:
        return site
    if recorded:
        reused = [identifier for identifier, ownership in recorded.items() if ownership == Ownership.REUSED]
        return api.site_by_id(reused[0]) if len(reused) == 1 else None
    sites = list_records(api, "sites", "sites", "siteId")
    matches = [site for site in sites.values() if str(site.get("name", "")).casefold() == name.casefold()]
    if len(matches) > 1:
        raise ValueError("Several Pangolin sites have the device's name. Check which site belongs to the host.")
    if matches:
        identifier = matches[0]["siteId"]
        site = api.site_by_id(str(identifier))
        if site is None:
            raise ValueError("The existing Pangolin site changed while checking its identity.")
        return site
    return None


def validate_site(site, name, *, allow_display_name=False):
    matches = site.get("niceId") == name or (
        allow_display_name and str(site.get("name", "")).casefold() == name.casefold()
    )
    if not matches or site.get("type") != "newt":
        raise ValueError("The Pangolin site identifier is in use by a different site.")
