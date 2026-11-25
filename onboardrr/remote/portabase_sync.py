"""Synchronize database containers with their local Portabase agent file."""

import json
import os
import re
import stat
import subprocess
import sys
from base64 import b64decode
from pathlib import Path
from tempfile import NamedTemporaryFile
from uuid import UUID, uuid4


ENGINES = {
    "postgres": ("postgresql", 5432, "POSTGRES_DB", ("POSTGRES_USER",), ("POSTGRES_PASSWORD",)),
    "postgres-documentdb": ("postgresql", 5432, "POSTGRES_DB", ("POSTGRES_USER",), ("POSTGRES_PASSWORD",)),
    "mysql": ("mysql", 3306, "MYSQL_DATABASE", ("MYSQL_USER",), ("MYSQL_PASSWORD",)),
    "mariadb": ("mariadb", 3306, "MARIADB_DATABASE", ("MARIADB_USER", "MYSQL_USER"),
                ("MARIADB_PASSWORD", "MYSQL_PASSWORD")),
    "mongo": ("mongodb", 27017, "MONGO_INITDB_DATABASE", ("MONGO_INITDB_ROOT_USERNAME",),
              ("MONGO_INITDB_ROOT_PASSWORD",)),
    "mongodb": ("mongodb", 27017, "MONGO_INITDB_DATABASE", ("MONGO_INITDB_ROOT_USERNAME",),
                ("MONGO_INITDB_ROOT_PASSWORD",)),
    "redis": ("redis", 6379, None, ("REDIS_USER",), ("REDIS_PASSWORD",)),
    "valkey": ("valkey", 6379, None, ("VALKEY_USER",), ("VALKEY_PASSWORD",)),
    "mssql": ("mssql", 1433, "MSSQL_DATABASE", ("MSSQL_USER",), ("MSSQL_SA_PASSWORD",)),
    "server": ("mssql", 1433, "MSSQL_DATABASE", ("MSSQL_USER",), ("MSSQL_SA_PASSWORD",)),
    "firebird": ("firebird", 3050, "FIREBIRD_DATABASE", ("FIREBIRD_USER",), ("FIREBIRD_PASSWORD",)),
}
PREFIX = "onboardrr.portabase."
NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}")
DATABASE_SUFFIX = re.compile(r"[-_.](?:postgres(?:ql)?|mongo(?:db)?|mysql|mariadb|redis|valkey|mssql|firebird|db)$")


def docker(*args):
    env = os.environ.copy()
    env.pop("DOCKER_CONTEXT", None)
    return subprocess.check_output(
        ["docker", "--host", "unix:///var/run/docker.sock", *args], env=env,
        stderr=subprocess.DEVNULL, text=True, timeout=35,
    )


def read_json(path, default):
    if not os.path.lexists(path):
        return default
    info = path.lstat()
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o022
            or info.st_size > 2 * 1024 * 1024):
        raise ValueError("Use operator-owned, regular Portabase configuration files.")
    return json.loads(path.read_text())


def environment(container):
    return dict(item.split("=", 1) for item in container["Config"].get("Env") or [] if "=" in item)


def image_repository(container):
    image = container["Config"]["Image"].split("@", 1)[0]
    namespace, separator, name = image.rpartition("/")
    return namespace + separator + name.split(":", 1)[0]


def source_id(container, name):
    labels = container["Config"].get("Labels") or {}
    project = labels.get("com.docker.compose.project", "")
    service = labels.get("com.docker.compose.service", "")
    if NAME.fullmatch(project) and NAME.fullmatch(service):
        return f"{project}/{service}"
    return name


def first(env, keys):
    return next((env[key] for key in keys if env.get(key)), "")


def entry_for(container, spec, agent_networks):
    name = container["Name"].lstrip("/")
    labels = container["Config"].get("Labels") or {}
    engine, default_port, db_key, users, passwords = spec
    env = environment(container)
    database = labels.get(PREFIX + "database") or env.get(db_key)
    if engine == "postgresql" and not database:
        database = env.get("POSTGRES_USER")
    if engine in {"mysql", "mariadb"} and not database:
        database = env.get("MYSQL_DATABASE")
    if engine in {"redis", "valkey"} and not database:
        database = "0"
    if not database:
        return None, "Database name is unavailable"
    username, password = first(env, users), first(env, passwords)
    if engine == "postgresql" and not username:
        username = "postgres"
    if engine == "mssql" and not username:
        username = "sa"
    if not password and engine in {"mysql", "mariadb"} and not username:
        username = "root"
        password = first(env, ("MARIADB_ROOT_PASSWORD", "MYSQL_ROOT_PASSWORD"))
    if engine in {"redis", "valkey"} and not password:
        command = container["Config"].get("Cmd") or []
        if any("--requirepass" in argument for argument in command) or any(env.get(key + "_FILE") for key in passwords):
            return None, "Database credentials are unavailable"
    elif not (username and password):
        return None, "Database credentials are unavailable"
    networks = (container.get("NetworkSettings") or {}).get("Networks") or {}
    if not (agent_networks & set(networks)):
        return None, "No Docker network shared with the Portabase agent"
    port = labels.get(PREFIX + "port") or str(default_port)
    if not port.isascii() or not port.isdecimal() or not 1 <= int(port) <= 65535:
        return None, "Invalid database port label"
    return {"name": name, "database": database, "type": engine, "host": name, "port": int(port),
            "username": username, "password": password}, None


def discover(containers, agent):
    targets, known, ambiguous, skipped = {}, set(), set(), []
    agent_networks = set((agent["NetworkSettings"].get("Networks") or {}).keys())
    for container in containers:
        name = container["Name"].lstrip("/")
        if not NAME.fullmatch(name):
            continue
        identifier = source_id(container, name)
        if identifier in known:
            ambiguous.add(identifier)
        known.add(identifier)
        labels = container["Config"].get("Labels") or {}
        repository = image_repository(container)
        spec = ENGINES.get(repository.rsplit("/", 1)[-1])
        if repository.rsplit("/", 1)[-1] == "server" and not repository.endswith("/mssql/server"):
            continue
        if not spec or labels.get(PREFIX + "skip", "").lower() == "true":
            continue
        if not container["State"]["Running"]:
            skipped.append({"container": name, "reason": "Container is stopped"})
            continue
        entry, reason = entry_for(container, spec, agent_networks)
        if reason:
            skipped.append({"container": name, "reason": reason})
        else:
            targets[identifier] = entry
    for identifier in targets.keys() & ambiguous:
        skipped.append({"container": targets.pop(identifier)["name"], "reason": "Several containers share the Compose service"})
    return targets, known, skipped


def agent_paths(agent):
    filename = environment(agent).get("DATABASES_CONFIG_FILE") or "config.json"
    if not NAME.fullmatch(filename) or not filename.endswith(".json") or filename == "dashboard_databases.json":
        raise ValueError("Use a JSON configuration file for the local Portabase agent.")
    sources = [Path(mount["Source"]) for mount in agent["Mounts"]
               if mount["Destination"] == "/config" and mount["Type"] == "bind"]
    if len(sources) != 1 or not sources[0].is_absolute():
        raise ValueError("Mount the Portabase agent configuration directory at /config.")
    folder = sources[0]
    info = folder.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o022:
        raise ValueError("Use an operator-owned Portabase configuration directory.")
    return folder / filename, folder / ".onboardrr-databases.json"


def source_key(entry):
    engine = entry.get("type")
    if engine == "postgresql-cluster":
        engine = "postgresql"
    address = entry.get("host")
    host = address.casefold().rstrip(".") if isinstance(address, str) else ""
    database = None if entry.get("type") == "postgresql-cluster" else entry.get("database")
    return engine, host, database


def project_hints(document, containers):
    hints = {}
    for container in containers:
        name = container["Name"].lstrip("/").casefold()
        labels = container["Config"].get("Labels") or {}
        stack = labels.get("docker.stack") or ""
        compose = labels.get("com.docker.compose.project") or ""
        stack = stack.casefold() if NAME.fullmatch(stack) else ""
        compose = compose.casefold() if NAME.fullmatch(compose) else ""
        if NAME.fullmatch(name) and (stack or compose):
            hints[name] = {"container": name, "application": DATABASE_SUFFIX.sub("", name),
                           "stack": stack, "compose": compose}
    return {entry["generated_id"]: hints[host] for entry in document["databases"]
            if (host := source_key(entry)[1]) in hints}


def write_json(path, value):
    with NamedTemporaryFile(mode="w", dir=path.parent, delete=False) as stream:
        staged = Path(stream.name)
        try:
            stream.write(json.dumps(value, indent=2) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
            staged.replace(path)
        finally:
            staged.unlink(missing_ok=True)


def validate_state(state):
    if not isinstance(state, dict) or len(state) > 4096:
        raise ValueError("Invalid Portabase source inventory.")
    ids = set()
    for source, record in state.items():
        if (len(source.split("/")) > 2 or not all(NAME.fullmatch(part) for part in source.split("/"))
                or not isinstance(record, dict) or set(record) != {"id", "name", "type", "database"}
                or not all(isinstance(value, str) and value for value in record.values())
                or not NAME.fullmatch(record["name"]) or str(UUID(record["id"])) != record["id"]
                or record["id"] in ids):
            raise ValueError("Invalid Portabase source inventory.")
        ids.add(record["id"])


def reconcile(document, state, targets, known):
    if not isinstance(document, dict) or not isinstance(entries := document.get("databases"), list):
        raise ValueError("Invalid Portabase agent configuration.")
    if any(not isinstance(entry, dict) or not isinstance(entry.get("generated_id"), str) for entry in entries):
        raise ValueError("Invalid Portabase agent database entry.")
    existing = {entry["generated_id"]: entry for entry in entries}
    if len(existing) != len(entries):
        raise ValueError("Portabase database IDs must be unique.")
    managed_ids = {record["id"] for record in state.values()}
    manual = {source_key(entry) for entry in entries if entry["generated_id"] not in managed_ids}
    added, updated, removed, skipped = [], [], [], []
    for source, entry in targets.items():
        engine, host, database = source_key(entry)
        if (engine, host, database) in manual or (engine, host, None) in manual:
            skipped.append({"container": entry["name"], "reason": "Already covered by a manual source"})
            continue
        record = state.get(source)
        if record and (record["type"], record["database"]) != (engine, database):
            skipped.append({"container": entry["name"], "reason": "The managed source changed its database identity"})
            continue
        identifier = record["id"] if record else str(uuid4())
        state[source] = {"id": identifier, "name": entry["name"], "type": engine, "database": database}
        item = {**entry, "generated_id": identifier}
        if identifier not in existing:
            added.append(entry["name"])
        elif existing[identifier] != item:
            updated.append(entry["name"])
        existing[identifier] = item
    retired = {}
    for source in sorted(state.keys() - known):
        record = state[source]
        if existing.pop(record["id"], None) is not None:
            removed.append(record["name"])
        retired[source] = record["id"]
    document["databases"] = list(existing.values())
    managed = sorted(record["id"] for source, record in state.items() if source in known and record["id"] in existing)
    return {"added": sorted(added), "updated": sorted(updated), "removed": sorted(removed),
            "skipped": skipped, "retired": retired, "managed": managed}


def sync(request):
    ids = docker("ps", "--all", "--quiet", "--no-trunc").split()
    if not ids:
        raise ValueError("No containers found on the local Docker engine.")
    containers = json.loads(docker("inspect", *ids))
    if len(containers) != len(ids):
        raise ValueError("The Docker inventory changed during discovery. Retry the operation.")
    agents = [container for container in containers if image_repository(container).split("/")[-2:] == ["portabase", "agent"]
              and container["State"]["Running"]]
    if len(agents) != 1:
        raise ValueError("A single running Portabase agent is required on this host.")
    agent, = agents
    key = environment(agent).get("EDGE_KEY", "").strip()
    try:
        identity = json.loads(key if key.startswith("{") else b64decode(key, validate=True))
        agent_id = str(UUID(identity["agentId"]))
        dashboard = identity["serverUrl"]
        if not isinstance(dashboard, str):
            raise ValueError
    except (KeyError, TypeError, ValueError):
        raise ValueError("The Portabase agent has an invalid Edge Key.") from None

    config, index = agent_paths(agent)
    document = read_json(config, {"databases": []})
    state = read_json(index, {})
    validate_state(state)
    previous = json.dumps(document)
    previous_state = dict(state)
    targets, known, skipped = discover(containers, agent)
    dashboard_sources = request.get("dashboard_sources", {}).get(agent_id, ())
    for source in sorted(set(dashboard_sources) & targets.keys()):
        skipped.append({"container": targets.pop(source)["name"], "reason": "Already managed through the dashboard"})
    report = reconcile(document, state, targets, known)
    report["skipped"].extend(skipped)
    report.update(agent_id=agent_id, origin=dashboard, project_hints=project_hints(document, containers))
    if not request.get("apply"):
        return report

    changed = json.dumps(document) != previous
    agent_user = (agent["Config"].get("User") or "").partition(":")[0]
    if changed and agent_user not in ("", "0", "root", str(os.getuid())):
        raise ValueError("The Portabase agent user cannot read the operator's private configuration.")
    if set(docker("ps", "--all", "--quiet", "--no-trunc").split()) != set(ids):
        raise ValueError("The Docker inventory changed during discovery. Retry the operation.")
    if json.dumps(read_json(config, {"databases": []})) != previous or read_json(index, {}) != previous_state:
        raise ValueError("The Portabase agent configuration changed concurrently. Retry the operation.")
    if state != previous_state:
        write_json(index, state)
    if changed:
        write_json(config, document)
        try:
            docker("restart", agent["Id"])
            if docker("inspect", "--format", "{{.State.Running}}", agent["Id"]).strip() != "true":
                raise ValueError("The Portabase agent stopped after restart.")
        except (OSError, ValueError, subprocess.SubprocessError):
            write_json(config, json.loads(previous))
            try:
                docker("restart", agent["Id"])
            except (OSError, subprocess.SubprocessError):
                raise ValueError("The previous Portabase configuration was restored. Restart the agent.") from None
            raise ValueError("Could not apply the Portabase configuration. The previous configuration was restored.") from None
    return report


if __name__ == "__main__":
    try:
        print(json.dumps(sync(json.load(sys.stdin))))
    except ValueError as error:
        print(json.dumps({"error": str(error) if type(error) is ValueError else "Invalid Portabase configuration."}))
    except (OSError, KeyError, TypeError, AttributeError, subprocess.SubprocessError):
        print(json.dumps({"error": "Could not inspect Docker or update the local Portabase agent."}))
