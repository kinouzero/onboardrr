"""Install optional platform stacks from the primary NAS references."""

import hashlib
import json
import re
import subprocess
from pathlib import Path

import yaml

from . import ssh
from .inventory import NAS_PROFILE, PROFILE, PROFILES, SERVER_PROFILE
from .kuma import kuma_on_host, kuma_tcp_values
from .primary_storage import storage_endpoint
from .runtime import system_environment
from .storage import device_lock


OPENBAO_SERVICES = ("openbao", "openbao-postgres")
PLATFORM_STACKS = {
    "openbao": OPENBAO_SERVICES,
    "authentik": ("authentik-postgres", "authentik-server", "authentik-valkey", "authentik-worker"),
    "komodo": ("komodo", "komodo-agent", "komodo-mongodb"),
    "pangolin": ("pangolin", "pangolin-gerbil", "pangolin-postgres", "pangolin-traefik"),
}
PLATFORM_PROFILES = {
    "openbao": (PROFILE, SERVER_PROFILE),
    "authentik": PROFILES,
    "komodo": PROFILES,
    "pangolin": PROFILES,
}
REQUIRED_VARIABLE = re.compile(r"(?<!\$)\$\{([A-Za-z_][A-Za-z0-9_]*)(?::?\?[^}]*)?\}")


def stack_reference(storage, stack):
    if stack not in PLATFORM_STACKS:
        raise ValueError("Select OpenBao, Authentik, Komodo, or Pangolin.")
    project = "secrets" if stack == "openbao" else stack
    reference = f"/share/compose/zero/docker-compose.{project}.yml"
    try:
        result = subprocess.run(
            ["ssh", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes", "-o", "ConnectTimeout=6",
             *storage.ssh_args(), f"head -c 262145 {reference}"],
            capture_output=True, check=True, timeout=20, env=system_environment(),
        )
        if not 0 < len(result.stdout) <= 262144:
            raise ValueError("The platform Compose reference is missing or too large.")
        source = result.stdout.decode().replace("/home/${USER}/", "${ONBOARDRR_HOME}/")
        if stack == "openbao":
            source = re.sub(r"/home/\$\{USER(?::-[^}]+)?\}/", "${HOME}/", result.stdout.decode())
            source = re.sub(r"\$\{HOSTNAME(?::-[^}]+)?\}", "${DOCKER_HOSTNAME}", source)
        model = yaml.safe_load(source)
    except (OSError, subprocess.SubprocessError, UnicodeError, yaml.YAMLError) as error:
        raise ValueError(f"Could not read the {stack} Compose reference from the primary NAS.") from error
    if (not isinstance(model, dict) or model.get("name") != project
            or not isinstance(model.get("services"), dict) or set(model["services"]) != set(PLATFORM_STACKS[stack])):
        raise ValueError(f"The primary NAS returned an unexpected {stack} Compose project.")
    return model, hashlib.sha256(result.stdout).hexdigest()


class ComposeDumper(yaml.SafeDumper):
    def increase_indent(self, flow=False, indentless=False):
        return super().increase_indent(flow, indentless=False)

    def ignore_aliases(self, data):
        return True

    def represent_mapping(self, tag, mapping, flow_style=None):
        node = super().represent_mapping(tag, mapping, flow_style)
        for key, value in node.value:
            if key.value == "healthcheck" and isinstance(value, yaml.MappingNode):
                for field, test in value.value:
                    if field.value == "test" and isinstance(test, yaml.SequenceNode):
                        test.flow_style = True
        return node


def platform_compose(model):
    services = {}
    for name, source in sorted(model["services"].items()):
        service = dict(source)
        environment = service.get("environment")
        if isinstance(environment, dict):
            service["environment"] = dict(sorted(environment.items()))
        elif isinstance(environment, list):
            service["environment"] = sorted(environment, key=lambda item: item.partition("=")[0])
        if "network_mode" not in service:
            service.setdefault("networks", ["default"])
        service["restart"] = "always"
        services[name] = dict(sorted(service.items()))

    sections = []
    for key, value in model.items():
        if key == "services":
            blocks = [yaml.dump({name: service}, Dumper=ComposeDumper, sort_keys=False, width=4096).rstrip()
                      for name, service in services.items()]
            sections.append("services:\n" + "\n\n".join(
                "\n".join("  " + line for line in block.splitlines()) for block in blocks
            ))
        else:
            sections.append(yaml.dump({key: value}, Dumper=ComposeDumper, sort_keys=False, width=4096).rstrip())
    return "\n\n".join(sections) + "\n"


PANGOLIN_PORT_ALIASES = {
    "80": "PANGOLIN_HTTP_PORT",
    "443": "PANGOLIN_HTTPS_PORT",
    "21820/udp": "PANGOLIN_EXTRA_UDP_PORT",
    "51820/udp": "PANGOLIN_WIREGUARD_PORT",
}


def pangolin_published_ports(gerbil):
    defaults = {}
    ports = []
    for item in gerbil.get("ports", []):
        if isinstance(item, str):
            published, _, target = item.partition(":")
            if published.isdecimal() and (alias := PANGOLIN_PORT_ALIASES.get(target)):
                fallback = f"ONBOARDRR_{alias}"
                defaults[fallback] = published
                item = f"${{{alias}:-${{{fallback}}}}}:{target}"
        ports.append(item)
    gerbil["ports"] = ports
    return defaults


def stack_payload(model, stack, name, apply, *, kuma_local=None):
    services = model["services"]
    port_defaults = {}
    if stack == "pangolin" and (gerbil := services.get("pangolin-gerbil")):
        port_defaults = pangolin_published_ports(gerbil)
    if stack == "komodo":
        core = model["services"]["komodo"]
        core["command"] = ["/bin/bash", "/run/komodo-openbao/watch-core.sh"]
        core.setdefault("environment", {})["KOMODO_CONFIG_PATHS"] = "/config,/run/komodo-openbao/rendered/core.config.toml"
        core.setdefault("volumes", []).extend([
            "${ONBOARDRR_HOME}/.config/docker/openbao/komodo/watch-core.sh:/run/komodo-openbao/watch-core.sh:ro",
            "${ONBOARDRR_KOMODO_RENDERED}:/run/komodo-openbao/rendered:ro",
        ])
    probe_ports = {
        "komodo": {"komodo": {"9120": "KOMODO_PORT"}, "komodo-agent": {"8120": "KOMODO_PORT_AGENT"}},
        "pangolin": {"pangolin-gerbil": {"3003": "GERBIL_PORT"}},
    }
    for service, ports in probe_ports.get(stack, {}).items():
        test = services.get(service, {}).get("healthcheck", {}).get("test")
        if isinstance(test, list):
            for index, argument in enumerate(test):
                if isinstance(argument, str):
                    for port, alias in ports.items():
                        argument = re.sub(rf"(127\.0\.0\.1[:/]|localhost:){port}(?![0-9])",
                                          lambda match: match[1] + f"${{{alias}}}", argument)
                    test[index] = argument
    if kuma_local is not None:
        hostname = "${DOCKER_HOSTNAME}" if stack == "openbao" else "${HOSTNAME}"
        connection = ('"socket", "/var/run/docker.sock"' if kuma_local
                      else '"tcp", "http://${HOST_IP}:${DOCKER_API_PORT}"')
        for service in model["services"].values():
            labels = service.get("labels", {})
            if "kuma.__docker" in labels:
                labels["kuma.__docker"] = f'"{hostname}", {connection}'
    compose = yaml.safe_dump(model, sort_keys=False) if stack == "openbao" else platform_compose(model)
    request = {"name": name, "stack": stack, "compose": compose,
               "required": sorted(set(REQUIRED_VARIABLE.findall(compose))), "apply": apply}
    if port_defaults:
        request["port_defaults"] = port_defaults
    if stack == "komodo":
        from .linux import PROVISIONING

        request["watcher"] = (PROVISIONING / "roles/linux/files/watch-komodo-core.sh").read_text()
    return request


def merge_openbao(model, current):
    if not current:
        return model
    try:
        existing = yaml.safe_load(current)
        services = existing["services"]
        if existing["name"] != "secrets" or not isinstance(services, dict) or not services:
            raise ValueError
        names = set(services)
        if names <= {"openbao-agent-compose", "openbao-agent-komodo"}:
            services.update(model["services"])
            for key, value in model.items():
                if key in ("name", "services"):
                    continue
                if key in existing:
                    if not isinstance(existing[key], dict) or not isinstance(value, dict):
                        raise ValueError
                    if any(name in existing[key] and existing[key][name] != item for name, item in value.items()):
                        raise ValueError
                    existing[key].update(value)
                else:
                    existing[key] = value
        elif not set(OPENBAO_SERVICES) <= names:
            raise ValueError
    except (KeyError, TypeError, ValueError, yaml.YAMLError) as error:
        raise ValueError("Review the existing compose.openbao.yml before adding OpenBao Server.") from error
    return existing


def use_ferretdb(model):
    services = model["services"]
    mongo = services.pop("komodo-mongodb")
    core = services["komodo"]
    core["depends_on"].pop("komodo-mongodb")
    core["depends_on"]["komodo-ferretdb"] = {"condition": "service_started"}
    core["environment"]["KOMODO_DATABASE_ADDRESS"] = "komodo-ferretdb:27017"
    core["environment"]["KOMODO_DATABASE_PASSWORD"] = "${POSTGRES_PASSWORD}"
    network = mongo.get("networks", ["default"])
    credentials = {"POSTGRES_DB": "postgres", "POSTGRES_PASSWORD": "${POSTGRES_PASSWORD}", "POSTGRES_USER": "${USER}"}
    common = {"restart": "always", "networks": network}
    def labels(name, icon, repository):
        return {
            "docker.stack": mongo["labels"]["docker.stack"],
            "homepage.group": "Databases",
            "homepage.href": "${URL_DB}",
            "homepage.icon": icon,
            "homepage.name": name,
            "komodo.skip": None,
            "kuma.__docker": mongo["labels"].get("kuma.__docker", '"${HOSTNAME}", "socket", "/var/run/docker.sock"'),
            "wud.display.icon": icon,
            "wud.display.name": name,
            "wud.link.template": f"https://github.com/FerretDB/{repository}/releases",
            "wud.watch": "true",
        }

    services["komodo-postgres"] = {
        **common,
        "image": "ghcr.io/ferretdb/postgres-documentdb:17-0.107.0-ferretdb-2.7.0",
        "labels": labels("komodo-postgres", "sh-postgresql", "documentdb"),
        "environment": credentials,
        "volumes": ["${ONBOARDRR_HOME}/.config/docker/komodo/db/documentdb:/var/lib/postgresql/data"],
        "healthcheck": {"test": ["CMD", "pg_isready", "-d", "postgres", "-U", "${USER}"],
                        "interval": "5s", "timeout": "5s", "retries": 12},
    }
    services["komodo-ferretdb"] = {
        **common,
        "image": "ghcr.io/ferretdb/ferretdb:2.7.0",
        "labels": labels("komodo-ferretdb", "sh-ferretdb", "FerretDB"),
        "environment": {
            "FERRETDB_POSTGRESQL_URL": "postgres://komodo-postgres:5432/postgres",
            "PGUSER": credentials["POSTGRES_USER"], "PGPASSWORD": credentials["POSTGRES_PASSWORD"],
        },
        "depends_on": {"komodo-postgres": {"condition": "service_healthy"}},
        "volumes": ["${ONBOARDRR_HOME}/.config/docker/komodo/db/ferretdb:/state"],
    }
    services["komodo-postgres"]["labels"].update({
        "wud.tag.include": r"^\d+-\d+\.\d+\.\d+-ferretdb-2\.\d+\.\d+$$",
        "wud.tag.transform": r"^\d+-(\d+\.\d+\.\d+)-ferretdb-.*$$ => $$1",
    })
    services["komodo-ferretdb"]["labels"]["wud.tag.include"] = r"^2\.\d+\.\d+$$"


def install_stack(database, device, stack, public_key, known_hosts, *, apply, prepare_agent):
    if device["profile"] not in PLATFORM_PROFILES.get(stack, ()):
        raise ValueError(f"Installing {stack} is unavailable for the {device['profile']} profile.")
    model, reference_digest = stack_reference(storage_endpoint(database), stack)
    key = ssh.load_public_key(Path(public_key).expanduser())
    nas = device["profile"] == NAS_PROFILE
    backend = None
    prefer_ferretdb = device["profile"] == PROFILE
    if stack == "komodo":
        payload = json.dumps({"name": device["name"], "stack": stack, "probe": True,
                              "prefer_ferretdb": prefer_ferretdb}).encode()
        with ssh.agent_identity(key) as identity, ssh.connect(device, known_hosts, key=identity) as client:
            probe = json.loads(ssh.script(client, "platform_stack.py", payload, entware=nas))
        if probe.get("error"):
            raise ValueError(probe["error"])
        if probe.get("blockers"):
            return {"device": device["name"], "stack": stack, "blockers": probe["blockers"]}
        backend = probe["backend"]
        if backend == "ferretdb":
            use_ferretdb(model)
    # Preparation takes its own device lock before the Compose transaction.
    if apply and stack != "openbao":
        prepare_agent()
    with device_lock(database, device), ssh.agent_identity(key) as identity:
        with ssh.connect(device, known_hosts, key=identity) as client:
            def run(payload):
                result = json.loads(ssh.script(client, "platform_stack.py", json.dumps(payload).encode(),
                                               timeout=900, entware=device["profile"] == NAS_PROFILE))
                if result.get("error"):
                    raise ValueError(result["error"])
                return result

            kuma_local = kuma_on_host(client, entware=nas)
            kuma_tcp = {} if kuma_local else kuma_tcp_values(device)
            if stack == "openbao":
                current = run({"name": device["name"], "stack": stack, "read": True}).get("compose", "")
                model = merge_openbao(model, current)
                request = stack_payload(model, stack, device["name"], apply, kuma_local=kuma_local)
                request["kuma_tcp"] = kuma_tcp
                request["previous_digest"] = hashlib.sha256(current.encode()).hexdigest()
                return {**run(request), "services": list(model["services"])}
            request = stack_payload(model, stack, device["name"], apply, kuma_local=kuma_local)
            request["kuma_tcp"] = kuma_tcp
            request["reference_digest"] = reference_digest
            if backend:
                request.update(backend=backend, prefer_ferretdb=prefer_ferretdb)
            return run(request)
