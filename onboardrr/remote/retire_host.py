"""Detach a Linux host's recorded Goku mounts without deleting data."""

import json
import os
import pwd
import re
import shutil
import stat
import subprocess
import sys
from hashlib import sha256
from pathlib import Path
from tempfile import NamedTemporaryFile


def unescape(value):
    return re.sub(rb"\\([0-7]{3})", lambda match: bytes([int(match[1], 8)]), value)


def fstab_plan(contents, mounts):
    lines = contents.splitlines(keepends=True)
    matches = {}
    for index, line in enumerate(lines):
        fields = line.split()
        if len(fields) < 2 or line.lstrip().startswith(b"#"):
            continue
        target = os.fsdecode(unescape(fields[1]))
        if target not in mounts:
            continue
        source, filesystem = mounts[target]
        if (len(fields) < 3 or target in matches
                or (os.fsdecode(unescape(fields[0])), os.fsdecode(fields[2])) != (source, filesystem)):
            raise ValueError("A recorded mount has a conflicting or duplicate fstab entry.")
        matches[target] = index
    removed = set(matches.values())
    return b"".join(line for index, line in enumerate(lines) if index not in removed)


def run(*args, absent=False):
    result = subprocess.run(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=15)
    if absent and result.returncode == 1 and not result.stderr:
        return None
    if result.returncode != 0:
        raise ValueError(f"{args[0]} failed during host detachment.")
    return result.stdout


def inspect_mount(target, expected):
    data = run("findmnt", "--json", "--output", "SOURCE,FSTYPE", "--mountpoint", target, absent=True)
    if data is None:
        return False
    filesystems = json.loads(data)["filesystems"]
    if len(filesystems) != 1 or (filesystems[0]["source"], filesystems[0]["fstype"]) != tuple(expected):
        raise ValueError("An active mount does not match the expected Goku source and filesystem.")
    return True


def matches_outbound(container, files, outbound_file, device):
    if not outbound_file or len(files) != 1 or container.get("Name") != "/komodo-agent":
        return False
    legacy = Path(outbound_file)
    workspace = legacy.parents[3] / ".docker/compose" / device / "periphery/compose.yaml"
    canonical = legacy.parents[3] / ".docker/compose/compose.komodo.yml"
    if files[0] not in {str(legacy), str(workspace), str(canonical)}:
        return False
    environment = set(container["Config"].get("Env") or [])
    data = f"/share/docker/komodo/data/{device}"
    keys = str(Path(outbound_file).parent / "keys")
    bindings = {(mount.get("Source"), mount.get("Destination")) for mount in container["Mounts"]
                if mount.get("Type") == "bind"}
    return (
        {f"PERIPHERY_CONNECT_AS={device}", "PERIPHERY_SERVER_ENABLED=false",
         "PERIPHERY_PRIVATE_KEY=file:/config/keys/periphery.key"} <= environment
        and {(data, data), (keys, "/config/keys")} <= bindings
        and not container["HostConfig"].get("PortBindings")
    )


def inspect_containers(mounts, compose_file, stop_periphery, *, outbound_file=None, device=None,
                       owns_outbound=False):
    ids = run("docker", "ps", "-aq").decode().split()
    containers = json.loads(run("docker", "inspect", *ids)) if ids else []
    agent = None
    attached = False
    for container in containers:
        labels = container["Config"].get("Labels") or {}
        files = labels.get("com.docker.compose.project.config_files", "").split(",")
        agent_labels = (
            labels.get("com.docker.compose.project") == "komodo"
            and labels.get("com.docker.compose.service") == "komodo-agent"
        )
        inbound = agent_labels and compose_file and compose_file in files
        outbound = agent_labels and matches_outbound(container, files, outbound_file, device)
        periphery = inbound or outbound
        can_stop = bool(inbound or (outbound and owns_outbound))
        if periphery:
            if agent:
                raise ValueError("Several running containers match the recorded Periphery deployment.")
            agent = {
                "id": container["Id"],
                "running": container["State"]["Running"],
                "auto_restart": container["HostConfig"]["RestartPolicy"]["Name"] not in {"no", ""},
                "can_stop": can_stop,
            }
        if not container["State"]["Running"]:
            continue
        if can_stop and stop_periphery:
            continue
        for mount in container["Mounts"]:
            source = mount.get("Source")
            if source and any(Path(source).is_relative_to(path) or Path(path).is_relative_to(source)
                              for path in mounts):
                attached = True
    return agent, attached


def backup_and_replace(path, before, after, *, label, expected_uid=None):
    if before == after:
        return None
    metadata = path.lstat()
    if (not stat.S_ISREG(metadata.st_mode) or path.read_bytes() != before
            or (expected_uid is not None and (metadata.st_uid != expected_uid or metadata.st_nlink != 1))):
        raise ValueError(f"The {label} file changed during retirement.")
    with NamedTemporaryFile(prefix=f"onboardrr-{label}-backup-", dir=path.parent, delete=False) as backup:
        backup.write(before)
        backup.flush()
        os.fsync(backup.fileno())
    with NamedTemporaryFile(prefix=f".onboardrr-{label}-", dir=path.parent, delete=False) as updated:
        temporary = Path(updated.name)
        try:
            updated.write(after)
            updated.flush()
            shutil.copystat(path, temporary)
            os.fchown(updated.fileno(), metadata.st_uid, metadata.st_gid)
            os.fsync(updated.fileno())
            current = path.lstat()
            if ((current.st_dev, current.st_ino, current.st_ctime_ns)
                    != (metadata.st_dev, metadata.st_ino, metadata.st_ctime_ns)
                    or path.read_bytes() != before):
                raise ValueError(f"The {label} file changed during retirement.")
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)
    return backup.name


def retire_shell_aliases(path, name, profile, *, apply):
    if not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", name):
        raise ValueError("Invalid device name for shell retirement.")
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return {"status": "absent"}
    if (not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.getuid() or metadata.st_nlink != 1
            or metadata.st_size > 1048576):
        raise ValueError("The operator's .bashrc must be a regular file owned by the SSH account.")
    before = path.read_bytes()
    lines = before.splitlines(keepends=True)
    text = [line.rstrip(b"\r\n") for line in lines]
    begins = [index for index, line in enumerate(text) if line == b"# BEGIN ONBOARDRR MANAGED BLOCK"]
    ends = [index for index, line in enumerate(text) if line == b"# END ONBOARDRR MANAGED BLOCK"]
    if not begins and not ends:
        return {"status": "absent"}
    if len(begins) != 1 or len(ends) != 1 or begins[0] >= ends[0]:
        return {"status": "review"}
    start, end = begins[0], ends[0]
    expected = [f'alias cdcompose=\'cd "$HOME/.docker/compose/{name}"\''.encode()]
    if profile == "server":
        expected.append(b'alias cdconfig=\'cd "$HOME/.config/docker"\'')
    legacy_dcu = f'alias dcu=\'bash "$HOME/scripts/docker-env-generate-and-up.sh" {name}\''.encode()
    managed_dcu = b"alias dcu='/usr/local/bin/onboardrr-dcu'"
    current = [b'alias cdcompose=\'cd "$HOME/.docker/compose"\'', *expected[1:]]
    if text[start + 1:end] not in (expected, [*expected, legacy_dcu], [*expected, managed_dcu], [*current, managed_dcu]):
        return {"status": "review"}
    if not apply:
        return {"status": "eligible"}
    after = b"".join(lines[:start] + lines[end + 1:])
    backup = backup_and_replace(path, before, after, label="bashrc", expected_uid=os.getuid())
    return {"status": "removed", "backup": backup}


def private_smb_file(path, limit):
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return None, None
    with os.fdopen(descriptor, "rb") as stream:
        info = os.fstat(stream.fileno())
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_nlink != 1
                or stat.S_IMODE(info.st_mode) not in {0o400, 0o600} or info.st_size > limit):
            raise ValueError("The managed SMB file must be private, root-owned and regular.")
        content = stream.read(limit + 1)
        if len(content) > limit:
            raise ValueError("The managed SMB file exceeds its expected size.")
    return content, (info.st_dev, info.st_ino, info.st_ctime_ns)


def fstab_uses_smb_credentials(contents, path):
    for line in contents.splitlines():
        fields = line.split()
        if len(fields) < 4 or fields[0].startswith(b"#"):
            continue
        for option in unescape(fields[3]).split(b","):
            key, _, value = option.partition(b"=")
            if key in {b"cred", b"credentials"}:
                candidate = Path(os.fsdecode(value))
                if candidate.is_absolute() and candidate.resolve() == path:
                    return True
    return False


def retire_smb_file(path, device_id, fstab, managed_targets, *, apply):
    try:
        parent = path.parent.lstat()
    except FileNotFoundError:
        return {"status": "absent"}
    if not stat.S_ISDIR(parent.st_mode) or parent.st_uid != 0 or stat.S_IMODE(parent.st_mode) != 0o700:
        raise ValueError("The SMB credential directory must be private and root-owned.")
    marker = path.with_name(path.name + ".onboardrr")
    provenance, marker_identity = private_smb_file(marker, 1024)
    if provenance is None:
        try:
            path.lstat()
        except FileNotFoundError:
            return {"status": "absent"}
        return {"status": "unverified"}
    try:
        ownership = json.loads(provenance)
    except ValueError:
        return {"status": "unverified"}
    if (not isinstance(ownership, dict) or ownership.get("device_id") != device_id
            or not isinstance(ownership.get("sha256"), str) or not re.fullmatch(r"[0-9a-f]{64}", ownership["sha256"])):
        return {"status": "unverified"}
    credential, credential_identity = private_smb_file(path, 16384)
    if credential is not None and sha256(credential).hexdigest() != ownership["sha256"]:
        return {"status": "changed"}
    if fstab_uses_smb_credentials(fstab.read_bytes(), path):
        return {"status": "in-use"}
    data = run("findmnt", "--json", "--list", "--types", "cifs,smb3", "--output", "TARGET", absent=True)
    if data is not None and any(item["target"] in managed_targets for item in json.loads(data)["filesystems"]):
        return {"status": "in-use"}
    if not apply:
        return {"status": "eligible"}
    for expected_path, identity in ((path, credential_identity), (marker, marker_identity)):
        if identity is None:
            continue
        observed = expected_path.lstat()
        if (observed.st_dev, observed.st_ino, observed.st_ctime_ns) != identity:
            raise ValueError("The SMB credential file or its ownership marker changed during retirement.")
        expected_path.unlink()
    return {"status": "removed" if credential is not None else "absent"}


def network_status(network, stopping_agent=None):
    name = network["name"]
    ids = run("docker", "network", "ls", "--no-trunc", "--filter", f"name=^{name}$", "--format", "{{.ID}}")
    found = ids.decode().split()
    if not found:
        return "absent", None
    if len(found) != 1 or found[0] not in network["ids"]:
        return "identity-mismatch", None
    identifier = found[0]
    details, = json.loads(run("docker", "network", "inspect", identifier))
    if (details.get("Name") != name or details.get("Id") != identifier or details.get("Driver") != "bridge"
            or details.get("Scope") != "local"
            or (details.get("Labels") or {}).get("onboardrr.device-id") != network["device_id"]):
        return "identity-mismatch", None
    attached = set(details.get("Containers") or {})
    if attached - {stopping_agent}:
        return "in-use", identifier
    return "eligible", identifier


def detach(path, request):
    if not stat.S_ISREG(path.lstat().st_mode):
        raise ValueError("The fstab file must be a regular file.")
    mounts = request["mounts"]
    before = path.read_bytes()
    after = fstab_plan(before, mounts)
    active = [target for target, expected in mounts.items() if inspect_mount(target, expected)]
    outbound_file = request.get("outbound_file")
    if not outbound_file and (operator := request.get("outbound_operator")):
        outbound_file = str(Path(pwd.getpwnam(operator).pw_dir) / ".config/onboardrr/periphery/compose.yaml")
    agent, attached = inspect_containers(
        mounts, request.get("compose_file"), request["stop_periphery"], outbound_file=outbound_file,
        device=request.get("device"), owns_outbound=request.get("owns_outbound", False),
    )
    blockers = []
    if agent and (agent["running"] or agent["auto_restart"]) and not request["stop_periphery"]:
        blockers.append("periphery-running")
    if agent and request["stop_periphery"] and not agent["can_stop"]:
        blockers.append("periphery-not-owned")
    if attached:
        blockers.append("container-uses-mount")
    report = {"active_mounts": active, "fstab_change": before != after, "blockers": blockers,
              "periphery_running": bool(agent and agent["running"])}
    network = request.get("network")
    if network:
        stopping_agent = agent["id"] if agent and agent["can_stop"] and request["stop_periphery"] else None
        status, selected_id = network_status(network, stopping_agent)
        report["network"] = status
        if status not in {"eligible", "absent"}:
            blockers.append(f"docker-network-{status}")
    if request["apply"] and not blockers:
        if agent and request["stop_periphery"]:
            if agent["auto_restart"]:
                run("docker", "update", "--restart=no", "--", agent["id"])
            if agent["running"]:
                run("docker", "stop", "--", agent["id"])
        for target in active:
            run("umount", "--", target)
            if inspect_mount(target, mounts[target]):
                raise ValueError("A recorded mount is still active.")
        report["fstab_backup"] = backup_and_replace(path, before, after, label="fstab")
        if mounts and Path("/run/systemd/system").is_dir():
            run("systemctl", "daemon-reload")
        if network and report["network"] == "eligible":
            status, current_id = network_status(network)
            if status == "eligible" and current_id == selected_id:
                run("docker", "network", "rm", "--", selected_id)
                status = "removed" if network_status(network)[0] == "absent" else "identity-mismatch"
            elif status == "eligible":
                status = "identity-mismatch"
            report["network"] = status
            if status not in {"removed", "absent"}:
                blockers.append(f"docker-network-{status}")
        report["status"] = "detached"
    return report


if __name__ == "__main__":
    try:
        request = json.loads(sys.argv[1]) if len(sys.argv) == 2 else json.load(sys.stdin)
        if request.get("operation") == "shell":
            report = retire_shell_aliases(Path.home() / ".bashrc", request["device"], request["profile"],
                                          apply=request["apply"])
        elif request.get("operation") == "smb":
            name = request["device"]
            if not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", name) or os.geteuid() != 0:
                raise ValueError("SMB retirement requires a valid device name and sudo.")
            home = Path(pwd.getpwnam(request["operator"]).pw_dir)
            targets = {str(home / item) for item in (".docker/compose", "dev", "scripts", "downloads")}
            targets.add("/mnt/goku_plex")
            report = retire_smb_file(Path("/etc/onboardrr") / f"{name}-goku.smb", request["device_id"],
                                     Path("/etc/fstab"), targets, apply=request["apply"])
        else:
            report = detach(Path("/etc/fstab"), request)
        json.dump(report, sys.stdout)
    except ValueError as error:
        json.dump({"error": str(error)}, sys.stdout)
    except (OSError, subprocess.SubprocessError):
        json.dump({"error": "A host detachment operation failed."}, sys.stdout)
