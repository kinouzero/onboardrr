import io
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import Mock, patch

from onboardrr.__main__ import main
from onboardrr.http_json import HttpStatusError
from onboardrr.inventory import Inventory, NAS_PROFILE, PROFILE
from onboardrr.primary_storage import select_primary_storage, ssh_destination, storage_endpoint
from onboardrr.secrets import deployment_kv_path, storage_smb_record
from onboardrr.tui import command_args


class PrimaryStorageTest(unittest.TestCase):
    def setUp(self):
        directory = self.enterContext(tempfile.TemporaryDirectory())
        self.inventory = Inventory(Path(directory) / "inventory.sqlite3")
        self.inventory.add("goku", "192.168.1.20", "operator", 22, NAS_PROFILE)
        self.inventory.add("luffy", "luffy.local", "storage-admin", 2222, NAS_PROFILE)
        self.inventory.add("lab", "lab.local", "operator", 22, PROFILE)
        self.bao = self.enterContext(patch("onboardrr.primary_storage.configured_openbao", return_value=Mock())).return_value

    def select(self, name, ip=None, *, apply=True):
        return select_primary_storage(self.inventory, name, ip, apply=apply)

    def test_switch_routes_to_inventory_ssh_and_publishes_the_shared_ip(self):
        self.bao.fields_with_version.side_effect = [({"value": "192.168.1.20"}, 2), ({"value": "192.168.1.20"}, 2)]
        self.bao.read.return_value = ("192.168.1.21",)
        self.assertEqual(self.select("goku")["status"], "applied")
        self.assertEqual(self.select("luffy", "192.168.1.21", apply=False)["previous"], "goku")
        self.bao.patch.assert_not_called()
        self.assertEqual(self.select("luffy", "192.168.1.21")["status"], "applied")
        self.bao.patch.assert_called_once_with("storage/IP", {"value": "192.168.1.21"}, version=2)
        target = storage_endpoint(self.inventory.database)
        self.assertEqual(ssh_destination(target), ["-p", "2222", "-l", "storage-admin", "luffy.local"])
        self.assertEqual(target.mount_sources("lab"), ("//192.168.1.21/compose", "192.168.1.21:/onboardrr-lab"))
        self.assertEqual(storage_endpoint(self.inventory.database, override="192.168.1.21"), target)
        with self.assertRaisesRegex(ValueError, "selected primary"):
            target.mount_sources("lab", cifs="//goku.local/compose")
        with self.assertRaisesRegex(ValueError, "another primary"):
            self.inventory.set_profile("luffy", PROFILE)

    def test_denied_write_preserves_the_previous_nas(self):
        self.bao.fields_with_version.return_value = ({"value": "192.168.1.20"}, 2)
        self.select("goku")
        self.bao.patch.side_effect = HttpStatusError("OpenBao", "patch", 403)
        with self.assertRaisesRegex(ValueError, "services/data/storage/IP"):
            self.select("luffy", "192.168.1.21")
        self.assertEqual(storage_endpoint(self.inventory.database).name, "goku")
        self.bao.read.assert_not_called()

    def test_retry_finishes_an_interrupted_switch_without_writing_the_ip_again(self):
        self.bao.fields_with_version.side_effect = [
            ({"value": "192.168.1.20"}, 2), ({"value": "192.168.1.21"}, 3),
        ]
        self.bao.read.side_effect = OSError("connection lost after the patch")
        with self.assertRaisesRegex(OSError, "connection lost"):
            self.select("luffy", "192.168.1.21")
        self.assertIsNone(self.inventory.primary_storage())
        with self.assertRaisesRegex(ValueError, "pending storage switch"):
            storage_endpoint(self.inventory.database)
        self.assertEqual(self.select("luffy")["status"], "applied")
        self.bao.patch.assert_called_once()
        self.assertEqual(storage_endpoint(self.inventory.database).name, "luffy")

    def test_split_smb_credentials_and_cli_action_are_specific_to_the_nas(self):
        with patch.dict("os.environ", {"ONBOARDRR_BAO_LAYOUT": "split"}):
            path = storage_smb_record("luffy")
            self.assertEqual(deployment_kv_path("onboardrr", path, layout="split"), "hosts/data/luffy/smb")
        options = {"argument": "", "public_key": "", "known_hosts": "", "run_id": ""}
        self.assertEqual(command_args("storage-nas", self.inventory.get("luffy"), "--ipv4 192.168.1.21", apply=True,
                                      **options),
                         ["set-storage-nas", "luffy", "--apply", "--ipv4", "192.168.1.21"])
        with self.assertRaisesRegex(ValueError, "NAS profile"):
            self.select("lab", "192.168.1.30")

    def test_periphery_cli_routes_staging_to_the_selected_storage(self):
        self.bao.fields_with_version.return_value = ({"value": "192.168.1.21"}, 1)
        self.select("luffy", "192.168.1.21")
        with (redirect_stdout(io.StringIO()),
              patch("onboardrr.__main__.stage_goku_files", return_value={}) as stage):
            status = main(["--database", str(self.inventory.database), "periphery", "lab", "--ipv4", "192.168.1.40"])
        self.assertEqual(status, 0)
        device, address, target, _ = stage.call_args.args
        self.assertEqual((device["name"], address, target.name, target.host), ("lab", "192.168.1.40", "luffy", "luffy.local"))
        self.assertEqual(stage.call_args.kwargs["nfs_export"], "/onboardrr-lab")
