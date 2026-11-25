"""Manage provisioning secrets in an OpenBao KV v2 mount."""

import os
import re
import stat
from ipaddress import IPv4Address
from pathlib import Path

from .http_json import HttpStatusError, JsonService
from .inventory import HOST_LABEL


PATH_SEGMENT = re.compile(r"[A-Za-z0-9_-]+")
INTEGRATION_FIELDS = {
    "komodo": {"api_key": "ONBOARDRR_KOMODO_API_KEY", "api_secret": "ONBOARDRR_KOMODO_API_SECRET"},
    "pangolin": {"api_token": "ONBOARDRR_PANGOLIN_API_TOKEN"},
    "wud": {"api_token": "ONBOARDRR_WUD_TOKEN"},
}

SPLIT_RECORD_PATHS = {
    "deployments/goku": ("hosts", "goku/smb"),
    "deployments/monitoring": ("services", "beszel/onboardrr"),
    **{f"integrations/{name}": ("services", f"{name}/onboardrr") for name in INTEGRATION_FIELDS},
}


def deployment_layout():
    layout = os.environ.get("ONBOARDRR_BAO_LAYOUT", "legacy")
    if layout not in {"legacy", "split"}:
        raise ValueError("Set ONBOARDRR_BAO_LAYOUT to legacy or split.")
    return layout


def deployment_kv_path(mount, path, *, metadata=False, layout="legacy"):
    parts = path.split("/")
    if not all(PATH_SEGMENT.fullmatch(part) for part in parts):
        raise ValueError("Invalid OpenBao secret path.")
    if layout == "split":
        kind = parts[1] if parts[0] == "deployments" and len(parts) > 2 else None
        if kind in {"compose-agent", "compose", "periphery"}:
            mount = "hosts"
            path = "/".join((parts[2], kind, *parts[3:]))
        elif kind == "storage" and len(parts) == 3:
            mount, path = "hosts", f"{parts[2]}/smb"
        elif kind == "wifi":
            mount, path = "hosts", "/".join(("common", *parts[1:]))
        elif path in SPLIT_RECORD_PATHS:
            mount, path = SPLIT_RECORD_PATHS[path]
        else:
            raise ValueError("No dedicated OpenBao path is defined for this deployment record.")
    operation = "metadata" if metadata else "data"
    return f"{mount}/{operation}/{path}"


def token_file(path, *, service="OpenBao"):
    with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK), "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077:
            raise ValueError(f"The {service} token must be a private file owned by the controller account.")
        content = stream.read(8193)
    if len(content) > 8192:
        raise ValueError(f"The {service} token file is too large.")
    token = content.decode("ascii").strip()
    if not token or not token.isascii() or any(not 33 <= ord(character) <= 126 for character in token):
        raise ValueError(f"The {service} token file does not contain a valid token.")
    return token


class OpenBao:
    def __init__(self, url, mount, token, *, layout="legacy"):
        if not PATH_SEGMENT.fullmatch(mount):
            raise ValueError("Use a single valid OpenBao mount name.")
        self.service = JsonService("OpenBao", url)
        self.mount = mount
        self.layout = layout
        self.headers = {"X-Vault-Token": token}

    def _kv_path(self, path, *, metadata=False):
        return "/v1/" + deployment_kv_path(self.mount, path, metadata=metadata, layout=self.layout)

    def _read_record(self, path):
        result = self.service.request(
            "GET", self._kv_path(path), headers=self.headers, operation="KV read"
        )
        try:
            record = result["data"]
            if not isinstance(record["data"], dict):
                raise TypeError
        except (KeyError, TypeError) as error:
            raise ValueError("OpenBao did not return a KV record.") from error
        return record

    def read(self, path, *fields):
        data = self._read_record(path)["data"]
        try:
            values = tuple(data[field] for field in fields)
        except KeyError as error:
            raise ValueError("OpenBao did not return the required KV fields.") from error
        if not all(isinstance(value, str) and value for value in values):
            raise ValueError("OpenBao returned empty or invalid KV fields.")
        return values

    def fields_with_version(self, path, *fields):
        record = self._read_record(path)
        metadata = record.get("metadata")
        version = metadata.get("version") if isinstance(metadata, dict) else None
        if type(version) is not int or version < 1:
            raise ValueError("OpenBao did not return a valid KV version.")
        return {field: record["data"][field] for field in fields if field in record["data"]}, version

    def create(self, path, fields):
        self.service.request(
            "POST", self._kv_path(path),
            headers={**self.headers, "Content-Type": "application/json"}, operation="KV create",
            data={"options": {"cas": 0}, "data": fields},
        )

    def secret_metadata(self, path):
        return self.service.request(
            "GET", self._kv_path(path, metadata=True),
            headers=self.headers, operation="KV metadata read",
        )

    def delete_secret(self, path):
        self.service.request(
            "DELETE", self._kv_path(path, metadata=True),
            headers=self.headers, operation="KV metadata delete",
        )

    def patch(self, path, fields, *, version):
        self.service.request(
            "PATCH", self._kv_path(path),
            headers={**self.headers, "Content-Type": "application/merge-patch+json"}, operation="KV patch",
            data={"options": {"cas": version}, "data": fields},
        )


def configured_openbao(*, mount=None):
    url = os.environ.get("ONBOARDRR_BAO_URL")
    if not url:
        return None
    token_path = os.environ.get("ONBOARDRR_BAO_TOKEN_FILE")
    if not token_path:
        raise ValueError("Set ONBOARDRR_BAO_TOKEN_FILE for OpenBao authentication.")
    token = token_file(Path(token_path).expanduser())
    if mount is not None:
        return OpenBao(url, mount, token)
    return deployment_openbao(url, token)


def deployment_openbao(url, token):
    mount = os.environ.get("ONBOARDRR_BAO_MOUNT", "secret")
    return OpenBao(url, mount, token, layout=deployment_layout())


def storage_smb_record(name):
    if name not in (None, "", "goku") and deployment_layout() == "split":
        return f"deployments/storage/{name}"
    return "deployments/goku"


def configured_newt_openbao():
    url = os.environ.get("ONBOARDRR_BAO_URL")
    token_path = os.environ.get("ONBOARDRR_KOMODO_BAO_TOKEN_FILE")
    if not url or not token_path:
        raise ValueError("Set ONBOARDRR_BAO_URL and ONBOARDRR_KOMODO_BAO_TOKEN_FILE.")
    mount = os.environ.get("ONBOARDRR_NEWT_BAO_MOUNT", "komodo")
    if mount not in {"komodo", "services"}:
        raise ValueError("Set ONBOARDRR_NEWT_BAO_MOUNT to komodo or services.")
    return OpenBao(url, mount, token_file(Path(token_path).expanduser()))


def stored_host_ipv4(name, *, nas_client=False):
    bao = configured_openbao(mount="hosts")
    if bao is None:
        return None
    keys = ("value", "nas_client_ipv4") if nas_client else ("value",)
    try:
        fields, _ = bao.fields_with_version(f"{name}/IP", *keys)
    except HttpStatusError as error:
        if error.status == 404:
            return None
        raise
    value = fields.get("nas_client_ipv4", fields.get("value")) if nas_client else fields.get("value")
    if not isinstance(value, str):
        raise ValueError(f"Set hosts/{name}/IP to the host's IPv4 address.")
    return str(IPv4Address(value))


def publish_host_ipv4(bao, name, ipv4, *, nas_client=None):
    if not HOST_LABEL.fullmatch(name):
        raise ValueError("The device name must be a DNS label.")
    address = str(IPv4Address(ipv4))
    desired = {"value": address}
    if nas_client is not None:
        desired["nas_client_ipv4"] = str(IPv4Address(nas_client))
    path = f"{name}/IP"
    for attempt in range(3):
        try:
            try:
                fields, version = bao.fields_with_version(path, *desired)
            except HttpStatusError as error:
                if error.status != 404:
                    raise
                bao.create(path, desired)
            else:
                if fields == desired:
                    return
                bao.patch(path, desired, version=version)
            if bao.read(path, *desired) == tuple(desired.values()):
                return
        except HttpStatusError as error:
            if error.status == 403:
                raise ValueError(f"Grant the controller read, create and patch access to hosts/data/{path}.") from None
            if error.status not in (400, 409) or attempt == 2:
                raise
    raise ValueError(f"OpenBao did not confirm hosts/{path}.")


def integration_credentials(name, *, optional=False):
    fields = INTEGRATION_FIELDS[name]
    configured = tuple(os.environ.get(variable, "") for variable in fields.values())
    if all(configured):
        return configured
    if any(configured):
        raise ValueError(f"Provide the complete {name} API credentials or use OpenBao.")
    bao = configured_openbao()
    if bao:
        try:
            return bao.read(f"integrations/{name}", *fields)
        except HttpStatusError as error:
            if not optional or error.status != 404:
                raise
    return configured
