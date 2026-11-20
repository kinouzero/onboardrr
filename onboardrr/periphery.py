"""Render device-specific Periphery files from the approved Goku reference."""

import ipaddress
import os
import re
import shlex
import subprocess
import tarfile
from hashlib import sha256
from io import BytesIO
from pathlib import Path
from tempfile import NamedTemporaryFile

import yaml

from .inventory import HOST_LABEL, NAS_PROFILE
from .primary_storage import StorageEndpoint, ssh_destination
from .runs import Ownership, Resource, ResourceKind
from .runtime import system_environment
from .storage import device_lock


COMPOSE = "docker-compose.komodo.yml"
COMPOSE_ROOT = "/share/compose"
TEMPLATE = "template.env"
HOSTNAME_SETTING = re.compile(r"^(?:DOCKER_)?HOSTNAME=[^\n]*$", re.MULTILINE)
SECRET_ASSIGNMENT = re.compile(r"^\s*(?:export\s+)?(?:KOMODO_PASSKEY|PERIPHERY_PRIVATE_KEY)=", re.MULTILINE)
REFERENCE_NFS_DEVICE = ":/docker/komodo/data/${HOSTNAME}"
PERIPHERY_ROOT = "${KOMODO_PERIPHERY_ROOT_DIRECTORY}"
LEGACY_AGENT_ENV = "/home/${USER}/.config/docker/compose-openbao/cache/periphery.env"
AGENT_ENV = "${HOME}/.config/docker/openbao/compose/cache/periphery.env"
REFERENCE_ENV = """# Docker
HOSTNAME='naruto'
DOCKER_API_PORT=2380

# Hosts
{{__HOSTS}}

# Misc
DOMAIN={{__DOMAIN}}

# URLs
URL_KOMODO=https://komodo.${DOMAIN}

# Komodo
KOMODO_PERIPHERY_ROOT_DIRECTORY=/share/docker/komodo/data/${HOSTNAME}
KOMODO_PORT=9120
KOMODO_AGENT_PORT=8120
{{_KOMODO_SECRETS}}
"""


class GokuPeripheryError(ValueError):
    """A file access failure with a safe, user-visible explanation."""


def goku_periphery(host, name, action, payload=None):
    if not HOST_LABEL.fullmatch(name):
        raise ValueError("Use a valid Periphery device name.")
    source = Path(__file__).with_name("remote").joinpath("goku_periphery.sh").read_text()
    try:
        result = subprocess.run(
            ["ssh", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes", "-o", "ConnectTimeout=7",
             *ssh_destination(host), shlex.join(["/bin/sh", "-c", source, "--", action, name])],
            input=payload or b"", capture_output=True, timeout=35, env=system_environment(),
        )
    except (OSError, subprocess.SubprocessError):
        raise GokuPeripheryError("Could not access Goku over SSH to check the Periphery files.") from None
    if result.returncode:
        if action in {"verify", "verify-local"} and result.returncode == 12:
            raise GokuPeripheryError(f"Stage both Periphery files under {COMPOSE_ROOT}/{name} on Goku.")
        if action == "verify-local" and result.returncode == 11:
            raise GokuPeripheryError(f"Create /share/docker/komodo/data/{name} on the primary storage NAS.")
        if action == "verify" and result.returncode == 11:
            raise GokuPeripheryError(
                f"Create /share/docker/komodo/data/{name} on Goku and configure the device's NFS export."
            )
        raise GokuPeripheryError("Could not access the Periphery files on Goku. Check SSH and share access.")
    return result.stdout


def read_goku_files(host, name, action):
    contents = goku_periphery(host, name, action)
    try:
        with tarfile.open(fileobj=BytesIO(contents), mode="r:") as archive:
            members = archive.getmembers()
            if len(members) != 2 or {item.name for item in members} != {COMPOSE, TEMPLATE}:
                raise ValueError
            if not all(item.isfile() and 0 < item.size <= 1048576 for item in members):
                raise ValueError
            return {item.name: archive.extractfile(item).read().decode() for item in members}
    except (OSError, tarfile.TarError, ValueError):
        raise ValueError("Goku returned invalid Periphery files.") from None


def stage_goku_files(device, address, host, runs, *, nfs_export=None):
    from .goku_export import DATA_ROOT, ensure_goku_export

    name = device["name"]
    nas = device.get("profile") == NAS_PROFILE
    local = nas and name == (host.name if isinstance(host, StorageEndpoint) else "goku")
    with device_lock(runs.database, device):
        history = runs.list(name)
        if not history or history[0]["next_step"] != "goku-files":
            raise ValueError("Run Komodo registration before staging the device's Goku files.")
        compose = goku_periphery(host, name, "reference").decode()
        files = render_reference(name, address, compose, REFERENCE_ENV, nfs_export=nfs_export, nas=local)
        payload = BytesIO()
        with tarfile.open(fileobj=payload, mode="w") as archive:
            for filename, contents in files.items():
                data = contents.encode()
                item = tarfile.TarInfo(filename)
                item.size = len(data)
                item.mode = 0o600
                archive.addfile(item, BytesIO(data))
        created = set(goku_periphery(host, name, "stage", payload.getvalue()).decode().splitlines())
        if not created.issubset(files):
            raise ValueError("Goku did not confirm the expected staged Periphery files.")
        for filename in sorted(created):
            runs.record_resource(history[0]["id"], "goku-files", Resource(
                ResourceKind.GOKU_FILE, f"{COMPOSE_ROOT}/{name}/{filename}", Ownership.CREATED,
                content_sha256=sha256(files[filename].encode()).hexdigest(),
            ))
        result = {"device": name, "files": [f"{COMPOSE_ROOT}/{name}/{filename}" for filename in files]}
        if local or nfs_export == f"/onboardrr-{name}":
            status = ensure_goku_export(host, name, address, **({"local": True} if local else {}))
            data = f"{DATA_ROOT}/{name}"
            recorded = runs.show(history[0]["id"])["resources"]
            if not any(item["kind"] == ResourceKind.GOKU_DIRECTORY and item["reference"] == data for item in recorded):
                ownership = Ownership.CREATED if status == "created-directory" else Ownership.REUSED
                runs.record_resource(history[0]["id"], "goku-files", Resource(
                    ResourceKind.GOKU_DIRECTORY, data, ownership,
                ))
            if not local:
                result["nfs_export"] = nfs_export
        return result


def bind_nas_storage(compose):
    try:
        model = yaml.safe_load(compose)
        mounts = model["services"]["komodo-agent"]["volumes"]
        volumes = model["volumes"]
        selected = [index for index, mount in enumerate(mounts) if isinstance(mount, dict)
                    and mount.get("source") == "komodo_data" and mount.get("target") == PERIPHERY_ROOT]
        if len(selected) != 1 or volumes["komodo_data"]["driver_opts"]["type"] != "nfs":
            raise ValueError
        mounts[selected[0]] = {"type": "bind", "source": PERIPHERY_ROOT, "target": PERIPHERY_ROOT,
                               "bind": {"create_host_path": False}}
        del volumes["komodo_data"]
        if not volumes:
            del model["volumes"]
    except (KeyError, TypeError, ValueError, yaml.YAMLError):
        raise ValueError("The Goku reference does not contain the expected Periphery volume.") from None
    return yaml.safe_dump(model, sort_keys=False)


def has_nas_bind(compose):
    try:
        mounts = yaml.safe_load(compose)["services"]["komodo-agent"]["volumes"]
        return any(
            mount == f"{PERIPHERY_ROOT}:{PERIPHERY_ROOT}" or (
                isinstance(mount, dict) and mount.get("type") == "bind"
                and mount.get("source") == mount.get("target") == PERIPHERY_ROOT
            )
            for mount in mounts
        )
    except (KeyError, TypeError, yaml.YAMLError):
        return False


def render_reference(name, address, compose, template, *, nfs_export=None, nas=False):
    ipv4 = str(ipaddress.IPv4Address(address))
    compose = re.sub(r"\bDOCKER_HOSTNAME\b", "HOSTNAME", compose)
    if (not all(token in compose for token in ("${HOSTNAME}", "${KOMODO_PERIPHERY_ROOT_DIRECTORY}"))
            or not any(token in compose for token in ("${NARUTO_IP}", "${HOST_IP}", "${DOCKER_HOST_IP}"))
            or not any(token in compose for token in ("${KOMODO_PASSKEY}", LEGACY_AGENT_ENV, AGENT_ENV))):
        raise ValueError("The Compose file does not match the expected Periphery reference.")
    markers = ("{{__HOSTS}}", "{{__DOMAIN}}", "{{_KOMODO_SECRETS}}")
    if not all(marker in template for marker in markers) or SECRET_ASSIGNMENT.search(template):
        raise ValueError("Provide the unexpanded Periphery template with secret placeholders.")
    if len(HOSTNAME_SETTING.findall(template)) != 1 or re.search(r"^DOCKER_HOST_IP=", template, re.MULTILINE):
        raise ValueError("The reference must contain exactly one HOSTNAME setting.")

    compose = compose.replace("${NARUTO_IP}", "${DOCKER_HOST_IP}")
    if LEGACY_AGENT_ENV in compose or AGENT_ENV in compose:
        compose = compose.replace("${HOST_IP}", "${DOCKER_HOST_IP}").replace(LEGACY_AGENT_ENV, AGENT_ENV)
    if nas:
        compose = bind_nas_storage(compose)
    elif nfs_export is not None:
        if (
            not re.fullmatch(r"/[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)*", nfs_export)
            or any(part in {".", ".."} for part in nfs_export.split("/"))
        ):
            raise ValueError("Provide an absolute NFS export path.")
        if compose.count(REFERENCE_NFS_DEVICE) != 1:
            raise ValueError("The Compose reference must contain one Periphery NFS device.")
        compose = compose.replace(REFERENCE_NFS_DEVICE, f":{nfs_export}", 1)
    template = HOSTNAME_SETTING.sub(f"HOSTNAME='{name}'\nDOCKER_HOST_IP='{ipv4}'", template)
    template = template.replace("${DOCKER_HOSTNAME}", "${HOSTNAME}")
    return {COMPOSE: compose, TEMPLATE: template}


def render_files(device, address, reference_directory, output_directory, *, nfs_export=None, record_created=None):
    files = render_reference(
        device["name"], address,
        (reference_directory / COMPOSE).read_text(),
        (reference_directory / TEMPLATE).read_text(),
        nfs_export=nfs_export,
        nas=device.get("profile") == NAS_PROFILE and nfs_export is None,
    )
    if output_directory.is_symlink():
        raise ValueError("Choose a real directory for the rendered Periphery files.")
    output_directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    missing = []
    for filename, contents in files.items():
        destination = output_directory / filename
        if destination.is_symlink() or (
            destination.exists() and (not destination.is_file() or destination.read_text() != contents)
        ):
            raise ValueError(f"The output already contains a different {filename}.")
        if not destination.exists():
            missing.append((destination, contents))
    for destination, contents in missing:
        data = contents.encode("utf-8")
        with NamedTemporaryFile(dir=output_directory) as stream:
            stream.write(data)
            stream.flush()
            os.link(stream.name, destination)
        if record_created is not None:
            record_created(destination.name, sha256(data).hexdigest())

    return {"device": device["name"], "files": [str(output_directory / filename) for filename in files]}


def stage_files(device, address, reference_directory, compose_root, runs, *, nfs_export=None):
    name = device["name"]
    root = compose_root.resolve(strict=True)
    if not root.is_dir():
        raise ValueError("Provide the mounted Goku Compose share root.")
    with device_lock(runs.database, device):
        history = runs.list(name)
        if not history or history[0]["next_step"] != "goku-files":
            raise ValueError("Run Komodo registration before staging the device's Goku files.")
        run_id = history[0]["id"]

        def record_created(filename, digest):
            runs.record_resource(run_id, "goku-files", Resource(
                ResourceKind.GOKU_FILE, f"{COMPOSE_ROOT}/{name}/{filename}", Ownership.CREATED, content_sha256=digest,
            ))

        return render_files(device, address, reference_directory, root / name, nfs_export=nfs_export,
                            record_created=record_created)
