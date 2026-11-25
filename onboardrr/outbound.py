"""Recover an existing outbound Periphery registration using the enrolled SSH identity."""

import json
import re
import time
from base64 import b64decode
from pathlib import PurePosixPath
from urllib.parse import urlsplit

from .inventory import NAS_PROFILE
from .komodo import MONGO_ID, REGISTRATION_STEP
from .kuma import kuma_on_host, kuma_tcp_values
from .primary_storage import storage_endpoint
from .runs import Ownership, Resource, ResourceKind
from .storage import device_lock


def x25519_key(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9+/]{59}=", value):
        raise ValueError("Komodo and Periphery must provide valid X25519 public keys.")
    der = b64decode(value, validate=True)
    if len(der) != 44 or not der.startswith(bytes.fromhex("302a300506032b656e032100")):
        raise ValueError("Komodo and Periphery must provide valid X25519 public keys.")
    return value


def outbound_server(komodo, name, identifier, *, bootstrap_host=None):
    server = komodo._request("/read", "GetServer", {"server": identifier})
    config = server["config"]
    address, enabled = config.get("address"), config.get("enabled")
    valid = address == "" and enabled is True
    if bootstrap_host and enabled is False and isinstance(address, str):
        endpoint = urlsplit(address)
        valid = not address or (
            endpoint.scheme == "wss" and endpoint.hostname == bootstrap_host
            and endpoint.username is None and not (endpoint.path or endpoint.query or endpoint.fragment)
        )
    if (server.get("_id", {}).get("$oid") != identifier or server.get("name") != name
            or server.get("template") is not False or not valid or config.get("insecure_tls") is not False):
        raise ValueError("The expected Komodo registration is not an enabled, verified outbound server.")
    return server


def advance_recovery(runs, run, server_id, name, compose_mount, network_id=None, *, nas=False):
    known = {(item["kind"], item["reference"]) for item in run["resources"]}
    compose_path = f"/share/compose/{name}"
    data_path = f"/share/docker/komodo/data/{name}"
    owned_network = bool(network_id and re.fullmatch(r"[0-9a-f]{64}", network_id))
    network_reference = f"{name}/{network_id}" if owned_network else name
    stages = {
        REGISTRATION_STEP: [(ResourceKind.KOMODO_SERVER, server_id)],
        "goku-files": [(ResourceKind.GOKU_DIRECTORY, compose_path), (ResourceKind.GOKU_DIRECTORY, data_path),
                       (ResourceKind.GOKU_FILE, f"{compose_path}/docker-compose.komodo.yml"),
                       (ResourceKind.GOKU_FILE, f"{compose_path}/.env")],
        "linux": [] if nas else [(ResourceKind.MOUNT, data_path),
                                 (ResourceKind.DOCKER_NETWORK, network_reference)],
        "periphery": [(ResourceKind.DOCKER_NETWORK, network_reference)] if nas and owned_network else [],
    }
    while run["next_step"] in stages:
        step = run["next_step"]
        for kind, reference in stages[step]:
            if (kind, reference) not in known:
                ownership = (Ownership.CREATED if kind == ResourceKind.DOCKER_NETWORK and owned_network
                             else Ownership.REUSED)
                runs.record_resource(run["id"], step, Resource(kind, reference, ownership))
        runs.complete_step(run["id"], step)
        run = runs.show(run["id"])
    return run["next_step"]


def recover_outbound(device, runs, komodo, *, known_hosts, public_key, cifs_source, nfs_source,
                     expected_version=None, server_id=None, apply=False, replace_agent_key=False,
                     bootstrap=False):
    from . import ssh

    nas = device["profile"] == NAS_PROFILE
    if not nas and not nfs_source:
        raise ValueError("Provide the expected Periphery NFS mount source.")
    core_version = komodo.version() if bootstrap or expected_version else None
    if bootstrap and expected_version is None:
        expected_version = core_version
    if not re.fullmatch(r"2\.\d+\.\d+", expected_version or "") or core_version != expected_version:
        raise ValueError("Confirm the observed Komodo Core 2.x version before recovering Periphery.")
    if replace_agent_key and not apply:
        raise ValueError("Use --replace-agent-key with --apply.")
    if bootstrap and replace_agent_key:
        raise ValueError("Bootstrap and key replacement must be run separately.")
    name = device["name"]
    with device_lock(runs.database, device):
        histories = runs.list(name)
        if not histories or histories[0]["next_step"] == "ssh":
            raise ValueError("Complete SSH enrollment before recovering Periphery.")
        run = runs.show(histories[0]["id"])
        recorded_ids = {
            item["reference"] for history in histories for item in runs.show(history["id"])["resources"]
            if item["kind"] == ResourceKind.KOMODO_SERVER
        }
        if server_id is None and len(recorded_ids) == 1:
            server_id = next(iter(recorded_ids))
        if not server_id or not MONGO_ID.fullmatch(server_id):
            raise ValueError("Provide the verified Komodo server ID when history does not identify one server.")
        if any(item["reference"] != server_id for item in run["resources"]
               if item["kind"] == ResourceKind.KOMODO_SERVER):
            raise ValueError("The Komodo server does not match the recorded registration.")
        if bootstrap and (run["next_step"] != "periphery" or not any(
            item["kind"] == ResourceKind.KOMODO_SERVER and item["reference"] == server_id
            and item["ownership"] == Ownership.CREATED for item in run["resources"]
        )):
            raise ValueError("Bootstrap requires a server created by this run at the Periphery step.")
        bootstrap_host = device["host"] if bootstrap else None
        server = outbound_server(komodo, name, server_id, bootstrap_host=bootstrap_host)
        core_key = x25519_key(komodo._request("/read", "GetCoreInfo").get("public_key"))
        host = komodo.service.host
        authority = f"[{host}]" if ":" in host else host
        if komodo.service.port not in (None, 443):
            authority += f":{komodo.service.port}"
        payload = {"device": name, "device_id": device["id"], "version": expected_version,
                   "core_url": f"https://{authority}",
                   "core_public_key": core_key, "nfs_source": nfs_source,
                   "bootstrap": bootstrap}
        if nas:
            if run["next_step"] in {REGISTRATION_STEP, "nas-prepare", "goku-files", "linux"}:
                raise ValueError("Complete the Goku storage checks before recovering QNAP Periphery.")
            payload["nas"] = True
            payload["local_storage"] = name == storage_endpoint(runs.database).name
        ssh_options = {"entware": True} if nas else {}
        with ssh.agent_identity(ssh.load_public_key(public_key)) as agent:
            with ssh.connect(device, known_hosts, key=agent) as client:
                def remote(action, **fields):
                    request = {**payload, "action": action, **fields}
                    result = json.loads(ssh.script(client, "reconcile.py", json.dumps(request).encode(),
                                                   timeout=500 if action == "start" else 80, **ssh_options))
                    if "compose_mount" not in result:
                        raise ValueError(result.get("error", "Outbound Periphery host checks or startup failed."))
                    return result

                observed = remote("inspect")
                mount = PurePosixPath(observed["compose_mount"])
                if not mount.is_absolute() or mount.parts[-2:] != (".docker", "compose") or ".." in mount.parts:
                    raise ValueError("The target did not report a valid Compose mount.")
                local_key = x25519_key(observed["public_key"]) if observed["public_key"] else None
                registered_key = server.get("info", {}).get("public_key")
                needs_replacement = not local_key or local_key != registered_key
                if bootstrap and ((registered_key and needs_replacement)
                                  or (server["config"]["enabled"] and needs_replacement)):
                    raise ValueError("The recorded Komodo key does not match the device. Use Periphery recovery.")
                report = {"device": name, "server_id": server_id, "next_step": run["next_step"],
                          "agent_running": observed["running"],
                          "requires_key_replacement": needs_replacement and not bootstrap}
                if not apply:
                    return report
                if needs_replacement and not (replace_agent_key or bootstrap):
                    raise ValueError("Replacing the Komodo agent key requires --replace-agent-key.")
                if needs_replacement and not bootstrap and komodo._request(
                    "/read", "GetServerState", {"server": server_id},
                ).get("status") != "NotOk":
                    raise ValueError("Replace the agent key only when the existing Komodo server is offline.")
                if bootstrap or not observed["running"] or needs_replacement:
                    payload["kuma_local"] = kuma_on_host(client, entware=nas)
                    if not payload["kuma_local"]:
                        values = kuma_tcp_values(device)
                        payload["docker_endpoint"] = f"http://{values['HOST_IP']}:{values['DOCKER_API_PORT']}"
                    prepared = remote("stage")
                    observed = prepared
                    local_key = x25519_key(prepared["public_key"])
                    current = outbound_server(komodo, name, server_id, bootstrap_host=bootstrap_host)
                    latest = current.get("info", {}).get("public_key")
                    if latest != local_key:
                        if latest != registered_key or not (
                            replace_agent_key or (bootstrap and not latest and not current["config"]["enabled"])
                        ):
                            raise ValueError("The Komodo agent key changed during recovery.")
                        if not bootstrap and komodo._request(
                            "/read", "GetServerState", {"server": server_id},
                        ).get("status") != "NotOk":
                            raise ValueError("Replace the agent key only when the existing Komodo server is offline.")
                        komodo._request("/write", "UpdateServerPublicKey", {"server": server_id, "public_key": local_key})
                    if bootstrap and not current["config"]["enabled"]:
                        komodo._request("/write", "UpdateServer", {
                            "id": server_id, "config": {"address": "", "enabled": True},
                        })
                    if bootstrap and outbound_server(komodo, name, server_id).get("info", {}).get("public_key") != local_key:
                        raise ValueError("Komodo did not save the prepared agent key.")
                    if not prepared["running"] and remote("start", agent_public_key=local_key)["running"] is not True:
                        raise ValueError("Outbound Periphery did not start.")
        deadline = time.monotonic() + 60
        while komodo._request("/read", "GetServerState", {"server": server_id}).get("status") != "Ok":
            if time.monotonic() >= deadline:
                raise ValueError("Komodo did not report the recovered Periphery connection as ready.")
            time.sleep(2)
        active = komodo._request("/read", "GetPeripheryInformation", {"server": server_id})
        if active.get("public_key") != local_key or active.get("version") != expected_version:
            raise ValueError("The active Komodo connection does not match the recovered agent.")
        return {**report, "agent_running": True, "status": "ready",
                "next_step": advance_recovery(runs, run, server_id, name, str(mount), observed.get("owned_network"),
                                              **({"nas": True} if nas else {}))}
