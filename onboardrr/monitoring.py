"""Reconcile the monitoring hub when a device has reachable host agents."""

import json
import os
import re
import shlex
import subprocess
from hashlib import sha256
from pathlib import Path
from time import monotonic, sleep
from urllib.parse import urlencode

import yaml

from .http_json import JsonService
from .inventory import normalize_host
from .remote.monitoring import insert_yaml, validate_target, yaml_value
from .runtime import system_environment
from .secrets import configured_openbao
from .stacks import ASSIGNMENT, baseline_address, wait_update
from .storage import connect, provisioning_lock


STATE = Path(__file__).with_name("remote") / "monitoring.py"
DOZZLE_KEY = "DOZZLE_REMOTE_HOST"
MONITORING_PORT_PATHS = {
    "DOCKER_API_PORT": "docker/API_PORT",
    "NODE_EXPORTER_PORT": "node-exporter/PORT",
    "BESZEL_AGENT_PORT": "beszel/AGENT_PORT",
}
PORT_VARIABLES = tuple(MONITORING_PORT_PATHS)
WUD_CRON = "0 */6 * * *"
WUD_FIELDS = ("host", "port", "cron")
COMPONENTS = ("wud", "dozzle", "prometheus", "beszel")
ENDPOINTS = {"wud": "docker_api", "dozzle": "docker_api", "prometheus": "node_exporter", "beszel": "beszel_agent"}


def monitoring_registration(database, device_id):
    with connect(database) as connection:
        row = connection.execute("SELECT registration FROM monitoring_resources WHERE device_id = ?", (device_id,)).fetchone()
    return json.loads(row["registration"]) if row else None


def save_monitoring_registration(database, device_id, registration):
    with connect(database, write=True) as connection:
        if registration is None:
            connection.execute("DELETE FROM monitoring_resources WHERE device_id = ?", (device_id,))
        else:
            connection.execute(
                """INSERT INTO monitoring_resources VALUES (?, ?)
                   ON CONFLICT (device_id) DO UPDATE SET registration = excluded.registration""",
                (device_id, json.dumps(registration)),
            )


def watcher_prefix(name):
    suffix = name.upper() if "-" not in name else "ONB" + sha256(name.encode()).hexdigest()[:16].upper()
    return f"WUD_WATCHER_{suffix}"


def monitoring_variables(komodo, name, ipv4, *, device=None, components=COMPONENTS):
    needed = tuple(key for key, endpoint in zip(PORT_VARIABLES, ("docker_api", "node_exporter", "beszel_agent"))
                   if endpoint in {ENDPOINTS[component] for component in components})
    address_key = name.upper().replace("-", "_") + "_IP"
    names = (address_key, *needed)
    values = {}
    for item in komodo._request("/read", "ListVariables"):
        key = item["name"]
        if key in names:
            if key in values:
                raise ValueError(f"Komodo returned more than one {key} variable.")
            values[key] = item.get("value")
    needs_address = device is not None and not values.get(address_key)
    missing_ports = [key for key in needed if not values.get(key)]
    if (needs_address or missing_ports) and os.environ.get("ONBOARDRR_BAO_URL"):
        secrets = set(komodo._request("/read", "ListSecrets"))
        if needs_address and address_key in secrets:
            values[address_key] = baseline_address(device, configured_openbao(mount="hosts"), values, secrets)
        published_ports = [key for key in missing_ports if key in secrets]
        if published_ports:
            services = configured_openbao(mount="services")
            for key in published_ports:
                values[key], = services.read(MONITORING_PORT_PATHS[key], "value")
    missing = [key for key in names if not values.get(key)]
    if missing:
        raise ValueError("Set the following Komodo monitoring variables: " + ", ".join(missing) + ".")
    for key in needed:
        if not re.fullmatch(r"[0-9]{1,5}", str(values[key])):
            raise ValueError(f"Set {key} to a valid TCP port in Komodo.")
    ports = tuple(int(values[key]) if key in needed else None for key in PORT_VARIABLES)
    checked_ports = [port for port in ports if port is not None]
    address = validate_target(name, values[address_key], checked_ports)
    if ipv4 and validate_target(name, ipv4, checked_ports) != address:
        raise ValueError(f"The requested address does not match the Komodo variable {address_key}.")
    return address_key, address, ports


def variable_bindings(environment, *names):
    assignments = ASSIGNMENT.findall(environment)
    for name in names:
        reference = "[[" + name + "]]"
        existing = [value.strip() for key, value in assignments if key == name]
        if not existing:
            environment += ("" if not environment or environment.endswith("\n") else "\n") + f"{name}={reference}\n"
        elif existing != [reference]:
            raise ValueError(f"Bind {name} to its Komodo variable in the monitoring stack environment.")
    return environment


def compose_scalar(value, mapping):
    # Let the YAML emitter add quotes only when the block scalar needs them.
    encoded = yaml.safe_dump({"value": value}, width=float("inf"), allow_unicode=True)
    scalar = encoded.removeprefix("value: ").rstrip("\n")
    if "\n" in scalar or mapping.flow_style:
        return json.dumps(value, ensure_ascii=False)
    return scalar


def compose_source(text, request, address_key, components=("wud", "dozzle")):
    def environments(source):
        services = yaml_value(yaml.compose(source, Loader=yaml.SafeLoader), "services")
        return (
            yaml_value(yaml_value(services, "wud"), "environment") if "wud" in components else None,
            yaml_value(yaml_value(services, "dozzle"), "environment") if "dozzle" in components else None,
        )

    wud, dozzle = environments(text)
    remote = yaml_value(dozzle, DOZZLE_KEY) if "dozzle" in components else None
    if ("wud" in components and not isinstance(wud, yaml.MappingNode)) or (
        "dozzle" in components and not isinstance(remote, yaml.ScalarNode)
    ):
        raise ValueError("The monitoring stack requires mapping-form WUD and Dozzle environment variables.")

    address = "${" + address_key + "}"
    port = "${" + PORT_VARIABLES[0] + "}"
    host, tcp_port = request["address"], str(request["ports"][0])
    desired = yaml.safe_load(text)
    edits = []
    additional = []
    changed = set()
    if "wud" in components:
        for suffix, expected, previous in (("HOST", address, host), ("PORT", port, tcp_port), ("CRON", WUD_CRON, None)):
            key = f"{request['watcher']}_{suffix}"
            node = yaml_value(wud, key)
            if node is None:
                additional.append(f"{key}: {expected}")
            elif suffix == "CRON" and isinstance(node, yaml.ScalarNode) and node.value.strip() and node.value != expected:
                continue
            elif not isinstance(node, yaml.ScalarNode) or node.value not in (expected, previous):
                raise ValueError("An existing WUD watcher conflicts with this device.")
            elif node.value == expected:
                if not wud.flow_style and node.style in ("'", '"'):
                    edits.append((node.start_mark.index, node.end_mark.index, expected))
                continue
            else:
                scalar = json.dumps(expected) if wud.flow_style else expected
                edits.append((node.start_mark.index, node.end_mark.index, scalar))
            desired["services"]["wud"]["environment"][key] = expected
            changed.add("wud")

    if "dozzle" in components:
        entry = f"tcp://{address}:{port}|{request['name']}"
        previous = f"tcp://{host}:{tcp_port}|{request['name']}"
        remotes = remote.value.split(",") if remote.value else []
        matches = [value for value in remotes if value.rsplit("|", 1)[-1] == request["name"]]
        if matches and (len(matches) != 1 or matches[0] not in (entry, previous)):
            raise ValueError("An existing Dozzle host conflicts with this device.")
        if matches != [entry]:
            if matches:
                remotes[remotes.index(matches[0])] = entry
            else:
                remotes.append(entry)
            changed.add("dozzle")
        value = ",".join(remotes)
        if "dozzle" in changed or remote.style in ("'", '"'):
            scalar = compose_scalar(value, dozzle)
            if "dozzle" in changed or scalar == value:
                edits.append((remote.start_mark.index, remote.end_mark.index, scalar))
        desired["services"]["dozzle"]["environment"][DOZZLE_KEY] = value

    for start, end, value in sorted(edits, reverse=True):
        text = text[:start] + value + text[end:]
    if additional:
        text = insert_yaml(text, environments(text)[0], additional)
    if yaml.safe_load(text) != desired:
        raise ValueError("Monitoring Compose YAML failed the preservation check.")
    return text, changed


def retire_compose(text, environment, request, address_key, owned):
    model = yaml.safe_load(text)
    services = yaml_value(yaml.compose(text, Loader=yaml.SafeLoader), "services")
    wud = (yaml_value(yaml_value(services, "wud"), "environment")
           if any(key.startswith("wud-") for key in owned) else None)
    dozzle = yaml_value(yaml_value(services, "dozzle"), "environment") if "dozzle" in owned else None
    address, port = "${" + address_key + "}", "${" + PORT_VARIABLES[0] + "}"
    edits, changed = [], set()
    fields = (("HOST", address, request["address"]), ("PORT", port, str(request["ports"][0])),
              ("CRON", WUD_CRON, WUD_CRON))
    for suffix, reference, literal in fields:
        component = "wud-" + suffix.lower()
        if component not in owned:
            continue
        key = f"{request['watcher']}_{suffix}"
        node = yaml_value(wud, key)
        if node is None:
            continue
        expected = (reference, literal) if request.get("legacy") else (reference,)
        if not isinstance(node, yaml.ScalarNode) or node.value not in expected or wud.flow_style:
            raise ValueError("The owned WUD watcher differs from the registered configuration.")
        line = text.rfind("\n", 0, node.start_mark.index) + 1
        end = text.find("\n", node.end_mark.index)
        if text[line:node.start_mark.index].strip() != key + ":":
            raise ValueError("The owned WUD setting is not an inline mapping value.")
        edits.append((line, len(text) if end < 0 else end + 1, ""))
        del model["services"]["wud"]["environment"][key]
        changed.add("wud")
    if "wud" in changed and not model["services"]["wud"]["environment"]:
        key_start = text.rfind("\n", 0, wud.start_mark.index - 1)
        environment_line = text.rfind("\n", 0, key_start) + 1
        colon = text.index(":", environment_line, key_start)
        if text[environment_line:colon].strip() != "environment":
            raise ValueError("The owned WUD environment has an unsupported format.")
        edits.append((colon + 1, colon + 1, " {}"))

    if "dozzle" in owned:
        node = yaml_value(dozzle, DOZZLE_KEY)
        if not isinstance(node, yaml.ScalarNode):
            raise ValueError("The monitoring stack requires a scalar Dozzle remote host list.")
        entries = node.value.split(",") if node.value else []
        matches = [entry for entry in entries if entry.rsplit("|", 1)[-1] == request["name"]]
        expected = f"tcp://{address}:{port}|{request['name']}"
        previous = f"tcp://{request['address']}:{request['ports'][0]}|{request['name']}"
        if matches and matches != [expected] and not (request.get("legacy") and matches == [previous]):
            raise ValueError("The owned Dozzle host differs from the registered configuration.")
        if matches:
            entries.remove(matches[0])
            value = ",".join(entries)
            edits.append((node.start_mark.index, node.end_mark.index, compose_scalar(value, dozzle)))
            model["services"]["dozzle"]["environment"][DOZZLE_KEY] = value
            changed.add("dozzle")
    for start, end, value in sorted(edits, reverse=True):
        text = text[:start] + value + text[end:]
    if yaml.safe_load(text) != model:
        raise ValueError("Monitoring retirement YAML failed the preservation check.")
    if "binding" in owned:
        reference = re.compile(r"\$\{?" + re.escape(address_key) + r"(?![A-Za-z0-9_])")
        if not reference.search(text):
            environment = re.sub(rf"(?m)^[ \t]*{re.escape(address_key)}[ \t]*=\[\[{re.escape(address_key)}\]\][ \t]*\r?\n?",
                                 "", environment)
    return text, environment, changed


class Beszel:
    records = "/api/collections/systems/records"

    def __init__(self, url, identity, password):
        self.service = JsonService("Beszel", url)
        authenticated = self.service.request(
            "POST", "/api/collections/users/auth-with-password",
            headers={"Content-Type": "application/json"}, operation="authenticate monitoring account",
            data={"identity": identity, "password": password},
        )
        self.account = authenticated["record"]["id"]
        self.headers = {"Content-Type": "application/json", "Authorization": authenticated["token"]}

    def system(self, name, address, port):
        query = urlencode({"filter": f'(name="{name}" || host="{address}")', "perPage": 50})
        records = self.service.request(
            "GET", f"{self.records}?{query}", headers=self.headers, operation="read monitoring devices",
        )
        if records["totalItems"] != len(records["items"]) or len(records["items"]) > 1:
            raise ValueError("Beszel returned ambiguous monitoring devices.")
        for item in records["items"]:
            if (item.get("name") != name or item.get("host") != address or str(item.get("port")) != str(port)
                    or self.account not in item.get("users", [])):
                raise ValueError("An existing Beszel system conflicts with this device.")
            return item
        return None

    def enroll(self, name, address, port):
        created = self.service.request(
            "POST", self.records, headers=self.headers, operation="add monitoring device",
            data={"name": name, "host": address, "port": str(port), "users": [self.account]},
        )
        current = self.system(name, address, port)
        if not current or current["id"] != created.get("id"):
            raise ValueError("Beszel did not report the enrolled monitoring device.")
        return current["id"]

    def retire(self, identifier, name, address, port):
        if not isinstance(identifier, str) or not re.fullmatch(r"[A-Za-z0-9]+", identifier):
            raise ValueError("The recorded Beszel ID is invalid.")
        current = self.system(name, address, port)
        if not current:
            return
        if current["id"] != identifier or current.get("users") != [self.account]:
            raise ValueError("The Beszel system was replaced or shared after enrollment.")
        self.service.request(
            "DELETE", f"{self.records}/{identifier}", headers=self.headers, operation="remove monitoring device",
        )
        if self.system(name, address, port):
            raise ValueError("Beszel still reports the retired system.")


class Monitoring:
    def __init__(self, komodo, host, stack, beszel, database=None):
        self.komodo = komodo
        self.host = normalize_host(host)
        self.stack = stack
        self.beszel = beszel
        self.database = database

    def scope(self, stack):
        service = self.beszel.service if self.beszel is not None else None
        return [self.host, stack["_id"]["$oid"], stack["config"]["project_name"],
                service.host if service else None, service.port if service else None]

    def check_scope(self, current, recorded, *, check_beszel):
        if current[:3] != recorded[:3] or (
            check_beszel and recorded[3:] != [None, None] and current[3:] != recorded[3:]
        ):
            raise ValueError("The monitoring hub does not match the recorded enrollment.")

    def save(self, device, registration):
        save_monitoring_registration(self.database, device["id"], registration)

    def hub(self, request, *, apply=False):
        command = shlex.join(["python3", "-c", STATE.read_text()])
        try:
            response = subprocess.run(
                ["ssh", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes", "-o", "ConnectTimeout=7",
                 self.host, command],
                input=json.dumps({**request, "apply": apply}), text=True, capture_output=True,
                check=True, timeout=115, env=system_environment(),
            )
        except (OSError, subprocess.SubprocessError) as error:
            raise ValueError("The monitoring hub could not be reached or did not finish its checks.") from error
        result = json.loads(response.stdout)
        if "error" in result:
            raise ValueError(result["error"])
        return result

    def run(self, device, *, apply=False, ipv4=None, provision=None, component=None):
        selected = COMPONENTS if component is None else (component,)
        endpoints = {ENDPOINTS[key] for key in selected}
        compose_components = tuple(key for key in selected if key in ("wud", "dozzle"))
        includes_beszel = "beszel" in selected
        name = device["name"]
        address_key, address, ports = monitoring_variables(self.komodo, name, ipv4, device=device,
                                                                 components=selected)
        stack = self.komodo._request("/read", "GetStack", {"stack": self.stack})
        config = stack["config"]
        original = config.get("file_contents")
        if (not original or not config.get("project_name") or any(
            config.get(key) for key in ("repo", "linked_repo", "files_on_host", "swarm_id")
        )):
            raise ValueError("Use the hub's existing inline Komodo monitoring stack.")
        request = {"name": name, "address": address, "ports": ports,
                   "project": config["project_name"], "watcher": watcher_prefix(name)}
        if component:
            request["components"] = list(selected)
        live = self.hub(request)
        exists = self.beszel.system(name, address, ports[2]) if includes_beszel else None
        environment = config.get("environment") or ""
        desired, changed = (compose_source(original, request, address_key, compose_components)
                            if compose_components else (original, set()))
        bindings = variable_bindings(environment, address_key, PORT_VARIABLES[0]) if compose_components else environment
        updates = {}
        if desired != original:
            updates["file_contents"] = desired
        if bindings != environment:
            updates["environment"] = bindings
        deploy = [service for service in compose_components if service in changed or not live[service]]
        ready = all(live[key] for key in endpoints)
        if not apply:
            live.pop("prometheus_configured", None)
            checks = {key: live[key] for key in (*selected, *sorted(endpoints)) if key != "beszel"}
            if includes_beszel:
                checks["beszel"] = bool(exists)
            return {"status": "preview", "device": name, "address": address, "endpoints_ready": ready,
                    "checks": checks, "stack_update": bool(updates)}
        if not ready and provision:
            provision(address, ports, any(live[key] for key in endpoints))
            for _ in range(4):
                live = self.hub(request)
                ready = all(live[key] for key in endpoints)
                if ready:
                    break
                sleep(2)
        if not ready:
            raise ValueError("The monitoring hub cannot reach the selected endpoints: " + ", ".join(sorted(endpoints)) + ".")
        identifier = stack["_id"]["$oid"]
        registration = None
        if self.database is not None:
            scope = self.scope(stack)
            registration = monitoring_registration(self.database, device["id"])
            if registration:
                self.check_scope(scope, registration["scope"], check_beszel=includes_beszel)
                recorded_ports = registration["ports"]
                if registration["address"] != address or any(
                    old is not None and new is not None and old != new for old, new in zip(recorded_ports, ports)
                ):
                    raise ValueError("The monitoring settings differ from the recorded enrollment.")
                registration["ports"] = [new if new is not None else old for old, new in zip(recorded_ports, ports)]
                if includes_beszel:
                    registration["scope"] = scope
            else:
                registration = {"scope": scope, "address": address, "ports": list(ports), "owned": {}}
            if updates:
                model = yaml.safe_load(original)["services"]
                owned = registration["owned"]
                if "wud" in compose_components:
                    old_wud = model["wud"]["environment"]
                    for suffix in WUD_FIELDS:
                        if f"{request['watcher']}_{suffix.upper()}" not in old_wud:
                            owned[f"wud-{suffix}"] = True
                if "dozzle" in compose_components:
                    old_dozzle = model["dozzle"]["environment"][DOZZLE_KEY].split(",")
                    if not any(value.rsplit("|", 1)[-1] == name for value in old_dozzle):
                        owned["dozzle"] = True
                if not any(key == address_key for key, _ in ASSIGNMENT.findall(environment)):
                    owned["binding"] = True
        if updates:
            latest = self.komodo._request("/read", "GetStack", {"stack": identifier})["config"]
            if latest.get("file_contents") != original or (latest.get("environment") or "") != environment:
                raise ValueError("The monitoring stack changed concurrently. Retry enrollment.")
            if registration and registration["owned"]:
                self.save(device, registration)
            updated = self.komodo._request("/write", "UpdateStack", {"id": identifier, "config": updates})["config"]
            if any(updated.get(key) != value for key, value in updates.items()):
                raise ValueError("Komodo did not save the monitoring stack configuration.")
        if deploy:
            update = self.komodo._request("/execute", "DeployStack", {"stack": identifier, "services": deploy})
            wait_update(self.komodo, update, monotonic() + 180)
        if includes_beszel and not exists:
            beszel_id = self.beszel.enroll(name, address, ports[2])
            if registration:
                registration["owned"]["beszel"] = beszel_id
                self.save(device, registration)
        if registration and "prometheus" in selected and not live["prometheus"]:
            # An existing job on disk may simply need a reload. Only claim a new job.
            if live.get("prometheus_configured") is False:
                registration["owned"]["prometheus"] = True
                self.save(device, registration)
        live = self.hub(request, apply="prometheus" in selected and not live["prometheus"])
        checks = {key: live[key] for key in (*selected, *sorted(endpoints)) if key != "beszel"}
        if includes_beszel:
            checks["beszel"] = bool(self.beszel.system(name, address, ports[2]))
        if not all(checks.values()):
            raise ValueError("Monitoring enrollment is incomplete. Review the preview and retry.")
        return {"status": "enrolled", "device": name, "address": address, "checks": checks}

    def adopt_retirement(self, device):
        name = device["name"]
        address_key, address, ports = monitoring_variables(self.komodo, name, None)
        stack = self.komodo._request("/read", "GetStack", {"stack": self.stack})
        config = stack["config"]
        model = yaml.safe_load(config["file_contents"])["services"]
        watcher = watcher_prefix(name)
        owned = {}
        for suffix in WUD_FIELDS:
            if f"{watcher}_{suffix.upper()}" in model["wud"]["environment"]:
                owned[f"wud-{suffix}"] = True
        remotes = model["dozzle"]["environment"][DOZZLE_KEY].split(",")
        if any(entry.rsplit("|", 1)[-1] == name for entry in remotes):
            owned["dozzle"] = True
        if (address_key, "[[" + address_key + "]]") in ASSIGNMENT.findall(config.get("environment") or ""):
            owned["binding"] = True
        owned["prometheus"] = True
        existing = self.beszel.system(name, address, ports[2])
        if existing:
            if existing.get("users") != [self.beszel.account]:
                raise ValueError("The existing Beszel system is shared.")
            owned["beszel"] = existing["id"]
        return {"scope": self.scope(stack), "address": address, "ports": list(ports), "owned": owned, "legacy": True}

    def retire(self, device, registration, *, component=None):
        owned = registration["owned"]
        selected = {key: value for key, value in owned.items()
                    if component is None or key == component or key.startswith(component + "-")}
        if not selected:
            return {"status": "absent", "components": []}
        if component in ("wud", "dozzle") and "binding" in owned:
            selected["binding"] = owned["binding"]
        stack = self.komodo._request("/read", "GetStack", {"stack": self.stack})
        self.check_scope(self.scope(stack), registration["scope"], check_beszel="beszel" in selected)
        config = stack["config"]
        if not config.get("project_name") or any(config.get(key) for key in ("repo", "linked_repo", "files_on_host", "swarm_id")):
            raise ValueError("Use the recorded hub's inline Komodo monitoring stack.")
        identifier = stack["_id"]["$oid"]
        name = device["name"]
        address, ports = registration["address"], registration["ports"]
        request = {"name": name, "address": address, "ports": ports, "project": config["project_name"],
                   "watcher": watcher_prefix(name), "retire": list(selected)}
        request["components"] = [key for key in COMPONENTS
                                 if key in selected or any(field.startswith(key + "-") for field in selected)]
        if registration.get("legacy"):
            request["legacy"] = True
        original, environment = config["file_contents"], config.get("environment") or ""
        address_key = name.upper().replace("-", "_") + "_IP"
        compose, bindings, changed = retire_compose(original, environment, request, address_key, selected)
        live = self.hub(request)
        if monitoring_registration(self.database, device["id"]) != registration:
            self.save(device, registration)
        if live.get("wud") and any(f"wud-{field}" in selected for field in WUD_FIELDS):
            changed.add("wud")
        if live.get("dozzle") and "dozzle" in selected:
            changed.add("dozzle")
        if compose != original or bindings != environment:
            latest = self.komodo._request("/read", "GetStack", {"stack": identifier})["config"]
            if latest.get("file_contents") != original or (latest.get("environment") or "") != environment:
                raise ValueError("The monitoring stack changed concurrently. Retry retirement.")
            updates = {"file_contents": compose, "environment": bindings}
            updated = self.komodo._request("/write", "UpdateStack", {"id": identifier, "config": updates})["config"]
            if any(updated.get(key) != value for key, value in updates.items()):
                raise ValueError("Komodo did not save the monitoring retirement.")
        if changed:
            update = self.komodo._request("/execute", "DeployStack", {"stack": identifier, "services": sorted(changed)})
            wait_update(self.komodo, update, monotonic() + 180)
        live = self.hub(request, apply="prometheus" in selected and not live["prometheus"])
        if ((live.get("wud") and any(f"wud-{field}" in selected for field in WUD_FIELDS))
                or (live.get("dozzle") and "dozzle" in selected) or ("prometheus" in selected and not live["prometheus"])):
            raise ValueError("The hub still reports retired monitoring resources.")
        if "beszel" in selected:
            self.beszel.retire(selected["beszel"], name, address, ports[2])
        removed = set(selected)
        if "binding" in removed and bindings == environment and any(key == address_key for key, _ in ASSIGNMENT.findall(environment)):
            removed.remove("binding")
        remaining = {key: value for key, value in owned.items() if key not in removed}
        self.save(device, {**registration, "owned": remaining} if remaining and component else None)
        return {"status": "removed", "components": sorted(removed)}


def configured_monitoring(komodo, database, *, retire=False, adopt=False):
    host = os.environ.get("ONBOARDRR_MONITORING_HOST")
    if not host:
        return None

    def enroll(device, *, apply=False, ipv4=None, provision=None, registration=None, component=None):
        with provisioning_lock(database, "monitoring-hub"):
            beszel = None
            if component in (None, "beszel"):
                bao = configured_openbao()
                if not bao:
                    raise ValueError("Configure OpenBao to supply the Beszel monitoring account.")
                account, _ = bao.fields_with_version(
                    "deployments/monitoring", "beszel_identity", "beszel_password", "beszel_agent_key",
                )
                identity = account.get("beszel_identity")
                if not isinstance(identity, str) or not identity:
                    raise ValueError("Configure the Beszel account identity in OpenBao.")
                password = account.get("beszel_password")
                if password is None:
                    password, = configured_openbao(mount="services").read("beszel/PASSWORD", "value")
                if not isinstance(password, str) or not password:
                    raise ValueError("Configure the Beszel account password in OpenBao.")
                beszel = Beszel(os.environ.get("ONBOARDRR_BESZEL_URL", ""), identity, password)
            monitoring = Monitoring(komodo, host, os.environ.get("ONBOARDRR_MONITORING_STACK", "zero-monitoring"),
                                    beszel, database)
            if retire:
                if monitoring_registration(database, device["id"]) != registration:
                    raise ValueError("Monitoring ownership changed concurrently. Retry retirement.")
                if adopt:
                    adopted = monitoring.adopt_retirement(device)
                    if registration:
                        if any(registration[key] != adopted[key] for key in ("scope", "address", "ports")):
                            raise ValueError("The monitoring settings differ from the recorded enrollment.")
                        adopted["owned"].update(registration["owned"])
                    registration = adopted
                return monitoring.retire(device, registration, component=component) if registration else {"status": "absent"}
            def provision_nas(address, ports, existing_endpoints):
                from .nas_monitoring import compose_payload

                key = None
                if component in (None, "beszel"):
                    key = account.get("beszel_agent_key")
                    if key is None:
                        key, = configured_openbao(mount="services").read("beszel/AGENT_KEY", "value")
                    if not isinstance(key, str):
                        raise ValueError("Configure the Beszel agent key in OpenBao.")
                provision(compose_payload(address, ports, key, component=component), existing_endpoints)

            try:
                return monitoring.run(device, apply=apply, ipv4=ipv4,
                                      provision=provision_nas if provision else None, component=component)
            except yaml.YAMLError:
                raise ValueError("The monitoring stack contains invalid YAML.") from None

    return enroll
