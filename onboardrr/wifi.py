"""Build and apply a persistent NetworkManager Wi-Fi profile."""

from base64 import b64encode
from contextlib import nullcontext
from hashlib import pbkdf2_hmac, sha256
from ipaddress import IPv4Address
import re
from uuid import NAMESPACE_URL, uuid5

from .linux import run_playbook, run_ssh_probe
from .secrets import publish_host_ipv4
from .storage import device_lock


WIFI_INTERFACE = re.compile(r"[a-zA-Z0-9_.-]{1,15}")
RAW_PSK = re.compile(r"[0-9a-fA-F]{64}")
PASSPHRASE = re.compile(r"[ -~]{8,63}")
ROUTE_METRIC = 32766
BANDS = {"2G": "bg", "5G": "a"}
WIFI_SECRETS = "deployments/wifi"


def network_profile(name, profile_id, interface, ssid, password, *, band=None):
    ssid_bytes = ssid.encode()
    if not 1 <= len(ssid_bytes) <= 32 or b"\0" in ssid_bytes:
        raise ValueError("Use a Wi-Fi SSID between 1 and 32 bytes.")
    if RAW_PSK.fullmatch(password):
        psk = password.lower()
    elif PASSPHRASE.fullmatch(password):
        psk = pbkdf2_hmac("sha1", password.encode("ascii"), ssid_bytes, 4096, 32).hex()
    else:
        raise ValueError("Provide an 8–63 character WPA2 passphrase or a 64-digit hexadecimal PSK.")

    ssid_list = "".join(f"{byte};" for byte in ssid_bytes)
    suffix = f"-{band.lower()}" if band else ""
    band_setting = f"band={BANDS[band]}\n" if band else ""
    return f"""[connection]
id=onboardrr-{name}-wifi{suffix}
uuid={profile_id}
type=wifi
interface-name={interface}
autoconnect=true

[wifi]
mode=infrastructure
ssid={ssid_list}
{band_setting}
[wifi-security]
key-mgmt=wpa-psk
psk={psk}

[ipv4]
method=auto
may-fail=false
route-metric={ROUTE_METRIC}

[ipv6]
method=auto
route-metric={ROUTE_METRIC}
""".encode()


def probe_network(device, known_hosts, address=None):
    return run_ssh_probe(device, known_hosts, "wifi_network.py", address=address)


def verified_ethernet(device, known_hosts, network=None):
    if network is None:
        network = probe_network(device, known_hosts)
    for interface, link in network["links"].items():
        if link["type"] != "ethernet":
            continue
        for candidate in link["addresses"]:
            address = str(IPv4Address(candidate))
            try:
                wired = probe_network(device, known_hosts, address)
            except ValueError:
                continue
            wired_link = wired["links"].get(interface, {})
            if (wired["route"] == interface and wired["local"] == address
                    and wired_link.get("type") == "ethernet" and address in wired_link["addresses"]):
                return interface, address
    raise ValueError("Connect Ethernet and verify trusted SSH over its IPv4 address.")


def wireless_address(network, interface):
    link = network["links"].get(interface, {})
    if link.get("type") == "wifi" and link["addresses"]:
        return str(IPv4Address(link["addresses"][0]))
    return None


def update_host_address(result, bao, device, address, redeploy=None):
    result["ipv4"] = address
    if bao is not None:
        try:
            nfs_client = result.get("storage_ip_sync", {}).get("nfs_client")
            options = {"nas_client": nfs_client} if nfs_client else {}
            publish_host_ipv4(bao, device["name"], address, **options)
        except ValueError as error:
            raise ValueError(f"Verified {device['name']} at {address}, but OpenBao sync failed: {error}") from error
        result["openbao_path"] = f"hosts/{device['name']}/IP"
        if redeploy is not None:
            try:
                result.update(redeploy(device, address))
            except ValueError as error:
                result["blockers"] = [f"OpenBao saved the IP. Stack redeployment requires attention: {error}"]
    return result


def configure_wifi(device, known_hosts, interface, *, credentials=None, become_password=None, band=None,
                   host_bao=None, database=None, redeploy=None):
    if not WIFI_INTERFACE.fullmatch(interface):
        raise ValueError("Use a valid Wi-Fi interface name.")
    if band is not None and band not in BANDS:
        raise ValueError("Choose the 2G or 5G Wi-Fi band.")
    prefix = f"onboardrr:{device['id']}:wifi"
    identifiers = {
        value: str(uuid5(NAMESPACE_URL, f"{prefix}:{value}" if value else prefix))
        for value in (None, *BANDS)
    }
    identifier = identifiers[band]
    variables = {
        "onboardrr_wifi_interface": interface,
        "onboardrr_wifi_uuid": identifier,
        "onboardrr_wifi_switchable_uuids": list(identifiers.values()) if band else [identifier],
        "onboardrr_wifi_band": band,
        "onboardrr_wifi_path": f"/etc/NetworkManager/system-connections/onboardrr-{identifier}.nmconnection",
        "onboardrr_wifi_metric": ROUTE_METRIC,
    }
    if credentials is not None:
        content = network_profile(device["name"], identifier, interface, *credentials, band=band)
        variables["onboardrr_wifi_profile_b64"] = b64encode(content).decode("ascii")
        variables["onboardrr_wifi_profile_checksum"] = sha256(content).hexdigest()
    with device_lock(database, device) if database is not None and credentials is not None else nullcontext():
        result = run_playbook(device, known_hosts, "wifi.yml", variables,
                              check=credentials is None, become_password=become_password)
        if credentials is None or host_bao is None:
            return result
        address = wireless_address(probe_network(device, known_hosts), interface)
        if address is None:
            raise ValueError("Could not verify the active Wi-Fi IPv4 address for OpenBao.")
        return update_host_address(result, host_bao, device, address, redeploy)


def disable_wifi(device, known_hosts, *, apply, become_password=None, host_bao=None, database=None, redeploy=None):
    with device_lock(database, device) if database is not None and apply else nullcontext():
        interface, address = verified_ethernet(device, known_hosts)
        if not apply:
            return {"device": device["name"], "mode": "check", "ethernet_interface": interface, "ipv4": address}
        variables = {"onboardrr_ethernet_interface": interface, "onboardrr_ethernet_address": address}
        result = run_playbook(device, known_hosts, "wifi-disable.yml", variables, check=False,
                              become_password=become_password, address=address)
        wired = probe_network(device, known_hosts, address)
        if wired["route"] != interface or wired["local"] != address or wired["radio"] != "disabled":
            raise ValueError("Could not verify the Ethernet SSH connection with Wi-Fi disabled.")
        return update_host_address(result, host_bao, device, address, redeploy)


def sync_host_network(device, known_hosts, interface, *, apply, host_bao=None, database=None, redeploy=None,
                      storage_sync=None):
    if not WIFI_INTERFACE.fullmatch(interface):
        raise ValueError("Use a valid Wi-Fi interface name.")
    if apply and host_bao is None:
        raise ValueError("Configure the controller's OpenBao connection to sync the host IP.")
    with device_lock(database, device) if database is not None and apply else nullcontext():
        network = probe_network(device, known_hosts)
        address = wireless_address(network, interface) if network["radio"] == "enabled" else None
        kind = "wifi"
        if address is None:
            interface, address = verified_ethernet(device, known_hosts, network)
            kind = "ethernet"
        result = {"device": device["name"], "mode": "apply" if apply else "check",
                  "interface": interface, "connection_type": kind, "ipv4": address}
        if storage_sync is not None:
            try:
                result.update(storage_sync(device, address, apply=apply))
            except ValueError as error:
                result["blockers"] = [f"Storage IP sync requires attention: {error}"]
                return result
        return update_host_address(result, host_bao, device, address, redeploy) if apply else result
