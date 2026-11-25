"""Reconcile the manual Linux preparation stages with the current run."""

import json
import re
from pathlib import PurePosixPath

from .inventory import NAS_PROFILE
from .periphery import COMPOSE, TEMPLATE, GokuPeripheryError, SECRET_ASSIGNMENT, has_nas_bind, read_goku_files
from .primary_storage import StorageEndpoint, storage_endpoint
from .runs import Failure, Ownership, Resource, ResourceKind
from .storage import device_lock


STAGES = ("goku-files", "linux", "periphery")


def staged_resources(name, compose_root, data_root, *, nas=False):
    if compose_root is None or data_root is None:
        raise ValueError("Provide both Goku share roots to verify the staged files.")
    compose_dir, data_dir = compose_root / name, data_root / name
    if any(path.is_symlink() or not path.is_dir() for path in (compose_dir, data_dir)):
        raise ValueError("The device's Goku directories must exist and must be real directories.")
    compose, template = compose_dir / COMPOSE, compose_dir / TEMPLATE
    for path in (compose, template):
        if path.is_symlink() or not path.is_file() or not 0 < path.stat().st_size <= 1024 * 1024:
            raise ValueError("The expected Goku deployment files are missing or invalid.")
    return verified_staged_resources(name, {COMPOSE: compose.read_text(), TEMPLATE: template.read_text()},
                                     nas=nas)


def verified_staged_resources(name, files, *, nas=False):
    text = files[TEMPLATE]
    assignments = re.findall(r"(?m)^((?:DOCKER_)?HOSTNAME)=(.*)$", text)
    if (len(assignments) != 1 or assignments[0][1].strip().strip("'\"") != name
            or SECRET_ASSIGNMENT.search(text) or "{{_KOMODO_SECRETS}}" not in text):
        raise ValueError("Goku does not contain the device's unexpanded deployment template.")
    if "${" + assignments[0][0] + "}" not in files[COMPOSE]:
        raise ValueError("The Goku Compose file does not reference the device template.")
    if nas and not has_nas_bind(files[COMPOSE]):
        raise ValueError("The staged NAS Compose file needs a local Periphery data bind.")
    return [
        Resource(ResourceKind.GOKU_DIRECTORY, f"/share/compose/{name}", Ownership.REUSED),
        Resource(ResourceKind.GOKU_DIRECTORY, f"/share/docker/komodo/data/{name}", Ownership.REUSED),
        Resource(ResourceKind.GOKU_FILE, f"/share/compose/{name}/{COMPOSE}", Ownership.REUSED),
        Resource(ResourceKind.GOKU_FILE, f"/share/compose/{name}/{TEMPLATE}", Ownership.REUSED),
    ]


def host_resources(device, known_hosts, public_key, cifs_source, nfs_source, *, stage, mounts_only=False,
                   storage_name="goku"):
    from . import ssh

    nas = device["profile"] == NAS_PROFILE
    if not public_key or (stage == "linux" and not nas and not nfs_source):
        raise ValueError("Provide an enrolled SSH key and the expected Periphery NFS source.")
    name = device["name"]
    key = ssh.load_public_key(public_key)
    options = {"entware": True} if nas else {}
    payload = json.dumps({
        "device": name, "device_id": device["id"], "nfs_source": nfs_source, "cifs_source": cifs_source,
        "periphery": stage == "periphery", "mounts_only": mounts_only,
        **({"nas": True, "local_storage": name == storage_name} if nas else {}),
    }).encode()
    with ssh.agent_identity(key) as agent, ssh.connect(device, known_hosts, key=agent) as client:
        result = json.loads(ssh.script(client, "reconcile.py", payload, timeout=55, **options))
    if result.get("ready") is not True:
        raise ValueError(result.get("error", "Host readiness checks did not pass."))
    if stage == "periphery" or nas:
        return []
    mounts = [Resource(ResourceKind.MOUNT, f"/share/docker/komodo/data/{name}", Ownership.REUSED)]
    if mounts_only:
        return mounts
    network_id = result.get("owned_network")
    owned = isinstance(network_id, str) and re.fullmatch(r"[0-9a-f]{64}", network_id)
    network = Resource(ResourceKind.DOCKER_NETWORK, f"{name}/{network_id}" if owned else name,
                       Ownership.CREATED if owned else Ownership.REUSED)
    return [*mounts, network]


def reconcile(device, runs, komodo, *, compose_root, data_root, known_hosts, public_key,
              cifs_source, nfs_source, apply, goku_host=None, through=None, only=None):
    name = device["name"]
    nas = device["profile"] == NAS_PROFILE
    storage = goku_host if isinstance(goku_host, StorageEndpoint) else storage_endpoint(runs.database)
    verification = "verify-local" if nas and name == storage.name else "verify"
    with device_lock(runs.database, device):
        history = runs.list(name)
        if not history or history[0]["next_step"] in {"ssh", "nas-prepare", "komodo-registration"}:
            raise ValueError("Enroll the SSH key and register this device before reconciling.")
        run = runs.show(history[0]["id"])
        step = run["next_step"]
        report = {"device": name, "run_id": run["id"], "next_step": step, "checks": {}}
        limit = only or through
        if limit and step in STAGES:
            if STAGES.index(step) > STAGES.index(limit):
                return {**report, "status": "ready"}
            if only and step != only:
                return {**report, "status": "incomplete", "blockers": [f"Complete {step} before checking {only}."]}
        while step in STAGES:
            try:
                if step == "goku-files":
                    resources = (verified_staged_resources(name, read_goku_files(goku_host, name, verification),
                                                          nas=verification == "verify-local")
                                 if goku_host else staged_resources(name, compose_root, data_root,
                                                                   nas=verification == "verify-local"))
                else:
                    resources = host_resources(
                        device, known_hosts, public_key, cifs_source, nfs_source, stage=step,
                        **({"mounts_only": True} if only == "linux" else {}),
                        **({"storage_name": storage.name} if nas else {}),
                    )
                if step == "periphery":
                    ids = {item["reference"] for item in run["resources"] if item["kind"] == ResourceKind.KOMODO_SERVER}
                    servers = komodo._request("/read", "ListServers", {"limit": 0})
                    if not any(server["id"] in ids and server["name"] == name
                               and server["info"].get("state") == "Ok" for server in servers):
                        raise ValueError("The recorded Periphery server is not ready in Komodo.")
            except (OSError, ValueError) as error:
                report["checks"][step] = False
                if isinstance(error, GokuPeripheryError) or device["profile"] == NAS_PROFILE:
                    report["blockers"] = [str(error)]
                if apply:
                    runs.mark_failed(run["id"], step, Failure.STEP_FAILED)
                return {**report, "status": "incomplete"}
            report["checks"][step] = True
            if not apply:
                return {**report, "status": "ready"}
            recorded = {(item["kind"], item["reference"]) for item in run["resources"]}
            for resource in resources:
                if (resource.kind, resource.reference) not in recorded:
                    runs.record_resource(run["id"], step, resource)
            runs.complete_step(run["id"], step)
            run = runs.show(run["id"])
            if step == limit:
                report["next_step"] = run["next_step"]
                break
            step = report["next_step"] = run["next_step"]
        return {**report, "status": "ready"}
