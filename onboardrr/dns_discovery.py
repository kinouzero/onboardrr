"""Find DHCPv4 DNS candidates among the controller's inventory hosts."""

import socket
from concurrent.futures import ThreadPoolExecutor
from contextlib import suppress
from ipaddress import IPv4Address, IPv4Network

from dns import exception, flags, message, query, rcode

from .host_resolution import resolve_addresses
from .inventory import Inventory
from .secrets import stored_host_ipv4


LAN_NETWORKS = tuple(IPv4Network(network) for network in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16"))


def answers_dns(address):
    try:
        for transport in (query.udp, query.tcp):
            request = message.make_query("example.org.", "A")
            reply = transport(request, address, timeout=1.5)
            if (reply.rcode() != rcode.NOERROR or not reply.flags & flags.RA
                    or reply.flags & flags.TC or not reply.resolve_chaining().answer):
                return False
    except (OSError, exception.DNSException):
        return False
    return True


def host_dns_address(device):
    preferred = None
    with suppress(OSError, ValueError):
        stored = stored_host_ipv4(device["name"])
        if stored:
            preferred = IPv4Address(stored)
    if preferred and any(preferred in network for network in LAN_NETWORKS) and answers_dns(str(preferred)):
        return str(preferred)
    for address in sorted(resolve_addresses(device["host"], socket.AF_INET)):
        if address != preferred and any(address in network for network in LAN_NETWORKS) and answers_dns(str(address)):
            return str(address)
    return None


def discover_dns(database):
    devices = Inventory(database).list()
    candidates = {}
    with ThreadPoolExecutor(max_workers=8) as executor:
        for device, address in zip(devices, executor.map(host_dns_address, devices)):
            if address:
                candidates.setdefault(address, device["name"])
    servers = [{"host": host, "address": address} for address, host in candidates.items()]
    return {"status": "detected", "servers": servers}
