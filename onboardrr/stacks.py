"""Apply baseline Compose stacks from the live Komodo templates."""

import re
from functools import cache
from ipaddress import IPv4Address, IPv4Network
from time import monotonic, sleep

from .image_versions import ARCHITECTURES, resolve_images
from .http_json import HttpStatusError
from .inventory import HOST_LABEL
from .komodo import MONGO_ID, add_resource_tags
from .pangolin_network import destination_ipv4
from .runs import Ownership, Resource, ResourceKind
from .secrets import deployment_layout
from .storage import device_lock


BASELINE = ("docker", "monitoring", "network")
STEP = "komodo-stacks"
LAN_NETWORKS = tuple(IPv4Network(block) for block in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16"))
REFERENCE = re.compile(r"\[\[\s*([A-Za-z_][A-Za-z0-9_]*)\s*\]\]")
COMPOSE_REFERENCE = re.compile(r"(?<!\$)\$\{([A-Za-z_][A-Za-z0-9_]*)([^}]*)\}")
ASSIGNMENT = re.compile(r"^[ \t]*([A-Za-z_][A-Za-z0-9_]*)[ \t]*=(.*)$", re.MULTILINE)
DOCKER_PORT = "${DOCKER_API_PORT}:${DOCKER_API_INTERNAL_PORT}"
LAN_DOCKER_PORT = "${HOST_IP}:" + DOCKER_PORT
HOST_VARIABLES = {
    "HOSTNAME": "HOSTNAME",
    "DOCKER_HOSTNAME": "HOSTNAME",
    "HOST_IP": "IP",
    "NEWT_ID": "NEWT_ID",
    "NEWT_SECRET": "NEWT_SECRET",
}


def named_resources(items):
    resources = {}
    for item in items:
        key = item["name"].casefold()
        if key in resources:
            raise ValueError("Komodo returned ambiguous resource names.")
        resources[key] = item
    return resources


def template_requirements(config, host_key, *, kind=None):
    compose = config.get("file_contents") or ""
    if not compose.strip() or any(config.get(key) for key in ("repo", "linked_repo", "files_on_host", "swarm_id")):
        raise ValueError("The Komodo baseline template must use inline Compose.")
    environment = dict(ASSIGNMENT.findall(config.get("environment") or ""))
    compose_keys = {
        key for key, suffix in COMPOSE_REFERENCE.findall(compose) if not suffix or suffix.startswith(("?", ":?"))
    }
    if kind == "docker":
        compose_keys.add("HOST_IP")
    managed = {key: f"{host_key}_{part}" for key, part in HOST_VARIABLES.items() if key in compose_keys}
    references = set(REFERENCE.findall(compose))
    for key, value in environment.items():
        if key not in managed:
            references.update(REFERENCE.findall(value))
    return {
        "variables": sorted(references | set(managed.values())),
        "missing_environment_keys": sorted(compose_keys - environment.keys()),
    }


def validate_lan_address(value):
    try:
        if not isinstance(value, str):
            raise TypeError
        host = IPv4Address(value)
    except (ValueError, TypeError):
        raise ValueError("The device address must be a literal LAN IPv4 address.") from None
    if not any(host in network for network in LAN_NETWORKS):
        raise ValueError("The Docker API must be bound to a private LAN IPv4 address.")


def hostname_variable(name):
    return name.upper().replace("-", "_") + "_HOSTNAME"


def stored_host_value(bao, name, field):
    if not HOST_LABEL.fullmatch(name):
        raise ValueError("The device name must be a DNS label.")
    try:
        value, = bao.read(f"{name}/{field}", "value")
    except HttpStatusError as error:
        if error.status != 404:
            raise
        return None
    return value


def stored_hostname(bao, name):
    hostname = stored_host_value(bao, name, "HOSTNAME")
    if hostname is not None and hostname != name:
        raise ValueError(f"OpenBao hosts/{name}/HOSTNAME must match the device's inventory name.")
    return hostname


def ensure_host_value(bao, name, field, value):
    if field == "IP":
        validate_lan_address(value)
    current = stored_hostname(bao, name) if field == "HOSTNAME" else stored_host_value(bao, name, field)
    if current is None:
        try:
            bao.create(f"{name}/{field}", {"value": value})
        except HttpStatusError as error:
            if error.status not in (400, 409) or stored_host_value(bao, name, field) != value:
                raise
    elif current != value:
        raise ValueError(f"OpenBao hosts/{name}/{field} changed during baseline preparation.")


def publish_host_variable(api, key, value, variables, secrets):
    if key in variables:
        if variables[key] != value:
            raise ValueError(f"Komodo variable {key} differs from the OpenBao value.")
        return False
    if key in secrets:
        return False
    result = api._request("/write", "CreateVariable", {"name": key, "value": value})
    if result.get("name") != key or result.get("value") != value:
        raise ValueError(f"Komodo did not confirm creation of {key}.")
    return True


def publish_hostname(api, bao, name, variables, secrets):
    key = hostname_variable(name)
    if key in variables and variables[key] != name:
        raise ValueError(f"Komodo variable {key} differs from the enrolled device name.")
    ensure_host_value(bao, name, "HOSTNAME", name)
    return publish_host_variable(api, key, name, variables, secrets)


def wait_for_host_aliases(api, name, secrets):
    required = {hostname_variable(name), host_ip_variable(name)}
    deadline = monotonic() + 90
    while not required <= secrets:
        if monotonic() >= deadline:
            raise ValueError("Komodo Core has not loaded the host aliases from OpenBao. Retry after its Agent reloads.")
        sleep(2)
        secrets = set(api._request("/read", "ListSecrets"))


def host_ip_variable(name):
    return name.upper().replace("-", "_") + "_IP"


def baseline_address(device, bao, variables, secrets):
    name = device["name"]
    key = host_ip_variable(name)
    saved = stored_host_value(bao, name, "IP")
    if saved is None:
        saved = stored_host_value(bao, name, "HOST_IP")
    current = variables.get(key)
    if saved is None and key in secrets:
        raise ValueError(f"Set OpenBao hosts/{name}/IP to validate the configured Komodo secret {key}.")
    if saved is not None and current is not None and saved != current:
        raise ValueError(f"Komodo variable {key} differs from the stored OpenBao address for {name}.")
    address = saved if saved is not None else current
    if address is None:
        try:
            address = destination_ipv4(device, None)
        except ValueError:
            raise ValueError(f"The device must resolve to one LAN IPv4 address or have OpenBao hosts/{name}/IP.") from None
    validate_lan_address(address)
    return address


def preview_baseline(api, device, *, primary_server, host_bao=None, kinds=BASELINE):
    result, server, _, templates, variables, secrets = baseline_plan(api, device, primary_server, host_bao=host_bao, kinds=kinds)
    if result["status"] == "preview" and not result["blockers"]:
        if host_bao is not None and f"{device['name']}-docker" in templates:
            variables[host_ip_variable(device["name"])] = baseline_address(device, host_bao, variables, secrets)
        baseline_configs(api, device["name"], server["id"], result["stacks"], templates, variables)
    return result


def baseline_plan(api, device, primary_server, *, host_bao=None, kinds=BASELINE):
    name = device["name"]
    if name == primary_server:
        return {"device": name, "status": "skipped", "reason": "primary-linux-server", "stacks": []}, None, {}, {}, {}, set()
    servers = named_resources(api._request("/read", "ListServers", {"limit": 0}))
    stacks = named_resources(api._request("/read", "ListStacks", {"limit": 0}))
    variables = {item["name"]: item.get("value") for item in api._request("/read", "ListVariables")}
    secrets = set(api._request("/read", "ListSecrets"))
    available = variables.keys() | secrets
    if host_bao is not None:
        stored_hostname(host_bao, name)
        key = hostname_variable(name)
        if key in variables and variables[key] != name:
            raise ValueError(f"Komodo variable {key} differs from the enrolled device name.")
        available.update((key, host_ip_variable(name)))
    server = servers.get(name)
    state = server["info"].get("state") if server else None
    blockers = []
    if state != "Ok":
        blockers.append("Register the device and verify that Komodo can reach Periphery.")

    plan = []
    templates = {}
    host_key = name.upper().replace("-", "_")
    for kind in kinds:
        target_name = f"{name}-{kind}"
        template_name = f"stack-{kind}"
        item = {"name": target_name, "template": template_name}
        existing = stacks.get(target_name)
        if existing:
            info = existing["info"]
            same_host = server and info.get("server_id") == server["id"] and not info.get("swarm_id")
            item["action"] = "reuse" if same_host and not existing.get("template") else "conflict"
            if item["action"] == "conflict":
                blockers.append(f"{target_name} is already in use by another resource.")
        elif not stacks.get(template_name, {}).get("template"):
            item["action"] = "blocked"
            blockers.append(f"Komodo is missing the {template_name} template.")
        else:
            template = stacks[template_name]
            config = api._request("/read", "GetStack", {"stack": template["id"]})["config"]
            templates[target_name] = config
            requirements = template_requirements(config, host_key, kind=kind)
            item.update(action="create", **requirements)
            item["missing_variables"] = sorted(set(item["variables"]) - available)
            if set(item["missing_environment_keys"]) - HOST_VARIABLES.keys() or item["missing_variables"]:
                blockers.append(f"{target_name} requires template environment or variable configuration.")
        plan.append(item)

    result = {"device": name, "status": "preview", "server_state": state, "stacks": plan, "blockers": blockers}
    return result, server, stacks, templates, variables, secrets


def instance_config(kind, template, device, server_id, variables):
    compose = re.sub(r"\bDOCKER_HOSTNAME\b", "HOSTNAME", template["file_contents"])
    environment = re.sub(r"\bDOCKER_HOSTNAME\b", "HOSTNAME", template.get("environment") or "")
    host_key = device.upper().replace("-", "_")
    keys = {name for name, _ in COMPOSE_REFERENCE.findall(compose)}
    if kind == "docker":
        keys.add("HOST_IP")
    for key, suffix in HOST_VARIABLES.items():
        if key not in keys:
            continue
        line = f"{key}=[[{host_key}_{suffix}]]"
        environment, count = re.subn(rf"(?m)^[ \t]*{key}[ \t]*=.*$", line, environment)
        if count > 1:
            raise ValueError(f"The {kind} template defines {key} more than once.")
        if count == 0:
            environment = environment.rstrip() + "\n" + line + "\n"

    if kind == "docker":
        validate_lan_address(variables.get(f"{host_key}_IP"))
        compose, count = re.subn(
            rf'(?m)^([ \t]+- )[\'"]?{re.escape(DOCKER_PORT)}[\'"]?[ \t]*$',
            lambda match: f'{match[1]}"{LAN_DOCKER_PORT}"',
            compose,
        )
        if count != 1:
            raise ValueError("The Docker template must contain a single supported API port mapping.")
    if kind == "network" and "image: fosrl/pangolin-cli:0.19.0" in compose:
        compose = compose.replace("grep -aq newt /proc/1/cmdline || exit 1", "grep -qx pangolin-cli /proc/1/comm")
    if kind == "network" and not {"NEWT_ID", "NEWT_SECRET"} <= keys:
        raise ValueError("The network template must reference the Newt identity and secret.")

    config = {
        "server_id": server_id,
        "project_name": template.get("project_name") or kind,
        "file_contents": compose,
        "environment": environment.lstrip("\n"),
        "auto_update": template.get("auto_update", False),
    }
    if "HOSTNAME" in keys:
        config["compose_cmd_wrapper"] = template.get("compose_cmd_wrapper") or "env -u HOSTNAME [[COMPOSE_COMMAND]]"
        config["compose_cmd_wrapper_include"] = template.get("compose_cmd_wrapper_include") or [
            "config", "build", "pull", "up", "run",
        ]
    return config


def baseline_configs(api, device, server_id, plan, templates, variables):
    @cache
    def host_architecture():
        info = api._request("/read", "GetSystemInformation", {"server": server_id})
        architecture = info.get("cpu_arch")
        architecture = ARCHITECTURES.get(architecture, architecture)
        if architecture not in ("amd64", "arm64", "386", "ppc64le", "s390x", "riscv64"):
            raise ValueError("Komodo did not report a supported CPU architecture for WUD tag selection.")
        return architecture

    configs = {}
    tags = {}
    for item in plan:
        name = item["name"]
        if name not in templates:
            continue
        config = instance_config(name.rsplit("-", 1)[1], templates[name], device, server_id, variables)
        config["file_contents"], versions = resolve_images(config["file_contents"], host_architecture, tags)
        if versions:
            item["image_updates"] = versions
        configs[name] = config
    return configs


def deploy_stack(api, stack_id, state):
    if state == "running":
        return
    deadline = monotonic() + 180
    if state in {"down", "unknown"}:
        update = api._request("/execute", "DeployStack", {"stack": stack_id})
        wait_update(api, update, deadline)
    elif state != "deploying":
        raise ValueError("The baseline stack is not ready to deploy.")
    while monotonic() < deadline:
        stacks = api._request("/read", "ListStacks", {"limit": 0})
        current = next((s for s in stacks if s.get("id") == stack_id), {})
        if current.get("info", {}).get("state", "").casefold() == "running":
            return
        sleep(2)
    raise ValueError("Komodo did not report the baseline stack as running before the timeout.")


def wait_update(api, update, deadline):
    update_id = update.get("_id", {}).get("$oid") or ""
    if not MONGO_ID.fullmatch(update_id) or not update.get("success"):
        raise ValueError("Komodo did not accept the stack operation.")
    while update["status"] != "Complete" and monotonic() < deadline:
        sleep(2)
        update = api._request("/read", "GetUpdate", {"id": update_id})
    if update["status"] != "Complete" or not update.get("success"):
        raise ValueError("Komodo did not complete the stack operation successfully.")


def apply_baseline(api, device, *, primary_server, runs, host_bao=None, kinds=BASELINE):
    with device_lock(runs.database, device):
        result, server, stacks, templates, variables, secrets = baseline_plan(
            api, device, primary_server, host_bao=host_bao, kinds=kinds,
        )
        if result["status"] == "skipped":
            return result
        if result["blockers"]:
            raise ValueError(" ".join(result["blockers"]))
        histories = runs.list(device["name"])
        if not histories:
            raise ValueError("Complete Linux and Periphery preparation before applying the baseline.")
        history = histories[0]
        run = runs.show(history["id"])
        recorded = {item["reference"]: item["ownership"] for item in run["resources"]
                    if item["kind"] == ResourceKind.KOMODO_STACK and item["step"] == STEP}
        if not any(item["reference"] == server["id"] and item["kind"] == ResourceKind.KOMODO_SERVER
                   for item in run["resources"]):
            raise ValueError("The run does not record this Komodo server.")
        active = history["next_step"] == STEP
        if not active and (
            history["next_step"] not in ("pangolin", "verify", None)
            or any(item["action"] != "reuse" or stacks[item["name"]]["id"] not in recorded
                   or stacks[item["name"]]["info"].get("state", "").casefold() != "running"
                   for item in result["stacks"])
        ):
            raise ValueError("The run must be at the Komodo stacks checkpoint to change its baseline.")
        key = device["name"].upper().replace("-", "_")
        host_address = baseline_address(device, host_bao, variables, secrets) if active and host_bao is not None else None
        config_variables = {**variables, f"{key}_IP": host_address} if host_address is not None else variables
        docker = stacks.get(f"{device['name']}-docker")
        if "docker" in kinds and docker and docker["info"].get("state", "").casefold() in {"down", "unknown"}:
            config = api._request("/read", "GetStack", {"stack": docker["id"]})["config"]
            validate_lan_address(config_variables.get(f"{key}_IP"))
            if (
                f'"{LAN_DOCKER_PORT}"' not in config["file_contents"]
                or dict(ASSIGNMENT.findall(config["environment"])).get("HOST_IP") != f"[[{key}_IP]]"
            ):
                raise ValueError("Configure the existing Docker API stack to publish on the device's LAN address.")
        network = f"{device['name']}-network"
        existing_network = stacks.get(network)
        network_state = existing_network["info"].get("state", "").casefold() if existing_network else ""
        if "network" in kinds and network_state != "running":
            expected_id, expected_secret = f"{key}_NEWT_ID", f"{key}_NEWT_SECRET"
            if expected_id not in variables.keys() | secrets or expected_secret not in secrets:
                raise ValueError("Configure the device's Newt identity and secret in Komodo before deploying.")
            if existing_network:
                config = api._request("/read", "GetStack", {"stack": existing_network["id"]})["config"]
                environment = {k: value.strip() for k, value in ASSIGNMENT.findall(config["environment"])}
                if (environment.get("NEWT_ID"), environment.get("NEWT_SECRET")) != (
                    f"[[{expected_id}]]", f"[[{expected_secret}]]"
                ):
                    raise ValueError("The existing network stack does not reference the device's Newt credentials.")
        configs = baseline_configs(api, device["name"], server["id"], result["stacks"], templates, config_variables)
        if active and host_bao is not None and deployment_layout() == "split":
            ensure_host_value(host_bao, device["name"], "HOSTNAME", device["name"])
            ensure_host_value(host_bao, device["name"], "IP", host_address)
            wait_for_host_aliases(api, device["name"], secrets)
        elif active and host_bao is not None:
            if publish_hostname(api, host_bao, device["name"], variables, secrets):
                runs.record_resource(run["id"], STEP, Resource(
                    ResourceKind.KOMODO_VARIABLE, hostname_variable(device["name"]), Ownership.CREATED,
                ))
            ensure_host_value(host_bao, device["name"], "IP", host_address)
            address_key = host_ip_variable(device["name"])
            if publish_host_variable(api, address_key, host_address, variables, secrets):
                runs.record_resource(run["id"], STEP, Resource(
                    ResourceKind.KOMODO_VARIABLE, address_key, Ownership.CREATED,
                ))
        ready = {
            name for name, stack in stacks.items()
            if stack.get("id") in recorded and not stack.get("template") and stack["info"].get("server_id") == server["id"]
            and stack["info"].get("state", "").casefold() == "running"
        }
        for item in result["stacks"]:
            name = item["name"]
            existing = stacks.get(name)
            if existing:
                stack_id = existing["id"]
                ownership = Ownership(recorded.get(stack_id, Ownership.REUSED))
                state = existing["info"].get("state", "").casefold()
            else:
                created = api._request("/write", "CreateStack", {"name": name, "config": configs[name]})
                stack_id = created.get("_id", {}).get("$oid", "")
                if not MONGO_ID.fullmatch(stack_id):
                    raise ValueError(f"Komodo did not return a valid ID for {name}.")
                ownership = Ownership.CREATED
                state = "unknown"
            if active:
                runs.record_resource(run["id"], STEP, Resource(ResourceKind.KOMODO_STACK, stack_id, ownership))
                template = stacks.get(item["template"], {})
                if ownership == Ownership.CREATED and template.get("template"):
                    add_resource_tags(api, "Stack", stack_id, template.get("tags", []))
                deploy_stack(api, stack_id, state)
            item["state"] = "running"
            ready.add(name)
        if active and all(f"{device['name']}-{kind}" in ready for kind in BASELINE):
            runs.complete_step(run["id"], STEP)
        result["status"] = "applied"
        return result
