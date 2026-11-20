"""Inspect and enroll targets in the hub's local monitoring services."""

import fcntl
import hashlib
import http.client
import ipaddress
import json
import os
import re
import socket
import stat
import subprocess
import sys
import time
from contextlib import nullcontext
from pathlib import Path

import yaml


NAME = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?")
LAN = tuple(ipaddress.IPv4Network(block) for block in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16"))
PROMETHEUS_PATH = "/etc/prometheus/prometheus.yml"


def validate_target(name, addr, ports):
    if not NAME.fullmatch(name):
        raise ValueError("Use a valid monitoring device name.")
    address = ipaddress.IPv4Address(addr)
    if not any(address in network for network in LAN):
        raise ValueError("Monitoring endpoints must use a private LAN IPv4 address.")
    if any(type(port) is not int or not 1 <= port <= 65535 for port in ports):
        raise ValueError("Use valid TCP ports for monitoring.")
    return str(address)


def yaml_value(node, key):
    if not isinstance(node, yaml.MappingNode):
        raise ValueError("Unsupported monitoring YAML format.")
    values = [value for name, value in node.value if isinstance(name, yaml.ScalarNode) and name.value == key]
    if len(values) > 1:
        raise ValueError(f"Duplicate monitoring YAML field {key}.")
    return values[0] if values else None


def insert_yaml(text, block, new_lines):
    if block.flow_style:
        raise ValueError("Use block-form monitoring YAML for managed entries.")
    end = block.end_mark
    offset = text.rfind("\n", 0, end.index) + 1 if end.column and end.index < len(text) else end.index
    prefix = "\n" if offset and text[offset - 1] != "\n" else ""
    indent = " " * block.start_mark.column
    return text[:offset] + prefix + "".join(indent + line + "\n" for line in new_lines) + text[offset:]


def yaml_content_end(node):
    if isinstance(node, yaml.ScalarNode) or node.flow_style:
        return node.end_mark.index
    if isinstance(node, yaml.MappingNode):
        return max(yaml_content_end(value) for _, value in node.value)
    return max(yaml_content_end(value) for value in node.value)


def prometheus_source(text, name, target, *, remove=False):
    document = yaml.compose(text, Loader=yaml.SafeLoader)
    jobs = yaml_value(document, "scrape_configs")
    if not isinstance(jobs, yaml.SequenceNode):
        raise ValueError("Prometheus requires a block-form scrape_configs list.")
    current = yaml.safe_load(text)
    if not all(isinstance(job, dict) for job in current["scrape_configs"]):
        raise ValueError("Prometheus scrape jobs must be mappings.")
    matches = [job for job in current["scrape_configs"] if job.get("job_name") == name]
    job = {"job_name": name, "static_configs": [{"targets": [target]}]}
    if remove:
        if not matches:
            return text
        if matches != [job] or jobs.flow_style:
            raise ValueError("The Prometheus job differs from the device's managed job.")
        index = current["scrape_configs"].index(job)
        item = jobs.value[index]
        start = text.rfind("\n", 0, item.start_mark.index) + 1
        newline = text.find("\n", yaml_content_end(item))
        end = len(text) if newline < 0 else newline + 1
        remaining = current["scrape_configs"]
        remaining.pop(index)
        if not remaining:
            start = jobs.start_mark.index
            desired = text[:start] + "[]\n" + text[end:]
        else:
            desired = text[:start] + text[end:]
        if yaml.safe_load(desired) != current:
            raise ValueError("Prometheus YAML failed the preservation check.")
        return desired
    if matches:
        if len(matches) != 1 or not any(
            target in group.get("targets", []) for group in matches[0].get("static_configs", [])
        ):
            raise ValueError("An existing Prometheus job uses this name with different targets.")
        return text
    lines = yaml.safe_dump([job], sort_keys=False).splitlines()
    if jobs.flow_style and not jobs.value:
        indent = " " * (document.start_mark.column + 2)
        desired = text[:jobs.start_mark.index] + "\n" + "\n".join(indent + line for line in lines) + text[jobs.end_mark.index:]
    else:
        desired = insert_yaml(text, jobs, lines)
    current["scrape_configs"] = [*current["scrape_configs"], job]
    if yaml.safe_load(desired) != current:
        raise ValueError("Prometheus YAML failed the preservation check.")
    return desired


def docker(*args, data=None):
    environment = {**os.environ}
    for name in ("DOCKER_HOST", "DOCKER_CONTEXT"):
        environment.pop(name, None)
    try:
        return subprocess.run(
            ["docker", *args], input=data, capture_output=True, check=True, timeout=25, env=environment,
        ).stdout
    except (OSError, subprocess.SubprocessError) as error:
        raise ValueError(f"Monitoring hub Docker operation failed ({args[0]}).") from error


def inspect_container(name, service, project, *, required=True):
    if not required:
        candidates = docker(
            "ps", "--all", "--quiet", "--no-trunc",
            "--filter", f"label=com.docker.compose.project={project}",
            "--filter", f"label=com.docker.compose.service={service}",
        ).decode().split()
        if not candidates:
            return None
        if len(candidates) != 1:
            raise ValueError(f"The monitoring hub has multiple {service} containers.")
        name = candidates[0]
    try:
        container, = json.loads(docker("inspect", name))
    except (TypeError, ValueError) as error:
        raise ValueError(f"The monitoring hub's {service} container is unavailable.") from error
    labels = container["Config"]["Labels"]
    if labels.get("com.docker.compose.service") != service or labels.get("com.docker.compose.project") != project:
        raise ValueError(f"The monitoring hub's {service} container does not match its Compose project.")
    if not container["State"]["Running"]:
        if not required:
            return None
        raise ValueError(f"The monitoring hub's {service} container is stopped.")
    return container


def active_prometheus(container, name, target):
    raw = docker("exec", container, "wget", "-qO-", "http://127.0.0.1:9090/api/v1/status/config")
    try:
        config = json.loads(raw)["data"]["yaml"]
        return prometheus_source(config, name, target) == config
    except (KeyError, TypeError, ValueError, yaml.YAMLError):
        raise ValueError("Prometheus returned an invalid active configuration.") from None


def reload_prometheus(container, name, target, *, present=True):
    docker("kill", "--signal", "HUP", container)
    for _ in range(15):
        if active_prometheus(container, name, target) == present:
            return
        time.sleep(1)
    raise ValueError("Prometheus did not load the monitoring target.")


def private_state():
    state = Path.home() / ".local/state/onboardrr/monitoring"
    state.mkdir(parents=True, exist_ok=True, mode=0o700)
    if state.is_symlink() or state.stat().st_uid != os.getuid() or state.stat().st_mode & 0o077:
        raise ValueError("The monitoring backup directory must be private.")
    return state


def edit_prometheus(container, name, target, original, desired, *, present=True):
    temporary = docker("exec", "-u", "0", container, "mktemp", "/tmp/onboardrr-prometheus.XXXXXX").decode().strip()
    if not re.fullmatch(r"/tmp/onboardrr-prometheus\.[A-Za-z0-9]+", temporary):
        raise ValueError("Prometheus returned an invalid validation file path.")
    try:
        docker("exec", "-i", "-u", "0", container, "sh", "-c", 'cat > "$1"', "sh", temporary,
               data=desired.encode())
        docker("exec", "-u", "0", container, "promtool", "check", "config", temporary)
        # Keep the bind-mount inode. Only replace the bytes checked by promtool.
        if docker("exec", container, "cat", PROMETHEUS_PATH) != original.encode():
            raise ValueError("The Prometheus configuration changed concurrently. Retry enrollment.")
        digest = hashlib.sha256(original.encode()).hexdigest()
        backup = private_state() / f"prometheus-{digest}.yml"
        try:
            with os.fdopen(os.open(backup, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w") as stream:
                stream.write(original)
        except FileExistsError:
            pass
        try:
            docker("exec", "-i", "-u", "0", container, "sh", "-c", 'cat > "$1"', "sh", PROMETHEUS_PATH,
                   data=desired.encode())
            reload_prometheus(container, name, target, present=present)
        except ValueError:
            docker("exec", "-i", "-u", "0", container, "sh", "-c", 'cat > "$1"', "sh", PROMETHEUS_PATH,
                   data=original.encode())
            reload_prometheus(container, name, target, present=not present)
            raise
    finally:
        docker("exec", "-u", "0", container, "rm", "-f", "--", temporary)


def health(address, port, path, expected):
    connection = http.client.HTTPConnection(address, port, timeout=5)
    try:
        connection.request("GET", path)
        response = connection.getresponse()
        return response.status == 200 and expected in response.read(8192)
    except (OSError, http.client.HTTPException):
        return False
    finally:
        connection.close()


def reconcile_prometheus(project, name, target, *, apply, node_ready, remove=False):
    lock = nullcontext()
    if apply:
        descriptor = os.open(private_state() / "prometheus.lock", os.O_CREAT | os.O_RDONLY | os.O_NOFOLLOW, 0o600)
        lock = os.fdopen(descriptor, "rb")
    with lock as file:
        if file is not None:
            metadata = os.fstat(file.fileno())
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.getuid() or metadata.st_mode & 0o077:
                raise ValueError("The Prometheus update lock is invalid.")
            try:
                fcntl.flock(file, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise ValueError("Another Prometheus update is in progress. Retry enrollment.") from None
        container = inspect_container("prometheus", "prometheus", project)
        if sum(m["Destination"] == PROMETHEUS_PATH and m["RW"] for m in container.get("Mounts", [])) != 1:
            raise ValueError("Prometheus must use the expected writable configuration mount.")
        identifier = container["Id"]
        original = docker("exec", identifier, "cat", PROMETHEUS_PATH).decode()
        desired = prometheus_source(original, name, target, remove=remove)
        active = active_prometheus(identifier, name, target)
        satisfied = desired == original and active != remove
        if not apply or satisfied:
            return satisfied, desired == original
        if not remove and not node_ready:
            raise ValueError("Prometheus cannot reach the new host's Node Exporter.")
        if desired == original:
            reload_prometheus(identifier, name, target, present=not remove)
        else:
            edit_prometheus(identifier, name, target, original, desired, present=not remove)
        return True, True


def execute(request):
    name = request["name"]
    address = request["address"]
    docker_port, node_port, beszel_port = request["ports"]
    selected = request.get("components", ("wud", "dozzle", "prometheus", "beszel"))
    required_ports = []
    if "wud" in selected or "dozzle" in selected:
        required_ports.append(docker_port)
    if "prometheus" in selected:
        required_ports.append(node_port)
    if "beszel" in selected:
        required_ports.append(beszel_port)
    address = validate_target(name, address, required_ports)
    project = request["project"]
    owned = request.get("retire")
    result = {}
    if owned is None:
        if "beszel" in selected:
            try:
                with socket.create_connection((address, beszel_port), timeout=5):
                    result["beszel_agent"] = True
            except OSError:
                result["beszel_agent"] = False
        if "wud" in selected or "dozzle" in selected:
            result["docker_api"] = health(address, docker_port, "/_ping", b"OK")
        if "prometheus" in selected:
            result["node_exporter"] = health(address, node_port, "/metrics", b"# HELP")
    for service in ("wud", "dozzle"):
        if service not in selected:
            continue
        record = inspect_container(service, service, project, required=False)
        if not record:
            result[service] = False
            continue
        entries = dict(line.split("=", 1) for line in record["Config"]["Env"] if "=" in line)
        if service == "wud":
            prefix = request["watcher"]
            fields = {
                "wud-host": entries.get(f"{prefix}_HOST") == address,
                "wud-port": entries.get(f"{prefix}_PORT") == str(docker_port),
                "wud-cron": bool(entries.get(f"{prefix}_CRON")),
            }
            result["wud"] = all(fields.values()) if owned is None else any(fields[field] for field in owned if field in fields)
        else:
            result["dozzle"] = f"tcp://{address}:{docker_port}|{name}" in entries.get("DOZZLE_REMOTE_HOST", "").split(",")
    if "prometheus" in selected and (owned is None or "prometheus" in owned):
        active, configured = reconcile_prometheus(
            project, name, f"{address}:{node_port}", apply=request.get("apply"),
            node_ready=result.get("node_exporter"), remove=owned is not None,
        )
        result["prometheus"] = active
        if owned is None:
            result["prometheus_configured"] = configured
    return result


if __name__ == "__main__":
    try:
        print(json.dumps(execute(json.load(sys.stdin))))
    except (OSError, TypeError, KeyError, ValueError, yaml.YAMLError) as error:
        # Parser diagnostics may include private configuration values.
        detail = str(error) if type(error) is ValueError else "Invalid monitoring hub configuration."
        print(json.dumps({"error": detail}))
