"""Inspect Komodo and prepare a server for Periphery enrollment."""

import json
import re
from hmac import compare_digest
from ipaddress import ip_address
from secrets import token_urlsafe
from urllib.parse import urlsplit

from .http_json import HttpStatusError, JsonService
from .inventory import NAS_PROFILE, PROFILE, SERVER_PROFILE
from .runs import Failure, Ownership, Resource, ResourceKind
from .storage import device_lock


REGISTRATION_STEP = "komodo-registration"
MONGO_ID = re.compile(r"[0-9a-f]{24}")
SERVER_TAGS = {PROFILE: "rpi", SERVER_PROFILE: "server", NAS_PROFILE: "nas"}


def add_resource_tags(api, kind, identifier, required):
    if not required:
        return
    lookup = {kind.lower(): identifier}
    current = api._request("/read", f"Get{kind}", lookup).get("tags", [])
    if set(required).issubset(current):
        return
    tags = list(dict.fromkeys([*current, *required]))
    api._request("/write", "UpdateResourceMeta", {
        "target": {"type": kind, "id": identifier},
        "tags": tags,
    })
    updated = api._request("/read", f"Get{kind}", lookup).get("tags", [])
    if not set(tags).issubset(updated):
        raise ValueError(f"Komodo did not save all {kind.lower()} tags.")


class Komodo:
    def __init__(self, url, key, secret):
        self.service = JsonService("Komodo", url)
        if not key or not secret:
            raise ValueError("Komodo API credentials are required.")
        self.headers = {
            "Content-Type": "application/json",
            "X-Api-Key": key,
            "X-Api-Secret": secret,
        }

    def _request(self, route, action, parameters=None):
        return self.service.request(
            "POST", route, headers=self.headers, operation=action,
            data={"type": action, "params": parameters or {}},
        )

    def version(self):
        result = self._request("/read", "GetVersion")
        version = result.get("version") if isinstance(result, dict) else None
        if not isinstance(version, str) or not version:
            raise ValueError("Komodo did not report its Core version.")
        return version

    def tag_id(self, name):
        def lookup():
            matches = [tag for tag in self._request("/read", "ListTags") if tag["name"] == name]
            if len(matches) > 1:
                raise ValueError(f"Multiple Komodo tags match {name}.")
            return next(iter(matches), None)

        tag = lookup()
        if tag is None:
            try:
                tag = self._request("/write", "CreateTag", {"name": name})
            except HttpStatusError:
                tag = lookup()
                if tag is None:
                    raise
        identifier = tag.get("_id", {}).get("$oid") or tag.get("id", "")
        if not MONGO_ID.fullmatch(identifier):
            raise ValueError(f"Komodo did not return a valid ID for tag {name}.")
        return identifier

    def reference(self, name):
        server = self._request("/read", "GetServer", {"server": name})
        config = server["config"]
        stacks = self._request("/read", "ListStacks", {"limit": 0})
        return {
            "core_version": self.version(),
            "server": {
                "name": server["name"],
                "address": config.get("address"),
                "tls_verification": config.get("insecure_tls") is False,
                "has_passkey_override": bool(config.get("passkey")),
            },
            "stack_templates": sorted(
                (
                    {"name": stack["name"], "id": stack["id"]}
                    for stack in stacks
                    if stack.get("template") is True
                ),
                key=lambda stack: stack["name"],
            ),
        }

    def existing_server(self, name, address):
        servers = self._request("/read", "ListServers", {"limit": 0})
        existing = None
        for server in servers:
            if server["name"].casefold() == name.casefold():
                if existing is not None:
                    raise ValueError("Multiple Komodo servers match this device name.")
                existing = server
            elif server["info"].get("address") == address:
                raise ValueError("Another Komodo server uses this Periphery address.")
        if existing and (existing.get("template") or existing["info"].get("address") != address):
            raise ValueError("The device name is already used by a different Komodo server.")
        return existing

    def register(self, name, address, passkey, *, allow_create=True):
        existing = self.existing_server(name, address)
        if existing:
            server = self._request("/read", "GetServer", {"server": existing["id"]})
            config = server["config"]
            if config.get("address") != address:
                raise ValueError("The Komodo server's address changed during registration.")
            if config.get("insecure_tls") is not False:
                raise ValueError("The existing Komodo server does not verify the Periphery certificate.")
            expected = config.get("passkey")
            if not isinstance(expected, str) or not compare_digest(expected.encode(), passkey.encode()):
                raise ValueError("Could not verify the existing server's Periphery passkey.")
            return existing["id"], Ownership.REUSED

        if not allow_create:
            raise ValueError("The previously registered Komodo server is missing.")

        server = self._request("/write", "CreateServer", {
            "name": name,
            "config": {
                "address": address,
                "enabled": False,
                "insecure_tls": False,
                "passkey": passkey,
            },
        })
        try:
            identifier = server["_id"]["$oid"]
        except (KeyError, TypeError) as error:
            raise ValueError("Komodo did not return the created server ID. Recheck registration before retrying.") from error
        if not isinstance(identifier, str) or not MONGO_ID.fullmatch(identifier):
            raise ValueError("Komodo returned an invalid server ID. Recheck registration before retrying.")
        return identifier, Ownership.CREATED


def periphery_address(host, port):
    if not 1 <= port <= 65535:
        raise ValueError("Periphery port must be between 1 and 65535.")
    host = f"[{host}]" if ":" in host else host
    return f"wss://{host}:{port}"


def legacy_periphery(komodo, device, port, known_hosts, public_key, bao):
    from . import ssh

    matches = [item for item in komodo._request("/read", "ListServers", {"limit": 0})
               if item["name"].casefold() == device["name"].casefold()]
    if len(matches) > 1:
        raise ValueError("Multiple Komodo servers match this device name.")
    if not matches:
        return None
    candidate = matches[0]
    raw_address = candidate["info"].get("address") or ""
    if not raw_address and device.get("profile") == NAS_PROFILE:
        from .outbound import outbound_server

        identifier = candidate["id"]
        if not MONGO_ID.fullmatch(identifier) or candidate["info"].get("state") != "Ok":
            raise ValueError("The existing QNAP Periphery agent is not ready.")
        outbound_server(komodo, device["name"], identifier)
        key = ssh.load_public_key(public_key)
        with ssh.agent_identity(key) as agent, ssh.connect(device, known_hosts, key=agent) as client:
            identity = json.loads(ssh.script(client, "periphery_identity.py",
                                            json.dumps({"outbound": device["name"]}).encode(), entware=True))
        verify_periphery_key(komodo, identifier, identity)
        return {"server_id": identifier, "address": "", "registration": "existing-outbound"}
    endpoint = urlsplit(raw_address)
    if endpoint.scheme != "https":
        return None
    if (candidate.get("template") or endpoint.port != port or endpoint.username is not None
            or endpoint.path not in {"", "/"} or endpoint.query or endpoint.fragment):
        raise ValueError("The existing Komodo HTTPS endpoint differs from the expected Periphery configuration.")
    try:
        expected_ip = ip_address(endpoint.hostname)
    except ValueError:
        raise ValueError("The existing Komodo HTTPS endpoint must specify the device's IP address.") from None
    identifier = candidate["id"]
    if not MONGO_ID.fullmatch(identifier) or candidate["info"].get("state") != "Ok":
        raise ValueError("The existing Komodo HTTPS agent is not ready.")
    config = komodo._request("/read", "GetServer", {"server": identifier})["config"]
    if config.get("address") != raw_address or config.get("enabled") is not True:
        raise ValueError("The existing Komodo HTTPS configuration changed during verification.")
    if bao is None:
        raise ValueError("Configure OpenBao to verify the existing Periphery passkey.")
    passkey, = bao.read(f"deployments/periphery/{device['name']}", "passkey")
    key = ssh.load_public_key(public_key)
    with ssh.agent_identity(key) as agent, ssh.connect(device, known_hosts, key=agent) as client:
        options = {"entware": True} if device.get("profile") == NAS_PROFILE else {}
        request = {"passkey": passkey, "address": str(expected_ip), **({"nas": True} if options else {})}
        identity = json.loads(ssh.script(client, "periphery_identity.py", json.dumps(request).encode(), **options))
    verify_periphery_key(komodo, identifier, identity)
    return {"server_id": identifier, "address": raw_address, "registration": "legacy-inbound"}


def verify_periphery_key(komodo, identifier, identity):
    from .outbound import x25519_key

    if "error" in identity:
        raise ValueError(identity["error"])
    active = komodo._request("/read", "GetPeripheryInformation", {"server": identifier})
    if (active.get("docker_connected") is not True
            or not compare_digest(x25519_key(active.get("public_key")), x25519_key(identity.get("public_key")))):
        raise ValueError("Komodo and the trusted SSH host report different Periphery identities.")


def adopt_legacy_periphery(komodo, device, version, port, known_hosts, public_key, bao, runs):
    if not version or komodo.version() != version:
        raise ValueError("Confirm the observed Komodo Core version before adopting the existing server.")
    with device_lock(runs.database, device):
        histories = runs.list(device["name"])
        if not histories or histories[0]["next_step"] in {"ssh", "nas-prepare"}:
            raise ValueError("Complete SSH enrollment before adopting the existing Komodo server.")
        run = runs.show(histories[0]["id"])
        verified = legacy_periphery(komodo, device, port, known_hosts, public_key, bao)
        if verified is None:
            return None
        identifier = verified["server_id"]
        if run["next_step"] == REGISTRATION_STEP:
            runs.record_resource(run["id"], REGISTRATION_STEP,
                                 Resource(ResourceKind.KOMODO_SERVER, identifier, Ownership.REUSED))
            runs.complete_step(run["id"], REGISTRATION_STEP)
            run = runs.show(run["id"])
        if not any(resource["kind"] == ResourceKind.KOMODO_SERVER
                   and resource["reference"] == identifier and resource["ownership"] == Ownership.REUSED
                   for resource in run["resources"]):
            raise ValueError("The current run does not own the existing Komodo adoption.")
        # An adopted agent keeps its existing storage and deployment.
        while run["next_step"] in {"goku-files", "linux", "periphery"}:
            if any(resource["kind"] in {ResourceKind.GOKU_DIRECTORY, ResourceKind.GOKU_FILE, ResourceKind.MOUNT}
                   and resource["ownership"] == Ownership.CREATED for resource in run["resources"]):
                raise ValueError("This run already created host storage. Review it before adopting the existing agent.")
            runs.complete_step(run["id"], run["next_step"])
            run = runs.show(run["id"])
        return {**run, **verified}


def create_periphery_passkey(bao, path):
    try:
        bao.create(path, {"passkey": token_urlsafe(48)})
    except HttpStatusError as write_error:
        if write_error.status not in {400, 403, 409}:
            raise
        try:
            return bao.read(path, "passkey")[0]
        except HttpStatusError as read_error:
            if read_error.status != 404:
                raise
        if write_error.status == 403:
            raise ValueError(f"The controller needs OpenBao create permission for {path}.") from None
        raise ValueError(f"OpenBao refused to create {path}. Check the KV entry in OpenBao.") from None
    return bao.read(path, "passkey")[0]


def register_device(komodo, device, address, version, passkey, runs, *, openbao=None):
    if not passkey and openbao is None:
        raise ValueError("Load the Periphery passkey before registering the server.")
    if not version or komodo.version() != version:
        raise ValueError("Confirm the observed Komodo Core version before registering the server.")
    with device_lock(runs.database, device):
        histories = runs.list(device["name"])
        if not histories or histories[0]["next_step"] in {"ssh", "nas-prepare"}:
            raise ValueError("Complete SSH enrollment before registering the server in Komodo.")
        run_id = histories[0]["id"]
        if openbao is not None:
            path = f"deployments/periphery/{device['name']}"
            try:
                passkey, = openbao.read(path, "passkey")
            except HttpStatusError as error:
                if error.status != 404:
                    raise
                if (histories[0]["next_step"] != REGISTRATION_STEP
                        or komodo.existing_server(device["name"], address) is not None):
                    raise ValueError(f"Restore the existing Periphery passkey in OpenBao at {path}.") from None
                passkey = create_periphery_passkey(openbao, path)
        if histories[0]["next_step"] != REGISTRATION_STEP:
            run = runs.show(run_id)
            recorded = {
                item["reference"] for item in run["resources"]
                if item["kind"] == ResourceKind.KOMODO_SERVER
            }
            if not recorded:
                raise ValueError("The current run has no recorded Komodo server.")
            server_id, _ = komodo.register(device["name"], address, passkey, allow_create=False)
            if server_id not in recorded:
                raise ValueError("The Komodo server does not match the recorded registration.")
            return run
        try:
            server_id, ownership = komodo.register(device["name"], address, passkey)
            prior = next(
                (
                    item for item in runs.show(run_id)["resources"]
                    if item["kind"] == ResourceKind.KOMODO_SERVER and item["reference"] == server_id
                ),
                None,
            )
            if prior:
                ownership = Ownership(prior["ownership"])
            runs.record_resource(
                run_id, REGISTRATION_STEP, Resource(ResourceKind.KOMODO_SERVER, server_id, ownership)
            )
            if ownership == Ownership.CREATED:
                tag_name = SERVER_TAGS[device["profile"]]
                add_resource_tags(komodo, "Server", server_id, [komodo.tag_id(tag_name)])
            runs.complete_step(run_id, REGISTRATION_STEP)
        except (OSError, ValueError):
            runs.mark_failed(run_id, REGISTRATION_STEP, Failure.KOMODO_REGISTER)
            raise
        return runs.show(run_id)
