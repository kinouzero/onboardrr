"""Redeploy active Komodo stacks that use a host IP after its vault update."""

import json
import os
import re
from time import monotonic, sleep

from .komodo import MONGO_ID
from .linux import run_ssh_probe
from .stacks import ASSIGNMENT, COMPOSE_REFERENCE, REFERENCE, host_ip_variable, wait_update
from .storage import connect


SIMPLE_REFERENCE = re.compile(r"(?<!\$)\$([A-Za-z_][A-Za-z0-9_]*)")
INACTIVE_STATES = {"down", "stopped", "paused", "created"}


def compose_variables(contents):
    return {name for name, _ in COMPOSE_REFERENCE.findall(contents)} | set(SIMPLE_REFERENCE.findall(contents))


def uses_host_ip(stack, key):
    config, info = stack["config"], stack["info"]
    if config.get("skip_secret_interp"):
        return False
    assignments = ASSIGNMENT.findall(config.get("environment") or "")
    bindings = {name for name, value in assignments if key in REFERENCE.findall(value)}
    while True:
        expanded = bindings | {name for name, value in assignments if compose_variables(value) & bindings}
        if expanded == bindings:
            break
        bindings = expanded
    sources = [config["file_contents"]] if config.get("file_contents") else [
        item["contents"] for item in (info.get("remote_contents") or info.get("deployed_contents") or [])
        if isinstance(item, dict) and isinstance(item.get("contents"), str)
    ]
    if not sources:
        return bool(bindings)
    for contents in sources:
        compose = "\n".join(line for line in contents.splitlines() if not line.lstrip().startswith("#"))
        if key in REFERENCE.findall(compose) or compose_variables(compose).intersection(bindings):
            return True
    return False


def dependent_stacks(api, key):
    states = {stack["id"]: stack["info"].get("state", "").casefold()
              for stack in api._request("/read", "ListStacks", {"limit": 0})}
    for stack in api._request("/read", "ListFullStacks", {"limit": 0}):
        identifier = stack.get("_id", {}).get("$oid", "")
        if stack.get("template") or not MONGO_ID.fullmatch(identifier) or not uses_host_ip(stack, key):
            continue
        state = states.get(identifier, "unknown")
        if state not in INACTIVE_STATES and (
            state != "unknown" or stack["info"].get("deployed_config")
        ):
            yield identifier, stack["name"], state


def wait_for_core_ip(api, inventory, known_hosts, key, address):
    if key not in api._request("/read", "ListSecrets"):
        variable = next((item for item in api._request("/read", "ListVariables") if item["name"] == key), {})
        if variable.get("value") == address:
            return
        if variable:
            raise ValueError(f"The Komodo variable {key} differs from OpenBao.")
    core = inventory.get(os.environ.get("ONBOARDRR_KOMODO_CORE_DEVICE", "zero"))
    deadline = monotonic() + 90
    while monotonic() < deadline:
        if run_ssh_probe(core, known_hosts, "komodo_host_ip.py", payload={"key": key, "address": address}).get("ready"):
            try:
                api.version()
            except ValueError:
                pass
            else:
                return
        sleep(3)
    raise ValueError(f"Komodo Core has not reloaded the verified value of {key}.")


def refresh_komodo_host_ip(api, inventory, device, known_hosts, address):
    try:
        result = api.sync_periphery_ip(device["name"], address, apply=True)
    except ValueError as error:
        return {"blockers": [f"OpenBao saved the IP. Komodo Periphery address sync requires attention: {error}"]}
    try:
        result.update(redeploy_host_stacks(api, inventory, device, known_hosts, address))
    except ValueError as error:
        result["blockers"] = [f"OpenBao saved the IP. Stack redeployment requires attention: {error}"]
    return result


def redeploy_host_stacks(api, inventory, device, known_hosts, address):
    key = host_ip_variable(device["name"])
    parameters = (device["id"], address)
    with connect(inventory.database, write=True) as connection:
        row = connection.execute("SELECT ipv4, completed FROM host_ip_stack_deployments WHERE device_id = ?",
                                 parameters[:1]).fetchone()
        completed = set(json.loads(row["completed"])) if row and row["ipv4"] == address else set()
        if not row or row["ipv4"] != address:
            connection.execute("""INSERT INTO host_ip_stack_deployments (device_id, ipv4, completed) VALUES (?, ?, '[]')
                                  ON CONFLICT (device_id) DO UPDATE SET ipv4 = excluded.ipv4, completed = '[]'""",
                               parameters)

    pending = [(identifier, name, state) for identifier, name, state in dependent_stacks(api, key)
               if identifier not in completed]
    if pending:
        wait_for_core_ip(api, inventory, known_hosts, key, address)
        pending = [(identifier, name, state) for identifier, name, state in dependent_stacks(api, key)
                   if identifier not in completed]
    running = [(identifier, name) for identifier, name, state in pending if state == "running"]
    blockers = [f"{name} is not reachable or ready in Komodo ({state})."
                for _, name, state in pending if state != "running"]
    redeployed = []
    for identifier, name in running:
        try:
            update = api._request("/execute", "DeployStack", {"stack": identifier})
            wait_update(api, update, monotonic() + 180)
        except ValueError as error:
            blockers.append(f"{name}: {error}")
            continue
        completed.add(identifier)
        with connect(inventory.database, write=True) as connection:
            connection.execute("UPDATE host_ip_stack_deployments SET completed = ? WHERE device_id = ? AND ipv4 = ?",
                               (json.dumps(sorted(completed)), *parameters))
        redeployed.append(name)
    return {"redeployed_stacks": redeployed, "blockers": blockers}
