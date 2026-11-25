"""Report active IPv4 addresses and the route used by this SSH session."""

import json
import os
import subprocess


def command(*args):
    return subprocess.check_output(args, text=True, timeout=5, env={**os.environ, "LC_ALL": "C"}).strip()


client, _, local, _ = os.environ["SSH_CONNECTION"].split()
route = json.loads(command("ip", "-j", "route", "get", client, "from", local))[0]["dev"]
active = {}
for row in command("nmcli", "-t", "-f", "DEVICE,TYPE,STATE", "device", "status").splitlines():
    name, kind, state = row.split(":", 2)
    if kind in {"ethernet", "wifi"} and state in {"connected", "connected (externally)"}:
        active[name] = kind
links = {
    item["ifname"]: {"type": active[item["ifname"]], "addresses": [addr["local"] for addr in item["addr_info"]]}
    for item in json.loads(command("ip", "-j", "-4", "address", "show", "scope", "global"))
    if item["ifname"] in active
}
print(json.dumps({"route": route, "local": local, "links": links, "radio": command("nmcli", "radio", "wifi")}))
