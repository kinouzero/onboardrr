"""Relay one controller request to Pangolin on Zero's Docker network."""

import http.client
import json
import subprocess
import sys
from ipaddress import IPv4Address


request = json.load(sys.stdin)
networks = json.loads(subprocess.check_output([
    "docker", "inspect", "--format", "{{json .NetworkSettings.Networks}}", "pangolin",
], timeout=5))
addresses = [network["IPAddress"] for network in networks.values() if network.get("IPAddress")]
if len(addresses) != 1 or not IPv4Address(addresses[0]).is_private:
    sys.exit(1)

connection = http.client.HTTPConnection(addresses[0], 3003, timeout=10)
try:
    body = json.dumps(request["data"]) if request["data"] is not None else None
    connection.request(request["method"], request["path"], body=body, headers=request["headers"])
    response = connection.getresponse()
    content = response.read(4 * 1024 * 1024 + 1)
    if len(content) > 4 * 1024 * 1024:
        sys.exit(1)
    data = json.loads(content) if 200 <= response.status < 300 else None
    json.dump({"status": response.status, "response": data}, sys.stdout)
finally:
    connection.close()
