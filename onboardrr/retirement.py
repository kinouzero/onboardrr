"""Plan retirement and remove verified, device-owned baseline stacks."""

import re
from contextlib import nullcontext
from time import monotonic

from .http_json import HttpStatusError
from .komodo import MONGO_ID
from .monitoring import monitoring_registration
from .runs import Ownership, ResourceKind, RunStore, device_id
from .stacks import BASELINE, wait_update
from .storage import connect, device_lock, provisioning_lock


ORDER = {
    ResourceKind.KOMODO_STACK: 0,
    ResourceKind.KOMODO_VARIABLE: 1,
    ResourceKind.NEWT_CREDENTIALS: 1,
    ResourceKind.PANGOLIN_PRIVATE_ROUTE: 2,
    ResourceKind.PANGOLIN_SITE: 2,
    ResourceKind.KOMODO_SERVER: 3,
    ResourceKind.GOKU_FILE: 4,
    ResourceKind.GOKU_DIRECTORY: 5,
    ResourceKind.MOUNT: 6,
    ResourceKind.DOCKER_NETWORK: 7,
    ResourceKind.HOST_FILE: 8,
    ResourceKind.PACKAGE: 9,
    ResourceKind.SSH_KEY: 10,
}


def retirement_plan(database, name, *, purge=False):
    with connect(database) as connection:
        identifier = device_id(connection, name)
        observations = connection.execute(
            """
            SELECT kind, reference, ownership, backup_id, content_sha256
            FROM run_resources AS resource
            JOIN runs ON runs.id = resource.run_id
            WHERE runs.device_id = ?
            ORDER BY runs.rowid
            """,
            (identifier,),
        ).fetchall()

    history = {}
    for row in observations:
        key = (row["kind"], row["reference"])
        first = history.setdefault(key, dict(row))
        if row["content_sha256"] or (first["ownership"] == Ownership.REUSED and row["ownership"] == Ownership.MODIFIED):
            history[key] = dict(row)

    actions = []
    for key, item in sorted(history.items(), key=lambda entry: (ORDER[entry[0][0]], entry[0][1])):
        kind = key[0]
        ownership = item["ownership"]
        if ownership == Ownership.MODIFIED:
            action = "restore"
        elif ownership == Ownership.REUSED:
            action = "preserve"
        elif kind == ResourceKind.GOKU_DIRECTORY and not purge:
            action = "preserve"
        elif kind == ResourceKind.PACKAGE:
            action = "review-package-use"
        elif kind == ResourceKind.SSH_KEY:
            action = "remove-after-fallback-ssh-check"
        else:
            action = "remove"
        if item["content_sha256"] is None:
            del item["content_sha256"]
        actions.append({**item, "action": action})

    result = {"device_id": identifier, "purge": purge, "actions": actions}
    registration = monitoring_registration(database, identifier) if not purge else None
    if registration:
        result["monitoring"] = {"status": "pending", "components": sorted(registration["owned"])}
    return result


def retire_baseline(database, device, komodo, *, pangolin=None, unregister_server=False, detach_private_routes=False,
                    delete_owned_private_route=False, monitoring=None, adopt_monitoring=False,
                    remove_stacks=True, remove_monitoring=True, stack_kind=None):
    """Remove owned baseline resources while preserving volumes and shared sites."""
    with device_lock(database, device):
        result = retirement_plan(database, device["name"])
        actions = result["actions"]
        set_initial_status(actions)
        if remove_monitoring and ("monitoring" in result or adopt_monitoring):
            if monitoring:
                registration = monitoring_registration(database, device["id"])
                result["monitoring"] = monitoring(device, registration=registration)
            elif adopt_monitoring:
                raise ValueError("Configure the monitoring hub before including an older enrollment.")
            else:
                result["monitoring"]["reason"] = "monitoring-hub-not-configured"
        if remove_stacks:
            retire_komodo_stacks(device, komodo, actions, stack_kind=stack_kind)
        if pangolin:
            for item in actions:
                if item["kind"] != ResourceKind.PANGOLIN_SITE or item["action"] != "remove":
                    continue
                if remove_stacks and any(
                    stack["kind"] == ResourceKind.KOMODO_STACK
                    and (stack["status"] == "preserved" or (
                        stack["status"] == "pending" and stack.get("reason") != "periphery-unavailable"
                    ))
                    for stack in actions
                ):
                    item["reason"] = "komodo-stacks-pending"
                    continue
                lock = (provisioning_lock(database, f"pangolin-{pangolin.organization}")
                        if detach_private_routes else nullcontext())
                owned_routes = None
                if delete_owned_private_route:
                    scope = re.escape(f'{pangolin.organization}/{item["reference"]}/')
                    owned_routes = {
                        route["reference"]: route for route in actions
                        if route["kind"] == ResourceKind.PANGOLIN_PRIVATE_ROUTE and route["action"] == "remove"
                        and re.fullmatch(scope + r"[1-9][0-9]*/[0-9.]+", route["reference"])
                    }
                with lock:
                    outcome = pangolin.retire_site(device["name"], item["reference"],
                                                   detach_private_routes=detach_private_routes, owned_routes=owned_routes)
                if outcome in {"removed", "absent"}:
                    item["status"] = outcome
                else:
                    item["reason"] = outcome
        if unregister_server:
            retire_komodo_server(device, komodo, actions)
        return result


def set_initial_status(actions):
    for item in actions:
        item["status"] = "preserved" if item["action"] == "preserve" else "pending"


def ensure_stacks_detached(komodo, name, actions):
    servers = komodo._request("/read", "ListServers", {"limit": 0})
    stacks = komodo._request("/read", "ListStacks", {"limit": 0})
    server_ids = {item["reference"] for item in actions if item["kind"] == ResourceKind.KOMODO_SERVER}
    server_ids.update(server["id"] for server in servers if server["name"] == name)
    stack_ids = {item["reference"] for item in actions if item["kind"] == ResourceKind.KOMODO_STACK}
    if any(stack["id"] in stack_ids or stack["info"].get("server_id") in server_ids for stack in stacks):
        raise ValueError("Remove the host's Komodo stacks before detaching its Goku storage.")


def retire_komodo_registration(database, device, komodo, *, apply=False, bao=None):
    with device_lock(database, device):
        actions = [
            item for item in retirement_plan(database, device["name"])["actions"]
            if item["kind"] == ResourceKind.KOMODO_SERVER and item["action"] == "remove"
        ]
        servers = {item["id"]: item for item in komodo._request("/read", "ListServers", {"limit": 0})}
        owned = {item["reference"] for item in actions}
        result = {"device": device["name"], "mode": "apply" if apply else "preview", "actions": actions,
                  "passkey": "preserved", "blockers": []}
        if not owned or any(server["name"].casefold() == device["name"].casefold() and sid not in owned
                            for sid, server in servers.items()):
            result["blockers"].append("Onboardrr could not confirm ownership of the Komodo server.")
            return result
        set_initial_status(actions)
        retire_komodo_server(device, komodo, actions, apply=False, servers=servers)
        result["blockers"] = [item["reason"] for item in actions if "reason" in item]
        if result["blockers"]:
            return result

        if bao is not None:
            path = f"deployments/periphery/{device['name']}"
            result["passkey"] = "pending" if periphery_secret_exists(bao, path) else "absent"
        if apply:
            servers = {item["id"]: item for item in komodo._request("/read", "ListServers", {"limit": 0})}
            if any(server["name"].casefold() == device["name"].casefold() and sid not in owned
                   for sid, server in servers.items()):
                result["blockers"].append("The Komodo registration changed during retirement.")
                return result
            retire_komodo_server(device, komodo, actions, servers=servers)
            result["blockers"] = [item["reason"] for item in actions if "reason" in item]
            if result["blockers"]:
                return result
            RunStore(database).unregister_komodo(device["id"], owned)
            if bao is not None and result["passkey"] == "pending":
                current = komodo._request("/read", "ListServers", {"limit": 0})
                if any(s["id"] in owned or s["name"].casefold() == device["name"].casefold() for s in current):
                    raise ValueError("The Komodo server is still registered. The passkey was preserved.")
                bao.delete_secret(path)
                if periphery_secret_exists(bao, path):
                    raise ValueError("OpenBao did not confirm removal of the Periphery passkey.")
                result["passkey"] = "removed"
        return result


def periphery_secret_exists(bao, path):
    try:
        bao.secret_metadata(path)
    except HttpStatusError as error:
        if error.status == 404:
            return False
        raise
    return True


def retire_komodo_server(device, komodo, actions, *, apply=True, servers=None):
    if servers is None:
        servers = {item["id"]: item for item in komodo._request("/read", "ListServers", {"limit": 0})}
    for item in actions:
        if item["kind"] != ResourceKind.KOMODO_SERVER or item["action"] != "remove":
            continue
        identifier = item["reference"]
        server = servers.get(identifier)
        if not server:
            item["status"] = "absent"
            continue
        if (not MONGO_ID.fullmatch(identifier) or server["name"] != device["name"] or server.get("template")):
            raise ValueError("The recorded Komodo registration no longer matches the device.")
        if server["info"].get("state") not in {"NotOk", "Disabled"}:
            item["reason"] = "periphery-still-connected"
            continue
        if komodo_server_has_dependents(komodo, identifier):
            item["reason"] = "komodo-server-has-dependencies"
            continue
        if not apply:
            continue
        deleted = komodo._request("/write", "DeleteServer", {"id": identifier})
        if deleted.get("_id", {}).get("$oid") != identifier:
            raise ValueError("Komodo did not confirm removal of the expected server.")
        item["status"] = "removed"


def komodo_server_has_dependents(komodo, identifier):
    for action in ("ListStacks", "ListDeployments", "ListRepos"):
        items = komodo._request("/read", action, {"limit": 0})
        if any(item["info"]["server_id"] == identifier for item in items):
            return True
    builders = komodo._request("/read", "ListBuilders", {"limit": 0})
    for builder in builders:
        config = komodo._request("/read", "GetBuilder", {"builder": builder["id"]})["config"]
        if config["type"] == "Server":
            params = config["params"]
            if params.get("server_id") == identifier or identifier in params.get("server_ids", []):
                return True
    return any(
        key.get("copy_server") == identifier or identifier in key.get("onboarded", [])
        for key in komodo._request("/read", "ListOnboardingKeys", {"limit": 0})
    )


def retire_komodo_stacks(device, komodo, actions, *, stack_kind=None):
    stack_actions = [item for item in actions
                     if item["kind"] == ResourceKind.KOMODO_STACK and item["action"] == "remove"]
    if not stack_actions:
        return
    servers = {s["id"]: s for s in komodo._request("/read", "ListServers", {"limit": 0})}
    stacks = {s["id"]: s for s in komodo._request("/read", "ListStacks", {"limit": 0})}
    recorded_servers = {item["reference"] for item in actions if item["kind"] == ResourceKind.KOMODO_SERVER}
    baseline_names = {f"{device['name']}-{kind}" for kind in BASELINE}
    if stack_kind:
        target = f"{device['name']}-{stack_kind}"
        for item in stack_actions:
            stack = stacks.get(item["reference"])
            if stack and stack["name"] != target:
                item["status"] = "preserved"
                item["action"] = "preserve"
        stack_actions = [item for item in stack_actions if item["action"] == "remove"]
    for item in stack_actions:
        stack = stacks.get(item["reference"])
        if not stack:
            item["status"] = "absent"
            continue
        server_id = stack["info"].get("server_id")
        server = servers.get(server_id)
        if (
            not MONGO_ID.fullmatch(item["reference"]) or stack.get("template")
            or stack["name"] not in baseline_names or stack["info"].get("swarm_id")
            or server_id not in recorded_servers or not server or server["name"] != device["name"]
        ):
            raise ValueError("A recorded stack no longer matches the device's Komodo baseline.")
    active_servers = {
        stack["info"]["server_id"] for stack in (stacks.get(item["reference"]) for item in stack_actions)
        if stack and servers[stack["info"]["server_id"]]["info"].get("state") == "Ok"
    }
    projects = {}
    for stack in stacks.values():
        server_id = stack["info"].get("server_id")
        if server_id not in active_servers:
            continue
        config = komodo._request("/read", "GetStack", {"stack": stack["id"]})["config"]
        project = config.get("project_name")
        if not project:
            raise ValueError("Verify explicit Compose project names on the server before retirement.")
        projects.setdefault((server_id, project), []).append(stack["id"])
    shared = {identifier for group in projects.values() if len(group) > 1 for identifier in group}
    for item in stack_actions:
        if item["status"] == "absent":
            continue
        identifier = item["reference"]
        if identifier in shared:
            item["reason"] = "compose-project-shared"
            continue
        stack = stacks[identifier]
        server = servers[stack["info"]["server_id"]]
        if server["info"].get("state") != "Ok":
            item["reason"] = "periphery-unavailable"
            continue
        if stack["info"].get("state", "").casefold() not in {"running", "stopped", "down", "unhealthy"}:
            item["reason"] = "stack-state-unavailable"
            continue
        if stack["info"]["state"].casefold() != "down":
            update = komodo._request(
                "/execute", "DestroyStack", {"stack": identifier, "remove_orphans": False},
            )
            wait_update(komodo, update, monotonic() + 180)
        current = {s["id"]: s for s in komodo._request("/read", "ListStacks", {"limit": 0})}
        live = current.get(identifier)
        if not live:
            item["status"] = "absent"
            continue
        if live["name"] != stack["name"] or live["info"].get("server_id") != server["id"] or live.get("template"):
            raise ValueError("The stack changed during retirement.")
        if live["info"].get("state", "").casefold() != "down":
            item["reason"] = "stack-still-present"
            continue
        deleted = komodo._request("/write", "DeleteStack", {"id": identifier})
        if deleted.get("_id", {}).get("$oid") != identifier:
            raise ValueError("Komodo did not confirm removal of the expected stack.")
        item["status"] = "removed"
