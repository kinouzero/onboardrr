"""Store device records without connecting to their SSH endpoints."""

import ipaddress
import os
import re
import sqlite3
import uuid
from pathlib import Path

from .storage import connect, device_lock, provisioning_lock


PROFILE = "raspberry-pi"
NAS_PROFILE = "nas"
SERVER_PROFILE = "server"
PROFILES = (PROFILE, NAS_PROFILE, SERVER_PROFILE)
HOST_LABEL = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?")
SSH_USER = re.compile(r"[a-zA-Z_][a-zA-Z0-9_.-]*\$?")


def default_database():
    state_home = Path(os.environ.get("XDG_STATE_HOME", ""))
    if not state_home.is_absolute():
        state_home = Path.home() / ".local" / "state"
    return state_home / "onboardrr" / "inventory.sqlite3"


def normalize_host(value):
    if "%" not in value:
        try:
            return str(ipaddress.ip_address(value))
        except ValueError:
            pass

    hostname = value.removesuffix(".").lower()
    labels = hostname.split(".")
    if (
        len(hostname) > 253
        or not value.isascii()
        or not all(HOST_LABEL.fullmatch(label) for label in labels)
        or all(label.isdecimal() for label in labels)
    ):
        raise ValueError("Host must be an IPv4 address, an IPv6 address, or a DNS name.")
    return hostname


def normalize_profile(profile):
    if profile == "serveur":
        profile = SERVER_PROFILE
    if profile not in PROFILES:
        raise ValueError(f"Unsupported profile: {profile}.")
    return profile


class Inventory:
    def __init__(self, database):
        self.database = database

    def add(self, name, host, ssh_user, ssh_port, profile):
        if not name.isascii():
            raise ValueError("Device name must be a DNS label.")
        name = name.lower()
        if not HOST_LABEL.fullmatch(name):
            raise ValueError("Device name must be a DNS label.")
        host = normalize_host(f"{name}.local" if host is None else host)
        if not SSH_USER.fullmatch(ssh_user):
            raise ValueError("Invalid SSH username.")
        if not 1 <= ssh_port <= 65535:
            raise ValueError("SSH port must be between 1 and 65535.")
        profile = normalize_profile(profile)

        with connect(self.database, write=True) as connection:
            cursor = connection.execute(
                """
                INSERT INTO devices (id, name, host, ssh_user, ssh_port, profile)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT DO NOTHING
                """,
                (str(uuid.uuid4()), name, host, ssh_user, ssh_port, profile),
            )
            existing = connection.execute(
                "SELECT * FROM devices WHERE name = ?", (name,)
            ).fetchone()
            if cursor.rowcount == 1:
                return dict(existing)
            if existing is None:
                raise ValueError(f"SSH endpoint {host} on port {ssh_port} is already registered.")
            saved_settings = tuple(existing[key] for key in ("host", "ssh_user", "ssh_port", "profile"))
            if (host, ssh_user, ssh_port, profile) != saved_settings:
                raise ValueError(f"Device name '{name}' is already registered with different settings.")
            return dict(existing)

    def list(self):
        with connect(self.database) as connection:
            rows = connection.execute("SELECT * FROM devices ORDER BY name").fetchall()
            return [dict(row) for row in rows]

    def set_host(self, name, host):
        host = normalize_host(host)
        with connect(self.database, write=True) as connection:
            name = name.lower()
            try:
                result = connection.execute("UPDATE devices SET host = ? WHERE name = ?", (host, name))
            except sqlite3.IntegrityError as error:
                raise ValueError(f"SSH endpoint {host} is already registered on this device's SSH port.") from error
            if result.rowcount == 0:
                raise ValueError("Unknown device. Register it with onboardrr add.")
            return dict(connection.execute("SELECT * FROM devices WHERE name = ?", (name,)).fetchone())

    def get(self, name):
        with connect(self.database) as connection:
            row = connection.execute(
                "SELECT * FROM devices WHERE name = ?", (name.lower(),)
            ).fetchone()
        if row is None:
            raise ValueError("Unknown device. Register it with onboardrr add.")
        return dict(row)

    def primary_storage(self, *, pending=False):
        return self.storage_roles().get("pending" if pending else "active")

    def storage_roles(self):
        with connect(self.database) as connection:
            rows = connection.execute(
                "SELECT devices.*, primary_storage.ipv4, primary_storage.state FROM primary_storage "
                "JOIN devices ON devices.id = primary_storage.device_id"
            ).fetchall()
        roles = {}
        for row in rows:
            item = dict(row)
            roles[item.pop("state")] = item
        return roles

    def set_profile(self, name, profile):
        from .runs import change_run_workflow

        profile = normalize_profile(profile)
        device = self.get(name)
        with (
            provisioning_lock(self.database, f"auto-{device['id']}"),
            device_lock(self.database, device),
            connect(self.database, write=True) as connection,
        ):
            previous = connection.execute("SELECT profile FROM devices WHERE id = ?", (device["id"],)).fetchone()
            if profile != NAS_PROFILE and connection.execute(
                "SELECT 1 FROM primary_storage WHERE device_id = ?", (device["id"],),
            ).fetchone():
                raise ValueError("Select another primary storage NAS before changing this device's profile.")
            row = connection.execute(
                "UPDATE devices SET profile = ? WHERE id = ? RETURNING *", (profile, device["id"]),
            ).fetchone()
            if (previous["profile"] == NAS_PROFILE) != (profile == NAS_PROFILE):
                change_run_workflow(connection, device["id"], profile)
            return dict(row)
