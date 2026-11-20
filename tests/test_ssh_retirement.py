import unittest
from contextlib import nullcontext
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from onboardrr.inventory import Inventory, PROFILE
from onboardrr.remote.install_key import install, remove
from onboardrr.runs import Ownership, Resource, ResourceKind, RunStore
from onboardrr.ssh_retirement import retire_key


class AuthorizedKeyRemovalTest(unittest.TestCase):
    def setUp(self):
        self.home = Path(self.enterContext(TemporaryDirectory()))
        self.key = "ssh-ed25519 fixture-managed-key"
        self.other = b'restrict ssh-ed25519 fixture-fallback-key administrator\n'
        self.path = self.home / ".ssh" / "authorized_keys"
        install(self.key, self.home)

    def test_removes_only_plain_owned_key_and_is_idempotent(self):
        with self.path.open("ab") as file:
            file.write(self.other)
        self.assertEqual(remove(self.key, self.home), "removed")
        self.assertEqual(self.path.read_bytes(), self.other)
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(remove(self.key, self.home), "absent")

    def test_preserves_modified_or_duplicated_authorization(self):
        for content in (
            f"restrict {self.key}\n".encode(),
            f"{self.key}\n{self.key} changed\n".encode(),
        ):
            self.path.write_bytes(content)
            with self.assertRaises(ValueError):
                remove(self.key, self.home)
            self.assertEqual(self.path.read_bytes(), content)

    @patch("onboardrr.ssh.script", return_value=b"created\n")
    @patch("onboardrr.ssh.verify_key", side_effect=ValueError("fallback failed"))
    @patch("onboardrr.ssh.command", side_effect=[(0, b""), (0, b"removed\n")])
    @patch("onboardrr.ssh.connect", return_value=nullcontext("fallback-channel"))
    @patch("onboardrr.ssh.agent_identity", return_value=nullcontext("fallback-agent"))
    @patch("onboardrr.ssh.fingerprint", return_value="SHA256:owned")
    @patch("onboardrr.ssh.load_public_key")
    def test_restores_key_if_independent_fallback_recheck_fails(
        self, load, fingerprint, agent, connect, command, verify, restore,
    ):
        from unittest.mock import Mock

        enrolled = Mock()
        enrolled.asbytes.return_value = b"enrolled"
        enrolled.get_name.return_value = "ssh-ed25519"
        enrolled.get_base64.return_value = "fixture-managed-key"
        fallback = Mock()
        fallback.asbytes.return_value = b"fallback"
        load.side_effect = [enrolled, fallback]
        database = self.home / "inventory.sqlite3"
        device = Inventory(database).add("lab", "lab.example", "operator", 22, PROFILE)
        runs = RunStore(database)
        run = runs.open("lab")
        runs.record_resource(run["id"], "ssh", Resource(ResourceKind.SSH_KEY, "SHA256:owned", Ownership.CREATED))
        with self.assertRaisesRegex(ValueError, "was restored"):
            retire_key(database, device, "known", "enrolled", "fallback", apply=True)
        connect.assert_called_once_with(device, "known", key="fallback-agent")
        self.assertEqual(command.call_args_list[0].args, ("fallback-channel", "true"))
        verify.assert_called_once_with(device, "known", "fallback-agent")
        restore.assert_called_once_with("fallback-channel", "install_key.py",
                                        self.key.encode(), entware=False)
