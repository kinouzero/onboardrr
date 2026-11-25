"""Inspect or repair existing storage on a Linux device during IP sync."""

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


def run(*args):
    return subprocess.run(args, capture_output=True, text=True, timeout=12)


def inspect_storage(request):
    address, name = request["storage_ip"], request["device"]
    fstab = Path("/etc/fstab")
    original = fstab.read_text()
    lines = original.splitlines(keepends=True)
    mounts = {}
    dedicated = False
    export = f"/onboardrr-{name}"
    legacy = f"/docker/komodo/data/{name}"
    periphery_root = Path(f"/share{legacy}")
    nas_names = set(request["storage_names"]) | {address}
    for index, line in enumerate(lines):
        fields = line.split()
        if len(fields) < 4 or line.lstrip().startswith("#"):
            continue
        source, target, kind = fields[:3]
        target = Path(target)
        if kind in {"nfs", "nfs4"} and target == periphery_root and source in {
            f"{host}:{export}" for host in nas_names
        }:
            dedicated = True
            desired = f"{address}:{export}"
        else:
            continue
        if target in mounts:
            raise ValueError(f"Multiple fstab entries exist for {target}.")
        active = run("findmnt", "-n", "--mountpoint", str(target), "-o", "SOURCE,FSTYPE")
        if active.returncode not in {0, 1} or (active.returncode == 0 and active.stdout.strip() not in {
            f"{endpoint} {fstype}" for endpoint in (source, desired)
            for fstype in {"nfs", "nfs4"}
        }):
            raise ValueError(f"Unexpected mount at {target}.")
        mounts[target] = active.returncode == 0
        if source != desired:
            lines[index] = re.sub(r"^(\s*)\S+", lambda match: match[1] + desired, line, count=1)

    agent = run("docker", "inspect", "--type", "container", "komodo-agent") if shutil.which("docker") else None
    container = json.loads(agent.stdout)[0] if agent and agent.returncode == 0 else {}
    if agent and agent.returncode and "No such" not in agent.stderr:
        raise ValueError("Could not inspect the existing Docker Periphery container.")
    paths = {export} if dedicated else set()
    agent_uses_nfs = dedicated and any(mount["Source"] == str(periphery_root)
                                      for mount in container.get("Mounts", []))
    for mount in container.get("Mounts", []):
        if mount.get("Type") != "volume":
            continue
        volume = run("docker", "volume", "inspect", mount["Name"])
        if volume.returncode:
            raise ValueError("Could not inspect the Periphery volume.")
        options = json.loads(volume.stdout)[0].get("Options") or {}
        source = options.get("device", "")
        if (options.get("type") in {"nfs", "nfs4"} and source in {f":{legacy}", f":{export}"}
                and f"addr={address}" in options.get("o", "").split(",")):
            paths.add(source[1:])
            agent_uses_nfs = True
    restart_agent = bool(container and agent_uses_nfs and not container["State"]["Running"])
    replacement = "".join(lines)
    changed = replacement != original or any(not active for active in mounts.values())
    action = request["action"]
    nfs_client = None
    if paths:
        route = run("ip", "-j", "route", "get", address)
        if route.returncode or not (nfs_client := json.loads(route.stdout)[0].get("prefsrc")):
            raise ValueError("Could not verify the local IPv4 used to reach the NAS.")
    if action == "repair" and changed:
        repair_mounts(fstab, original, replacement, mounts, name)
    if action == "start" and restart_agent:
        result = run("docker", "start", "komodo-agent")
        if result.returncode:
            raise ValueError("Docker could not start Periphery after the NFS update.")
    return {"mounts_pending": changed, "nfs_exports": sorted(paths), "start_periphery": restart_agent,
            "nfs_client": nfs_client}


def repair_mounts(fstab, original, replacement, mounts, name):
    for target, active in mounts.items():
        if active:
            continue
        if target.is_symlink() or not target.is_dir():
            raise ValueError(f"Missing or invalid mount directory {target}.")
        entries = list(target.iterdir())
        workspace = target / name
        if entries and not (target.name == "compose" and entries == [workspace] and workspace.is_dir()
                            and not workspace.is_symlink() and not any(workspace.iterdir())):
            raise ValueError(f"Local data would be hidden by the mount at {target}.")
    completed = []
    with tempfile.TemporaryDirectory(prefix=".onboardrr-fstab-", dir=fstab.parent) as directory:
        candidate = Path(directory) / "fstab"
        candidate.write_text(replacement)
        shutil.copystat(fstab, candidate)
        try:
            for target, active in mounts.items():
                if not active:
                    result = run("mount", "-T", str(candidate), str(target))
                    if result.returncode:
                        raise ValueError(f"Could not mount {target} from the primary NAS.")
                    completed.append(target)
            if fstab.read_text() != original:
                raise ValueError("fstab changed while verifying the mounts.")
            if original != replacement:
                candidate.replace(fstab)
        except BaseException:
            for target in reversed(completed):
                run("umount", str(target))
            raise
    if original != replacement and run("systemctl", "daemon-reload").returncode:
        raise ValueError("Mounts are active, but systemd could not reload the new fstab.")


if __name__ == "__main__":
    try:
        print(json.dumps(inspect_storage(json.load(sys.stdin))))
    except ValueError as error:
        print(json.dumps({"error": str(error)}))
    except (OSError, subprocess.SubprocessError):
        print(json.dumps({"error": "The host storage check failed or timed out."}))
