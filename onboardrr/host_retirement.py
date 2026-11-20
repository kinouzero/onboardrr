"""Select owned Linux mounts and detach them over verified SSH."""

import json
import re
import shlex
from pathlib import PurePosixPath

from .inventory import NAS_PROFILE
from .periphery import COMPOSE, TEMPLATE
from .retirement import ensure_stacks_detached, retirement_plan
from .runs import ResourceKind
from .storage import device_lock


def run_host_retirement(device, known_hosts, public_key, request, *, privileged, password):
    from . import ssh

    payload = json.dumps(request).encode()
    key = ssh.load_public_key(public_key)
    source = (ssh.REMOTE / "retire_host.py").read_text()
    command = "python3 -c " + shlex.quote(source)
    if privileged and device["ssh_user"] != "root":
        if password:
            # Keep the request separate from sudo's password stream.
            command = (
                "IFS= read -r onboardrr_password || exit 1; "
                "onboardrr_request=$(cat) || exit 1; "
                "printf '%s\\n' \"$onboardrr_password\" | sudo -S -p '' -- "
                + command + ' "$onboardrr_request"'
            )
            payload = (password + "\n").encode() + payload
        else:
            command = "sudo -n -- " + command
    with ssh.agent_identity(key) as agent, ssh.connect(device, known_hosts, key=agent) as client:
        status, output = ssh.command(client, command, payload, timeout=100)
    if status != 0:
        raise ValueError("Could not run host retirement over SSH. Verify sudo access when required.")
    report = json.loads(output)
    if "error" in report:
        raise ValueError(report["error"])
    return {"device": device["name"], "mode": "apply" if request["apply"] else "preview", **report}


def retire_smb_credentials(database, device, known_hosts, public_key, *, apply, password):
    if device["profile"] == NAS_PROFILE:
        raise ValueError("SMB credential retirement supports Linux device profiles.")
    with device_lock(database, device):
        return run_host_retirement(
            device, known_hosts, public_key, {"operation": "smb", "device": device["name"], "device_id": device["id"],
                                             "operator": device["ssh_user"], "apply": apply},
            privileged=True, password=password,
        )


def retire_shell(database, device, known_hosts, public_key, *, apply):
    from . import ssh

    if device["profile"] == NAS_PROFILE:
        raise ValueError("Shell shortcut retirement supports Linux device profiles.")
    request = json.dumps({"operation": "shell", "device": device["name"], "profile": device["profile"],
                          "apply": apply}).encode()
    key = ssh.load_public_key(public_key)
    with device_lock(database, device), ssh.agent_identity(key) as agent:
        with ssh.connect(device, known_hosts, key=agent) as client:
            report = json.loads(ssh.script(client, "retire_host.py", request))
    if "error" in report:
        raise ValueError(report["error"])
    return {"device": device["name"], "mode": "apply" if apply else "preview", **report}


def retire_host(database, device, komodo, known_hosts, public_key, *,
                cifs_source, nfs_source, stop_periphery, apply, password, remove_network=False):
    if device["profile"] == NAS_PROFILE:
        raise ValueError("Host detachment supports Linux device profiles.")
    name = device["name"]
    for source in (cifs_source, nfs_source):
        if source and any(ord(character) < 32 or ord(character) == 127 for character in source):
            raise ValueError("Mount sources must not contain control characters.")
    with device_lock(database, device):
        actions = retirement_plan(database, name)["actions"]
        owned = {item["reference"] for item in actions
                 if item["kind"] == ResourceKind.MOUNT and item["action"] == "remove"}
        cifs = sorted(path for path in owned if PurePosixPath(path).is_absolute()
                      and PurePosixPath(path).parts[-2:] == (".docker", "compose"))
        nfs_path = f"/share/docker/komodo/data/{name}"
        mounts = {}
        if nfs_source:
            if nfs_path not in owned:
                raise ValueError("The run does not record ownership of the selected NFS mount.")
            mounts[nfs_path] = (nfs_source, "nfs4")
        compose_file = None
        if cifs_source:
            if len(cifs) != 1 or ".." in PurePosixPath(cifs[0]).parts:
                raise ValueError("The run does not identify a single owned Compose mount.")
            mounts[cifs[0]] = (cifs_source, "cifs")
        network = None
        if remove_network:
            prefix = name + "/"
            references = [item["reference"].removeprefix(prefix) for item in actions
                          if item["kind"] == ResourceKind.DOCKER_NETWORK and item["action"] == "remove"
                          and item["reference"].startswith(prefix)]
            if not references or any(not re.fullmatch(r"[0-9a-f]{64}", reference) for reference in references):
                raise ValueError("The run history does not record an owned Docker network with a verified ID.")
            network = {"name": name, "ids": references, "device_id": device["id"]}
        if not mounts and network is None and not stop_periphery:
            raise ValueError("Supply the expected source for at least one owned Goku mount.")
        outbound_file = None
        owns_outbound = False
        registered_mounts = sorted({
            item["reference"] for item in actions if item["kind"] == ResourceKind.MOUNT
            and PurePosixPath(item["reference"]).is_absolute()
            and PurePosixPath(item["reference"]).parts[-2:] == (".docker", "compose")
        })
        if len(registered_mounts) == 1 and ".." not in PurePosixPath(registered_mounts[0]).parts:
            staged_path = f"/share/compose/{name}/{COMPOSE}"
            if any(item["kind"] == ResourceKind.GOKU_FILE and item["reference"] == staged_path
                   and item["action"] == "remove" for item in actions):
                compose_file = f"{registered_mounts[0]}/{name}/{COMPOSE}"
            recorded_environment = any(item["kind"] == ResourceKind.GOKU_FILE
                                       and item["reference"] in {f"/share/compose/{name}/.env",
                                                                 f"/share/compose/{name}/{TEMPLATE}"} for item in actions)
            if recorded_environment:
                home = PurePosixPath(registered_mounts[0]).parents[1]
                outbound_file = str(home / ".config/onboardrr/periphery/compose.yaml")
                servers = [item for item in actions if item["kind"] == ResourceKind.KOMODO_SERVER]
                owns_outbound = len(servers) == 1 and servers[0]["action"] == "remove"
        if stop_periphery and not (compose_file or outbound_file):
            raise ValueError("The run does not record a Periphery deployment that can be checked for retirement.")
        if apply:
            ensure_stacks_detached(komodo, name, actions)

        return run_host_retirement(
            device, known_hosts, public_key, {
                "mounts": mounts, "compose_file": compose_file, "stop_periphery": stop_periphery, "apply": apply,
                "device": name, "outbound_file": outbound_file, "owns_outbound": owns_outbound, "network": network,
            }, privileged=apply, password=password,
        )
