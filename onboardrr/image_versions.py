"""Resolve the image tags allowed by WUD rules in new Compose stacks."""

import re

import yaml

from .http_json import JsonService


TAG = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}", re.ASCII)
REPOSITORY = re.compile(r"[a-z0-9]+(?:[._-]+[a-z0-9]+)*")
VERSION = re.compile(r"(?P<prefix>[A-Za-z_-]+[-_]|v)?(?P<number>\d+(?:\.\d+){0,2})(?:-(?P<suffix>[\w.-]+))?", re.ASCII)
PRERELEASE = re.compile(r"(?:rc|beta|alpha|preview|dev)[\d.-]*|\d+(?:\.\d+)*", re.IGNORECASE)
ARCHITECTURES = {"x86_64": "amd64", "aarch64": "arm64"}
DOCKER_HUB = JsonService("Docker Hub", "https://hub.docker.com")


def docker_hub_tags(repository):
    namespace, name = repository.split("/")
    for page in range(1, 41):
        payload = DOCKER_HUB.request(
            "GET", f"/v2/namespaces/{namespace}/repositories/{name}/tags?page_size=100&page={page}",
            headers={"Accept": "application/json"}, operation=f"listing {repository} tags",
        )
        if not isinstance(payload, dict) or not isinstance(payload.get("results"), list) or "next" not in payload:
            raise ValueError("Docker Hub returned an invalid tag list.")
        for entry in payload["results"]:
            if (
                not isinstance(entry, dict)
                or not isinstance(entry.get("name"), str)
                or not isinstance(entry.get("images"), list)
                or not all(isinstance(image, dict) for image in entry["images"])
            ):
                raise ValueError("Docker Hub returned invalid tag metadata.")
            yield entry
        if payload["next"] is None:
            return
    raise ValueError(f"Docker Hub tag pagination limit reached for {repository}.")


def version_parts(tag):
    match = VERSION.fullmatch(tag)
    if not match:
        return None
    prefix, number, suffix = match.groups()
    digits = tuple(map(int, number.split(".")))
    numeric = digits + (0,) * (3 - len(digits))
    return prefix or "", numeric, suffix or "", len(digits)


def version_order(parts):
    _, numeric, suffix, _ = parts
    prerelease = PRERELEASE.fullmatch(suffix)
    # Numeric identifiers sort before textual identifiers under SemVer.
    identifiers = tuple((0, int(s)) if s.isdecimal() else (1, s) for s in re.findall(r"\d+|[^.\d]+", suffix))
    return numeric, not prerelease, identifiers if prerelease else ()


def compile_pattern(value):
    # WUD uses JavaScript RegExp. The shared syntax covers the deployed rules.
    pattern = re.sub(r"\(\?<([a-zA-Z_]\w*)>", r"(?P<\1>", value)
    try:
        return re.compile(pattern, re.ASCII)
    except re.error as error:
        raise ValueError("Unsupported WUD tag regular expression in Compose.") from error


def tag_transform(value):
    if not value:
        return lambda tag: tag
    pattern, separator, replacement = value.replace("$$", "$").partition("=>")
    if not separator:
        raise ValueError("The WUD tag transform must use 'pattern => replacement'.")
    pattern = compile_pattern(pattern.strip())
    replacement = replacement.strip()
    references = re.findall(r"\$([1-9]\d*)", replacement)
    if any(int(group) > pattern.groups for group in references):
        raise ValueError("The WUD tag transform refers to a missing capture group.")

    def transform(tag):
        return pattern.sub(
            lambda match: re.sub(r"\$([1-9]\d*)", lambda ref: match[int(ref[1])] or "", replacement),
            tag, count=1,
        )

    return transform


def tag_filter(value, tag, parts):
    if not value:
        return None
    prefix, (major, minor, patch), suffix, _ = parts
    prerelease = PRERELEASE.fullmatch(suffix)
    variables = dict(major=major, minor=minor, patch=patch, version=f"{major}.{minor}.{patch}",
                     prefix=prefix, flavor="" if prerelease else suffix,
                     prerelease=suffix if prerelease else "", raw=tag, original=tag)

    def substitute(match):
        if match[1] not in variables:
            raise ValueError("Unsupported WUD tag filter variable in Compose.")
        return re.escape(str(variables[match[1]]))

    return compile_pattern(re.sub(r"\$\{(\w+)\}", substitute, value.replace("$$", "$")))


def compatible_tag(entry, platform):
    if entry.get("tag_status", "active") != "active":
        return False
    os_name, architecture, *variant = platform
    return any(
        image.get("os") == os_name
        and image.get("architecture") == architecture
        and image.get("status", "active") == "active"
        and (not variant or (image.get("variant") or ("v8" if architecture == "arm64" else "")) == variant[0])
        for image in entry.get("images", [])
    )


def select_tag(entries, current, labels, platform):
    transform = tag_transform(labels.get("wud.tag.transform"))
    base_parts = version_parts(current)
    normalized = version_parts(transform(current))
    if not normalized:
        return current
    include = tag_filter(labels.get("wud.tag.include"), current, base_parts or normalized)
    exclude = tag_filter(labels.get("wud.tag.exclude"), current, base_parts or normalized)
    baseline = version_order(normalized)
    best, best_order = current, baseline
    for entry in entries:
        tag = entry.get("name", "")
        if (not TAG.fullmatch(tag) or tag.endswith(".sig") or (include and not include.search(tag))
                or (exclude and exclude.search(tag))):
            continue
        candidate = version_parts(transform(tag))
        if not candidate:
            continue
        if not include and (candidate[0], candidate[2], candidate[3]) != (normalized[0], normalized[2], normalized[3]):
            continue
        order = version_order(candidate)
        if (baseline[1] and not order[1]) or (baseline[0][0] < 100 and order[0][0] >= 2000):
            continue
        if order > baseline and (order, tag) > (best_order, best) and compatible_tag(entry, platform):
            best, best_order = tag, order
    return best


def labels_dict(labels):
    if isinstance(labels, list):
        return {key: value for label in labels if isinstance(label, str) for key, _, value in [label.partition("=")]}
    return labels if isinstance(labels, dict) else {}


def hub_repository(repository):
    unqualified = repository.removeprefix("docker.io/").removeprefix("index.docker.io/").removeprefix("registry-1.docker.io/")
    parts = unqualified.split("/")
    if len(parts) == 1:
        parts.insert(0, "library")
    if len(parts) != 2 or "." in parts[0] or not all(REPOSITORY.fullmatch(part) for part in parts):
        raise ValueError("WUD tag selection for new Compose stacks requires a public Docker Hub image.")
    return "/".join(parts)


def mapping_node(node, name):
    if isinstance(node, yaml.MappingNode):
        matches = [value for key, value in node.value if key.value == name]
        if len(matches) > 1:
            raise ValueError(f"Duplicate Compose mapping key: {name}.")
        if matches:
            return matches[0]
    return None


def resolve_images(compose, host_architecture, cache):
    if "wud.tag." not in compose:
        return compose, {}
    try:
        source = yaml.compose(compose, Loader=yaml.SafeLoader)
        document = yaml.safe_load(compose)
    except yaml.YAMLError:
        raise ValueError("Invalid YAML in a Compose template with WUD labels.") from None

    projects = mapping_node(source, "services")
    replacements = []
    versions = {}
    services = (document or {}).get("services", {}) if isinstance(document, dict) else {}
    if not isinstance(services, dict) or any(not isinstance(service, dict) for service in services.values()):
        raise ValueError("Invalid services in a Compose template with WUD labels.")
    for name, service in services.items():
        labels = labels_dict(service.get("labels"))
        if str(labels.get("wud.watch", "")).lower() == "false" or not any(
            labels.get("wud.tag." + key) for key in ("include", "exclude", "transform")
        ):
            continue
        image = service.get("image", "")
        if not isinstance(image, str):
            raise ValueError("Use a string image reference for WUD tag selection.")
        repository, separator, current = image.rpartition(":")
        if "@" in image or "$" in image or "[[" in image or not separator or not TAG.fullmatch(current):
            continue
        if not version_parts(tag_transform(labels.get("wud.tag.transform"))(current)):
            continue
        key = hub_repository(repository)
        platform = service.get("platform") or f"linux/{host_architecture()}"
        if not isinstance(platform, str) or not re.fullmatch(r"linux/[a-z0-9]+(?:/v[0-9]+)?", platform):
            raise ValueError("Set a concrete Linux platform for WUD tag selection.")
        platform = platform.split("/")
        if key not in cache:
            cache[key] = tuple(docker_hub_tags(key))
        selected = select_tag(cache[key], current, labels, platform)
        if selected == current:
            continue
        target = f"{repository}:{selected}"
        service_node = mapping_node(projects, name)
        image_node = mapping_node(service_node, "image")
        if not isinstance(image_node, yaml.ScalarNode) or image_node.style not in (None, "'", '"'):
            raise ValueError("Use an inline image reference for WUD tag selection.")
        start, end = image_node.start_mark.index, image_node.end_mark.index
        quote = image_node.style or ""
        if compose[start:end] != f"{quote}{image}{quote}":
            raise ValueError("Use an inline image reference without a YAML anchor for WUD tag selection.")
        replacements.append((start, end, f"{quote}{target}{quote}"))
        versions[name] = {"from": image, "to": target}
    if len({start for start, _, _ in replacements}) != len(replacements):
        raise ValueError("Use distinct inline images for services with WUD tag selection.")
    for start, end, target in sorted(replacements, reverse=True):
        compose = compose[:start] + target + compose[end:]
    return compose, versions
