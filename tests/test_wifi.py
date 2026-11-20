import unittest
from base64 import b64decode
from contextlib import redirect_stdout
from hashlib import sha256
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from onboardrr.__main__ import main, wifi_command
from onboardrr.http_json import HttpStatusError
from onboardrr.wifi import configure_wifi, network_profile


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
            wifi_command(SimpleNamespace(device="lab", apply=True, band=None), inventory)
