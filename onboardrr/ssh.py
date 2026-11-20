"""Authenticate, inspect, and enroll an inventory device over SSH."""

import base64
import binascii
import hashlib
import json
import shlex
import socket
import time
from contextlib import ExitStack, closing, contextmanager
from pathlib import Path

import paramiko

from .host_resolution import openssh_destination
from .inventory import NAS_PROFILE, SERVER_PROFILE
from .runs import Failure, Ownership, Resource, ResourceKind
from .storage import device_lock


REMOTE = Path(__file__).with_name("remote")
TIMEOUT = 25
MAX_OUTPUT = 64 * 1024
SSH_STEP = "ssh"
SERVICE_PORTS = {"goku": (445, 2049), "komodo": (443,), "pangolin": (443,)}


def load_public_key(path):
    with path.open("rb") as stream:
        data = stream.read(32769)
    fields = data.split()
    if len(data) > 32768 or len(fields) < 2 or len(fields[1]) > 16384:
        raise ValueError("Expected an OpenSSH public key.")
    try:
        algorithm = fields[0].decode("ascii")
        data = base64.b64decode(fields[1], validate=True)
        parsed = paramiko.PKey.from_type_string(algorithm, data)
    except (ValueError, binascii.Error, paramiko.SSHException) as error:
        raise ValueError("Expected a supported OpenSSH public key.") from error
    if parsed.get_name() != algorithm or parsed.asbytes() != data:
        raise ValueError("Invalid OpenSSH public key.")
    return parsed


def fingerprint(key):
    digest = base64.b64encode(hashlib.sha256(key.asbytes()).digest()).decode().rstrip("=")
    return f"SHA256:{digest}"


class SSHAuthenticationError(ValueError):
    """The trusted SSH server rejected the selected authentication method."""


@contextmanager
def connect(device, known_hosts, *, password=None, key=None):
    host, port = device["host"], device["ssh_port"]
    options = {
        "port": port, "username": device["ssh_user"], "password": password, "pkey": key,
        "allow_agent": False, "look_for_keys": False, "timeout": 8, "banner_timeout": 8,
        "auth_timeout": 8, "channel_timeout": 8,
    }
    with ExitStack() as connections, paramiko.SSHClient() as client:
        client.load_host_keys(str(known_hosts))
        client.set_missing_host_key_policy(paramiko.RejectPolicy())
        try:
            try:
                client.connect(host, **options)
            except socket.gaierror:
                destination = openssh_destination(host)
                if not destination:
                    raise
                # Keep the inventory hostname for host-key validation.
                peer = connections.enter_context(socket.create_connection((destination, port), timeout=8))
                client.connect(host, sock=peer, **options)
        except paramiko.BadHostKeyException as error:
            raise ValueError("SSH host key does not match the trusted known_hosts entry.") from error
        except paramiko.AuthenticationException as error:
            raise SSHAuthenticationError("SSH authentication failed for the configured account.") from error
        except (OSError, paramiko.SSHException) as error:
            raise ValueError("SSH connection failed. Verify host reachability and the trusted known_hosts entry.") from error
        yield client


def command(client, command_line, payload=b"", *, timeout=TIMEOUT, max_output=MAX_OUTPUT):
    try:
        with client.get_transport().open_session(timeout=8) as channel:
            channel.settimeout(8)
            channel.exec_command(command_line)
            if payload:
                channel.sendall(payload)
            channel.shutdown_write()
            output = bytearray()
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                if channel.recv_ready():
                    output.extend(channel.recv(8192))
                    if len(output) > max_output:
                        raise ValueError("SSH command produced too much output.")
                if channel.recv_stderr_ready():
                    channel.recv_stderr(8192)
                if channel.exit_status_ready() and not channel.recv_ready():
                    return channel.recv_exit_status(), bytes(output)
                time.sleep(0.01)
    except (EOFError, OSError, paramiko.SSHException) as error:
        raise ValueError("SSH command could not complete.") from error
    raise ValueError("SSH command timed out.")


def script(client, name, payload, *, timeout=TIMEOUT, entware=False, max_output=MAX_OUTPUT):
    source = (REMOTE / name).read_text()
    interpreter = 'PATH="$HOME/.config/onboardrr/bin:/opt/bin:$PATH" python3' if entware else "python3"
    options = {"max_output": max_output} if max_output != MAX_OUTPUT else {}
    status, output = command(client, f"{interpreter} -c {shlex.quote(source)}", payload, timeout=timeout, **options)
    if status != 0:
        raise ValueError(f"Remote {name.removesuffix('.py')} failed.")
    return output


def inspect(client, device, hosts, password, public_key):
    endpoints = {name: (hosts[name], ports) for name, ports in SERVICE_PORTS.items()}
    facts = json.loads(script(client, "preflight.py", json.dumps(endpoints).encode()))
    blockers = []
    operating_system = facts["os"]
    identity = (operating_system.get("ID"), operating_system.get("VERSION_CODENAME"))
    if device["profile"] == SERVER_PROFILE:
        mint_noble = identity[0] == "linuxmint" and operating_system.get("UBUNTU_CODENAME") == "noble"
        supported = mint_noble or identity in {("debian", "trixie"), ("ubuntu", "noble")}
        if not supported or facts["architecture"] not in {"aarch64", "x86_64"}:
            blockers.append("The server profile requires Debian 13, Ubuntu 24.04, or Linux Mint 22.x on ARM64 or x86-64.")
    elif identity not in {("debian", "trixie"), ("raspbian", "trixie")} or facts["architecture"] != "aarch64":
        blockers.append("The Raspberry Pi profile requires Trixie ARM64.")
    sudo = sudo_access(client, facts["uid"], password)
    if sudo == "unavailable":
        blockers.append("The controller could not verify sudo access.")

    for name, ports in SERVICE_PORTS.items():
        results = facts["services"][name]
        if results is None:
            blockers.append(f"No {name} endpoint configured.")
        elif any(results.get(str(port)) is not True for port in ports):
            blockers.append(f"The target could not reach all required {name} ports.")
    if public_key is None:
        blockers.append("No operator public key supplied.")

    return {
        "device": device["name"],
        "profile": device["profile"],
        "ssh_user": device["ssh_user"],
        "facts": facts,
        "sudo": sudo,
        "operator_key": fingerprint(public_key) if public_key else None,
        "planned_actions": [
            "Install the operator key and verify an independent key-only SSH session.",
            "Register the server in Komodo and stage Periphery files on Goku.",
            "Prepare Linux, Docker, and mounts, then start Periphery.",
            "Configure and verify the baseline stacks and Pangolin connection.",
        ],
        "blockers": blockers,
    }


def sudo_access(client, uid, password):
    sudo = "root" if uid == 0 else "unavailable"
    if sudo != "root" and command(client, "sudo -n -- true")[0] == 0:
        sudo = "passwordless"
    elif sudo != "root" and password is not None and command(client, "sudo -S -k -p '' -- true", (password + "\n").encode())[0] == 0:
        sudo = "password"
    return sudo


def verify_key(device, known_hosts, agent_key):
    with connect(device, known_hosts, key=agent_key) as verification:
        if command(verification, "true")[0] != 0:
            raise ValueError("The key-only SSH session could not execute a verification command.")


@contextmanager
def agent_identity(public_key):
    with closing(paramiko.Agent()) as agent:
        agent_key = next((key for key in agent.get_keys() if key.asbytes() == public_key.asbytes()), None)
        if agent_key is None:
            raise ValueError("Load the matching operator identity into the local SSH agent first.")
        yield agent_key


def enroll(client, device, known_hosts, public_key, runs, *, complete=True, reuse_key=False):
    with device_lock(runs.database, device), agent_identity(public_key) as agent_key:
        run = runs.open(device["name"])
        if run["next_step"] != SSH_STEP:
            if not complete:
                verify_key(device, known_hosts, agent_key)
            return run
        run_id = run["id"]
        reference = fingerprint(public_key)
        failure = Failure.SSH_KEY_INSTALL
        try:
            key_line = f"{public_key.get_name()} {public_key.get_base64()}".encode()
            kwargs = {"entware": True} if device.get("profile") == NAS_PROFILE else {}
            if reuse_key:
                failure = Failure.SSH_KEY_VERIFY
                verify_key(device, known_hosts, agent_key)
                result = Ownership.REUSED
            else:
                result = script(client, "install_key.py", key_line, **kwargs).decode().strip()
            if result not in {Ownership.CREATED, Ownership.REUSED}:
                raise ValueError("SSH key installation returned an unexpected result.")
            existing = next(
                (
                    item for item in runs.show(run_id)["resources"]
                    if item["kind"] == ResourceKind.SSH_KEY and item["reference"] == reference
                ),
                None,
            )
            ownership = Ownership(existing["ownership"] if existing else result)
            runs.record_resource(run_id, SSH_STEP, Resource(ResourceKind.SSH_KEY, reference, ownership))
            failure = Failure.SSH_KEY_VERIFY
            if not reuse_key:
                verify_key(device, known_hosts, agent_key)
            if complete:
                runs.complete_step(run_id, SSH_STEP)
        except (OSError, ValueError):
            runs.mark_failed(run_id, SSH_STEP, failure)
            raise
        return runs.show(run_id)
