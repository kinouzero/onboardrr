import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

from onboardrr.primary_storage import StorageEndpoint
from onboardrr.remote.storage_ip_sync import inspect_storage, repair_mounts
from onboardrr.secrets import publish_host_ipv4, stored_host_ipv4
from onboardrr.storage_ip_sync import sync_storage_ip


class StorageIpSyncTest(unittest.TestCase):
    def setUp(self):
        self.device = {"name": "lab", "ssh_user": "kinou"}
        self.known_hosts = Path("known_hosts")
        self.storage = StorageEndpoint("goku.local", "192.0.2.2")
        self.report = {"mounts_pending": True, "nfs_exports": ["/docker/komodo/data/lab"],
                       "start_periphery": True, "nfs_client": "192.0.2.15"}

    def test_apply_reauthorizes_the_nas_route_then_repairs_mounts_and_starts_periphery(self):
        operations = []

        def probe(*args, payload, **kwargs):
            operations.append((payload["action"], kwargs["privileged"]))
            return self.report

        def authorize(*args):
            operations.append(args)
            return True

        with (patch("onboardrr.storage_ip_sync.storage_endpoint", return_value=self.storage),
              patch("onboardrr.storage_ip_sync.run_ssh_probe", side_effect=probe),
              patch("onboardrr.storage_ip_sync.stored_host_ipv4", return_value="192.0.2.10"),
              patch("onboardrr.storage_ip_sync.update_nfs_client", side_effect=authorize)):
            result = sync_storage_ip(Path("inventory"), self.device, self.known_hosts, "192.0.2.20", apply=True)

        self.assertEqual(operations, [("inspect", False), (self.storage, "lab", "legacy", "192.0.2.10", "192.0.2.15"),
                                      ("repair", True), ("start", False)])
        self.assertTrue(result["storage_ip_sync"]["mounts_repaired"])
        self.assertEqual(result["storage_ip_sync"]["nfs_exports_updated"], ["/docker/komodo/data/lab"])

    def test_preview_never_mounts_starts_or_writes_the_nas(self):
        with (patch("onboardrr.storage_ip_sync.storage_endpoint", return_value=self.storage),
              patch("onboardrr.storage_ip_sync.run_ssh_probe", return_value=self.report) as probe,
              patch("onboardrr.storage_ip_sync.update_nfs_client") as update):
            result = sync_storage_ip(Path("inventory"), self.device, self.known_hosts, "192.0.2.20", apply=False)
        self.assertEqual(result["storage_ip_sync"], self.report)
        self.assertEqual(probe.call_args.kwargs["payload"]["action"], "inspect")
        probe.assert_called_once()
        update.assert_not_called()

    def test_failed_second_mount_rolls_back_first_and_preserves_fstab(self):
        with TemporaryDirectory() as directory:
            fstab, compose, periphery = (Path(directory) / name for name in ("fstab", "compose", "periphery"))
            fstab.write_text("original\n")
            compose.mkdir()
            periphery.mkdir()
            with patch("onboardrr.remote.storage_ip_sync.run", side_effect=[
                Mock(returncode=0), Mock(returncode=1), Mock(returncode=0),
            ]) as command:
                with self.assertRaisesRegex(ValueError, "Could not mount"):
                    repair_mounts(fstab, "original\n", "replacement\n", {compose: False, periphery: False}, "lab")
            command.assert_called_with("umount", str(compose))
            self.assertEqual(fstab.read_text(), "original\n")

    def test_storage_sync_does_not_revive_the_retired_smb_mounts(self):
        shares = ("//goku.local/scripts /home/kinou/scripts cifs _netdev 0 0\n"
                  "//goku.local/compose /home/kinou/.docker/compose cifs _netdev 0 0\n")
        probe = {"storage_ip": "192.0.2.2", "storage_names": ["goku.local"], "device": "lab",
                 "user": "kinou", "action": "repair"}
        with (patch("onboardrr.remote.storage_ip_sync.Path.read_text", return_value=shares),
              patch("onboardrr.remote.storage_ip_sync.shutil.which", return_value=None),
              patch("onboardrr.remote.storage_ip_sync.run") as command):
            self.assertFalse(inspect_storage(probe)["mounts_pending"])
        command.assert_not_called()

    def test_nas_client_is_remembered_independently_of_the_published_ip(self):
        bao = Mock()
        bao.fields_with_version.return_value = ({"value": "192.0.2.20"}, 3)
        bao.read.return_value = ("192.0.2.20", "192.0.2.15")
        publish_host_ipv4(bao, "lab", "192.0.2.20", nas_client="192.0.2.15")
        fields = {"value": "192.0.2.20", "nas_client_ipv4": "192.0.2.15"}
        bao.patch.assert_called_once_with("lab/IP", fields, version=3)
        bao.fields_with_version.return_value = (fields, 4)
        with patch("onboardrr.secrets.configured_openbao", return_value=bao):
            self.assertEqual(stored_host_ipv4("lab", nas_client=True), "192.0.2.15")
