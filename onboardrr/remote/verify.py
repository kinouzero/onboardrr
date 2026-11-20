"""Report Docker and mount readiness without reading configuration contents."""

import json
import re
import subprocess
import sys
from pathlib import Path


def output(*args):
    try:
        return subprocess.check_output(args, stderr=subprocess.DEVNULL, timeout=5, text=True).strip()
    except (OSError, subprocess.SubprocessError):
        return ""


request = json.load(sys.stdin)
if request.get("mount_check_only"):
    result = {}
    compose_mount = str(Path.home() / ".docker" / "compose") if request.get("include_compose") else None
    mounts = [*request["mounts"], compose_mount] if compose_mount else request["mounts"]
    for path in dict.fromkeys(mounts):
        command = ["findmnt", "--noheadings", "--mountpoint", path]
        active = subprocess.run(command, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=5)
        persistent = subprocess.run(
            [*command, "--fstab"], stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=5,
        )
        result[path] = all(item.returncode == 1 and not item.stderr for item in (active, persistent))
    report = {"detached": result}
    if compose_mount:
        report["compose_mount"] = compose_mount
    json.dump(report, sys.stdout)
    sys.exit()

mounts = {path: output("findmnt", "-n", "-o", "FSTYPE", "--mountpoint", path) for path in request["mounts"]}
docker = output("docker", "version", "--format", "{{.Server.Version}}")
compose = output("docker", "compose", "version", "--short")
projects = {}
for name in request["projects"]:
    containers = output("docker", "ps", "-aq", "--filter", "label=com.docker.compose.project=" + name).split()
    states = output(
        "docker", "inspect", "--format",
        "{{.State.Status}} {{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}", *containers,
    ).splitlines() if containers else []
    projects[name] = bool(containers and len(states) == len(containers)
                          and all(state in {"running healthy", "running none"} for state in states))
version = re.compile(r"v?[0-9]+\.[0-9]+\.[0-9]+[-+0-9a-zA-Z.]*")
nas_device = request.get("nas_device")
nas_storage = {}
if nas_device:
    mounts_json = output("docker", "inspect", "--format", "{{json .Mounts}}", "komodo-agent")
    expected = f"/share/docker/komodo/data/{nas_device}"
    local = request.get("local_storage") is True
    workspace = Path.home() / ".docker/compose"
    nas_storage["nas_storage"] = any(
        item.get("Destination") == expected and item.get("RW") is True and (
            (item.get("Type") == "bind" and item.get("Source")
             and Path(item["Source"]).resolve() == Path(expected).resolve()) if local else
            (item.get("Type") == "volume" and item.get("Name") == "komodo_onboardrr-periphery-data")
        ) for item in json.loads(mounts_json or "[]")
    )
    if local:
        nas_storage["nas_shares"] = workspace.is_symlink() and workspace.resolve() == Path("/share/compose").resolve()
    else:
        filesystems = {}
        for line in Path("/proc/self/mounts").read_text().splitlines():
            _, target, kind, *_ = line.split()
            target = re.sub(r"\\([0-7]{3})", lambda match: chr(int(match[1], 8)), target)
            filesystems[target] = kind
        nas_storage["nas_shares"] = (filesystems.get(str(workspace.resolve())) in {"cifs", "smb3"}
                                     and filesystems.get(str(Path(expected).resolve())) in {"nfs", "nfs4"})
json.dump({
    "mounts": {path: fstype if fstype in {"cifs", "smb3", "nfs", "nfs4"} else None
               for path, fstype in mounts.items()},
    "docker": docker if version.fullmatch(docker) else None,
    "compose": compose if version.fullmatch(compose) else None,
    "projects": projects,
    **nas_storage,
}, sys.stdout)
