"""Build and apply a persistent NetworkManager Wi-Fi profile."""

from base64 import b64encode
from hashlib import pbkdf2_hmac, sha256
import re
from uuid import NAMESPACE_URL, uuid5

from .linux import run_playbook


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


def configure_wifi(device, known_hosts, interface, *, credentials=None, become_password=None, band=None):
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
    return run_playbook(
        device, known_hosts, "wifi.yml", variables,
        check=credentials is None, become_password=become_password,
    )
