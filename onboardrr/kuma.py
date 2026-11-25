"""Resolve Kuma's Docker connection for generated Compose manifests."""

import json
import os

from . import ssh
from .http_json import HttpStatusError
from .pangolin_network import destination_ipv4
from .secrets import configured_openbao


def kuma_on_host(client, *, entware=False):
    try:
        return json.loads(ssh.script(client, "kuma.py", None, entware=entware))["local"]
    except (KeyError, ValueError) as error:
        raise ValueError("Could not inspect the target Docker engine for Uptime Kuma.") from error


def kuma_tcp_values(device):
    port = os.environ.get("ONBOARDRR_DOCKER_API_PORT")
    if not port and (bao := configured_openbao(mount="services")) is not None:
        try:
            port, = bao.read("docker/API_PORT", "value")
        except HttpStatusError as error:
            if error.status != 404:
                raise
    port = str(port or 2380)
    if not port.isdecimal() or not 1 <= int(port) <= 65535:
        raise ValueError("Configure a valid Docker API port for Uptime Kuma.")
    return {"HOST_IP": destination_ipv4(device, None), "DOCKER_API_PORT": str(int(port))}
