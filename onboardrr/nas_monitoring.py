"""Build the native Container Station monitoring stack for a new NAS."""

import json
import re


SERVICES = {"wud": "docker-telemetry", "dozzle": "docker-telemetry",
            "prometheus": "node-exporter", "beszel": "beszel-agent"}


def compose_payload(address, ports, agent_key, *, component=None):
    if component in (None, "beszel") and not re.fullmatch(
        r"(?:ssh-ed25519|ssh-rsa|ecdsa-sha2-nistp(?:256|384|521)) [A-Za-z0-9+/]+=*(?: [^\r\n]+)?",
        agent_key or "",
    ):
        raise ValueError("Provide the Beszel hub's OpenSSH public key for new NAS agents.")
    agent_key = " ".join((agent_key or "").split()[:2])
    docker_port, node_port, beszel_port = ports
    services = {
        "beszel-agent": {
            "image": "henrygd/beszel-agent:0.21.2@sha256:343254226ae57a5cc6f8c23c755b1464730aba2abc7043116a2e222711ec6e1a",
            "network_mode": "host",
            "environment": {"KEY": agent_key, "PORT": str(beszel_port)},
            "volumes": ["/var/run/docker.sock:/var/run/docker.sock:ro"],
        },
        "node-exporter": {
            "image": "prom/node-exporter:v1.12.1@sha256:1b4e4438faca4dd7e001dd445d161a4a2091b0fededa84093b3a8dfeae1f1be0",
            "network_mode": "host", "pid": "host",
            "command": ["--path.rootfs=/host", f"--web.listen-address={address}:{node_port}"],
            "volumes": ["/:/host:ro,rslave"],
        },
        "docker-telemetry": {
            "image": "tecnativa/docker-socket-proxy:3.4@sha256:027f9fa5ee70be5836eeaa10ce889d7a83f34a8db16a7f412818b92997e0beab",
            "ports": [f"{address}:{docker_port}:2375"],
            "environment": {"CONTAINERS": "1", "IMAGES": "1", "INFO": "1", "POST": "0"},
            "volumes": ["/var/run/docker.sock:/var/run/docker.sock:ro"],
        },
    }
    if component:
        service = SERVICES[component]
        services = {service: services[service]}
    for service in services.values():
        service["restart"] = "unless-stopped"
    return (json.dumps({"services": services}, indent=2) + "\n").encode()
