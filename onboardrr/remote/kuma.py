"""Detect an Uptime Kuma container on the host's local Docker engine."""

import json
import os
import subprocess


def uptime_kuma_present(containers):
    for row in containers.splitlines():
        image, name, service = row.split("\t")
        repository = image.split("@", 1)[0].rsplit("/", 1)[-1].split(":", 1)[0]
        if "uptime-kuma" in (repository, name, service):
            return True
    return False


def inspect_local():
    environment = os.environ.copy()
    environment.pop("DOCKER_CONTEXT", None)
    containers = subprocess.check_output(
        ["docker", "--host", "unix:///var/run/docker.sock", "ps", "--all", "--format",
         '{{.Image}}\t{{.Names}}\t{{.Label "com.docker.compose.service"}}'],
        env=environment, stderr=subprocess.DEVNULL, text=True, timeout=12,
    )
    return uptime_kuma_present(containers)


if __name__ == "__main__":
    try:
        print(json.dumps({"local": inspect_local()}))
    except (OSError, ValueError, subprocess.SubprocessError):
        raise SystemExit("Could not inspect the local Docker engine for Uptime Kuma.")
