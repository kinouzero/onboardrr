"""Check whether the running Komodo Core started with the rendered host IP."""

import json
import subprocess
import sys
import tomllib
from datetime import datetime
from pathlib import Path


RENDERED = "/run/komodo-openbao/rendered"
request = json.load(sys.stdin)
docker = ["docker", "--host", "unix:///var/run/docker.sock"]
running = subprocess.check_output([*docker, "ps", "--format", "{{.ID}} {{.Image}}"], text=True)
containers = []
for row in running.splitlines():
    identifier, image = row.split(None, 1)
    if image.split("@", 1)[0].rsplit(":", 1)[0].endswith("/komodo-core"):
        containers.append(identifier)
ready = False
if len(containers) == 1:
    core, = json.loads(subprocess.check_output([*docker, "inspect", containers[0]]))
    paths = next((row.partition("=")[2].split(",") for row in core["Config"]["Env"]
                  if row.startswith("KOMODO_CONFIG_PATHS=")), [])
    for mount in core["Mounts"]:
        if mount["Destination"] != RENDERED or f"{RENDERED}/core.config.toml" not in paths:
            continue
        path = Path(mount["Source"]) / "core.config.toml"
        try:
            with path.open("rb") as stream:
                config = tomllib.load(stream)
            started = datetime.fromisoformat(core["State"]["StartedAt"].replace("Z", "+00:00")).timestamp()
            ready = core["State"]["Running"] and started >= path.stat().st_mtime and (
                config.get("secrets", {}).get(request["key"]) == request["address"]
            )
        except (OSError, ValueError):
            pass
print(json.dumps({"ready": ready}))
