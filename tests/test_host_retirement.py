import json
import os
import subprocess
import sys
import unittest
from contextlib import nullcontext
from hashlib import sha256
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from onboardrr.host_retirement import retire_host
from onboardrr.remote.retire_host import (
    detach, fstab_plan, fstab_uses_smb_credentials, inspect_containers, network_status, retire_shell_aliases,
    retire_smb_file,
)


MOUNT = "/share/docker/komodo/data/lab"
SOURCE = "nas:/onboardrr-lab"
KEEP = b"# existing settings\nUUID=root / ext4 defaults 0 1\n"
ENTRY = f"{SOURCE} {MOUNT} nfs4 _netdev,nofail,vers=4 0 0\n".encode()
OUTBOUND = "/home/operator/.config/onboardrr/periphery/compose.yaml"
ALIASES = b"""# BEGIN ONBOARDRR MANAGED BLOCK
alias cdcompose='cd "$HOME/.docker/compose/lab"'
# END ONBOARDRR MANAGED BLOCK
"""


class HostRetirementTest(unittest.TestCase):
    def test_smb_retirement_preserves_existing_fstab_alias_and_changed_credentials(self):
        root = Path(self.enterContext(TemporaryDirectory())).resolve()
        credentials = root / "lab-goku.smb"
        alias = root / "alias.smb"
        fstab = root / "fstab"
        content = b"username=fixture\npassword=fixture\n"
        credentials.write_bytes(content)
        alias.symlink_to(credentials)
        fstab.write_bytes(f"//server/compose /mnt/compose cifs ro,cred={alias} 0 0\n".encode())
        provenance = json.dumps({"device_id": "fixture", "sha256": sha256(content).hexdigest()}).encode()
        self.assertTrue(fstab_uses_smb_credentials(fstab.read_bytes(), credentials))
        with (
            patch("onboardrr.remote.retire_host.private_smb_file",
                  side_effect=[(provenance, None), (content, None), (provenance, None), (b"edited", None)]),
            patch.object(Path, "lstat", return_value=type("Metadata", (), {"st_mode": 0o40700, "st_uid": 0})()),
            patch("onboardrr.remote.retire_host.fstab_uses_smb_credentials", return_value=True),
            patch("onboardrr.remote.retire_host.run") as run,
        ):
            result = retire_smb_file(credentials, "fixture", fstab, set(), apply=True)
            self.assertEqual(result, {"status": "in-use"})
            result = retire_smb_file(credentials, "fixture", fstab, set(), apply=True)
            self.assertEqual(result, {"status": "changed"})
            run.assert_not_called()
        self.assertEqual(credentials.read_bytes(), content)

    def test_shell_retirement_preserves_other_bytes_and_private_backup(self):
        root = Path(self.enterContext(TemporaryDirectory()))
        path = root / ".bashrc"
        before = b"alias personal='pwd'\r\n" + ALIASES + b"# trailing comment without newline"
        path.write_bytes(before)
        path.chmod(0o640)
        self.assertEqual(retire_shell_aliases(path, "lab", "raspberry-pi", apply=False), {"status": "eligible"})
        self.assertEqual(path.read_bytes(), before)
        result = retire_shell_aliases(path, "lab", "raspberry-pi", apply=True)
        self.assertEqual(result["status"], "removed")
        self.assertEqual(path.read_bytes(), b"alias personal='pwd'\r\n# trailing comment without newline")
        self.assertEqual(path.stat().st_mode & 0o777, 0o640)
        backup = Path(result["backup"])
        self.assertEqual(backup.read_bytes(), before)
        self.assertEqual(backup.stat().st_mode & 0o777, 0o600)
        self.assertEqual(retire_shell_aliases(path, "lab", "raspberry-pi", apply=True), {"status": "absent"})

    def test_shell_retirement_recognizes_both_dcu_alias_versions(self):
        root = Path(self.enterContext(TemporaryDirectory())).resolve()
        path = root / ".bashrc"
        for alias in (
            b"""alias dcu='bash "$HOME/scripts/docker-env-generate-and-up.sh" lab'""",
            b"alias dcu='/usr/local/bin/onboardrr-dcu'",
        ):
            with self.subTest(alias=alias):
                path.write_bytes(ALIASES.replace(b"# END", alias + b"\n# END"))
                self.assertEqual(retire_shell_aliases(path, "lab", "raspberry-pi", apply=False), {"status": "eligible"})

    def test_shell_retirement_protects_edited_ambiguous_or_linked_files(self):
        root = Path(self.enterContext(TemporaryDirectory()))
        path = root / ".bashrc"
        for content in (ALIASES.replace(b"compose/lab", b"compose/other"), ALIASES * 2,
                        ALIASES.replace(b"# END ONBOARDRR MANAGED BLOCK\n", b"")):
            with self.subTest(content=content):
                path.write_bytes(content)
                self.assertEqual(retire_shell_aliases(path, "lab", "raspberry-pi", apply=True), {"status": "review"})
                self.assertEqual(path.read_bytes(), content)
        hardlink = root / "other"
        os.link(path, hardlink)
        with self.assertRaisesRegex(ValueError, "regular file owned"):
            retire_shell_aliases(path, "lab", "raspberry-pi", apply=True)

    def test_fstab_plan_preserves_unrelated_bytes_and_rejects_ambiguous_entries(self):
        self.assertEqual(fstab_plan(KEEP + ENTRY, {MOUNT: (SOURCE, "nfs4")}), KEEP)
        for raw in (ENTRY * 2, ENTRY.replace(b"/onboardrr-lab", b"/another-device")):
            with self.assertRaisesRegex(ValueError, "conflicting or duplicate fstab"):
                fstab_plan(KEEP + raw, {MOUNT: (SOURCE, "nfs4")})
        escaped = b"//nas/compose /home/Lab\\040User/.docker/compose cifs defaults 0 0\n"
        self.assertEqual(fstab_plan(KEEP + escaped, {
            "/home/Lab User/.docker/compose": ("//nas/compose", "cifs"),
        }), KEEP)

    def test_apply_unmounts_then_backs_up_and_is_safe_to_repeat(self):
        root = Path(self.enterContext(TemporaryDirectory()))
        fstab = root / "fstab"
        fstab.write_bytes(KEEP + ENTRY)
        fstab.chmod(0o644)
        request = {"mounts": {MOUNT: (SOURCE, "nfs4")}, "stop_periphery": True, "apply": True}
        agent = {"id": "agent", "running": True, "auto_restart": True, "can_stop": True}
        with (
            patch("onboardrr.remote.retire_host.inspect_mount", side_effect=[True, True, False, False]),
            patch("onboardrr.remote.retire_host.inspect_containers", side_effect=[(agent, False), (agent, False), (None, False)]),
            patch("onboardrr.remote.retire_host.run") as run,
        ):
            preview = detach(fstab, {**request, "apply": False})
            run.assert_not_called()
            self.assertTrue(preview["fstab_change"])
            self.assertEqual(fstab.read_bytes(), KEEP + ENTRY)
            applied = detach(fstab, request)
            self.assertEqual(run.call_args_list[0].args, ("docker", "update", "--restart=no", "--", "agent"))
            self.assertEqual(run.call_args_list[1].args, ("docker", "stop", "--", "agent"))
            self.assertEqual(run.call_args_list[2].args, ("umount", "--", MOUNT))
            self.assertIsNone(detach(fstab, request)["fstab_backup"])
        backup = Path(applied["fstab_backup"])
        self.assertEqual(backup.read_bytes(), KEEP + ENTRY)
        self.assertEqual(os.stat(backup).st_mode & 0o777, 0o600)
        self.assertEqual(fstab.read_bytes(), KEEP)
        self.assertEqual(os.stat(fstab).st_mode & 0o777, 0o644)

    def test_outbound_preview_identifies_running_agent_and_requires_matching_deployment(self):
        container = {
            "Id": "agent", "Name": "/komodo-agent", "State": {"Running": True},
            "Config": {
                "Labels": {"com.docker.compose.project": "komodo", "com.docker.compose.service": "komodo-agent",
                           "com.docker.compose.project.config_files": OUTBOUND},
                "Env": ["PERIPHERY_CONNECT_AS=lab", "PERIPHERY_SERVER_ENABLED=false",
                        "PERIPHERY_PRIVATE_KEY=file:/config/keys/periphery.key"],
            },
            "HostConfig": {"PortBindings": {}, "RestartPolicy": {"Name": "unless-stopped"}},
            "Mounts": [{"Type": "bind", "Source": MOUNT, "Destination": MOUNT},
                       {"Type": "bind", "Source": str(Path(OUTBOUND).parent / "keys"),
                        "Destination": "/config/keys"}],
        }
        mounts = {MOUNT: (SOURCE, "nfs4")}
        with patch("onboardrr.remote.retire_host.run") as run:
            for owned, stop, expected_attached in ((True, False, True), (False, True, True), (True, True, False)):
                run.side_effect = [b"agent", json.dumps([container]).encode()]
                agent, attached = inspect_containers(mounts, None, stop, outbound_file=OUTBOUND,
                                                     device="lab", owns_outbound=owned)
                self.assertTrue(agent["running"])
                self.assertEqual(agent["can_stop"], owned)
                self.assertEqual(attached, expected_attached)
            container["Config"]["Env"][0] = "PERIPHERY_CONNECT_AS=another-device"
            run.side_effect = [b"agent", json.dumps([container]).encode()]
            self.assertEqual(inspect_containers(mounts, None, True, outbound_file=OUTBOUND,
                                                device="lab", owns_outbound=True), (None, True))

    def test_reused_outbound_agent_is_never_stopped(self):
        root = Path(self.enterContext(TemporaryDirectory()))
        fstab = root / "fstab"
        fstab.write_bytes(KEEP + ENTRY)
        agent = {"id": "agent", "running": True, "auto_restart": True, "can_stop": False}
        with (
            patch("onboardrr.remote.retire_host.inspect_mount", return_value=True),
            patch("onboardrr.remote.retire_host.inspect_containers", return_value=(agent, True)),
            patch("onboardrr.remote.retire_host.run") as run,
        ):
            report = detach(fstab, {"mounts": {MOUNT: (SOURCE, "nfs4")}, "stop_periphery": True, "apply": True})
        self.assertTrue(report["periphery_running"])
        self.assertIn("periphery-not-owned", report["blockers"])
        self.assertEqual(fstab.read_bytes(), KEEP + ENTRY)
        run.assert_not_called()

    def test_network_eligibility_requires_a_recorded_id_owner_and_no_other_endpoint(self):
        identifier = "a" * 64
        request = {"name": "lab", "ids": ["b" * 64, identifier], "device_id": "fixture"}
        details = {"Name": "lab", "Id": identifier, "Driver": "bridge", "Scope": "local",
                   "Labels": {"onboardrr.device-id": "fixture"}, "Containers": {"active": {}}}
        with patch("onboardrr.remote.retire_host.run") as run:
            for owner, agent, expected in (("other", "active", "identity-mismatch"), ("fixture", None, "in-use"),
                                           ("fixture", "active", "eligible")):
                details["Labels"]["onboardrr.device-id"] = owner
                run.side_effect = [identifier.encode(), json.dumps([details]).encode()]
                self.assertEqual(network_status(request, agent)[0], expected)

    def test_network_removal_rechecks_the_original_id_before_removing_it(self):
        root = Path(self.enterContext(TemporaryDirectory()))
        fstab = root / "fstab"
        fstab.write_bytes(KEEP)
        recorded, replacement = "a" * 64, "b" * 64
        request = {"mounts": {}, "stop_periphery": False, "apply": True,
                   "network": {"name": "lab", "ids": [recorded, replacement], "device_id": "fixture"}}
        with (
            patch("onboardrr.remote.retire_host.inspect_containers", return_value=(None, False)),
            patch("onboardrr.remote.retire_host.network_status",
                  side_effect=[("eligible", recorded), ("eligible", replacement)]) as inspect,
            patch("onboardrr.remote.retire_host.run") as run,
        ):
            self.assertIn("docker-network-identity-mismatch", detach(fstab, request)["blockers"])
            run.assert_not_called()
            inspect.side_effect = [("eligible", recorded), ("eligible", recorded), ("absent", None)]
            self.assertEqual(detach(fstab, request)["network"], "removed")
            run.assert_called_once_with("docker", "network", "rm", "--", recorded)
        self.assertEqual(fstab.read_bytes(), KEEP)

    def test_retirement_request_survives_sudo_that_does_not_consume_the_password(self):
        root = Path(self.enterContext(TemporaryDirectory()))
        sudo = root / "sudo"
        sudo.write_text('#!/bin/sh\nshift 4\nexec "$@"\n')
        sudo.chmod(0o700)
        (root / "python3").symlink_to(sys.executable)
        (root / "retire_host.py").write_text("import json, sys\nprint(json.dumps(json.loads(sys.argv[1])))\n")
        identifier = "a" * 64
        device = {"name": "lab", "id": "fixture", "profile": "raspberry-pi", "ssh_user": "operator"}
        plan = {"actions": [{"kind": "docker-network", "reference": f"lab/{identifier}", "action": "remove"}]}

        def remote(client, command, request, *, timeout):
            result = subprocess.run(["/bin/sh", "-c", command], input=request, capture_output=True, timeout=10,
                                    env={**os.environ, "PATH": f"{root}{os.pathsep}{os.environ['PATH']}"})
            return result.returncode, result.stdout

        with (
            patch("onboardrr.host_retirement.retirement_plan", return_value=plan),
            patch("onboardrr.host_retirement.ensure_stacks_detached"),
            patch("onboardrr.ssh.REMOTE", root),
            patch("onboardrr.ssh.load_public_key"),
            patch("onboardrr.ssh.agent_identity", return_value=nullcontext()),
            patch("onboardrr.ssh.connect", return_value=nullcontext()),
            patch("onboardrr.ssh.command", side_effect=remote),
        ):
            report = retire_host(root / "inventory.sqlite3", device, None, None, None, cifs_source=None,
                                 nfs_source=None, stop_periphery=False, apply=True, password="fixture-password",
                                 remove_network=True)
        self.assertEqual(report["network"]["ids"], [identifier])
        self.assertNotIn("fixture-password", json.dumps(report))

    def test_interrupted_unmount_leaves_fstab_available_for_retry(self):
        root = Path(self.enterContext(TemporaryDirectory()))
        fstab = root / "fstab"
        fstab.write_bytes(KEEP + ENTRY)
        request = {"mounts": {MOUNT: (SOURCE, "nfs4")}, "stop_periphery": False, "apply": True}
        with (
            patch("onboardrr.remote.retire_host.inspect_mount", return_value=True),
            patch("onboardrr.remote.retire_host.inspect_containers", return_value=(None, False)),
            patch("onboardrr.remote.retire_host.run", side_effect=ValueError("mount busy")),
        ):
            with self.assertRaisesRegex(ValueError, "mount busy"):
                detach(fstab, request)
        self.assertEqual(fstab.read_bytes(), KEEP + ENTRY)
