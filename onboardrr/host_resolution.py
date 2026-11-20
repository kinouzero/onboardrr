"""Resolve inventory endpoints on the Onboardrr controller."""

import socket
import subprocess
from ipaddress import ip_address

from .inventory import normalize_host


def openssh_destination(host):
    """Read the effective HostName from the controller's OpenSSH configuration."""
    try:
        host = normalize_host(host)
        result = subprocess.run(["ssh", "-G", "--", host], capture_output=True, text=True, timeout=3, check=True)
        destination = next(line.partition(" ")[2] for line in result.stdout.splitlines() if line.startswith("hostname "))
        destination = normalize_host(destination.strip())
    except (OSError, subprocess.SubprocessError, ValueError, StopIteration):
        return None
    return destination if destination != host else None


def dns_addresses(host, family=socket.AF_UNSPEC):
    try:
        return {ip_address(result[4][0])
                for result in socket.getaddrinfo(host, None, family, socket.SOCK_STREAM)}
    except OSError:
        return set()


def resolve_addresses(host, family=socket.AF_UNSPEC):
    addresses = dns_addresses(host, family)
    if addresses:
        return addresses
    destination = openssh_destination(host)
    return dns_addresses(destination, family) if destination else addresses


def ssh_destination(host):
    destination = openssh_destination(host)
    return destination if destination and not dns_addresses(host) else host
