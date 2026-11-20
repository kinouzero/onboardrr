import os
import subprocess
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from onboardrr.goku_export import ensure_goku_export, retire_goku_export
from onboardrr.inventory import Inventory, PROFILE
from onboardrr.primary_storage import StorageEndpoint
from onboardrr.runs import ResourceKind


class GokuExportCredentialsTest(unittest.TestCase):
    def test_retirement_requires_detachment_and_uses_the_selected_storage_identity(self):
        directory = self.enterContext(TemporaryDirectory())
        database = Path(directory) / "inventory.sqlite3"
        device = Inventory(database).add("lab", "192.168.1.55", "operator", 22, PROFILE)
        endpoint = StorageEndpoint("keeper.local", "192.168.1.20", "keeper", "admin", 2222)
        actions = [{"kind": kind, "reference": "/share/docker/komodo/data/lab", "action": "remove"}
                   for kind in (ResourceKind.GOKU_DIRECTORY, ResourceKind.MOUNT)]
        with (patch("onboardrr.goku_export.retirement_plan", return_value={"actions": actions}),
              patch("onboardrr.goku_export.check_host_mounts", side_effect=[False, True]),
              patch("onboardrr.goku_export.subprocess.run", return_value=subprocess.CompletedProcess([], 0, "removed\n")) as send):
            def retire():
                return retire_goku_export(database, device, endpoint, None, known_hosts=None,
                                          public_key=Path("/fixture.pub"), apply=True, password="fixture-sudo")
            with self.assertRaisesRegex(ValueError, "Detach the device's NFS mount"):
                retire()
            send.assert_not_called()
            self.assertEqual(retire()["status"], "removed")
        command = send.call_args.args[0]
        self.assertIn("keeper.local", command)
        self.assertIn("2222", command)
        self.assertNotIn("fixture-sudo", " ".join(command))
        self.assertEqual(send.call_args.kwargs["input"], "fixture-sudo\n")

    def test_ssh_override_cannot_request_the_configured_nas_sudo_password(self):
        with (
            patch.dict(os.environ, {"ONBOARDRR_GOKU_HOST": "goku.local"}),
            patch("onboardrr.goku_export.subprocess.run", return_value=subprocess.CompletedProcess([], 77)),
            patch("onboardrr.goku_export.configured_openbao") as credentials,
        ):
            with self.assertRaisesRegex(ValueError, "Set goku_host"):
                ensure_goku_export("other.local", "lab", "192.168.1.55")
            credentials.assert_not_called()


class QtsExportTest(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(TemporaryDirectory())).resolve()
        self.physical = self.root / "data/lab"
        self.physical.mkdir(parents=True)
        self.access = self.root / "access"
        self.access.write_text("TRUE\n")
        self.exports = self.root / "exports"
        self.unrelated = "/shared 192.168.1.20(rw)\n"
        self.dedicated = (
            f'"{self.physical}" 192.168.1.55(rw)\n'
            '"/share/NFSv=4/onboardrr-lab" 192.168.1.55(rw)\n'
        )
        self.exports.write_text(self.unrelated + self.dedicated)
        self.kernel = self.root / "kernel"
        self.kernel.write_text(self.unrelated + self.dedicated)
        (self.root / "both").write_text(self.unrelated + self.dedicated)
        (self.root / "shared").write_text(self.unrelated)
        (self.root / "shared-kernel").write_text(self.unrelated)
        fixtures = {
            "getcfg": '''
case "$1" in
onboardrr-lab) printf '%s\n' "$QTS_FIXTURE/data/lab" ;;
Access) cat "$QTS_FIXTURE/access" ;;
AllowIP) printf '192.168.1.55\n' ;;
esac
''',
            "setcfg": 'printf "%s\n" "$3" > "$QTS_FIXTURE/access"\n',
            "exportfs": 'cat "$QTS_FIXTURE/kernel"\n',
            "nfs": '''
if [ "$(cat "$QTS_FIXTURE/access")" = TRUE ]; then
    cat "$QTS_FIXTURE/both" > "$QTS_FIXTURE/exports"
    cat "$QTS_FIXTURE/both" > "$QTS_FIXTURE/kernel"
else
    cat "$QTS_FIXTURE/shared" > "$QTS_FIXTURE/exports"
    cat "$QTS_FIXTURE/shared-kernel" > "$QTS_FIXTURE/kernel"
fi
''',
        }
        for name, contents in fixtures.items():
            launcher = self.root / name
            launcher.write_text("#!/bin/sh\n" + contents)
            launcher.chmod(0o700)
        self.script = Path("onboardrr/remote/goku_export.sh").read_text()
        replacements = {
            "/sbin/getcfg": self.root / "getcfg", "/sbin/setcfg": self.root / "setcfg",
            "/usr/sbin/exportfs": self.root / "exportfs", "/etc/init.d/nfs": self.root / "nfs",
            "/etc/exports": self.exports, "/share/docker/komodo/data": self.root / "data",
            "$(id -u)": "0",
        }
        for original, replacement in replacements.items():
            self.script = self.script.replace(original, str(replacement))

    def operation(self, mode):
        return subprocess.run(
            ["sh", "-c", self.script, "--", "lab", "192.168.1.55", mode],
            env={**os.environ, "QTS_FIXTURE": str(self.root)}, capture_output=True, text=True,
        )

    def test_disable_both_qts_aliases_and_preserve_shared_exports(self):
        before = self.operation("preview")
        self.assertEqual((before.returncode, before.stdout), (0, "eligible\n"))
        self.assertEqual(self.exports.read_text(), self.unrelated + self.dedicated)
        applied = self.operation("apply")
        self.assertEqual((applied.returncode, applied.stdout), (0, "removed\n"), applied.stderr)
        self.assertEqual(self.exports.read_text(), self.unrelated)
        self.assertEqual(self.kernel.read_text(), self.unrelated)
        self.assertTrue(self.physical.exists())
        retry = self.operation("apply")
        self.assertEqual((retry.returncode, retry.stdout), (0, "absent\n"))

    def test_rolls_back_if_shared_exports_change(self):
        (self.root / "shared").write_text("/shared 192.168.1.20(ro)\n")
        applied = self.operation("apply")
        self.assertNotEqual(applied.returncode, 0)
        self.assertIn("removal could not be verified", applied.stderr)
        self.assertEqual(self.access.read_text(), "TRUE\n")
        self.assertEqual(self.exports.read_text(), self.unrelated + self.dedicated)

    def test_rolls_back_when_only_the_active_exports_change(self):
        (self.root / "shared-kernel").write_text("/shared 192.168.1.20(ro)\n")
        applied = self.operation("apply")
        self.assertNotEqual(applied.returncode, 0)
        self.assertIn("removal could not be verified", applied.stderr)
        self.assertEqual(self.access.read_text(), "TRUE\n")
        self.assertEqual(self.exports.read_text(), self.unrelated + self.dedicated)
        self.assertEqual(self.kernel.read_text(), self.unrelated + self.dedicated)


class QtsCreateExportTest(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(TemporaryDirectory())).resolve()
        (self.root / "data").mkdir()
        self.exports = self.root / "exports"
        self.shared = "/shared 192.168.1.20(rw)\n"
        self.exports.write_text(self.shared)
        (self.root / "shared").write_text(self.shared)
        (self.root / "smb.conf").touch()
        (self.root / "nfssetting").touch()
        self.alias = self.root / "nfs4/onboardrr-lab"
        self.script = Path("onboardrr/remote/goku_export_create.sh").read_text()
        fixtures = {
            "/sbin/getcfg": '''
if [ "$1" = onboardrr-lab ]; then
    key="$1-$2"
else
    key="$1-${2##*/}"
fi
if [ -f "$QTS_FIXTURE/$key" ]; then cat "$QTS_FIXTURE/$key"; else exit 250; fi
''',
            "/sbin/setcfg": '''
case "$1" in
    onboardrr-lab) exit 0 ;;
esac
printf '%s\n' "$3" > "$QTS_FIXTURE/$1-${2##*/}"
''',
            "/sbin/addshare": 'printf "%s\n" "$2" > "$QTS_FIXTURE/$1-path"',
            "/usr/sbin/exportfs": 'cat "$QTS_FIXTURE/exports"',
            "/etc/init.d/nfs": '''
cat "$QTS_FIXTURE/shared" > "$QTS_FIXTURE/exports"
if [ "$(cat "$QTS_FIXTURE/Access-lab")" = TRUE ]; then
    mkdir -p "$QTS_FIXTURE/nfs4/onboardrr-lab"
    client=$(cat "$QTS_FIXTURE/AllowIP-lab")
    for target in data/lab nfs4/onboardrr-lab; do
        printf '"%s/%s" %s(rw,all_squash,anonuid=1000,anongid=100)\n' \
            "$QTS_FIXTURE" "$target" "$client" >> "$QTS_FIXTURE/exports"
    done
    if [ -f "$QTS_FIXTURE/change-shared" ]; then printf '/unexpected 192.168.1.20(rw)\n' >> "$QTS_FIXTURE/exports"; fi
else
    rmdir "$QTS_FIXTURE/nfs4/onboardrr-lab"
fi
''',
            "stat -c": '''
case "$1" in
    %u) printf '1000\n' ;;
    %g) printf '100\n' ;;
    %d:%i) printf '1:2\n' ;;
esac
''',
        }
        for index, (command, contents) in enumerate(fixtures.items()):
            launcher = self.root / f"helper-{index}"
            launcher.write_text("#!/bin/sh\n" + contents + "\n")
            launcher.chmod(0o700)
            self.script = self.script.replace(command, str(launcher))
        for original, replacement in {
            "/share/docker/komodo/data": self.root / "data", "/share/NFSv=4": self.root / "nfs4",
            "/etc/config/smb.conf": self.root / "smb.conf", "/etc/config/nfssetting": self.root / "nfssetting",
            "/etc/exports": self.exports, "$(id -u)": "0", 'chown "$uid:$gid" "$physical"': ":",
        }.items():
            self.script = self.script.replace(original, str(replacement))

    def operation(self, client="192.168.1.55"):
        return subprocess.run(
            ["sh", "-c", self.script, "--", "lab", client, "1000", "100"],
            env={**os.environ, "QTS_FIXTURE": str(self.root)}, capture_output=True, text=True,
        )

    def test_creates_the_data_directory_and_dedicated_export_then_reuses_it(self):
        created = self.operation()
        self.assertEqual((created.returncode, created.stdout), (0, "created-directory\n"), created.stderr)
        self.assertTrue((self.root / "data/lab").is_dir())
        self.assertTrue(self.alias.is_dir())
        before = self.exports.read_text()
        self.assertTrue(before.startswith(self.shared))
        self.assertEqual(before.count("192.168.1.55(rw,all_squash"), 2)
        retry = self.operation()
        self.assertEqual((retry.returncode, retry.stdout), (0, "ready\n"), retry.stderr)
        self.assertEqual(self.operation("192.168.1.56").returncode, 13)
        self.assertEqual(self.exports.read_text(), before)

    def test_disables_the_new_export_if_an_unrelated_export_changed(self):
        (self.root / "change-shared").touch()
        attempted = self.operation()
        self.assertNotEqual(attempted.returncode, 0)
        self.assertEqual((self.root / "Access-lab").read_text(), "FALSE\n")
        self.assertEqual(self.exports.read_text(), self.shared)
        self.assertTrue((self.root / "data/lab").is_dir())
