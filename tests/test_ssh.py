import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import unittest
from contextlib import contextmanager
from io import StringIO
from pathlib import Path
from unittest.mock import patch

import paramiko

from onboardrr import ssh
from onboardrr.__main__ import main
from onboardrr.inventory import PROFILE, Inventory
from onboardrr.runs import RunStore


class TestServer(paramiko.ServerInterface):
    def __init__(self, public_key):
        self.public_key = public_key
        self.attempts = []

    def check_auth_password(self, username, password):
        self.attempts.append("password")
        if username == "operator" and password == "fixture-password":
            return paramiko.AUTH_SUCCESSFUL
        return paramiko.AUTH_FAILED

    def check_auth_publickey(self, username, key):
        self.attempts.append("key")
        if username == "operator" and key == self.public_key:
            return paramiko.AUTH_SUCCESSFUL
        return paramiko.AUTH_FAILED

    def check_channel_request(self, kind, channel_id):
        if kind == "session":
            return paramiko.OPEN_SUCCEEDED
        return paramiko.OPEN_FAILED_ADMINISTRATIVELY_PROHIBITED

    def check_channel_exec_request(self, channel, command):
        if command != b"fixture":
            return False

        def reply():
            with channel.makefile("rb") as stream:
                payload = stream.read()
            channel.sendall(payload.upper())
            channel.send_exit_status(0)
            channel.shutdown_write()

        threading.Thread(target=reply, daemon=True).start()
        return True


class RemoteKeyTest(unittest.TestCase):
    def test_preserves_existing_content_and_deduplicates_matching_keys(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            (home / ".ssh").mkdir(mode=0o700)
            target = home / ".ssh" / "authorized_keys"
            original = b'# first line\nfrom="host with spaces" ssh-ed25519 existing other\nssh-rsa old'
            target.write_bytes(original)
            target.chmod(0o600)
            environment = {**os.environ, "HOME": directory}
            for key, expected in [
                ("ssh-ed25519 existing", "reused"),
                ("ssh-ed25519 extra", "created"),
                ("ssh-ed25519 extra", "reused"),
            ]:
                result = subprocess.run(
                    [sys.executable, str(ssh.REMOTE / "install_key.py")],
                    input=key, capture_output=True, text=True, env=environment, check=True,
                )
                self.assertEqual(result.stdout.strip(), expected)
            self.assertEqual(target.read_bytes(), original + b"\nssh-ed25519 extra\n")

    def test_rejects_a_symlink_instead_of_writing_through_it(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            (home / ".ssh").mkdir(mode=0o700)
            target = home / "unrelated"
            target.write_bytes(b"untouched")
            (home / ".ssh" / "authorized_keys").symlink_to(target)
            result = subprocess.run(
                [sys.executable, str(ssh.REMOTE / "install_key.py")],
                input="ssh-ed25519 extra", capture_output=True, text=True,
                env={**os.environ, "HOME": directory},
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(target.read_bytes(), b"untouched")


class SSHTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.host_key = paramiko.RSAKey.generate(2048)
        cls.operator_key = paramiko.RSAKey.generate(2048)

    @contextmanager
    def server(self):
        server = TestServer(self.operator_key)
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            listener.listen()
            port = listener.getsockname()[1]
            stopped = threading.Event()

            def accept():
                with listener.accept()[0] as peer:
                    with paramiko.Transport(peer) as transport:
                        transport.add_server_key(self.host_key)
                        transport.start_server(server=server, event=threading.Event())
                        stopped.wait(5)

            thread = threading.Thread(target=accept, daemon=True)
            thread.start()
            try:
                yield port, server
            finally:
                stopped.set()
                thread.join(5)

    def known_hosts(self, directory, port, key):
        path = Path(directory) / "known_hosts"
        path.write_text(f"[127.0.0.1]:{port} {key.get_name()} {key.get_base64()}\n")
        return path

    def test_enforces_trusted_host_before_sending_credentials(self):
        with tempfile.TemporaryDirectory() as directory, self.server() as (port, server):
            device = {"host": "127.0.0.1", "ssh_port": port, "ssh_user": "operator"}
            path = self.known_hosts(directory, port, self.operator_key)
            with self.assertRaisesRegex(ValueError, "host key does not match"):
                with ssh.connect(device, path, password="fixture-password"):
                    self.fail("Must reject an unexpected host key.")
            self.assertEqual(server.attempts, [])

    def test_supports_initial_password_and_independent_key_only_connections(self):
        with tempfile.TemporaryDirectory() as directory:
            for method, credentials in [
                ("password", {"password": "fixture-password"}),
                ("key", {"key": self.operator_key}),
            ]:
                with self.subTest(method=method), self.server() as (port, server):
                    path = self.known_hosts(directory, port, self.host_key)
                    device = {"host": "127.0.0.1", "ssh_port": port, "ssh_user": "operator"}
                    with ssh.connect(device, path, **credentials) as client:
                        self.assertTrue(client.get_transport().is_authenticated())
                        self.assertEqual(ssh.command(client, "fixture", b"probe"), (0, b"PROBE"))
                    self.assertEqual(server.attempts, [method])

    def test_openssh_destination_fallback_uses_the_inventory_host_key(self):
        lookup = socket.getaddrinfo
        alias = "nas.local"

        def resolve(host, *args, **kwargs):
            if host == alias:
                raise socket.gaierror(socket.EAI_NONAME, "Fixture name does not resolve")
            return lookup(host, *args, **kwargs)

        with tempfile.TemporaryDirectory() as directory:
            for trusted in (True, False):
                with self.subTest(trusted=trusted), self.server() as (port, server):
                    key = self.host_key if trusted else self.operator_key
                    known_hosts = Path(directory) / "known_hosts"
                    known_hosts.write_text(f"[{alias}]:{port} {key.get_name()} {key.get_base64()}\n")
                    device = {"host": alias, "ssh_port": port, "ssh_user": "operator"}
                    config = subprocess.CompletedProcess([], 0, "hostname 127.0.0.1\nport 99\n", "")
                    with (patch.object(socket, "getaddrinfo", side_effect=resolve),
                          patch("onboardrr.host_resolution.subprocess.run", return_value=config) as command):
                        if trusted:
                            with ssh.connect(device, known_hosts, key=self.operator_key) as client:
                                self.assertTrue(client.get_transport().is_authenticated())
                        else:
                            with self.assertRaisesRegex(ValueError, "host key does not match"):
                                with ssh.connect(device, known_hosts, key=self.operator_key):
                                    self.fail("An alias cannot bypass host-key checking.")
                        command.assert_called_once_with(
                            ["ssh", "-G", "--", alias], capture_output=True, text=True, timeout=3, check=True,
                        )
                    self.assertEqual(server.attempts, ["key"] if trusted else [])

    def test_preflight_reports_supported_profile_and_service_blockers(self):
        device = {"name": "lab", "profile": PROFILE, "ssh_user": "operator"}
        facts = {
            "os": {"ID": "debian", "VERSION_CODENAME": "trixie"},
            "architecture": "aarch64",
            "home_free_bytes": 4 * 1024 ** 3,
            "root_free_bytes": 4 * 1024 ** 3,
            "uid": 1000,
            "services": {"goku": {"445": True, "2049": False}, "komodo": None, "pangolin": {"443": True}},
        }
        with (
            patch.object(ssh, "script", return_value=json.dumps(facts).encode()),
            patch.object(ssh, "command", side_effect=[(1, b""), (0, b"")]),
        ):
            result = ssh.inspect(None, device, dict.fromkeys(ssh.SERVICE_PORTS), "secret", self.operator_key)
        self.assertEqual(result["sudo"], "password")
        self.assertEqual(result["blockers"], [
            "The target could not reach all required goku ports.",
            "No komodo endpoint configured.",
        ])
        self.assertNotIn("secret", json.dumps(result))

    def test_zero_platform_uses_the_server_profile(self):
        device = {"name": "zero", "profile": "server", "ssh_user": "operator"}
        facts = {
            "os": {"ID": "linuxmint", "VERSION_CODENAME": "xia", "UBUNTU_CODENAME": "noble"},
            "architecture": "x86_64",
            "uid": 0,
            "services": {name: {str(port): True for port in ports} for name, ports in ssh.SERVICE_PORTS.items()},
        }
        with patch.object(ssh, "script", return_value=json.dumps(facts).encode()):
            server = ssh.inspect(None, device, dict.fromkeys(ssh.SERVICE_PORTS), None, self.operator_key)
            pi = ssh.inspect(None, {**device, "profile": PROFILE}, dict.fromkeys(ssh.SERVICE_PORTS), None, self.operator_key)
        self.assertEqual(server["blockers"], [])
        self.assertIn("The Raspberry Pi profile requires Trixie ARM64.", pi["blockers"])

    def test_failed_verification_can_resume_without_losing_key_ownership(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "inventory.sqlite3"
            device = Inventory(database).add("lab", "lab.local", "operator", 22, PROFILE)
            runs = RunStore(database)
            known_hosts = Path(directory) / "known_hosts"
            with (
                patch.object(ssh.paramiko, "Agent") as agent,
                patch.object(ssh, "script", side_effect=[b"created", b"reused"]),
                patch.object(ssh, "connect") as verify,
                patch.object(ssh, "command", return_value=(0, b"")),
            ):
                agent.return_value.get_keys.return_value = [self.operator_key]
                verify.side_effect = ValueError("Key verification failed.")
                with self.assertRaisesRegex(ValueError, "Key verification failed"):
                    ssh.enroll(None, device, known_hosts, self.operator_key, runs)
                failed = runs.show(runs.open("lab")["id"])
                self.assertEqual(failed["status"], "failed")
                self.assertEqual(failed["steps"][0]["last_failure"], "ssh-key-verify")
                verify.side_effect = None
                resumed = ssh.enroll(None, device, known_hosts, self.operator_key, runs)
                verify.assert_called_with(device, known_hosts, key=self.operator_key)
                self.assertEqual(resumed["next_step"], "komodo-registration")
                self.assertEqual(resumed["resources"][0]["ownership"], "created")

    def test_password_bootstrap_installs_the_key_before_service_preflight(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            database = path / "inventory.sqlite3"
            hosts = path / "known_hosts"
            public_key = path / "operator.pub"
            public_key.write_text(f"{self.operator_key.get_name()} {self.operator_key.get_base64()}\n")
            device = Inventory(database).add("lab", "lab.local", "operator", 22, PROFILE)
            arguments = [
                "onboardrr", "--database", str(database), "bootstrap-key", "lab",
                "--known-hosts", str(hosts), "--public-key", str(public_key), "--password-stdin",
            ]
            output = StringIO()
            with (
                patch("sys.argv", arguments),
                patch("sys.stdin", StringIO("fixture-password\n")),
                patch("sys.stdout", output),
                patch.object(ssh, "connect") as connect,
                patch.object(ssh.paramiko, "Agent") as agent,
                patch.object(ssh, "script", return_value=b"created") as install,
                patch.object(ssh, "command", return_value=(0, b"")),
                patch.object(ssh, "inspect") as inspect,
            ):
                agent.return_value.get_keys.return_value = [self.operator_key]
                self.assertEqual(main(), 0)
                inspect.assert_not_called()
                install.assert_called_once()
                self.assertEqual(connect.call_count, 2)
                self.assertEqual(connect.call_args_list[0].kwargs, {"password": "fixture-password"})
                connect.assert_called_with(device, hosts, key=self.operator_key)
            result = json.loads(output.getvalue())
            self.assertTrue(result["key_verified"])
            self.assertNotIn("fixture-password", output.getvalue())
            run = RunStore(database).show(result["run_id"])
            self.assertEqual(run["next_step"], "ssh")
            self.assertEqual(run["resources"][0]["ownership"], "created")

    def test_preflight_can_reuse_the_verified_key_without_an_ssh_password(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            database = path / "inventory.sqlite3"
            public_key = path / "operator.pub"
            public_key.write_text(f"{self.operator_key.get_name()} {self.operator_key.get_base64()}\n")
            device = Inventory(database).add("lab", "lab.local", "operator", 22, PROFILE)
            arguments = [
                "onboardrr", "--database", str(database), "preflight", "lab",
                "--public-key", str(public_key), "--use-key",
            ]
            with (
                patch("sys.argv", arguments),
                patch("sys.stdout", StringIO()),
                patch("onboardrr.__main__.getpass.getpass") as prompt,
                patch.object(ssh, "connect") as connect,
                patch.object(ssh.paramiko, "Agent") as agent,
                patch.object(ssh, "inspect", return_value={"blockers": []}) as inspect,
            ):
                agent.return_value.get_keys.return_value = [self.operator_key]
                self.assertEqual(main(), 0)
                prompt.assert_not_called()
                connect.assert_called_once_with(device, Path.home() / ".ssh/known_hosts", key=self.operator_key)
                self.assertIsNone(inspect.call_args.args[3])

    def test_ssh_only_validates_the_checkpoint_without_sudo_or_central_services(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            database = path / "inventory.sqlite3"
            public_key = path / "operator.pub"
            public_key.write_text(f"{self.operator_key.get_name()} {self.operator_key.get_base64()}\n")
            Inventory(database).add("lab", "lab.local", "operator", 22, PROFILE)
            arguments = [
                "--database", str(database), "preflight", "lab",
                "--public-key", str(public_key), "--use-key", "--ssh-only",
            ]
            runs = RunStore(database)
            run = runs.open("lab")
            output = StringIO()
            with (
                patch("sys.stdout", output),
                patch.object(ssh, "connect"),
                patch.object(ssh.paramiko, "Agent") as agent,
                patch.object(ssh, "command", return_value=(0, b"")) as command,
                patch.object(ssh, "inspect") as inspect,
                patch.object(ssh, "script") as script,
                patch("onboardrr.__main__.sudo_password") as sudo,
            ):
                agent.return_value.get_keys.return_value = [self.operator_key]
                self.assertEqual(main(arguments), 0)
                self.assertTrue(json.loads(output.getvalue())["key_verified"])
                self.assertEqual(runs.show(run["id"])["next_step"], "ssh")
                self.assertEqual(main([*arguments, "--apply-key"]), 0)
                inspect.assert_not_called()
                script.assert_not_called()
                sudo.assert_not_called()
                self.assertEqual([call.args[1] for call in command.call_args_list], ["true", "true"])
            updated = runs.show(run["id"])
            self.assertEqual(updated["next_step"], "komodo-registration")
            self.assertEqual(updated["resources"][0]["ownership"], "reused")
