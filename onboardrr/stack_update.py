"""Update an enrolled host's existing Compose project, optionally using WUD."""

import base64
import json
import logging
import os
from urllib.parse import urlsplit

import yaml

from .http_json import HttpStatusError, JsonService
from .image_versions import labels_dict, mapping_node
from .inventory import NAS_PROFILE
from .remote.compose_update import PROJECT, TAG, tag_reference
from .secrets import configured_openbao, integration_credentials
from .storage import device_lock


LOGGER = logging.getLogger(__name__)


def image_repository(reference):
    first, separator, rest = reference.partition("/")
    registry, repository = (first.lower(), rest) if separator and ("." in first or ":" in first or first == "localhost") else ("docker.io", reference)
    if registry in {"docker.io", "registry-1.docker.io", "index.docker.io"}:
        registry = "docker.io"
        if "/" not in repository:
            repository = "library/" + repository
    return registry, repository


def compose_version(image, labels, platform, hostname, observations):
    repository, _, current = image.rpartition(":")
    if not TAG.fullmatch(current) or "/" in current or "@" in image or "$" in image or "[[" in image:
        return None
    if str(labels.get("wud.watch", "")).lower() == "false":
        return None
    target = image_repository(repository)
    filters = {"includeTags": "wud.tag.include", "excludeTags": "wud.tag.exclude", "transformTags": "wud.tag.transform"}
    candidates = []
    for item in observations:
        if not isinstance(item, dict) or item.get("updateAvailable") is not True:
            continue
        watched, result = item.get("image"), item.get("result")
        if not isinstance(watched, dict) or not isinstance(result, dict):
            continue
        registry, tag, name = watched.get("registry"), watched.get("tag"), watched.get("name")
        if not isinstance(registry, dict) or not isinstance(tag, dict) or not isinstance(name, str):
            continue
        url = registry.get("url")
        if not isinstance(url, str) or not url:
            continue
        base = urlsplit(url if "://" in url else "//" + url).netloc
        new = result.get("tag")
        if (tag.get("value") != current or not isinstance(new, str) or not TAG.fullmatch(new) or new == current
                or image_repository(f"{base}/{name}") != target):
            continue
        if (watched.get("os"), watched.get("architecture")) != tuple(platform[:2]):
            continue
        if len(platform) > 2 and watched.get("variant") != platform[2]:
            continue
        if any(str(labels.get(label) or "").replace("$$", "$") != (item.get(key) or "")
               for key, label in filters.items()):
            continue
        candidates.append((str(item.get("watcher", "")).casefold() == hostname.casefold(), new))
    tags = {tag for same_host, tag in candidates if same_host} or {tag for _, tag in candidates}
    if len(tags) > 1:
        raise ValueError("WUD returned conflicting tags for the same image and update channel.")
    return next(iter(tags), None)


def plan_compose_file(text, architecture, hostname, observations, *, deployment_images=None):
    try:
        source = yaml.compose(text, Loader=yaml.SafeLoader)
        document = yaml.safe_load(text)
    except yaml.YAMLError:
        raise ValueError("Invalid Compose YAML.") from None
    services = document.get("services") if isinstance(document, dict) else None
    if not isinstance(services, dict):
        raise ValueError("The file does not declare Compose services.")
    nodes = mapping_node(source, "services")
    arch = {"aarch64": "arm64", "x86_64": "amd64", "armv7l": "arm/v7"}.get(architecture, architecture)
    edits = []
    report = {"services": list(services), "updates": {}, "skipped": {}}
    for name, service in services.items():
        if not isinstance(service, dict) or not isinstance(service.get("image"), str):
            continue
        image = service["image"]
        platform = service.get("platform") or f"linux/{arch}"
        if not isinstance(platform, str) or not 2 <= len(platform.split("/")) <= 3:
            report["skipped"][name] = "The service platform could not be resolved."
            continue
        try:
            tag = compose_version(image, labels_dict(service.get("labels")), platform.split("/"), hostname, observations)
        except ValueError as error:
            report["skipped"][name] = str(error)
            continue
        if not tag and deployment_images is None:
            continue
        node = mapping_node(mapping_node(nodes, name), "image")
        if not isinstance(node, yaml.ScalarNode) or node.style not in (None, "'", '"'):
            if tag:
                report["skipped"][name] = "The image must be declared as a literal scalar in this service."
            continue
        start, end = node.start_mark.index, node.end_mark.index
        quote = node.style or ""
        if text[start:end] != f"{quote}{image}{quote}":
            if tag:
                report["skipped"][name] = "The image uses a YAML anchor or a nonliteral value."
            continue
        if deployment_images is not None and deployable_image(image, service):
            deployment_images[name] = tag_reference(image, tag) if tag else image
        if not tag:
            continue
        target = tag_reference(image, tag)
        edits.append((start, end, f"{quote}{target}{quote}"))
        report["updates"][name] = {"from": image, "to": target}
    if len({start for start, _, _ in edits}) != len(edits):
        raise ValueError("The images to update share a YAML scalar.")
    for start, end, replacement in sorted(edits, reverse=True):
        text = text[:start] + replacement + text[end:]
    return text, report


def deployable_image(image, service):
    repository, _, tag = image.rpartition(":")
    return (bool(repository and TAG.fullmatch(tag)) and not any(marker in image for marker in ("@", "$", "[["))
            and str(labels_dict(service.get("labels")).get("wud.watch", "")).lower() != "false")


def wud_tag_changes(observations):
    for item in observations:
        if (not isinstance(item, dict) or not isinstance(item.get("id"), str)
                or item.get("updateAvailable") is not True):
            continue
        result, image = item.get("result"), item.get("image")
        watched = image.get("tag") if isinstance(image, dict) else None
        if not isinstance(result, dict) or not isinstance(watched, dict):
            continue
        old, new = watched.get("value"), result.get("tag")
        if isinstance(old, str) and isinstance(new, str) and TAG.fullmatch(new) and old != new:
            yield item["id"], old, new


def wud_versions(snapshot, observations):
    if not isinstance(observations, list):
        raise ValueError("WUD did not return its container list.")
    deployed = {container["id"]: container for container in snapshot["containers"]}
    service_images = {}
    for container in deployed.values():
        service_images.setdefault(container["service"], set()).add(container["image"])
    versions = {}
    for identifier, watched, tag in wud_tag_changes(observations):
        container = deployed.get(identifier)
        if container is None:
            continue
        service, image = container["service"], container["image"]
        if "@" in image or len(service_images[service]) != 1:
            continue
        old_tag = image.rsplit(":", 1)[-1] if ":" in image.rsplit("/", 1)[-1] else "latest"
        if watched != old_tag:
            continue
        if snapshot["images"].get(service) not in (image, tag_reference(image, tag)):
            continue
        if service in versions and versions[service] != tag:
            raise ValueError("WUD returned conflicting versions for replicas of one service.")
        versions[service] = tag
    return versions


def configured_wud(url=None):
    record = {}
    bao = configured_openbao(mount="services")
    if bao:
        try:
            record, _ = bao.fields_with_version("wud/onboardrr", "url", "username")
        except HttpStatusError as error:
            if error.status != 404:
                raise
    origin = url or os.environ.get("ONBOARDRR_WUD_URL") or record.get("url")
    if not origin:
        return None, {}
    service = JsonService("WUD", origin)
    override_token = os.environ.get("ONBOARDRR_WUD_TOKEN")
    if override_token:
        return service, {"Authorization": f"Bearer {override_token}"}
    if record.get("url"):
        configured_service = JsonService("WUD", record["url"])
        if (service.host, service.port or 443) != (configured_service.host, configured_service.port or 443):
            return service, {}
    if username := record.get("username"):
        if not isinstance(username, str) or ":" in username:
            raise ValueError("Configure a valid WUD Basic username.")
        password, = bao.read("wud/PASSWORD", "value")
        encoded = base64.b64encode(f"{username}:{password}".encode()).decode("ascii")
        return service, {"Authorization": f"Basic {encoded}"}
    token, = integration_credentials("wud", optional=True)
    return service, {"Authorization": f"Bearer {token}"} if token else {}


def manage_compose_files(database, device, known_hosts, public_key_file, *, files=(), workspace=None,
                         apply=False, deploy=False, wud_url=None):
    from . import ssh

    public_key = ssh.load_public_key(public_key_file)
    with (
        device_lock(database, device),
        ssh.agent_identity(public_key) as key,
        ssh.connect(device, known_hosts, key=key) as client,
    ):
        def request(action, *, timeout=45, **parameters):
            payload = json.dumps({"action": action, "host": device["name"],
                                  "nas": device["profile"] == NAS_PROFILE, **parameters}).encode()
            result = json.loads(ssh.script(client, "compose_update.py", payload, timeout=timeout,
                                           entware=device["profile"] == NAS_PROFILE, max_output=4 * 1024 * 1024))
            if "error" in result:
                raise ValueError(result["error"])
            return result

        if not files:
            LOGGER.info("Listing Compose files on %s", device["name"])
            return {"device": device["name"], **request("list-files")}
        LOGGER.info("Reading %d selected Compose files on %s", len(files), device["name"])
        snapshot = request("read-files", workspace=workspace, files=list(files))
        wud, headers = configured_wud(wud_url)
        if not wud:
            raise ValueError("Configure the WUD HTTPS origin to select image versions.")
        LOGGER.info("Checking available image tags in WUD")
        observations = wud.request("GET", "/api/containers", headers=headers, operation="read available versions")
        if not isinstance(observations, list):
            raise ValueError("WUD did not return its container list.")
        reports, changes, images = {}, {}, {}
        for path, original in snapshot["files"].items():
            targets = {} if deploy else None
            try:
                updated, reports[path] = plan_compose_file(
                    original["text"], snapshot["architecture"], device["name"], observations, deployment_images=targets)
            except ValueError as error:
                reports[path] = {"error": str(error)}
                continue
            if updated != original["text"]:
                changes[path] = updated
            if targets:
                images[path] = targets
        result = {"status": "no-updates" if apply else "preview"}
        watched = list(wud_tag_changes(observations)) if apply and deploy else []
        if apply and (changes or (images and watched)):
            LOGGER.info("Saving %d Compose files. Deployment %s", len(changes), "requested" if deploy else "disabled")
            options = {}
            if deploy:
                options = {
                    "timeout": 960 * len(images),
                    "deployment": {
                        "images": images,
                        "updated": {path: list(reports[path]["updates"]) for path in changes},
                        "wud_tags": watched,
                    },
                }
            result = request("write-files", workspace=snapshot["workspace"], files=list(files), changes=changes,
                             expected={path: value["digest"] for path, value in snapshot["files"].items()}, **options)
        invalid = [f"{path}: {report['error']}" for path, report in reports.items() if "error" in report]
        if invalid:
            result["blockers"] = [*result.get("blockers", []), *invalid]
            if apply:
                result["status"] = "deployment-incomplete" if deploy else "files-incomplete"
        return {"device": device["name"], "workspace": snapshot["workspace"], **result, "reports": reports}


def update_stack(database, device, project, known_hosts, public_key_file, *, apply, wud_url=None, token=None,
                 refresh=False, files_only=False):
    from . import ssh

    if project is not None and not PROJECT.fullmatch(project):
        raise ValueError("Use the existing Docker Compose project name.")
    if files_only and refresh:
        raise ValueError("File-only updates require WUD versions.")
    if refresh and (project is None or wud_url):
        raise ValueError("Manual refresh requires one Compose project and cannot be combined with --wud-url.")
    if refresh:
        wud, headers = None, {}
    elif token:
        wud, headers = JsonService("WUD", wud_url), {"Authorization": f"Bearer {token}"}
    else:
        wud, headers = configured_wud(wud_url)
    if (project is None or files_only) and wud is None:
        raise ValueError("Set the WUD origin to select image updates.")
    nas = device.get("profile") == NAS_PROFILE
    public_key = ssh.load_public_key(public_key_file)
    with (
        device_lock(database, device),
        ssh.agent_identity(public_key) as key,
        ssh.connect(device, known_hosts, key=key) as client,
    ):
        def request(values, timeout):
            payload = json.dumps({"nas": nas, **values}).encode()
            result = json.loads(ssh.script(client, "compose_update.py", payload, timeout=timeout, entware=nas))
            if "error" in result:
                raise ValueError(result["error"])
            return result

        observations = None
        if wud:
            observations = wud.request(
                "GET", "/api/containers", headers=headers, operation="read available versions"
            )
        projects = [project] if project is not None else request({"list": True}, 30)["projects"]
        reports = {}
        for name in projects:
            try:
                values = {"project": name, "wud": wud is not None}
                snapshot = request(values, 120)
                versions = wud_versions(snapshot, observations) if wud else {}
                if apply and (versions or wud is None):
                    result = request({**values, "apply": True, "expected": snapshot, "versions": versions,
                                      "files_only": files_only}, 930)
                else:
                    result = {"status": "no-updates" if apply else "preview", "images": snapshot["images"]}
                reports[name] = {**result, "wud_versions": versions, "skipped": snapshot.get("skipped", {})}
            except ValueError as error:
                if project is not None:
                    raise
                reports[name] = {"status": "failed", "error": str(error)}
    if project is not None:
        return {"device": device["name"], "project": project, **reports[project]}
    status = "partial" if any(report["status"] == "failed" or report["skipped"]
                              for report in reports.values()) else ("applied" if apply else "preview")
    return {"device": device["name"], "status": status, "projects": reports}
