"""Validate and deploy a platform Compose project using the local agent cache."""

import hashlib
import json
import os
import platform
import re
import stat
import subprocess
import sys
from pathlib import Path
from tempfile import NamedTemporaryFile
from time import monotonic, sleep


def private_file(path):
    if not os.path.lexists(path):
        return False
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise ValueError(f"Use a private, operator-owned file for {path.name}.")
    return True


def write_file(path, contents):
    with NamedTemporaryFile(mode="w", dir=path.parent, delete=False) as stream:
        pending = Path(stream.name)
        try:
            stream.write(contents)
            stream.flush()
            os.fsync(stream.fileno())
            # Do not replace a file concurrently created by the operator.
            os.link(pending, path)
        finally:
            pending.unlink()


def stage_manifest(path, contents, stack, host, reference_digest=None, previous_digest=None):
    exists = private_file(path)
    previous = path.read_bytes().decode() if exists else ""
    if previous_digest is not None and hashlib.sha256(previous.encode()).hexdigest() != previous_digest:
        raise ValueError("The OpenBao manifest changed during installation. Retry the operation.")
    if not exists:
        write_file(path, contents)
        return
    if previous == contents:
        return
    try:
        model = json.loads(previous)
        reusable = stack == "komodo" and model.get("name") == "komodo" and set(model["services"]) == {"komodo-agent"}
        reusable = reusable and model["services"]["komodo-agent"]["environment"]["PERIPHERY_CONNECT_AS"] == host
    except (KeyError, TypeError, ValueError, AttributeError):
        reusable = False
    is_reference = reference_digest == hashlib.sha256(previous.encode()).hexdigest()
    if not (reusable or is_reference or previous_digest is not None):
        raise ValueError(f"{path.name} already contains a different Compose project. Preserve it before replacing it.")
    backup = path.with_name(".compose.komodo.periphery.previous" if reusable else f".{path.name}.before-onboardrr")
    if not private_file(backup):
        write_file(backup, previous)
    elif backup.read_text() != previous and stack != "openbao":
        raise ValueError(f"The backup for {path.name} differs from the current manifest.")
    with NamedTemporaryFile(mode="w", dir=path.parent) as staged:
        staged.write(contents)
        staged.flush()
        os.fsync(staged.fileno())
        pending = Path(staged.name).with_suffix(".replacement")
        os.link(staged.name, pending)
        try:
            if path.read_bytes().decode() != previous:
                raise ValueError("The Compose manifest changed during installation.")
            pending.replace(path)
        finally:
            pending.unlink(missing_ok=True)


def compose_cli(home, environment):
    if not Path("/etc/config/uLinux.conf").is_file():
        return ["docker", "compose"]
    station = Path(subprocess.check_output([
        "/sbin/getcfg", "container-station", "Install_Path", "-d", "", "-f", "/etc/config/qpkg.conf",
    ], text=True, timeout=10).strip())
    if not station.is_absolute():
        raise ValueError("Container Station's Docker CLI is unavailable.")
    environment["DOCKER_CONFIG"] = str(home / ".docker")
    environment["DOCKER_HOST"] = "unix:///var/run/docker.sock"
    launcher = home / ".config/onboardrr/bin/docker"
    return [str(launcher if os.access(launcher, os.X_OK) else station / "bin/docker"), "compose"]


def komodo_backend(home, cli, environment, *, prefer_ferretdb=False):
    cpu = Path("/proc/cpuinfo").read_text()
    flag = {"x86_64": "avx", "aarch64": "asimddp"}.get(platform.machine())
    features = re.findall(r"(?m)^(?:flags|Features)[ \t]*:[ \t]*(.*)$", cpu)
    compatible = flag and features and all(flag in row.split() for row in features)
    active = set(subprocess.check_output(
        [cli[0], "ps", "--filter", "label=com.docker.compose.project=komodo",
         "--format", '{{.Label "com.docker.compose.service"}}'],
        env=environment, text=True, timeout=15,
    ).splitlines())
    if active & {"komodo-postgres", "komodo-ferretdb"}:
        if "komodo-mongodb" in active:
            return {"blockers": ["Both Komodo database backends are running. Review the intended database."]}
        return {"backend": "ferretdb"}

    database = home / ".config/docker/komodo/db"
    documentdb, mongo = database / "documentdb", database / "data"
    try:
        has_documentdb = documentdb.is_dir() and any(documentdb.iterdir())
        has_mongodb = mongo.is_dir() and any(mongo.iterdir())
    except PermissionError:
        return {"blockers": ["Could not verify the Komodo database directories or find a running FerretDB deployment."]}
    if has_documentdb and has_mongodb:
        return {"blockers": ["Both Komodo database directories contain data. Review the intended database before deployment."]}
    if has_documentdb:
        return {"backend": "ferretdb"}
    if compatible and not prefer_ferretdb:
        return {"backend": "mongodb"}
    containers = subprocess.check_output(
        [cli[0], "ps", "--all", "--quiet", "--filter", "name=^/komodo-mongodb$"],
        env=environment, text=True, timeout=15,
    )
    if containers.strip() or has_mongodb:
        if compatible:
            return {"backend": "mongodb"}
        return {"blockers": ["This CPU cannot run the reference MongoDB image. Restore a compatible database "
                             "before moving this existing Komodo installation."]}
    return {"backend": "ferretdb"}


def openbao_status(cli, environment):
    deadline = monotonic() + 90
    while monotonic() < deadline:
        result = subprocess.run(
            [cli[0], "exec", "openbao", "bao", "status", "-address=http://127.0.0.1:8200", "-format=json"],
            env=environment, capture_output=True, text=True, timeout=10,
        )
        if result.returncode in (0, 2):
            try:
                status = json.loads(result.stdout)
                if type(status["initialized"]) is bool and type(status["sealed"]) is bool:
                    return {"initialized": status["initialized"], "sealed": status["sealed"]}
            except (KeyError, TypeError, ValueError):
                pass
        sleep(2)
    raise ValueError("The OpenBao server did not answer its local status request. Check its container logs.")


def deploy(request):
    name, stack = request["name"], request["stack"]
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,62}", name) or stack not in {"openbao", "authentik", "komodo", "pangolin"}:
        raise ValueError("Invalid platform stack or host name.")
    home = Path.home()
    prefer_ferretdb = request.get("prefer_ferretdb", False)
    if stack == "komodo" and request.get("probe"):
        environment = {key: os.environ[key] for key in ("PATH", "HOME", "DOCKER_CONFIG", "XDG_RUNTIME_DIR")
                       if key in os.environ}
        return komodo_backend(home, compose_cli(home, environment), environment, prefer_ferretdb=prefer_ferretdb)
    workspace = home / ".docker/compose"
    target = workspace / f"compose.{stack}.yml"
    openbao = home / ".config/docker/openbao"
    server = stack == "openbao"
    if server and request.get("read"):
        if not private_file(target):
            return {"compose": ""}
        if target.stat().st_size > 262144:
            raise ValueError("The current OpenBao manifest exceeds 256 KiB.")
        return {"compose": target.read_bytes().decode()}
    status = {"device": name, "stack": stack, "compose": str(target), "mode": "check"}
    if "kuma_tcp" in request:
        status["kuma_docker_mode"] = "tcp" if request["kuma_tcp"] else "socket"
    if server:
        bootstrap = openbao / "bootstrap"
        sources = [bootstrap / f"{service}.env" for service in ("openbao", "openbao-postgres")]
        missing = [str(path.relative_to(home)) for path in sources
                   if not private_file(path) or not path.stat().st_size]
        config = openbao / "config"
        if not any(path.is_file() and not path.is_symlink() for path in config.glob("*.hcl")):
            missing.append(str(config.relative_to(home)) + "/*.hcl")
        if missing:
            return {**status, "blockers": ["Restore the local OpenBao bootstrap files: " + ", ".join(missing) + "."]}
    cache = openbao / "compose/cache"
    previous_cache = home / ".config/docker/compose-openbao/cache"
    environments = []
    for project in (() if server else dict.fromkeys(("config", name, stack))):
        for directory in (cache, previous_cache):
            if private_file(path := directory / f"{project}.env"):
                environments.append(path)
                break
    environment = {key: os.environ[key] for key in ("PATH", "HOME", "DOCKER_CONFIG", "XDG_RUNTIME_DIR")
                   if key in os.environ}
    environment.update(HOME=str(home), USER=home.name, HOSTNAME=name, ONBOARDRR_HOME=str(home),
                       COMPOSE_REMOVE_ORPHANS="false")
    kuma_tcp = request.get("kuma_tcp", {})
    port_defaults = request.get("port_defaults", {})
    environment.update(kuma_tcp, **port_defaults)
    agent_env = openbao / "compose.env"
    if server:
        if private_file(agent_env):
            environments.append(agent_env)
        else:
            environment.update(DOCKER_HOSTNAME=name, URL_BAO="http://openbao:8200")
    if stack == "komodo":
        candidates = (openbao / "komodo/rendered", home / ".docker/komodo-openbao/rendered")
        for rendered in candidates:
            if private_file(rendered / "core.config.toml"):
                environment["ONBOARDRR_KOMODO_RENDERED"] = str(rendered)
                break

    cli = compose_cli(home, environment)
    if stack == "komodo" and request.get("backend"):
        current_backend = komodo_backend(home, cli, environment, prefer_ferretdb=prefer_ferretdb)
        if current_backend.get("blockers"):
            return {**status, **current_backend}
        if current_backend["backend"] != request["backend"]:
            raise ValueError("The Komodo database changed during preparation. Retry the operation.")
        status["database"] = current_backend["backend"]
    project = "secrets" if server else stack
    command = [*cli, "--project-name", project, "--project-directory", str(workspace if workspace.is_dir() else home)]
    for path in environments or [Path(os.devnull)]:
        command.extend(["--env-file", str(path)])
    # Config can render secrets. Keep its stdout and stderr on the target.
    check_command = [*command, "--file", "-", "config", "--environment"]
    rendered = subprocess.run(check_command, input=request["compose"], env=environment, text=True,
                              capture_output=True, timeout=25)
    if rendered.returncode:
        # Older Compose versions still support validation with --quiet.
        effective = dict(environment)
        for path in environments:
            for row in path.read_text().splitlines():
                key, separator, value = row.partition("=")
                if separator and value.strip().strip("\"'"):
                    effective.setdefault(key.strip(), value)
    else:
        effective = dict(line.split("=", 1) for line in rendered.stdout.splitlines() if "=" in line)
    missing = [key for key in request["required"] if not effective.get(key)]
    if missing:
        status["blockers"] = ["The OpenBao Compose environment needs: " + ", ".join(missing) + "."]
    if stack == "pangolin" and not (home / ".config/docker/pangolin/config/config.yml").is_file():
        status.setdefault("blockers", []).append("Restore the Pangolin configuration at ~/.config/docker/pangolin/config/config.yml.")
    if status.get("blockers"):
        return status

    validated = subprocess.run([*command, "--file", "-", "config", "--quiet"], input=request["compose"],
                               text=True, env=environment, capture_output=True, timeout=30)
    if validated.returncode:
        raise ValueError(f"Docker Compose could not validate {stack}. Check its environment and external network.")
    if not request["apply"]:
        return status
    workspace.mkdir(mode=0o700, parents=True, exist_ok=True)
    stage_manifest(target, request["compose"], stack, name, request.get("reference_digest"),
                   request["previous_digest"] if server else None)
    command[command.index("--project-directory") + 1] = str(workspace)
    if server:
        if not environments:
            metadata = {"DOCKER_HOSTNAME": name, "URL_BAO": "http://openbao:8200", **kuma_tcp}
            write_file(agent_env, "".join(f"{key}={value}\n" for key, value in sorted(metadata.items())))
            command.extend(["--env-file", str(agent_env)])
    else:
        settings = home / ".config/onboardrr/platform" / f"{stack}.env"
        settings.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        values = {key: environment[key] for key in ("HOSTNAME", "ONBOARDRR_HOME", "ONBOARDRR_KOMODO_RENDERED", "USER")
                  if key in environment}
        values.update(kuma_tcp, **port_defaults)
        text = "".join(f"{key}={json.dumps(value)}\n" for key, value in sorted(values.items()))
        if private_file(settings):
            previous = settings.read_text()
            generated_keys = {"HOST_IP", "DOCKER_API_PORT", *port_defaults}
            previous_settings = sorted(line for line in previous.splitlines() if line.partition("=")[0] not in generated_keys)
            expected_settings = [line for line in text.splitlines() if line.partition("=")[0] not in generated_keys]
            if previous_settings != expected_settings:
                raise ValueError(f"Review the existing {stack} local Compose environment before deployment.")
            if previous != text:
                with NamedTemporaryFile(mode="w", dir=settings.parent, delete=False) as stream:
                    pending = Path(stream.name)
                    try:
                        stream.write(text)
                        stream.flush()
                        pending.replace(settings)
                    finally:
                        pending.unlink(missing_ok=True)
        else:
            write_file(settings, text)
        command.extend(["--env-file", str(settings)])
    if stack == "komodo":
        watcher = openbao / "komodo/watch-core.sh"
        watcher.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
        if not os.path.lexists(watcher):
            write_file(watcher, request["watcher"])
        if watcher.is_symlink() or not watcher.is_file() or not os.access(watcher, os.R_OK):
            raise ValueError("The Komodo configuration watcher is unavailable.")
    network = subprocess.run([cli[0], "network", "ls", "--format", "{{.Name}}"], env=environment,
                             capture_output=True, text=True, check=True, timeout=20)
    if name not in network.stdout.splitlines():
        subprocess.run([cli[0], "network", "create", "--driver", "bridge", name], env=environment,
                       capture_output=True, check=True, timeout=30)
    options = (["--pull", "missing", "openbao-postgres", "openbao"] if server
               else ["--wait", "--wait-timeout", "180"])
    applied = subprocess.run([*command, "--file", str(target), "up", "--detach", *options],
                             env=environment, capture_output=True, text=True, timeout=760)
    if applied.returncode:
        raise ValueError(f"Docker Compose saved {target.name} but could not start all {stack} services.")
    if server:
        state = openbao_status(cli, environment)
        if not state["initialized"]:
            state["next_action"] = "Initialize OpenBao and configure agent access before deploying its consumers."
        elif state["sealed"]:
            state["next_action"] = "Unseal OpenBao before deploying its consumers."
        return {**status, **state, "mode": "apply", "status": "running"}
    return {**status, "mode": "apply", "status": "running"}


if __name__ == "__main__":
    try:
        print(json.dumps(deploy(json.load(sys.stdin))))
    except ValueError as error:
        print(json.dumps({"error": str(error) if type(error) is ValueError else "Invalid platform Compose configuration."}))
    except (OSError, subprocess.SubprocessError, KeyError, TypeError):
        print(json.dumps({"error": "The platform Compose operation failed on the host."}))
