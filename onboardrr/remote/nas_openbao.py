"""Provision the OpenBao Compose agents with Container Station's local Docker engine."""

import json
import os
import re
import stat
import subprocess
import sys
import time
from base64 import b64decode
from pathlib import Path
from tempfile import NamedTemporaryFile


def docker(*arguments):
    environment = os.environ.copy()
    for name in ("DOCKER_CONTEXT", "DOCKER_HOST", "COMPOSE_FILE", "COMPOSE_PROJECT_NAME", "HOSTNAME"):
        environment.pop(name, None)
    environment.setdefault("DOCKER_CONFIG", str(Path.home() / ".docker"))
    environment["COMPOSE_REMOVE_ORPHANS"] = "false"
    try:
        return subprocess.check_output(["docker", *arguments], env=environment, text=True,
                                       stderr=subprocess.DEVNULL, timeout=180).strip()
    except (OSError, subprocess.SubprocessError):
        raise ValueError("Container Station could not deploy the OpenBao Agent.") from None


def directory(path):
    if path.is_symlink() or (path.exists() and (not path.is_dir() or path.stat().st_uid != os.getuid()
                                                or path.stat().st_mode & 0o022)):
        raise ValueError("The NAS OpenBao directories must be owned by the operator and protected.")
    path.mkdir(mode=0o700, exist_ok=True)


def check_private_file(path, *, allow_readonly=False):
    if path.is_symlink():
        raise ValueError("Existing NAS OpenBao files must be private and owned by the operator.")
    if not path.exists():
        return
    info = path.stat()
    mode = stat.S_IMODE(info.st_mode)
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or (
        mode != 0o600 and not (allow_readonly and mode == 0o400)
    ):
        raise ValueError("Existing NAS OpenBao files must be private and owned by the operator.")


def write_private(path, contents):
    check_private_file(path)
    if path.is_file() and path.read_bytes() == contents:
        return False
    with NamedTemporaryFile(dir=path.parent, delete=False) as stream:
        staged = Path(stream.name)
        try:
            stream.write(contents)
            stream.flush()
            os.replace(staged, path)
        finally:
            staged.unlink(missing_ok=True)
    return True


def agent_service(variant, request, root):
    name = f"openbao-agent-{variant}"
    output = "cache" if variant == "compose" else "rendered"
    arguments = ["agent", "-config=/agent/agent.hcl"]
    device = request["device"]
    transport, endpoint = (
        ("socket", "/var/run/docker.sock") if request.get("kuma_local")
        else ("tcp", f'http://{request["address"]}:{request["port"]}')
    )
    label = ", ".join(json.dumps(value) for value in (device, transport, endpoint))
    return {
        "container_name": name, "image": request["image"], "restart": "always",
        "user": f"{os.getuid()}:{os.getgid()}",
        "labels": {
            "docker.stack": "secrets", "homepage.href": request["agent"]["url"].replace("$", "$$"),
            "homepage.group": f"Docker {device}", "homepage.icon": "sh-openbao",
            "homepage.name": name, "komodo.skip": "", "kuma.__docker": label,
            "wud.display.icon": "sh-openbao", "wud.display.name": name,
            "wud.link.template": "https://github.com/" + (
                "openbao/openbao" if variant == "komodo" else "moghtech/komodo"
            ) + "/releases",
            "wud.tag.include": r"^\d+\.\d+\.\d+$$",
        },
        "volumes": [
            {"type": "bind", "source": str(root / "agent").replace("$", "$$"),
             "target": "/agent", "read_only": True},
            {"type": "bind", "source": str(root / output).replace("$", "$$"), "target": f"/{output}"},
        ],
        **({"command": arguments, "network_mode": "bridge"} if variant == "compose" else {
            "entrypoint": ["/usr/bin/bao", *arguments], "cap_drop": ["ALL"],
            "security_opt": ["no-new-privileges:true"],
        }),
    }


def legacy_deployment(root, variant, compose):
    name = f"openbao-agent-{variant}"
    identifiers = docker("ps", "-aq", "--filter", f"name=^/{name}$").split()
    if not identifiers:
        return None
    if len(identifiers) != 1:
        raise ValueError(f"Multiple containers match {name}.")
    format_spec = '{"labels":{{json .Config.Labels}},"mounts":{{json .Mounts}},"running":{{json .State.Running}}}'
    details = json.loads(docker("inspect", "--format", format_spec, identifiers[0]))
    labels = details["labels"] or {}
    conflict = ValueError(f"Cannot migrate {name}: its Compose project, service, or layout is unrecognized.")
    if labels.get("com.docker.compose.project") != "secrets" or labels.get("com.docker.compose.service") != name:
        raise conflict
    config = labels.get("com.docker.compose.project.config_files")
    if config in {str(root / "compose.yaml"), str(compose)} or (
        config and Path(config).parent.parent == compose.parent
        and Path(config).name == "compose.openbao-agent.yaml"
    ):
        return None
    legacy = root.with_name("compose-openbao")
    legacy_config = legacy / "compose.yaml"
    if variant != "compose" or config != str(legacy_config):
        raise conflict
    mounts = {(item["Source"], item["Destination"], item["Type"], item["RW"]) for item in details["mounts"] or []}
    if labels.get("docker.stack") != "secrets" or mounts != {
        (str(legacy / "agent"), "/agent", "bind", False),
        (str(legacy / "cache"), "/cache", "bind", True),
    } or not legacy.is_dir() or not legacy_config.is_file():
        raise conflict
    directory(legacy)
    check_private_file(legacy_config, allow_readonly=True)
    command = ("compose", "--project-name", "secrets")
    env = labels.get("com.docker.compose.project.environment_file")
    if env:
        legacy_env = legacy / "agent-compose.env"
        if env != str(legacy_env) or not legacy_env.is_file():
            raise conflict
        check_private_file(legacy_env, allow_readonly=True)
        command += ("--env-file", env)
    command += ("-f", config)
    if docker(*command, "config", "--services").split() != [name]:
        raise ValueError(f"Cannot migrate {name}: the previous Compose file must contain only that agent.")
    return command, name, details["running"]


def install(request):
    root = Path.home() / ".config/docker/openbao"
    workspace = Path.home() / ".docker/compose"
    compose, env = workspace / "compose.openbao.yml", root / "compose.env"
    agent = request["agent"]
    variants = {"compose": agent, **({"komodo": agent["komodo"]} if "komodo" in agent else {})}
    previous = [deployment for variant in variants if (deployment := legacy_deployment(root, variant, compose))]
    for path in (root.parent.parent, root.parent, root):
        directory(path)
    workspace.mkdir(parents=True, mode=0o700, exist_ok=True)
    services, rendered, credentials_changed = {}, {}, False
    for variant, credentials in variants.items():
        base = root / variant
        output = base / ("cache" if variant == "compose" else "rendered")
        for path in (base, base / "agent", output):
            directory(path)
        credentials_changed |= write_private(base / "agent/agent.hcl", b64decode(credentials["config_b64"], validate=True))
        credentials_changed |= write_private(base / "agent/role-id", (credentials["role_id"] + "\n").encode())
        credentials_changed |= write_private(base / "agent/secret-id", (credentials["secret_id"] + "\n").encode())
        services[f"openbao-agent-{variant}"] = agent_service(variant, request, base)
        names = [f"{item}.env" for item in credentials["projects"]] if variant == "compose" else ["core.config.toml"]
        for name in names:
            if not re.fullmatch(r"[a-z0-9][a-z0-9_.-]+", name):
                raise ValueError("Invalid NAS OpenBao render name.")
            file = output / name
            check_private_file(file)
            rendered[file] = file.stat().st_mtime_ns if previous and variant == "compose" and file.exists() else None
    values = {"URL_BAO": json.dumps(agent["url"]), "DOCKER_HOSTNAME": request["device"],
              "HOST_IP": request["address"], "DOCKER_API_PORT": request["port"]}
    write_private(env, "".join(f"{key}={value}\n" for key, value in values.items()).encode())
    write_private(compose, (json.dumps({"name": "secrets", "services": services}, indent=2) + "\n").encode())
    command = ("compose", "--project-name", "secrets", "--env-file", str(env), "-f", str(compose))
    force = ("--force-recreate",) if credentials_changed or previous else ()
    try:
        docker(*command, "up", "--detach", "--no-deps", *force, *services)
        for _ in range(30):
            running = set(docker(*command, "ps", "--status", "running", "--services").splitlines())
            if services.keys() <= running and all(
                file.is_file() and not file.is_symlink() and file.stat().st_uid == os.getuid()
                and stat.S_IMODE(file.stat().st_mode) == 0o600 and file.stat().st_size
                and file.stat().st_mtime_ns != prior_mtime
                for file, prior_mtime in rendered.items()
            ):
                return {"status": "ready", "projects": agent["projects"], **({"migrated": True} if previous else {})}
            time.sleep(2)
        raise ValueError("Onboardrr could not verify that the NAS OpenBao Agent rendered its environment.")
    except (OSError, ValueError):
        if not previous:
            raise
        prior_command, name, was_running = previous[0]
        restore = ("up", "--detach") if was_running else ("create",)
        try:
            docker(*prior_command, *restore, "--no-deps", "--force-recreate", name)
            if was_running and name not in docker(*prior_command, "ps", "--status", "running", "--services").splitlines():
                raise ValueError("The previous agent is not running.")
        except (OSError, ValueError):
            raise ValueError("The NAS OpenBao migration failed and the previous agent could not be restored.") from None
        raise ValueError("The NAS OpenBao migration failed. The previous agent was restored.") from None


if __name__ == "__main__":
    try:
        print(json.dumps(install(json.load(sys.stdin))))
    except (OSError, ValueError, KeyError, TypeError) as error:
        print(json.dumps({"error": str(error) if type(error) is ValueError else "NAS OpenBao setup failed."}))
