"""Keep existing primary NAS mounts and NFS access aligned with a host IPv4."""

import shlex
import subprocess
from ipaddress import IPv4Address
from pathlib import Path

from .goku_export import configured_qts_sudo
from .inventory import HOST_LABEL
from .linux import run_ssh_probe
from .primary_storage import storage_endpoint
from .runtime import system_environment
from .secrets import stored_host_ipv4


def update_nfs_client(storage, name, kind, previous, address):
    if not HOST_LABEL.fullmatch(name):
        raise ValueError("Use a valid Periphery device name.")
    script = Path(__file__).with_name("remote").joinpath("goku_export_ip.sh").read_text()
    command = shlex.join(["/bin/sh", "-c", script, "--", name, kind, previous or "", address])
    ssh = ["ssh", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes", "-o", "ConnectTimeout=7",
           *storage.ssh_args()]

    def execute(remote, password=""):
        return subprocess.run([*ssh, remote], input=password, capture_output=True, text=True,
                              timeout=55, env=system_environment())

    try:
        result = execute('if [ "$(id -u)" = 0 ]; then exec ' + command
                         + "; elif sudo -n true 2>/dev/null; then exec sudo -n -- " + command + "; else exit 77; fi")
        if result.returncode == 77:
            result = execute(*configured_qts_sudo(storage, command))
    except (OSError, subprocess.SubprocessError):
        raise ValueError("Could not update the existing NFS authorization over SSH.") from None
    if result.returncode == 13:
        raise ValueError("The NAS export does not match the recorded client and backing directory.")
    if result.returncode == 14:
        raise ValueError("QTS could not roll back the NFS authorization. Check the NAS export.")
    if result.returncode or result.stdout.strip() not in {"updated", "ready"}:
        raise ValueError("Could not verify the existing NAS NFS authorization.")
    return result.stdout.strip() == "updated"


def sync_storage_ip(database, device, known_hosts, address, *, apply):
    storage = storage_endpoint(database)
    try:
        storage_ip = str(IPv4Address(storage.mount_host))
    except ValueError:
        return {}
    name = device["name"]
    names = {storage.host, storage.name, f"{storage.name}.local"} - {"", ".local"}

    def probe(action, *, privileged=False):
        try:
            report = run_ssh_probe(device, known_hosts, "storage_ip_sync.py", address=address,
                                   payload={"action": action, "device": name, "user": device["ssh_user"],
                                            "storage_ip": storage_ip, "storage_names": sorted(names)},
                                   privileged=privileged, timeout=60)
        except ValueError as error:
            if privileged:
                raise ValueError("Could not repair mounts with passwordless sudo on the host.") from error
            raise
        if report.get("error"):
            raise ValueError(report["error"])
        return report

    report = probe("inspect")
    if not any(report.values()):
        return {}
    if not apply:
        return {"storage_ip_sync": report}
    previous = stored_host_ipv4(name, nas_client=True) if report["nfs_exports"] else None
    nfs_client = str(IPv4Address(report["nfs_client"])) if report["nfs_exports"] else None
    exports = [export for export in report["nfs_exports"] if update_nfs_client(
        storage, name, "dedicated" if export == f"/onboardrr-{name}" else "legacy", previous, nfs_client,
    )]
    if report["mounts_pending"]:
        probe("repair", privileged=True)
    if report["start_periphery"]:
        probe("start")
    return {"storage_ip_sync": {"mounts_repaired": report["mounts_pending"], "nfs_exports_updated": exports,
                                "periphery_started": report["start_periphery"], "nfs_client": nfs_client}}
