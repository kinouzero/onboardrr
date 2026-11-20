"""Register and retire an enrolled device's SSH host in Termix."""

import os
from ipaddress import ip_address

from .host_resolution import resolve_addresses
from .http_json import HttpStatusError, JsonService
from .inventory import NAS_PROFILE, PROFILE, SERVER_PROFILE, normalize_host
from .secrets import configured_openbao, stored_host_ipv4
from .storage import device_lock


HOSTS = "/host/db/host"
PROFILE_TAGS = {PROFILE: "rpi", SERVER_PROFILE: "server", NAS_PROFILE: "nas"}
MANAGED_TAGS = {*PROFILE_TAGS.values(), "onboardrr"}
HOST_PLUGIN_SETTINGS = {
    "docker": {"enableDocker": True},
    "file-manager": {"defaultPath": "/", "scpLegacy": False},
    "host-metrics": {"enabledWidgets": [
        "cpu", "memory", "disk", "network", "uptime", "system", "login_stats", "ports", "firewall", "processes",
    ]},
    "session-recording": {"enableSessionRecording": True},
    "ssh-terminal": {"moshCommand": "mosh-server new -s -l LANG=en_US.UTF-8"},
    "tunnels": {"enableTunnel": True},
}
PRESERVED_HOST_SETTINGS = (
    "pin", "jumpHosts", "statusCheckInterval", "forceKeyboardInteractive", "shareSshAuth",
    "connectionOrigin", "localOnly", "portKnockSequence",
)


def configured_termix():
    origin = os.environ.get("ONBOARDRR_TERMIX_URL")
    if not origin:
        raise ValueError("Set termix_url in the Onboardrr controller settings.")
    bao = configured_openbao(mount="services")
    if bao is None:
        raise ValueError("Configure OpenBao for the Termix integration.")
    try:
        fields, _ = bao.fields_with_version("termix/onboardrr", "api_token", "credential_id")
    except HttpStatusError as error:
        if error.status == 404:
            raise ValueError("Configure services/termix/onboardrr in OpenBao.") from None
        raise
    token, credential = fields.get("api_token"), fields.get("credential_id")
    if isinstance(credential, str) and credential.isascii() and credential.isdecimal() and len(credential) <= 16:
        credential = int(credential)
    if (not isinstance(token, str) or not token or any(not 33 <= ord(char) <= 126 for char in token)
            or type(credential) is not int or not 0 < credential <= 2**53 - 1):
        raise ValueError("Provide a Termix API token and the numeric ID of an existing SSH credential in OpenBao.")
    return Termix(origin, token, credential)


def ssh_address(device, override):
    host = normalize_host(override or device["host"])
    try:
        return str(ip_address(host))
    except ValueError:
        pass
    addresses = resolve_addresses(host)
    ipv4 = {address for address in addresses if address.version == 4}
    if not ipv4 and override is None:
        saved = stored_host_ipv4(device["name"])
        if saved is not None:
            return saved
    preferred = ipv4 or addresses
    if len(preferred) != 1:
        raise ValueError("The SSH name does not resolve to a unique IP address. Provide --address with the IP.")
    return str(next(iter(preferred)))


class Termix:
    def __init__(self, origin, token, credential):
        self.service = JsonService("Termix", origin)
        self.headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
        self.credential = credential

    def request(self, method, path=HOSTS, data=None):
        return self.service.request(method, path, headers=self.headers, operation="manage SSH host", data=data)

    def hosts(self):
        hosts = self.request("GET")
        if not isinstance(hosts, list) or not all(isinstance(host, dict) for host in hosts):
            raise ValueError("Termix did not return an SSH host list.")
        return hosts

    def credential_username(self):
        try:
            credential = self.request("GET", f"/credentials/{self.credential}")
        except HttpStatusError as error:
            if error.status == 403:
                return None
            raise
        if not isinstance(credential, dict):
            raise ValueError("Termix did not return credential metadata.")
        return credential.get("username")


def host_tags(host):
    tags = host.get("tags") or []
    if isinstance(tags, str):
        tags = [tag.strip() for tag in tags.split(",") if tag.strip()]
    if not isinstance(tags, list) or not all(isinstance(tag, str) for tag in tags):
        raise ValueError("Termix returned invalid host tags.")
    return tags


def host_settings_match(host, payload):
    return (
        not host.get("folder") and host.get("parentHostId") is None
        and {tag.casefold() for tag in host_tags(host)} & MANAGED_TAGS == set(payload["tags"])
        and all(host.get(key) == payload[key] for key in (
            "name", "connectionType", "enableSsh", "authType", "overrideCredentialUsername", "statusCheckEnabled",
        ))
    )


def plugin_changes(host):
    installed = host.get("pluginSettings") or {}
    return {
        plugin: changes
        for plugin, desired in HOST_PLUGIN_SETTINGS.items()
        if (changes := {key: value for key, value in desired.items()
                        if (installed.get(plugin) or {}).get(key) != value})
    }


def register(database, device, termix, *, apply, remove=False, address=None):
    name = device["name"]
    operator, port = device["ssh_user"], device["ssh_port"]
    display_name = f"{operator}@{name}"
    marker = f"Onboardrr device {device['id']}"
    report = {"device": name}
    with device_lock(database, device):
        hosts = termix.hosts()
        owned = [host for host in hosts if host.get("notes") == marker and host.get("isShared") is not True]
        if len(owned) > 1:
            raise ValueError("Multiple Termix hosts match this Onboardrr registration.")
        existing = next(iter(owned), None)
        if existing and (type(existing.get("id")) is not int or existing["id"] <= 0
                         or existing.get("name") not in (name, display_name)):
            raise ValueError("The managed Termix host has changed. Check it in Termix.")
        if remove:
            if not existing:
                return {**report, "status": "absent"}
            if not apply:
                return {**report, "status": "preview", "action": "remove"}
            try:
                termix.request("DELETE", f"{HOSTS}/{existing['id']}")
            except HttpStatusError as error:
                if error.status != 404:
                    raise
            if any(host.get("id") == existing["id"] for host in termix.hosts()):
                raise ValueError("Termix still lists the deleted host.")
            return {**report, "status": "removed"}

        host = ssh_address(device, address)
        tags = [PROFILE_TAGS[device["profile"]]]

        def same_endpoint(entry):
            return (entry.get("ip"), entry.get("sshPort") or entry.get("port"), entry.get("username")) == (
                host, port, operator)

        report.update(address=host, port=port, username=operator)
        if existing:
            if not same_endpoint(existing) or existing.get("credentialId") != termix.credential:
                raise ValueError("The managed Termix endpoint or credential has changed. Check it in Termix.")
        else:
            matches = [entry for entry in hosts if same_endpoint(entry)
                       and entry.get("connectionType", "ssh") == "ssh"]
            if len(matches) > 1:
                raise ValueError("Multiple Termix hosts match this SSH endpoint.")
            if matches:
                return {**report, "status": "present", "managed": False}
        names = {name.casefold(), display_name.casefold()}
        if any(entry is not existing and str(entry.get("name", "")).casefold() in names for entry in hosts):
            raise ValueError("This name already belongs to a different Termix host.")

        payload = {
            "name": display_name, "ip": host, "port": port, "sshPort": port, "username": operator,
            "connectionType": "ssh", "enableSsh": True, "authType": "credential", "statusCheckEnabled": True,
            "credentialId": termix.credential, "overrideCredentialUsername": termix.credential_username() != operator,
            "folder": "", "parentHostId": None, "notes": marker, "tags": tags,
        }
        update_host = existing is not None and not host_settings_match(existing, payload)
        updates = plugin_changes(existing or {})
        if existing and not update_host and not updates:
            return {**report, "status": "present", "managed": True}
        if not apply:
            return {**report, "status": "preview", "action": "update" if existing else "create", "tags": tags}

        def read_saved():
            saved = [entry for entry in termix.hosts() if entry.get("notes") == marker
                     and entry.get("isShared") is not True]
            if len(saved) != 1:
                raise ValueError("Could not verify the Termix host registration.")
            entry = saved[0]
            if (type(entry.get("id")) is not int or entry["id"] <= 0 or not same_endpoint(entry)
                    or entry.get("credentialId") != termix.credential or not host_settings_match(entry, payload)
                    or (existing and entry["id"] != existing["id"])):
                raise ValueError("Could not verify the Termix host registration.")
            return entry

        if existing is None:
            termix.request("POST", data=payload)
        elif update_host:
            if any(existing.get(key) for key in ("useSocks5", "socks5Host", "socks5ProxyChain")):
                raise ValueError("The managed Termix host uses a SOCKS proxy. Update its placement in Termix.")
            settings = {key: existing[key] for key in PRESERVED_HOST_SETTINGS if key in existing}
            custom_tags = [tag for tag in host_tags(existing) if tag.casefold() not in MANAGED_TAGS]
            termix.request("PUT", f"{HOSTS}/{existing['id']}", data={**settings, **payload, "tags": tags + custom_tags})

        saved = read_saved() if existing is None or update_host else existing
        for plugin, changes in plugin_changes(saved).items():
            termix.request("PUT", f"/plugins/{plugin}/settings/host/{saved['id']}", data=changes)
        if plugin_changes(read_saved()):
            raise ValueError("Could not verify the Termix host plugin settings.")
        return {**report, "status": "updated" if existing else "created", "managed": True, "tags": tags}
