"""Retire a dedicated QTS NFS export without touching its backing data."""

import os
import shlex
import subprocess
from ipaddress import IPv4Address
from pathlib import Path

from .goku_retirement import DATA_ROOT, check_host_mounts
from .inventory import HOST_LABEL, SSH_USER, normalize_host
from .primary_storage import StorageEndpoint, ssh_destination
from .retirement import retirement_plan
from .runs import ResourceKind
from .runtime import system_environment
from .secrets import configured_openbao, storage_smb_record
from .storage import device_lock


def configured_qts_sudo(host, command):
    selected = host if isinstance(host, StorageEndpoint) and host.user is not None else None
    endpoint = host.host if isinstance(host, StorageEndpoint) else normalize_host(host)
    if not selected and endpoint != normalize_host(os.environ.get("ONBOARDRR_GOKU_HOST", "goku.local")):
        raise ValueError("Set goku_host before using OpenBao sudo credentials on a different NAS endpoint.")
    bao = configured_openbao()
    if bao is None:
        raise ValueError("Configure OpenBao Goku credentials or passwordless QTS sudo.")
    user, password = bao.read(storage_smb_record(selected.name if selected else None), "smb_username", "smb_password")
    if not SSH_USER.fullmatch(user) or any(char in password for char in "\r\n"):
        raise ValueError("The OpenBao Goku credentials are invalid for QTS sudo.")
    return (
        '[ "$(id -un)" = ' + shlex.quote(user) + " ] || exit 78; exec sudo -S -p '' -- " + command,
        password + "\n",
    )


def ensure_goku_export(host, name, client_ip, *, local=False):
    from .stacks import validate_lan_address

    if not HOST_LABEL.fullmatch(name):
        raise ValueError("Use a valid Periphery device name.")
    validate_lan_address(client_ip)
    script = Path(__file__).with_name("remote").joinpath("goku_export_create.sh").read_text()
    command = shlex.join(["/bin/sh", "-c", script, "--", name, client_ip])
    command += ' "$(id -u)" "$(id -g)"' + (" local" if local else "")
    ssh = ["ssh", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes", "-o", "ConnectTimeout=7",
           *ssh_destination(host)]

    def execute(remote, password=""):
        return subprocess.run([*ssh, remote], input=password, capture_output=True, text=True,
                              timeout=90, env=system_environment())

    try:
        result = execute(
            'if [ "$(id -u)" = 0 ]; then exec ' + command
            + "; elif sudo -n true 2>/dev/null; then exec sudo -n -- " + command + "; else exit 77; fi"
        )
        if result.returncode == 77:
            result = execute(*configured_qts_sudo(host, command))
    except (OSError, subprocess.SubprocessError):
        raise ValueError("The Goku export setup could not complete over SSH.") from None
    if result.returncode == 78:
        raise ValueError("The Goku SSH account does not match the OpenBao account for QTS sudo.")
    if result.returncode == 13:
        raise ValueError("The existing Goku export has a different path, client, or access policy.")
    if result.returncode == 14:
        raise ValueError("QTS could not roll back the failed Goku export. Inspect the NAS export configuration.")
    if result.returncode or result.stdout.strip() not in {"created-directory", "ready"}:
        if local:
            raise ValueError("Could not prepare the local Periphery directory on Goku. Check QTS sudo and share access.")
        raise ValueError("Could not activate the Goku NFS export. Check QTS sudo and the NAS export configuration.")
    return result.stdout.strip()


def retire_goku_export(database, device, goku_host, client_ip, *, known_hosts, public_key, apply, password):
    name = device["name"]
    client = str(IPv4Address(client_ip or device["host"]))
    expected = f"{DATA_ROOT}/{name}"
    with device_lock(database, device):
        actions = retirement_plan(database, name, purge=True)["actions"]
        owned = {(item["kind"], item["reference"]) for item in actions if item["action"] == "remove"}
        if (ResourceKind.GOKU_DIRECTORY, expected) not in owned or (ResourceKind.MOUNT, expected) not in owned:
            raise ValueError("The device does not own both the dedicated Goku directory and its mount.")
        if apply and (not public_key or not check_host_mounts(device, known_hosts, public_key, [expected])):
            raise ValueError("Detach the device's NFS mount before removing its export.")
        script = Path(__file__).with_name("remote").joinpath("goku_export.sh").read_text()
        command = shlex.join(["/bin/sh", "-c", script, "--", name, client, "apply" if apply else "preview"])
        def execute(remote, stdin=""):
            return subprocess.run(
                ["ssh", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes",
                 "-o", "ConnectTimeout=7", *ssh_destination(goku_host), remote],
                input=stdin, capture_output=True, text=True, timeout=90,
                env=system_environment(),
            )

        try:
            if not apply:
                result = execute(command)
            elif password:
                result = execute('if [ "$(id -u)" = 0 ]; then exec ' + command
                                 + "; else exec sudo -S -p '' -- " + command + "; fi", password + "\n")
            else:
                result = execute('if [ "$(id -u)" = 0 ]; then exec ' + command
                                 + "; elif sudo -n true 2>/dev/null; then exec sudo -n -- " + command + "; else exit 77; fi")
                if result.returncode == 77:
                    result = execute(*configured_qts_sudo(goku_host, command))
        except (OSError, subprocess.SubprocessError):
            raise ValueError("The Goku export check could not complete over SSH.") from None
        outcome = result.stdout.strip()
        if result.returncode or outcome not in {"eligible", "removed", "absent"}:
            messages = {
                "Goku share path mismatch.", "Goku export does not belong exclusively to the selected client.",
                "Unknown QTS NFS access state.", "Unexpected clients or aliases share the Goku export.",
                "Goku apply requires an administrator.", "Goku export rollback failed.",
                "Goku export removal could not be verified.",
            }
            detail = result.stderr.strip()
            raise ValueError(detail if detail in messages else "Goku export operation failed. Check QTS access and sudo.")
        return {"device": name, "mode": "apply" if apply else "preview",
                "export": f"/onboardrr-{name}", "status": outcome}
