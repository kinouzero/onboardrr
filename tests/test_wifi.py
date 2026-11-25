import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from base64 import b64decode
from contextlib import redirect_stdout
from hashlib import sha256
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import yaml

from onboardrr.__main__ import main, wifi_command
from onboardrr.http_json import HttpStatusError
from onboardrr.secrets import publish_host_ipv4
from onboardrr.tui import command_args
from onboardrr.wifi import configure_wifi, disable_wifi, network_profile, sync_host_network


ANSIBLE = shutil.which("ansible-playbook", path=f"{Path(sys.executable).parent}{os.pathsep}{os.defpath}")
WIFI_PLAYBOOK = Path(__file__).resolve().parents[1] / "provisioning/wifi.yml"


class WifiTest(unittest.TestCase):
    def test_keyfile_has_expected_wpa2_key_and_preserves_ssid_bytes(self):
        result = network_profile("lab", "profile-id", "wlan0", "IEEE", "password")
        self.assertIn(b"ssid=73;69;69;69;\n", result)
        self.assertIn(b"psk=f42c6fc52df0ebef9ebb4b90b38a5f902e83fe1b135a70e23aed762e9710a12e\n", result)
        self.assertNotIn(b"password", result)
        self.assertIn(b"route-metric=32766", result)

    def test_accepts_raw_psk_and_rejects_invalid_credentials(self):
        raw = "AB" * 32
        self.assertIn(f"psk={raw.lower()}".encode(), network_profile("lab", "id", "wlan0", "ssid", raw))
        for ssid, password in [("x" * 33, "password"), ("", "password"), ("ssid", "short")]:
            with self.subTest(ssid=ssid, password=password), self.assertRaises(ValueError):
                network_profile("lab", "id", "wlan0", ssid, password)

    def test_preview_has_no_secret_and_apply_reuses_the_same_profile(self):
        device = {"id": "sample-id", "name": "lab"}
        hosts = Path("known_hosts")
        with patch("onboardrr.wifi.run_playbook") as playbook:
            configure_wifi(device, hosts, "wlan0")
            preview = playbook.call_args
            configure_wifi(device, hosts, "wlan0", credentials=("IEEE", "password"))
            applied = playbook.call_args
        self.assertTrue(preview.kwargs["check"])
        self.assertFalse(applied.kwargs["check"])
        self.assertEqual(preview.args[2], "wifi.yml")
        self.assertNotIn("onboardrr_wifi_profile_b64", preview.args[3])
        self.assertEqual(preview.args[3]["onboardrr_wifi_path"], applied.args[3]["onboardrr_wifi_path"])
        profile = b64decode(applied.args[3]["onboardrr_wifi_profile_b64"])
        self.assertEqual(applied.args[3]["onboardrr_wifi_profile_checksum"], sha256(profile).hexdigest())

    def test_rejects_untrusted_interface_values(self):
        with self.assertRaisesRegex(ValueError, "valid Wi-Fi interface"):
            configure_wifi({"id": "sample-id", "name": "lab"}, Path("known_hosts"), "wlan0\n[ipv4]")

    def test_bands_get_separate_profiles_and_can_replace_the_legacy_active_profile(self):
        device = {"id": "sample-id", "name": "lab"}
        with patch("onboardrr.wifi.run_playbook") as playbook:
            for band in (None, "2G", "5G"):
                configure_wifi(device, Path("known_hosts"), "wlan0", credentials=("same-ssid", "password"), band=band)
            legacy, low, high = [call.args[3] for call in playbook.call_args_list]
        identifiers = {item["onboardrr_wifi_uuid"] for item in (legacy, low, high)}
        self.assertEqual(len(identifiers), 3)
        for settings, selected in ((low, "bg"), (high, "a")):
            self.assertEqual(set(settings["onboardrr_wifi_switchable_uuids"]), identifiers)
            self.assertIn(f"\nband={selected}\n".encode(), b64decode(settings["onboardrr_wifi_profile_b64"]))
        self.assertNotIn(b"\nband=", b64decode(legacy["onboardrr_wifi_profile_b64"]))

    @patch("onboardrr.__main__.Inventory")
    @patch("onboardrr.__main__.configure_wifi", return_value={})
    @patch("onboardrr.__main__.configured_openbao")
    def test_cli_reads_only_the_selected_openbao_path(self, secret_store, configure, inventory):
        inventory.return_value.get.return_value = {"name": "lab", "profile": "raspberry-pi"}
        for band in ("2G", "5G"):
            with self.subTest(band=band):
                secret_store.return_value.read.reset_mock()
                secret_store.return_value.read.return_value = ("example", "fixture-password")
                with redirect_stdout(StringIO()):
                    self.assertEqual(main(["wifi", "lab", "--band", band.lower(), "--apply"]), 0)
                secret_store.return_value.read.assert_called_once_with(
                    f"deployments/wifi/{band}", "ssid", "psk",
                )
                self.assertEqual(configure.call_args.kwargs["band"], band)
        secret_store.return_value.read.reset_mock()
        with redirect_stdout(StringIO()):
            self.assertEqual(main(["wifi", "lab", "--band", "5G"]), 0)
        secret_store.return_value.read.assert_not_called()

    @patch("onboardrr.__main__.configured_openbao")
    def test_missing_legacy_entry_prompts_for_a_band(self, secret_store):
        inventory = Mock()
        inventory.get.return_value = {"name": "lab", "profile": "raspberry-pi"}
        secret_store.return_value.read.side_effect = HttpStatusError("OpenBao", "KV read", 404)
        with self.assertRaisesRegex(ValueError, "--band 2G or --band 5G"):
            wifi_command(SimpleNamespace(device="lab", apply=True, band=None, disable=False, sync=False), inventory)

    def test_enable_publishes_the_wireless_address_instead_of_the_ssh_address(self):
        network = {"route": "eth0", "local": "192.0.2.2", "links": {
            "eth0": {"type": "ethernet", "addresses": ["192.0.2.2"]},
            "wlan0": {"type": "wifi", "addresses": ["192.0.2.3"]},
        }}
        device = {"id": "sample-id", "name": "lab"}
        with (patch("onboardrr.wifi.run_playbook", return_value={"mode": "apply"}),
              patch("onboardrr.wifi.probe_network", return_value=network),
              patch("onboardrr.wifi.publish_host_ipv4") as publish):
            bao = Mock()

            def redeploy(host, address):
                publish.assert_called_once_with(bao, "lab", "192.0.2.3")
                self.assertEqual((host, address), (device, "192.0.2.3"))
                return {"redeployed_stacks": ["monitoring"]}

            result = configure_wifi(device, Path("known_hosts"), "wlan0", credentials=("ssid", "password"),
                                    host_bao=bao, redeploy=redeploy)
        publish.assert_called_once_with(bao, "lab", "192.0.2.3")
        self.assertEqual(result["openbao_path"], "hosts/lab/IP")
        self.assertEqual(result["redeployed_stacks"], ["monitoring"])

    def test_disable_requires_and_keeps_verified_ethernet_before_syncing_the_address(self):
        links = {"eth0": {"type": "ethernet", "addresses": ["192.0.2.2"]}}
        initial = {"route": "wlan0", "local": "192.0.2.3", "links": links, "radio": "enabled"}
        wired = {**initial, "route": "eth0", "local": "192.0.2.2"}
        device, hosts, bao = {"name": "lab"}, Path("known_hosts"), Mock()
        with (patch("onboardrr.wifi.probe_network") as probe,
              patch("onboardrr.wifi.run_playbook", return_value={"mode": "apply"}) as runner,
              patch("onboardrr.wifi.publish_host_ipv4") as publish):
            probe.side_effect = [initial, {**wired, "route": "wlan0"}]
            with self.assertRaisesRegex(ValueError, "Connect Ethernet"):
                disable_wifi(device, hosts, apply=True, host_bao=bao)
            runner.assert_not_called()
            publish.assert_not_called()
            probe.side_effect = [initial, wired, {**wired, "radio": "disabled"}]
            result = disable_wifi(device, hosts, apply=True, host_bao=bao)
        self.assertEqual(runner.call_args.args[2], "wifi-disable.yml")
        self.assertEqual(runner.call_args.kwargs["address"], "192.0.2.2")
        self.assertEqual(result["ipv4"], "192.0.2.2")
        publish.assert_called_once_with(bao, "lab", "192.0.2.2")

    def test_host_ip_patches_with_a_version_and_is_idempotent(self):
        bao = Mock()
        bao.fields_with_version.side_effect = [({"value": "192.0.2.2"}, 5), ({"value": "192.0.2.3"}, 6)]
        bao.read.return_value = ("192.0.2.3",)
        publish_host_ipv4(bao, "lab", "192.0.2.3")
        publish_host_ipv4(bao, "lab", "192.0.2.3")
        bao.patch.assert_called_once_with("lab/IP", {"value": "192.0.2.3"}, version=5)

    def test_maintenance_sync_uses_current_connections_without_running_a_playbook(self):
        host, known_hosts, bao = {"name": "lab"}, Path("known_hosts"), Mock()
        network = {"radio": "enabled", "route": "eth0", "local": "192.0.2.2", "links": {
            "eth0": {"type": "ethernet", "addresses": ["192.0.2.2"]},
            "wlan0": {"type": "wifi", "addresses": ["192.0.2.3"]},
        }}
        with (patch("onboardrr.wifi.probe_network", return_value=network) as probe,
              patch("onboardrr.wifi.run_playbook") as playbook,
              patch("onboardrr.wifi.publish_host_ipv4") as publish):
            redeploy = Mock(return_value={"redeployed_stacks": ["monitoring"]})
            preview = sync_host_network(host, known_hosts, "wlan0", apply=False, host_bao=bao, redeploy=redeploy)
            self.assertEqual((preview["connection_type"], preview["ipv4"]), ("wifi", "192.0.2.3"))
            publish.assert_not_called()
            redeploy.assert_not_called()
            result = sync_host_network(host, known_hosts, "wlan0", apply=True, host_bao=bao, redeploy=redeploy)
            self.assertEqual(result["redeployed_stacks"], ["monitoring"])
            publish.assert_called_once_with(bao, "lab", "192.0.2.3")
            redeploy.assert_called_once_with(host, "192.0.2.3")
            probe.return_value = {**network, "radio": "disabled"}
            wired = sync_host_network(host, known_hosts, "wlan0", apply=True, host_bao=bao, redeploy=redeploy)
            self.assertEqual((wired["interface"], wired["ipv4"]), ("eth0", "192.0.2.2"))
            publish.assert_called_with(bao, "lab", "192.0.2.2")
            playbook.assert_not_called()

    @patch("onboardrr.__main__.Inventory")
    @patch("onboardrr.__main__.sync_host_network", return_value={})
    @patch("onboardrr.__main__.configured_openbao")
    def test_sync_maintenance_action_uses_only_the_host_mount(self, secret_store, sync, inventory):
        device = {"name": "lab", "profile": "raspberry-pi"}
        inventory.return_value.get.return_value = device
        args = command_args("wifi-sync", device, "", apply=True, known_hosts="known_hosts")
        self.assertEqual(args, ["wifi", "lab", "--sync", "--known-hosts", "known_hosts", "--apply"])
        with redirect_stdout(StringIO()):
            self.assertEqual(main(args), 0)
        secret_store.assert_called_once_with(mount="hosts")
        sync.assert_called_once()

    @patch("onboardrr.__main__.Inventory")
    @patch("onboardrr.__main__.disable_wifi", return_value={})
    @patch("onboardrr.__main__.configured_openbao")
    def test_disable_cli_does_not_load_wifi_credentials(self, secret_store, disable, inventory):
        inventory.return_value.get.return_value = {"name": "lab", "profile": "raspberry-pi"}
        with redirect_stdout(StringIO()):
            self.assertEqual(main(["wifi", "lab", "--disable", "--apply"]), 0)
        secret_store.assert_called_once_with(mount="hosts")
        disable.assert_called_once()
        self.assertTrue(disable.call_args.kwargs["apply"])

    @unittest.skipUnless(ANSIBLE, "Ansible is required to exercise the Ethernet switch")
    def test_disable_playbook_refuses_a_wireless_management_route(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            scripts = {
                "ip": '#!/bin/sh\nprintf \'[{"dev":"%s"}]\\n\' "$WIFI_FIXTURE_ROUTE"\n',
                "nmcli": '#!/bin/sh\nif test "$*" = "radio wifi off"; then\n'
                         '    printf "disabled\\n" > "$WIFI_FIXTURE_RADIO"\nelse\n'
                         '    cat "$WIFI_FIXTURE_RADIO"\nfi\n',
            }
            for name, source in scripts.items():
                script = root / name
                script.write_text(source)
                script.chmod(0o700)
            radio = root / "radio"
            variables = {"ansible_python_interpreter": sys.executable, "ansible_become": False,
                         "onboardrr_ethernet_interface": "eth0", "onboardrr_ethernet_address": "192.0.2.2"}
            variables_path = root / "vars.yml"
            variables_path.write_text(yaml.safe_dump(variables))
            command = [ANSIBLE, "-i", "localhost,", "-c", "local", str(WIFI_PLAYBOOK.with_name("wifi-disable.yml")),
                       "-e", f"@{variables_path}"]
            for route, expected in (("wlan0", "enabled"), ("eth0", "disabled")):
                with self.subTest(route=route):
                    radio.write_text("enabled\n")
                    environment = {**os.environ, "PATH": f"{root}{os.pathsep}{os.defpath}",
                                   "SSH_CONNECTION": "192.0.2.1 45678 192.0.2.2 22",
                                   "WIFI_FIXTURE_RADIO": str(radio), "WIFI_FIXTURE_ROUTE": route}
                    result = subprocess.run(command, cwd=root, env=environment, capture_output=True, text=True, timeout=35)
                    self.assertEqual(result.returncode, 0 if route == "eth0" else 2, result.stdout + result.stderr)
                    self.assertEqual(radio.read_text().strip(), expected)

    @unittest.skipUnless(ANSIBLE, "Ansible is required to exercise the Wi-Fi radio tasks")
    def test_disabled_radio_is_enabled_and_rolled_back_if_activation_fails(self):
        tasks = yaml.safe_load(WIFI_PLAYBOOK.read_text())[0]["tasks"]
        apply = next(task["block"] for task in tasks if task["name"] == "Apply Wi-Fi only with wired access")
        install = next(task for task in apply if task["name"] == "Install and verify the Wi-Fi profile")
        enable = next(task for task in install["block"] if task["name"].startswith("Enable the Wi-Fi radio"))
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            nmcli = root / "nmcli"
            nmcli.write_text("""#!/bin/sh
printf '%s\\n' "$*" >> "$WIFI_FIXTURE/commands"
case "$*" in
    '-g GENERAL.AUTOCONNECT device show wlan0') printf 'yes\\n' ;;
    'radio wifi on') printf 'enabled\\n' > "$WIFI_FIXTURE/radio" ;;
    'radio wifi off') printf 'disabled\\n' > "$WIFI_FIXTURE/radio" ;;
    'radio wifi') cat "$WIFI_FIXTURE/radio" ;;
    'device set wlan0 autoconnect yes'|'device set wlan0 autoconnect no') ;;
    *) exit 1 ;;
esac
""")
            nmcli.chmod(0o700)
            fixture = root / "fixture.yml"
            for activation_fails in (False, True):
                with self.subTest(activation_fails=activation_fails):
                    (root / "radio").write_text("disabled\n")
                    (root / "commands").write_text("")
                    activation = {"ansible.builtin.assert": {"that": not activation_fails}}
                    fixture.write_text(yaml.safe_dump([{
                        "hosts": "localhost", "connection": "local", "gather_facts": False,
                        "environment": {"WIFI_FIXTURE": str(root), "PATH": f"{root}{os.pathsep}{os.defpath}"},
                        "vars": {"ansible_python_interpreter": sys.executable, "onboardrr_wifi_interface": "wlan0",
                                 "onboardrr_wifi_radio": {"stdout": "disabled"}, "onboardrr_wifi_previous_uuid": ""},
                        "tasks": [{"block": [enable, activation], "rescue": install["rescue"],
                                   "always": install["always"]}],
                    }]))
                    result = subprocess.run([ANSIBLE, "-i", "localhost,", str(fixture)], cwd=root,
                                            capture_output=True, text=True, timeout=35)
                    self.assertEqual(result.returncode, 2 if activation_fails else 0, result.stdout + result.stderr)
                    self.assertEqual((root / "radio").read_text(), "disabled\n" if activation_fails else "enabled\n")
                    self.assertEqual((root / "commands").read_text().splitlines(), [
                        "-g GENERAL.AUTOCONNECT device show wlan0", "device set wlan0 autoconnect no",
                        "radio wifi on", "radio wifi", *(["radio wifi off"] if activation_fails else []),
                        "device set wlan0 autoconnect yes",
                    ])
