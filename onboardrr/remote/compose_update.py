#!/usr/bin/env python3
"""Start a Compose project or update an existing one on its Docker host."""

import argparse
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


PROJECT = re.compile(r"[a-z0-9][a-z0-9_-]*")
TAG = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}")
COMPOSE_FILENAME = re.compile(r"(?:docker-)?compose(?:[.-][^.]+)*\.ya?ml|[^.]+-compose\.ya?ml")
DOCKER_CLI = "docker"
COMPOSE_CLI = None
NAS_DOCKER = False
AGENT_STATE = Path("/var/lib/onboardrr")
INSPECT_FORMAT = '[{{json .Id}},{{json .Config.Image}},' + ",".join(
    '{{json (index .Config.Labels "com.docker.compose.' + label + '")}}'
    for label in ("service", "project.working_dir", "project.config_files", "project.environment_file")
) + "]"


def docker_environment():
    environment = os.environ.copy()
    for name in ("DOCKER_HOST", "DOCKER_CONTEXT", "COMPOSE_FILE", "HOSTNAME"):
        environment.pop(name, None)
    if NAS_DOCKER:
        environment["DOCKER_HOST"] = "unix:///var/run/docker.sock"
    environment["COMPOSE_REMOVE_ORPHANS"] = "false"
    return environment


def docker_command(arguments):
    if COMPOSE_CLI and arguments[:1] == ["compose"]:
        return [COMPOSE_CLI, *arguments[1:]]
    return [DOCKER_CLI, *arguments]


def docker(arguments, stage, *, directory=None, payload=None, timeout=15):
    try:
        result = subprocess.run(
            docker_command(arguments), cwd=directory, input=payload, capture_output=True,
            text=True, env=docker_environment(), timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise ValueError(f"Docker could not complete {stage}.") from error
    if result.returncode:
        raise ValueError(f"Docker failed during {stage} (exit {result.returncode}).")
    return result.stdout


def agent_environment(project, directory, env_files):
    manifest = AGENT_STATE / "compose-agent-projects.json"
    legacy = AGENT_STATE / "compose-secrets" / f"{project}.env"
    try:
        info = manifest.lstat()
    except FileNotFoundError:
        if NAS_DOCKER:
            rendered = Path.home() / ".config/docker/openbao/compose/cache" / f"{project}.env"
            if os.path.lexists(rendered):
                verify_rendered_environment(rendered, os.getuid(), 0o600)
                env_files = env_files or ([str(directory / ".env")] if (directory / ".env").is_file() else [])
                return [*[file for file in env_files if file != str(rendered)], str(rendered)]
        return env_files
    if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
        raise ValueError("The OpenBao Compose project manifest has invalid ownership or permissions.")
    try:
        data = json.loads(manifest.read_text())
    except (UnicodeError, ValueError):
        raise ValueError("The OpenBao Compose project manifest is invalid.") from None
    managed, uid, mode = legacy, 0, 0o640
    previous_agent_files = {str(legacy)}
    projects = data
    if isinstance(data, dict):
        path = data.get("cache")
        uid = data.get("uid")
        if not isinstance(path, str) or type(uid) is not int or uid < 0:
            raise ValueError("The OpenBao Compose project manifest is invalid.")
        cache = Path(path)
        if not cache.is_absolute() or ".." in cache.parts:
            raise ValueError("The OpenBao Compose cache path is invalid.")
        if cache.parts[-5:] == (".config", "docker", "openbao", "compose", "cache"):
            previous_agent_files.add(str(cache.parent.parent / "cache" / f"{project}.env"))
        elif cache.parts[-4:] != (".config", "docker", "openbao", "cache"):
            raise ValueError("The OpenBao Compose cache path is invalid.")
        managed, mode = cache / f"{project}.env", 0o600
        projects = data.get("projects")
    if not isinstance(projects, list) or not all(isinstance(name, str) and PROJECT.fullmatch(name) for name in projects):
        raise ValueError("The OpenBao Compose project manifest is invalid.")
    previous_agent_files.add(str(managed))
    if project not in projects:
        if previous_agent_files.intersection(env_files):
            raise ValueError("OpenBao Agent no longer manages this Compose project's environment.")
        return env_files
    verify_rendered_environment(managed, uid, mode)
    previous = [file for file in env_files if file not in previous_agent_files]
    if not env_files and (directory / ".env").is_file():
        previous.append(str(directory / ".env"))
    return [*previous, str(managed)]


def verify_rendered_environment(path, uid, mode):
    try:
        info = path.lstat()
    except FileNotFoundError:
        raise ValueError("OpenBao Agent has not rendered a Compose environment for this project.") from None
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != uid or stat.S_IMODE(info.st_mode) != mode
            or not info.st_size or not os.access(path, os.R_OK)):
        raise ValueError("The OpenBao Compose environment is not private and readable by the operator.")


def dcu_project_name(compose, env_files, directory, *, interpolate):
    command = [*compose]
    for path in env_files:
        command.extend(["--env-file", path])
    command.extend(["config", "--no-consistency", "--format", "json"])
    if not interpolate:
        command.append("--no-interpolate")
    model = json.loads(docker(command, "Compose project name detection", directory=directory))
    project = model.get("name") if isinstance(model, dict) else None
    if not isinstance(project, str) or not PROJECT.fullmatch(project):
        raise ValueError("Specify the Compose project with dcu --project-name NAME.")
    return project


def dcu_command(arguments, directory):
    parser = argparse.ArgumentParser(
        prog="dcu", description="Start the local Compose project with its OpenBao Agent environment.",
        epilog="Additional arguments are passed to docker compose up.", allow_abbrev=False,
    )
    parser.add_argument("-p", "--project-name")
    parser.add_argument("-f", "--file", action="append", default=[])
    parser.add_argument("--project-directory")
    parser.add_argument("--env-file", action="append", default=[])
    parser.add_argument("--profile", action="append", default=[])
    options, up_options = parser.parse_known_args(arguments)
    compose = ["compose"]
    if options.project_directory:
        compose.extend(["--project-directory", options.project_directory])
    for path in options.file:
        compose.extend(["--file", path])
    for profile in options.profile:
        compose.extend(["--profile", profile])

    env_files = options.env_file
    project = options.project_name
    nas_cache = Path.home() / ".config/docker/openbao/compose/cache"
    if os.path.lexists(AGENT_STATE / "compose-agent-projects.json") or (NAS_DOCKER and nas_cache.is_dir()):
        if not project:
            if "-" in options.file:
                raise ValueError("Specify --project-name when reading the Compose file from stdin.")
            project = dcu_project_name(compose, env_files, directory, interpolate=False)
        elif not PROJECT.fullmatch(project):
            raise ValueError("Invalid Compose project name.")
        default_directory = directory
        if not (directory / ".env").is_file():
            if options.project_directory:
                default_directory = directory / options.project_directory
            elif options.file and options.file[0] != "-":
                default_directory = (directory / options.file[0]).parent
        env_files = agent_environment(project, default_directory, env_files)
        if not options.project_name and env_files != options.env_file:
            if dcu_project_name(compose, env_files, directory, interpolate=True) != project:
                raise ValueError("The OpenBao environment changed the Compose project name. Use dcu --project-name NAME.")

    if options.project_name or env_files != options.env_file:
        compose.extend(["--project-name", project])
    for path in env_files:
        compose.extend(["--env-file", path])
    return [*compose, "up", "--detach", *up_options]


def projects_on_host():
    output = docker(["ps", "--format", '{{.Label "com.docker.compose.project"}}'], "project discovery")
    return sorted({name for name in output.splitlines() if PROJECT.fullmatch(name)})


def source_environment():
    layout = Path.home() / ".config/onboardrr/compose-sources.json"
    try:
        info = layout.lstat()
    except FileNotFoundError:
        return {}, {}
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077:
        raise ValueError("The host's Compose source settings must be a private operator-owned file.")
    data = json.loads(layout.read_text())
    if not isinstance(data, dict) or data.keys() - {"replace_env", "append_env"}:
        raise ValueError("Invalid host Compose source settings.")
    result = []
    for mode in ("replace_env", "append_env"):
        values = data.get(mode, {})
        if not isinstance(values, dict) or not all(
            Path(key).is_absolute() and isinstance(files, list) and files
            and all(isinstance(file, str) and Path(file).is_absolute() for file in files)
            for key, files in values.items()
        ):
            raise ValueError("Compose source settings require absolute file paths.")
        result.append(values)
    return tuple(result)


def inspect_compose_containers(project=None, *, running_only):
    label = "label=com.docker.compose.project"
    arguments = [
        "ps", "--quiet", "--no-trunc",
        "--filter", f"{label}={project}" if project else label,
        "--filter", "label=com.docker.compose.oneoff=False",
    ]
    if not running_only:
        arguments.append("--all")
    ids = sorted(docker(arguments, "project discovery").split())
    if not ids:
        return []
    template = INSPECT_FORMAT
    if project is None:
        template = template[:-1] + ',{{json (index .Config.Labels "com.docker.compose.project")}}]'
    data = docker(["inspect", "--format", template, *ids], "container inspection")
    rows = [json.loads(line) for line in data.splitlines()]
    if len(rows) != len(ids):
        raise ValueError("The selected Compose project changed during discovery.")
    return rows


def discover(project, *, running_only):
    rows = inspect_compose_containers(project, running_only=running_only)
    if not rows:
        raise ValueError("The selected Compose project has no existing containers.")
    return group_sources(project, rows, *source_environment())


def group_sources(project, rows, replacements, supplements):
    groups = {}
    managed_name = re.compile(rf"\.onboardrr-{re.escape(project)}(?:-[0-9a-f]{{12}})?\.images\.yml")
    for identifier, image, service, working_dir, sources, environment in rows:
        directory = Path(working_dir or "")
        raw_sources = tuple(file for file in (sources or "").split(",") if file)
        managed = tuple(file for file in raw_sources if Path(file).parent == directory
                        and managed_name.fullmatch(Path(file).name))
        if len(managed) > 1:
            raise ValueError("An existing Compose service uses multiple Onboardrr overrides.")
        files = tuple(file for file in raw_sources if file not in managed)
        env_files = []
        for file in (environment or "").split(","):
            if file:
                env_files.extend(replacements.get(file, [file]) if not Path(file).is_file() else [file])
        for file in files:
            env_files.extend(supplements.get(file, ()))
        env_files = tuple(dict.fromkeys(env_files))
        group = groups.setdefault((directory, files, env_files), {"containers": [], "overrides": set()})
        group["containers"].append({"id": identifier, "image": image, "service": service})
        group["overrides"].update(managed)
    return groups


def source_command(project, key):
    directory, files, environment = key
    if not directory.is_absolute() or not directory.is_dir():
        raise ValueError("The recorded Compose working directory is unavailable.")
    env_files = agent_environment(project, directory, list(environment))
    if not files or not all(Path(file).is_absolute() and Path(file).is_file() for file in (*files, *env_files)):
        raise ValueError("A recorded Compose or environment file is missing. Check the source mount.")
    command = ["compose", "--project-name", project, "--project-directory", str(directory)]
    for file in env_files:
        command.extend(["--env-file", file])
    for file in files:
        command.extend(["--file", file])
    return command


def source_group(project, key, group, *, isolated):
    directory, files, environment = key
    if len(group["overrides"]) > 1:
        raise ValueError("The running services disagree on the managed Compose override.")
    suffix = ""
    if isolated:
        context = json.dumps([files, environment]).encode()
        suffix = "-" + hashlib.sha256(context).hexdigest()[:12]
    existing = next(iter(group["overrides"]), None)
    override = Path(existing) if existing else directory / f".onboardrr-{project}{suffix}.images.yml"
    command = source_command(project, key)
    images = configured_images(command, directory)
    saved = read_pins(override, project)
    pins = {name: pinned for name, (base, pinned) in saved.items() if images.get(name) == base}
    return directory, override, command, images, pins


def configured_images(command, directory, *, include_profiles=False):
    profiles = ["--profile", "*"] if include_profiles else []
    model = json.loads(docker(command + profiles + ["config", "--format", "json"],
                              "Compose validation", directory=directory))
    images = {name: service["image"] for name, service in model["services"].items() if service.get("image")}
    return images


def tag_reference(image, tag):
    if not isinstance(tag, str) or not TAG.fullmatch(tag):
        raise ValueError("WUD returned an invalid image tag.")
    if not isinstance(image, str) or "@" in image:
        raise ValueError("Version overrides require tag-based registry image references.")
    repository = image.rsplit(":", 1)[0] if ":" in image.rsplit("/", 1)[-1] else image
    return f"{repository}:{tag}"


def read_pins(override, project):
    if override.is_symlink():
        raise ValueError("The reserved Compose override path is a symlink.")
    if not override.exists():
        return {}
    try:
        data = json.loads(override.read_text())
        images = data["x-onboardrr-base-images"]
        services = data["services"]
        if data["x-onboardrr-project"] != project or images.keys() != services.keys():
            raise ValueError
        pins = {}
        for name, base in images.items():
            service = services[name]
            if service.keys() != {"image"} or not isinstance(service["image"], str):
                raise ValueError
            image = service["image"]
            if tag_reference(base, image.rsplit(":", 1)[-1]) != image:
                raise ValueError
            pins[name] = (base, image)
        return pins
    except (AttributeError, KeyError, TypeError, ValueError) as error:
        raise ValueError("The reserved Compose override is not an Onboardrr version file.") from error


def publish(path, content):
    with NamedTemporaryFile(mode="w", dir=path.parent, delete=False) as stream:
        temporary = Path(stream.name)
        try:
            stream.write(content)
            stream.flush()
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)


def update_source(project, source, versions, *, targeted, files_only):
    directory, override, command, images, pins = source
    current = {**images, **pins}
    for name, tag in versions.items():
        pins[name] = tag_reference(current[name], tag)
    targets = ["--", *sorted(versions)] if targeted else []
    if pins or override.exists():
        desired = json.dumps({
            "x-onboardrr-project": project,
            "x-onboardrr-base-images": {name: images[name] for name in pins},
            "services": {name: {"image": image} for name, image in pins.items()},
        }, indent=2, sort_keys=True) + "\n"
        proposed = command + ["--file", "-"]
        docker(proposed + ["config", "--quiet"], "version override validation",
               directory=directory, payload=desired)
        if targeted and not files_only:
            docker(proposed + ["pull", *targets], "image pull",
                   directory=directory, payload=desired, timeout=600)
        if not override.exists() or override.read_text() != desired:
            publish(override, desired)
        command.extend(["--file", str(override)])
    if files_only:
        return {**images, **pins}, str(override)
    pull = "never" if targeted else "always"
    scope = ["--no-deps"] if targeted else []
    docker(command + ["up", "--detach", "--pull", pull, "--no-build",
                      "--wait", "--wait-timeout", "120", *scope, *targets],
           "Compose update and health verification", directory=directory, timeout=240 if targeted else 840)
    return {**images, **pins}, str(override) if pins or override.exists() else None


def update(request):
    if request.get("list"):
        return {"projects": projects_on_host()}
    project = request["project"]
    if not PROJECT.fullmatch(project):
        raise ValueError("Invalid Compose project name.")
    use_wud = request.get("wud", False)
    files_only = request.get("files_only", False)
    if files_only and not use_wud:
        raise ValueError("File-only updates require WUD versions.")
    groups = discover(project, running_only=use_wud)
    if not use_wud and len(groups) > 1:
        raise ValueError("This project uses multiple Compose sources. Use WUD to update individual services.")
    names = set()
    current = {}
    sources = []
    skipped = {}
    containers = []
    for key, group in groups.items():
        services = {row["service"] for row in group["containers"]}
        if not all(isinstance(name, str) and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", name) for name in services):
            raise ValueError("The project has an invalid Compose service label.")
        if names & services:
            raise ValueError("Replicas of a service use different Compose sources.")
        names.update(services)
        containers.extend(group["containers"])
        try:
            source = source_group(project, key, group, isolated=sum(k[0] == key[0] for k in groups) > 1)
        except ValueError as error:
            if not use_wud:
                raise
            skipped.update(dict.fromkeys(sorted(services), str(error)))
            continue
        directory, override, command, images, pins = source
        skipped.update(dict.fromkeys(sorted(services - images.keys()), "No image in the recorded Compose source."))
        current.update({name: pins.get(name, images[name]) for name in services & images.keys()})
        sources.append((services, source))
    snapshot = {"containers": sorted(containers, key=lambda item: item["id"]), "images": current}
    if skipped:
        snapshot["skipped"] = skipped
    if not request.get("apply"):
        return snapshot

    if request.get("expected") != snapshot:
        raise ValueError("The deployed Compose project changed. Run a new preview.")
    versions = request.get("versions", {})
    if not versions.keys() <= current.keys():
        raise ValueError("A selected service has no configured registry image.")
    if len({source[1] for _, source in sources}) != len(sources):
        raise ValueError("Independent Compose sources must use separate image overrides.")
    overrides = []
    for services, source in sources:
        selected = {name: tag for name, tag in versions.items() if name in services}
        if selected or not use_wud:
            changed, override = update_source(project, source, selected, targeted=use_wud, files_only=files_only)
            current.update({name: changed[name] for name in services & changed.keys()})
            if override:
                overrides.append(override)
    status = ("files-updated" if files_only else "updated") if versions or not use_wud else "no-updates"
    return {"status": status, "images": current, "overrides": overrides}


def configure_nas_docker():
    result = subprocess.run(
        ["/sbin/getcfg", "container-station", "Install_Path", "-d", "", "-f", "/etc/config/qpkg.conf"],
        text=True, capture_output=True, timeout=10, check=False,
    )
    station = Path(result.stdout.strip())
    docker_path = station / "bin/docker"
    if result.returncode or not station.is_absolute() or not os.access(docker_path, os.X_OK):
        raise ValueError("The QNAP Container Station Docker CLI is unavailable.")
    return str(docker_path)


def configure_nas_launcher():
    global DOCKER_CLI, COMPOSE_CLI, NAS_DOCKER
    DOCKER_CLI = configure_nas_docker()
    NAS_DOCKER = True
    try:
        docker(["compose", "version", "--short"], "Container Station Compose version")
    except ValueError:
        standalone = Path(DOCKER_CLI).with_name("docker-compose")
        if not os.access(standalone, os.X_OK):
            raise
        COMPOSE_CLI = str(standalone)
        docker(["compose", "version", "--short"], "Container Station Compose version")


def compose_workspace(host):
    if not isinstance(host, str) or not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,62}", host):
        raise ValueError("Invalid host name for the Compose workspace.")
    home = Path.home()
    primary = home / ".docker/compose"
    alternate = home / ".config/docker/compose"
    for directory in (primary / host, alternate / host):
        if directory.is_dir():
            return directory.resolve(strict=True)
    workspaces = [directory for directory in (primary, alternate) if directory.is_dir()]
    for directory in workspaces:
        if any(directory.iterdir()):
            return directory.resolve(strict=True)
    if workspaces:
        return workspaces[0].resolve(strict=True)
    raise ValueError("The default Compose workspace is unavailable on this host.")


def compose_file(directory, name):
    path = Path(name)
    if (path.is_absolute() or ".." in path.parts or not COMPOSE_FILENAME.fullmatch(path.name)
            or any(part.startswith(".") for part in path.parts)):
        raise ValueError("Select Compose files within the host's workspace.")
    entry = directory / path
    if entry.resolve(strict=True) != entry or not entry.is_file():
        raise ValueError("Select a regular Compose file within the host's workspace.")
    return entry


def deployment_plan(originals, deployment):
    by_path = {originals[name][0]: images for name, images in deployment["images"].items()}
    pending = {(originals[name][0], service) for name, services in deployment["updated"].items()
               for service in services}
    matched = set()
    watched = {}
    for identifier, old, new in deployment["wud_tags"]:
        watched.setdefault((identifier, old), set()).add(new)
    by_project = {}
    affected = set()
    for *row, project in inspect_compose_containers(running_only=True):
        if not isinstance(project, str) or not PROJECT.fullmatch(project):
            continue
        by_project.setdefault(project, []).append(row)
        files = (row[4] or "").split(",")
        if any(Path(file).is_absolute() and Path(file).resolve() in by_path for file in files):
            affected.add(project)
    if not affected and not pending:
        return []
    sources = source_environment()
    plan = []
    for project in sorted(affected):
        groups = group_sources(project, by_project[project], *sources)
        service_sources = {}
        for key, group in groups.items():
            for container in group["containers"]:
                service_sources.setdefault(container["service"], set()).add(key)
        for key, group in groups.items():
            directory, files, _ = key
            paths = {Path(file).resolve() for file in files if Path(file).is_absolute()} & by_path.keys()
            if not paths:
                continue
            targets = {}
            for container in group["containers"]:
                service, running = container["service"], container["image"]
                for path in paths:
                    desired = by_path[path].get(service)
                    if not desired or service.startswith("-"):
                        continue
                    changed = (path, service) in pending
                    if changed:
                        matched.add((path, service))
                    if changed or pending_wud_upgrade(container["id"], running, desired, watched):
                        if targets.setdefault(service, desired) != desired:
                            raise ValueError(f"Selected Compose files disagree on {project}/{service}.")
            if targets:
                if ambiguous := sorted(name for name in targets if len(service_sources[name]) > 1):
                    raise ValueError(f"Running instances of {project}/{ambiguous[0]} use different Compose files.")
                command = source_command(project, key)
                if len(group["overrides"]) > 1:
                    raise ValueError(f"Running services in {project} disagree on their Compose override.")
                for path in sorted(group["overrides"]):
                    command.extend(["--file", path])
                plan.append((project, directory, command, targets))
    if missing := sorted(pending - matched):
        path, service = missing[0]
        raise ValueError(f"No running local Compose service matches {path.name}/{service}.")
    return plan


def pending_wud_upgrade(identifier, running, desired, watched):
    image_name, _, tag = running.rpartition(":")
    if not image_name or "@" in running:
        return False
    candidates = watched.get((identifier, tag), set())
    return len(candidates) == 1 and tag_reference(running, next(iter(candidates))) == desired


def deploy_selected(plan):
    results = []
    for project, directory, command, targets in plan:
        selected = sorted(targets)
        result = {"project": project, "services": selected}
        try:
            effective = configured_images(command, directory, include_profiles=True)
            if masked := [name for name in selected if effective.get(name) != targets[name]]:
                raise ValueError(f"The active Compose configuration overrides the selected image for {masked[0]}.")
            docker(command + ["pull", "--", *selected], "image pull", directory=directory, timeout=600)
            effective = configured_images(command, directory, include_profiles=True)
            if any(effective.get(name) != targets[name] for name in selected):
                raise ValueError("The selected Compose images changed during the pull. Run a new preview.")
            docker(command + ["up", "--detach", "--pull", "never", "--no-build", "--wait", "--wait-timeout", "120",
                              "--no-deps", "--", *selected],
                   "Compose deployment and health verification", directory=directory, timeout=240)
        except ValueError as error:
            result.update(status="failed", error=str(error))
        else:
            result["status"] = "deployed"
        results.append(result)
    return results


def compose_files(request):
    directory = compose_workspace(request["host"])
    if request.get("workspace") not in (None, str(directory)):
        raise ValueError("The default Compose workspace changed. Reload the file list.")
    if request["action"] == "list-files":
        files = []
        for root, folders, names in os.walk(directory):
            parent = Path(root)
            folders[:] = sorted(name for name in folders if not name.startswith(".") and not (parent / name).is_symlink())
            for name in sorted(names):
                path = parent / name
                if COMPOSE_FILENAME.fullmatch(name) and not path.is_symlink() and path.is_file():
                    files.append(str(path.relative_to(directory)))
        return {"workspace": str(directory), "files": sorted(files)}

    selected = request["files"]
    if not isinstance(selected, list) or not 1 <= len(selected) <= 100 or len(set(selected)) != len(selected):
        raise ValueError("Select up to 100 distinct Compose files.")
    originals = {}
    for name in selected:
        path = compose_file(directory, name)
        with path.open("rb") as stream:
            original = stream.read(256 * 1024 + 1)
        originals[name] = (path, original, hashlib.sha256(original).hexdigest())
    if sum(len(value[1]) for value in originals.values()) > 512 * 1024 or any(
        len(value[1]) > 256 * 1024 for value in originals.values()
    ):
        raise ValueError("The selected Compose files exceed the size limit. Select a smaller batch.")

    if request["action"] == "read-files":
        return {
            "workspace": str(directory), "architecture": platform.machine(),
            "files": {name: {"text": original.decode("utf-8"), "digest": digest}
                      for name, (_, original, digest) in originals.items()},
        }
    if request["action"] != "write-files":
        raise ValueError("Unknown Compose file operation.")
    if request["expected"] != {name: data[2] for name, data in originals.items()}:
        raise ValueError("A selected Compose file changed. Preview the versions again.")
    changes = request["changes"]
    if not isinstance(changes, dict) or not changes.keys() <= originals.keys():
        raise ValueError("Changes must belong to the selected Compose files.")
    deployment = request.get("deployment")
    if deployment and request.get("nas"):
        configure_nas_launcher()
    plan = deployment_plan(originals, deployment) if deployment else []
    staged = {}
    try:
        for name, text in changes.items():
            path, original, _ = originals[name]
            with NamedTemporaryFile(mode="wb", dir=path.parent, delete=False) as stream:
                staged[name] = Path(stream.name)
                stream.write(text.encode("utf-8"))
                os.fchmod(stream.fileno(), stat.S_IMODE(path.stat().st_mode))
                stream.flush()
                os.fsync(stream.fileno())
        for name in staged:
            path, original, _ = originals[name]
            if compose_file(directory, name).read_bytes() != original:
                raise ValueError("A selected Compose file changed. Preview the versions again.")
        for name, temporary in staged.items():
            os.replace(temporary, originals[name][0])
    finally:
        for temporary in staged.values():
            temporary.unlink(missing_ok=True)
    if not deployment:
        return {"status": "files-updated", "files": list(changes)}
    results = deploy_selected(plan)
    blockers = [f"{item['project']}: {item['error']}" for item in results if item["status"] == "failed"]
    status = "deployment-incomplete" if blockers else "deployed" if results else "no-updates"
    return {"status": status, "files": list(changes), "deployments": results, "blockers": blockers}


if __name__ == "__main__":
    if Path(sys.argv[0]).name == "onboardrr-dcu":
        try:
            arguments = sys.argv[1:]
            if len(arguments) == 2 and arguments[0] == "--workspace":
                print(compose_workspace(arguments[1]))
                sys.exit(0)
            if arguments[:1] == ["--nas"]:
                configure_nas_launcher()
                arguments = arguments[1:]
            command = docker_command(dcu_command(arguments, Path.cwd()))
            os.execvpe(command[0], command, docker_environment())
        except (OSError, KeyError, TypeError, ValueError) as error:
            detail = str(error) if type(error) is ValueError else "Could not resolve or start the Compose project."
            print(f"dcu: {detail}", file=sys.stderr)
            sys.exit(1)
    try:
        request = json.load(sys.stdin)
        if "action" in request:
            print(json.dumps(compose_files(request)))
            sys.exit(0)
        if request.get("nas"):
            configure_nas_launcher()
        print(json.dumps(update(request)))
    except (OSError, TypeError, KeyError, ValueError) as error:
        message = str(error) if type(error) is ValueError else "Invalid Docker project metadata or inaccessible source files."
        print(json.dumps({"error": message}))
