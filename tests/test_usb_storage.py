import tempfile
import unittest
from pathlib import Path
from unittest.mock import ANY, call, patch

from onboardrr.remote import usb_storage
from onboardrr.tui import command_args


ALIAS = "/dev/disk/by-id/usb-fixture-part2"
NODE = {"name": "/dev/sda2", "type": "part", "size": "100G", "fstype": "exfat",
        "uuid": "ABCD-1234", "ro": False, "mountpoints": [None], "parttype": "data"}


class USBStorageTest(unittest.TestCase):
    def test_candidates_exclude_efi_and_usb_system_disks(self):
        class Alias:
            def __init__(self, name, device):
                self.name, self.device = name, device

            def __str__(self):
                return "/dev/disk/by-id/" + self.name

            def __lt__(self, other):
                return self.name < other.name

            def is_symlink(self):
                return True

            def resolve(self):
                return self.device

        aliases = [Alias("usb-fixture-part1", "/dev/sda1"), Alias("usb-fixture-part2", "/dev/sda2"),
                   Alias("usb-system-part1", "/dev/sdb1")]
        efi = {**NODE, "name": "/dev/sda1", "parttype": "c12a7328-f81f-11d2-ba4b-00a0c93ec93b"}
        system = {**NODE, "name": "/dev/sdb1", "mountpoints": ["/"]}
        disks = [{"type": "disk", "tran": "usb", "children": children} for children in ([efi, NODE], [system])]
        with patch.object(usb_storage, "BY_ID") as by_id:
            by_id.glob.return_value = aliases
            self.assertEqual(list(usb_storage.candidates(disks)), [(ALIAS, NODE)])

    def test_format_requires_exact_confirmation_and_stops_when_unmount_fails(self):
        request = {"apply": True, "confirm_device": "wrong", "mountpoint": "/home/operator/ssd",
                   "uid": 1000, "gid": 1000}
        node = {**NODE, "mountpoints": ["/media/operator/disk"]}
        preview = usb_storage.format_volume({**request, "apply": False}, ALIAS, node, [node])
        self.assertEqual(preview["previous_mountpoints"], ["/media/operator/disk"])
        with patch.object(usb_storage, "run") as run, patch.object(usb_storage, "assert_current"):
            with self.assertRaisesRegex(ValueError, "exact"):
                usb_storage.format_volume(request, ALIAS, node, [node])
            request["confirm_device"] = ALIAS
            run.assert_not_called()
            with (tempfile.TemporaryDirectory() as directory,
                  patch.object(usb_storage, "FSTAB", Path(directory) / "fstab"),
                  patch.object(usb_storage, "mount_configuration") as configure,
                  patch.object(usb_storage, "unmount_volume") as unmount,
                  patch.object(usb_storage, "activate_mount") as activate):
                usb_storage.FSTAB.write_text("")
                configure.side_effect = ValueError("Mount point is occupied")
                with self.assertRaisesRegex(ValueError, "occupied"):
                    usb_storage.format_volume(request, ALIAS, node, [node])
                unmount.assert_not_called()
                run.assert_not_called()
                configure.side_effect = None
                unmount.side_effect = ValueError("Volume is busy")
                with self.assertRaisesRegex(ValueError, "busy"):
                    usb_storage.format_volume(request, ALIAS, node, [node])
                run.assert_not_called()
                unmount.side_effect = None
                report = usb_storage.format_volume(request, ALIAS, node, [node])
                activate.assert_called_once_with({**NODE, "fstype": "ext4", "uuid": report["uuid"], "mountpoints": []},
                                                 request["mountpoint"], configure.return_value)
                activate.side_effect = ValueError("Mount failed")
                incomplete = usb_storage.format_volume(request, ALIAS, node, [node])
                self.assertFalse(incomplete["mounted"])
                self.assertIn("Use Mount USB storage to retry", incomplete["blockers"][0])
            run.assert_any_call("/usr/sbin/mkfs.ext4", "-F", "-U", report["uuid"], "-E",
                                "nodiscard,root_owner=1000:1000", ALIAS, timeout=240, env=ANY)
            formats = [operation for operation in run.call_args_list if operation.args[0] == "/usr/sbin/mkfs.ext4"]
            self.assertTrue(all(operation.kwargs["env"]["UNIX_IO_NOZEROOUT"] == "1" for operation in formats))
            self.assertEqual(report["status"], "formatted")
            self.assertTrue(report["mounted"])

    def test_format_unmounts_then_replaces_the_managed_uuid_and_remounts(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory).resolve()
            target = str(base / "ssd")
            fstab = base / "fstab"
            unrelated = "# existing config\nUUID=0123-4567 /mnt/other exfat defaults 0 0\n"
            original = unrelated + f"UUID={NODE['uuid']} {target} exfat defaults,x-systemd.automount 0 0 # onboardrr-usb\n"
            fstab.write_text(original)
            request = {"apply": True, "confirm_device": ALIAS, "mountpoint": target, "uid": 1000, "gid": 1000}
            node = {**NODE, "mountpoints": [target]}
            operations = []

            def unmount(*args):
                operations.append("unmount")

            def run(*args, **kwargs):
                if args[0] == "/usr/sbin/mkfs.ext4":
                    operations.append("format")
                if args[0] == "systemd-escape":
                    return "mount-target.mount"
                if args[:2] == ("systemctl", "start"):
                    operations.append("remount")
                return ""

            with (patch.object(usb_storage, "FSTAB", fstab),
                  patch.object(usb_storage, "mount_types", return_value=[]),
                  patch.object(usb_storage, "assert_current"),
                  patch.object(usb_storage, "unmount_volume", side_effect=unmount),
                  patch.object(usb_storage, "run", side_effect=run)):
                report = usb_storage.format_volume(request, ALIAS, node, [node])
            self.assertTrue(report["mounted"])
            self.assertEqual(operations, ["unmount", "format", "remount"])
            new_contents = fstab.read_text()
            self.assertTrue(new_contents.startswith(unrelated))
            self.assertNotIn(f"UUID={NODE['uuid']}", new_contents)
            self.assertIn(f"UUID={report['uuid']}\t{target}\text4\t", new_contents)

    def test_format_write_failure_preserves_fstab_and_reports_a_disconnected_device(self):
        request = {"apply": True, "confirm_device": ALIAS, "mountpoint": "/home/operator/ssd",
                   "uid": 1000, "gid": 1000}
        for disconnect, expected in ((False, "Check the host's disk logs"), (True, "disconnected")):
            with (self.subTest(disconnect=disconnect),
                  patch.object(usb_storage, "mount_configuration"),
                  patch.object(usb_storage, "unmount_volume"),
                  patch.object(usb_storage, "assert_current") as identity,
                  patch.object(usb_storage, "run", side_effect=ValueError("mkfs: Input/output error")) as run,
                  patch.object(usb_storage, "write_fstab") as write,
                  patch.object(usb_storage, "activate_mount") as mount):
                identity.side_effect = [None, ValueError("The selected USB device was disconnected.") if disconnect else None]
                report = usb_storage.format_volume(request, ALIAS, NODE, [NODE])
                self.assertEqual(report["status"], "format-incomplete")
                self.assertFalse(report["mounted"])
                self.assertIn(expected, report["blockers"][0])
                self.assertIn("may be incomplete", report["blockers"][0])
                self.assertIn("Input/output error", report["format_error"])
                self.assertEqual(run.call_count, 1)
                write.assert_not_called()
                mount.assert_not_called()

    def test_default_mountpoint_is_shared_by_mount_and_format(self):
        home = Path("/srv/users/pi-operator")
        request = {"apply": False, "operator": "pi-operator", "usb_device": ALIAS}
        with (patch.object(usb_storage.pwd, "getpwnam") as account,
              patch.object(usb_storage, "tree", return_value=[NODE]),
              patch.object(usb_storage, "candidates", return_value=[(ALIAS, NODE)]),
              patch.object(usb_storage, "run", return_value="ID_USB_VENDOR_ID=abcd\nID_USB_MODEL_ID=1234\n")):
            account.return_value.pw_dir = str(home)
            for format_volume in (False, True):
                report = usb_storage.handle({**request, "format": format_volume})
                self.assertEqual(report["mountpoint"], str(home / "ssd"))
        self.assertEqual(usb_storage.resolve_mountpoint(home, "~/backup"), str(home / "backup"))
        self.assertEqual(usb_storage.resolve_mountpoint(home, "/mnt/ssd"), "/mnt/ssd")
        self.assertFalse(usb_storage.system_disk({"mountpoints": [str(home / "ssd")]}, home))
        self.assertTrue(usb_storage.system_disk({"mountpoints": [str(home)]}, home))

    def test_boot_quirk_preserves_other_parameters_and_existing_flags(self):
        original = "quiet usb-storage.quirks=abcd:1234:m,152D:1561:t  rootwait\r\n"
        updated = usb_storage.disable_uas_in_cmdline(original)
        self.assertEqual(updated, original.replace("152D:1561:t", "152D:1561:tu"))
        self.assertEqual(usb_storage.disable_uas_in_cmdline(updated), updated)
        self.assertEqual(usb_storage.disable_uas_in_cmdline("quiet usb-storage.quirks=abcd:1234:m\n"),
                         "quiet usb-storage.quirks=abcd:1234:m,152d:1561:u\n")
        for invalid in ("quiet\nrootwait\n", "quiet usb-storage.quirks=abcd:1234:m usb-storage.quirks=152d:1561:u\n",
                        "quiet usb-storage.quirks=152d:1561:u,152d:1561:t\n"):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                usb_storage.disable_uas_in_cmdline(invalid)

    def test_known_usb_bridge_stages_once_and_blocks_mount_and_format_until_reboot(self):
        base_request = {"operator": "pi-operator", "usb_device": ALIAS, "confirm_device": ALIAS,
                        "apply": False, "format": False}
        original = b"quiet rootwait\n"
        usb_properties = "ID_USB_VENDOR_ID=152d\nID_USB_MODEL_ID=1561\nID_USB_DRIVER="
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            boot = base / "cmdline.txt"
            active = base / "active"
            boot.write_bytes(original)
            boot.chmod(0o640)
            active.write_bytes(original)
            with (patch.object(usb_storage, "BOOT_CMDLINES", (boot,)),
                  patch.object(usb_storage, "ACTIVE_CMDLINE", active),
                  patch.object(usb_storage.Path, "is_mount", return_value=True),
                  patch.object(usb_storage.pwd, "getpwnam") as account,
                  patch.object(usb_storage, "tree", return_value=[NODE]),
                  patch.object(usb_storage, "candidates", return_value=[(ALIAS, NODE)]),
                  patch.object(usb_storage, "run", return_value=usb_properties + "uas\n") as run,
                  patch.object(usb_storage, "assert_current"),
                  patch.object(usb_storage, "mount_volume") as mount,
                  patch.object(usb_storage, "format_volume") as format_volume):
                account.return_value.pw_dir = "/srv/users/pi"
                preview = usb_storage.handle(base_request)
                self.assertTrue(preview["reboot_required"])
                self.assertEqual(boot.read_bytes(), original)
                request = {**base_request, "apply": True}
                applied = usb_storage.handle(request)
                self.assertEqual(applied["status"], "reboot-required")
                self.assertTrue(applied["blockers"])
                self.assertEqual(Path(applied["boot_backup"]).read_bytes(), original)
                self.assertEqual(boot.read_bytes(), b"quiet rootwait usb-storage.quirks=152d:1561:u\n")
                self.assertEqual(boot.stat().st_mode & 0o777, 0o640)
                retry = usb_storage.handle({**request, "format": True})
                self.assertEqual(retry["status"], "reboot-required")
                self.assertNotIn("boot_backup", retry)
                mount.assert_not_called()
                format_volume.assert_not_called()
                active.write_bytes(boot.read_bytes())
                with self.assertRaisesRegex(ValueError, "not using usb-storage"):
                    usb_storage.handle(request)
                run.return_value = usb_properties + "usb-storage\n"
                self.assertIs(usb_storage.handle(request), mount.return_value)
                self.assertIs(usb_storage.handle({**request, "format": True}), format_volume.return_value)
                self.assertEqual(len(list(base.glob("cmdline.txt.onboardrr-before-uas-*"))), 1)

    def test_mount_writes_persistent_entry_once_and_rolls_back_on_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory).resolve()
            target = str(base / "ssd")
            fstab = base / "fstab"
            original = "# keep existing contents\n"
            fstab.write_text(original)
            fstab.chmod(0o640)
            request = {"apply": True, "mountpoint": target, "uid": 1000, "gid": 1000}

            def run(*args, **kwargs):
                if args[0] == "systemd-escape":
                    return "mnt-ssd.mount"
                if args[1] == "start" and fail:
                    raise ValueError("Mount failed")
                return ""

            with (patch.object(usb_storage, "FSTAB", fstab),
                  patch.object(usb_storage, "run", side_effect=run),
                  patch.object(usb_storage, "mount_types", return_value=[]),
                  patch.object(usb_storage, "assert_current")):
                fail = True
                with self.assertRaisesRegex(ValueError, "Mount failed"):
                    usb_storage.mount_volume(request, ALIAS, NODE, [NODE])
                self.assertEqual(fstab.read_text(), original)
                fail = False
                for _ in range(2):
                    report = usb_storage.mount_volume(request, ALIAS, NODE, [NODE])
                    self.assertEqual(report["status"], "mounted")
                contents = fstab.read_text()
                self.assertEqual(contents.count("UUID=ABCD-1234"), 1)
                self.assertIn("nofail,x-systemd.automount", contents)
                self.assertIn("uid=1000,gid=1000", contents)
                self.assertEqual(fstab.stat().st_mode & 0o777, 0o640)
                with self.assertRaisesRegex(ValueError, "another connected device"):
                    usb_storage.mount_volume(request, ALIAS, NODE, [NODE, {**NODE, "name": "/dev/sdb2"}])

    def test_unmount_detaches_desktop_volume_without_forcing(self):
        target = "/media/operator/USB Data"
        node = {**NODE, "mountpoints": [target]}
        with (patch.object(usb_storage, "FSTAB") as fstab,
              patch.object(usb_storage, "mount_types", return_value=["exfat"]),
              patch.object(usb_storage, "current_mounts", side_effect=[{target}, set()]),
              patch.object(usb_storage, "assert_current"), patch.object(usb_storage, "run") as run):
            fstab.read_text.return_value = ""
            preview = usb_storage.unmount_volume({"apply": False}, ALIAS, node)
            self.assertEqual(preview["current_mountpoints"], [target])
            run.assert_not_called()
            report = usb_storage.unmount_volume({"apply": True}, ALIAS, node)
            self.assertEqual(report["status"], "unmounted")
            run.assert_called_once_with("umount", "--all-targets", "--", ALIAS, timeout=30)

    def test_unmount_stops_managed_automount_without_editing_fstab(self):
        target = "/home/operator/ssd"
        with (patch.object(usb_storage, "FSTAB") as fstab,
              patch.object(usb_storage, "mount_types", side_effect=[["autofs"], [], []]),
              patch.object(usb_storage, "current_mounts", return_value=set()),
              patch.object(usb_storage, "assert_current"), patch.object(usb_storage, "run") as run):
            fstab.read_text.return_value = (
                f"UUID=ABCD-1234 {target} exfat defaults,nofail,x-systemd.automount 0 0 # onboardrr-usb\n"
            )
            run.return_value = "home-operator-ssd\n"
            self.assertEqual(usb_storage.unmount_volume({"apply": True}, ALIAS, NODE)["status"], "unmounted")
            self.assertEqual(usb_storage.unmount_volume({"apply": True}, ALIAS, NODE)["status"], "already-unmounted")
            self.assertEqual(run.call_args_list, [
                call("systemd-escape", "--path", target),
                call("systemctl", "stop", "home-operator-ssd.mount", "home-operator-ssd.automount", timeout=30),
            ])
            self.assertTrue(all(operation == call.read_text() for operation in fstab.mock_calls))

    def test_tui_mount_unmount_and_format_use_separate_actions(self):
        device = {"name": "fly", "profile": "raspberry-pi"}
        options = f"--usb-device {ALIAS}"
        mounted = command_args("usb-storage", device, options, apply=True, public_key="zero.pub")
        unmounted = command_args("unmount-usb", device, options, apply=True, public_key="zero.pub")
        formatted = command_args("format-usb", device, options, apply=True, public_key="zero.pub")
        self.assertNotIn("--format", mounted)
        self.assertNotIn("--unmount", mounted)
        self.assertIn("--unmount", unmounted)
        self.assertNotIn("--format", unmounted)
        self.assertIn("--format", formatted)
        with self.assertRaisesRegex(ValueError, "dedicated USB format or unmount action"):
            command_args("usb-storage", device, options + " --format", apply=True, public_key="zero.pub")
