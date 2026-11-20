"""Collect read-only facts on the managed Linux host."""

import json
import os
import platform
import shlex
import shutil
import socket
import sys
from pathlib import Path


def identity():
    fields = {}
    for line in Path("/etc/os-release").read_text().splitlines():
        key, separator, value = line.partition("=")
        if separator and key in {"ID", "VERSION_ID", "VERSION_CODENAME", "UBUNTU_CODENAME"}:
            tokens = shlex.split(value)
            fields[key] = tokens[0] if tokens else ""
    return fields


def reachable(host, port):
    try:
        with socket.create_connection((host, port), timeout=3):
            return True
    except OSError:
        return False


def main():
    endpoints = json.load(sys.stdin)
    home = Path.home()
    paths = (home / ".docker/compose", Path("/share/docker"))
    filesystem = os.statvfs(home)
    root = os.statvfs("/")
    print(json.dumps({
        "os": identity(),
        "architecture": platform.machine(),
        "hostname": socket.gethostname(),
        "uid": os.getuid(),
        "gid": os.getgid(),
        "home_free_bytes": filesystem.f_bavail * filesystem.f_frsize,
        "root_free_bytes": root.f_bavail * root.f_frsize,
        "docker_present": shutil.which("docker") is not None,
        "paths": {str(path): {"exists": path.exists(), "mounted": path.is_mount()} for path in paths},
        "services": {
            name: {str(port): reachable(host, port) for port in ports} if host else None
            for name, (host, ports) in endpoints.items()
        },
    }))


if __name__ == "__main__":
    main()
