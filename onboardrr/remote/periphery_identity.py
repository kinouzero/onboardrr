"""Read the public identity of an existing Periphery container."""

import base64
import json
import re
import subprocess
import sys
from hmac import compare_digest
from ipaddress import ip_address


def identity(passkey=None, address=None, outbound=None, nas=False):
    if address:
        if nas:
            local = subprocess.check_output(["ip", "address", "show"], timeout=8,
                                            stderr=subprocess.DEVNULL, text=True)
            addresses = re.findall(r"(?m)^\s+inet6? ([0-9A-Fa-f:.]+)/\d+", local)
        else:
            local = json.loads(subprocess.check_output(["ip", "-j", "address", "show"], timeout=8,
                                                       stderr=subprocess.DEVNULL))
            addresses = [entry["local"] for interface in local for entry in interface.get("addr_info", [])]
        if not any(ip_address(entry) == ip_address(address) for entry in addresses):
            raise ValueError("Komodo's Periphery address does not match an address on the trusted SSH host.")
    data = subprocess.check_output(["docker", "inspect", "komodo-agent"], timeout=8, stderr=subprocess.DEVNULL)
    container, = json.loads(data)
    config = container["Config"]
    if not container["State"]["Running"] or not config["Image"].startswith("ghcr.io/moghtech/komodo-periphery:"):
        raise ValueError
    environment = dict(item.split("=", 1) for item in config.get("Env", []) if "=" in item)
    path = environment.get("PERIPHERY_PRIVATE_KEY", "").removeprefix("file:")
    if not re.fullmatch(r"/[A-Za-z0-9_./-]+", path) or ".." in path.split("/"):
        raise ValueError
    passkeys = environment.get("PERIPHERY_PASSKEYS", "").split(",")
    if outbound and (environment.get("PERIPHERY_CONNECT_AS") != outbound
                     or environment.get("PERIPHERY_SERVER_ENABLED") != "false"):
        raise ValueError("The existing QNAP Periphery container uses different outbound settings.")
    if not outbound and not (passkey and any(compare_digest(key.strip().encode(), passkey.encode()) for key in passkeys)):
        raise ValueError
    key = subprocess.check_output(
        ["docker", "exec", container["Id"], "openssl", "pkey", "-in", path, "-pubout", "-outform", "DER"],
        timeout=8, stderr=subprocess.DEVNULL,
    )
    return {"public_key": base64.b64encode(key).decode("ascii")}


if __name__ == "__main__":
    try:
        request = json.load(sys.stdin)
        print(json.dumps(identity(request.get("passkey"), request.get("address"), request.get("outbound"),
                                  nas=request.get("nas") is True)))
    except (OSError, KeyError, ValueError, subprocess.SubprocessError) as error:
        message = str(error) if type(error) is ValueError and error.args else (
            "Could not verify the running Periphery identity and passkey over SSH.")
        print(json.dumps({"error": message}))
