"""Select the primary QNAP storage and synchronize its shared IPv4 in OpenBao."""

import logging
import os
from dataclasses import dataclass

from .http_json import HttpStatusError
from .inventory import Inventory, NAS_PROFILE, normalize_host
from .secrets import configured_openbao
from .storage import connect, provisioning_lock


STORAGE_IP = "storage/IP"
LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class StorageEndpoint:
    host: str
    mount_host: str
    name: str = "goku"
    user: str | None = None
    port: int = 22

    def ssh_args(self):
        if self.user is not None:
            return ["-p", str(self.port), "-l", self.user, self.host]
        return [self.host]

    def mount_sources(self, device, cifs=None, nfs=None):
        if self.user:
            candidates = {self.host, self.mount_host, self.name}
            if cifs and (not cifs.startswith("//") or cifs[2:].partition("/")[0].lower() not in candidates):
                raise ValueError("Use an SMB source on the selected primary storage NAS.")
            if nfs:
                host, separator, _ = nfs.partition(":/")
                if not separator or host.lower() not in candidates:
                    raise ValueError("Use an NFS source on the selected primary storage NAS.")
        return cifs or f"//{self.mount_host}/compose", nfs or f"{self.mount_host}:/onboardrr-{device}"


def ssh_destination(endpoint):
    return endpoint.ssh_args() if isinstance(endpoint, StorageEndpoint) else [normalize_host(endpoint)]


def storage_endpoint(database, *, override=None, allow_pending=False):
    roles = Inventory(database).storage_roles()
    if not allow_pending and (pending := roles.get("pending")):
        raise ValueError(f"Retry the pending storage switch to {pending['name']} before using storage.")
    primary = roles.get("active")
    if primary and (not override or normalize_host(override) in {primary["host"], primary["name"], primary["ipv4"]}):
        return StorageEndpoint(primary["host"], primary["ipv4"], primary["name"],
                               primary["ssh_user"], primary["ssh_port"])
    host = normalize_host(override or os.environ.get("ONBOARDRR_GOKU_HOST", "goku.local"))
    mount_host = normalize_host(os.environ.get("ONBOARDRR_GOKU_MOUNT_HOST", host)) if not override else host
    return StorageEndpoint(host, mount_host, "" if primary and override else "goku")


def storage_ip_version(bao):
    try:
        fields, version = bao.fields_with_version(STORAGE_IP, "value")
    except HttpStatusError as error:
        if error.status != 404:
            raise
        return None, 0
    return fields.get("value"), version


def write_selection(connection, state, device, address):
    connection.execute(
        """INSERT INTO primary_storage (state, device_id, ipv4) VALUES (?, ?, ?)
           ON CONFLICT (state) DO UPDATE SET device_id = excluded.device_id, ipv4 = excluded.ipv4""",
        (state, device["id"], address),
    )


def publish_storage_ip(inventory, bao, device, address):
    with provisioning_lock(inventory.database, "primary-storage"):
        pending = inventory.primary_storage(pending=True)
        if pending and (pending["id"], pending["ipv4"]) != (device["id"], address):
            raise ValueError(f"First resume the pending storage switch to {pending['name']} at {pending['ipv4']}.")
        current, version = storage_ip_version(bao)
        with connect(inventory.database, write=True) as connection:
            enrolled = connection.execute("SELECT profile, host FROM devices WHERE id = ?", (device["id"],)).fetchone()
            if not enrolled or (enrolled["profile"], enrolled["host"]) != (NAS_PROFILE, device["host"]):
                raise ValueError("The selected NAS changed in the inventory. Preview the switch again.")
            write_selection(connection, "pending", device, address)

        if current != address:
            LOGGER.info("Publishing the primary storage address to OpenBao")
            try:
                if version:
                    bao.patch(STORAGE_IP, {"value": address}, version=version)
                else:
                    bao.create(STORAGE_IP, {"value": address})
            except HttpStatusError as error:
                if 400 <= error.status < 500 and not pending:
                    with connect(inventory.database, write=True) as connection:
                        connection.execute("DELETE FROM primary_storage WHERE state = 'pending'")
                raise
            if bao.read(STORAGE_IP, "value") != (address,):
                raise ValueError("OpenBao did not confirm the primary storage IP. Retry the pending switch.")

        with connect(inventory.database, write=True) as connection:
            write_selection(connection, "active", device, address)
            connection.execute("DELETE FROM primary_storage WHERE state = 'pending'")
        LOGGER.info("Primary storage confirmed for %s", device["name"])


def select_primary_storage(inventory, name, address, *, apply):
    from .pangolin_network import destination_ipv4
    from .stacks import validate_lan_address

    device = inventory.get(name)
    if device["profile"] != NAS_PROFILE:
        raise ValueError("The primary storage device must use the QNAP NAS profile.")
    roles = inventory.storage_roles()
    pending = roles.get("pending")
    if address is None and pending and pending["id"] == device["id"]:
        address = pending["ipv4"]
    address = destination_ipv4(device, address)
    validate_lan_address(address)
    previous = roles.get("active")
    result = {"device": device["name"], "ipv4": address,
              "previous": previous["name"] if previous else "goku",
              "openbao": "services/storage/IP", "status": "preview"}
    if not apply:
        return result
    bao = configured_openbao(mount="services")
    if bao is None:
        raise ValueError("Configure OpenBao before switching the primary NAS.")
    try:
        publish_storage_ip(inventory, bao, device, address)
    except HttpStatusError as error:
        if error.status == 403:
            raise ValueError("Grant the Onboardrr controller read, create and patch access to services/data/storage/IP.") from None
        raise
    return {**result, "status": "applied"}
