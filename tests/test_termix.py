import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from onboardrr.inventory import Inventory, NAS_PROFILE, PROFILE, SERVER_PROFILE
from onboardrr.termix import HOSTS, Termix, register, ssh_address


class TermixTest(unittest.TestCase):
    def setUp(self):
        directory = self.enterContext(tempfile.TemporaryDirectory())
        self.database = Path(directory) / "inventory.sqlite3"
        self.device = Inventory(self.database).add("lab", "192.0.2.20", "operator", 2222, SERVER_PROFILE)
        self.api = Termix("https://termix.example", "fixture-token", 12)
        self.api.service = Mock()
        self.hosts = []

        def request(method, path, *, data, **kwargs):
            if method == "GET":
                if path == "/credentials/12":
                    return {"username": "operator"}
                return list(self.hosts)
            if method == "POST":
                self.hosts.append({**data, "id": 35})
                return self.hosts[-1]
            if method == "PUT":
                host = self.hosts[0]
                if path == f"{HOSTS}/35":
                    host.update(data)
                else:
                    _, _, plugin, settings, scope, host_id = path.split("/")
                    self.assertEqual((settings, scope, host_id), ("settings", "host", "35"))
                    host.setdefault("pluginSettings", {}).setdefault(plugin, {}).update(data)
                return host
            self.assertEqual((method, path), ("DELETE", f"{HOSTS}/35"))
            self.hosts.clear()
            return None

        self.api.service.request.side_effect = request

    def test_preview_register_repeat_and_remove_owned_entry(self):
        preview = register(self.database, self.device, self.api, apply=False)
        self.assertEqual((preview["action"], preview["tags"]), ("create", ["server"]))
        self.assertEqual(self.hosts, [])
        self.assertEqual(register(self.database, self.device, self.api, apply=True)["status"], "created")
        host, = self.hosts
        self.assertEqual((host["ip"], host["port"], host["username"]), ("192.0.2.20", 2222, "operator"))
        self.assertEqual((host["authType"], host["credentialId"]), ("credential", 12))
        self.assertEqual((host["name"], host["folder"], host["parentHostId"]), ("operator@lab", "", None))
        self.assertIs(host["overrideCredentialUsername"], False)
        self.assertTrue(host["statusCheckEnabled"])
        self.assertEqual(host["pluginSettings"]["file-manager"], {"defaultPath": "/", "scpLegacy": False})
        for plugin, field in (("docker", "enableDocker"), ("session-recording", "enableSessionRecording"),
                              ("tunnels", "enableTunnel")):
            self.assertIs(host["pluginSettings"][plugin][field], True)
        self.assertIn("processes", host["pluginSettings"]["host-metrics"]["enabledWidgets"])
        self.assertIn("mosh-server", host["pluginSettings"]["ssh-terminal"]["moshCommand"])
        self.assertEqual(host["tags"], ["server"])
        self.assertNotIn("key", host)
        self.assertEqual(register(self.database, self.device, self.api, apply=True)["status"], "present")
        self.assertEqual(register(self.database, self.device, self.api, apply=False, remove=True)["action"], "remove")
        self.assertEqual(register(self.database, self.device, self.api, apply=True, remove=True)["status"], "removed")
        self.assertEqual([call.args[0] for call in self.api.service.request.call_args_list].count("POST"), 1)

    def test_moves_managed_legacy_host_to_root_and_resumes_plugin_updates(self):
        self.hosts.append({"id": 35, "ip": "192.0.2.20", "port": 2222, "name": "lab",
                           "username": "operator", "credentialId": 12, "folder": "Onboardrr",
                           "notes": f"Onboardrr device {self.device['id']}", "pin": True,
                           "tags": ["rpi", "onboardrr", "custom"],
                           "pluginSettings": {"docker": {"enableDocker": True, "other": 1}}})
        self.assertEqual(register(self.database, self.device, self.api, apply=False)["action"], "update")
        self.assertEqual(self.hosts[0]["folder"], "Onboardrr")
        self.assertEqual(register(self.database, self.device, self.api, apply=True)["status"], "updated")
        host, = self.hosts
        self.assertEqual((host["id"], host["folder"], host["name"]), (35, "", "operator@lab"))
        self.assertEqual(host["tags"], ["server", "custom"])
        self.assertEqual(host["pluginSettings"]["docker"]["other"], 1)
        self.assertTrue(host["pin"])
        del host["pluginSettings"]["tunnels"]
        self.api.service.request.reset_mock()
        self.assertEqual(register(self.database, self.device, self.api, apply=True)["status"], "updated")
        self.assertEqual([call.args[1] for call in self.api.service.request.call_args_list
                          if call.args[0] == "PUT"], ["/plugins/tunnels/settings/host/35"])

    def test_reuses_manual_endpoint_without_retiring_it(self):
        self.hosts.append({"id": 35, "ip": "192.0.2.20", "port": 2222,
                           "name": "lab", "username": "operator", "connectionType": "ssh"})
        self.assertFalse(register(self.database, self.device, self.api, apply=True)["managed"])
        self.assertEqual(register(self.database, self.device, self.api, apply=True, remove=True)["status"], "absent")
        self.assertEqual([call.args[0] for call in self.api.service.request.call_args_list], ["GET", "GET"])
        self.hosts[0]["ip"] = "192.0.2.21"
        with self.assertRaisesRegex(ValueError, "different Termix host"):
            register(self.database, self.device, self.api, apply=True)

    def test_resolves_mdns_or_accepts_an_explicit_destination(self):
        device = {**self.device, "host": "lab.local"}
        with patch("onboardrr.host_resolution.socket.getaddrinfo") as resolve:
            resolve.return_value = [(None, None, None, None, ("2001:db8::2", 0)),
                                    (None, None, None, None, ("192.0.2.2", 0))]
            self.assertEqual(ssh_address(device, None), "192.0.2.2")
            self.assertEqual(ssh_address(device, "lab.pang"), "192.0.2.2")
            self.assertEqual(ssh_address(device, "2001:db8::5"), "2001:db8::5")
            resolve.return_value += [(None, None, None, None, ("192.0.2.3", 0))]
            with self.assertRaisesRegex(ValueError, "unique IP"):
                ssh_address(device, None)

    def test_missing_dns_for_a_host_uses_its_configured_openssh_destination(self):
        device = {**self.device, "host": "main.example"}
        records = [(None, None, None, None, ("192.0.2.15", 0))]
        with (patch("onboardrr.host_resolution.socket.getaddrinfo", side_effect=[OSError(), records]),
              patch("onboardrr.host_resolution.openssh_destination", return_value="192.0.2.15") as alias):
            self.assertEqual(ssh_address(device, None), "192.0.2.15")
            alias.assert_called_once_with("main.example")

    def test_previews_only_the_profile_tag_for_each_device_type(self):
        for profile, tag in ((PROFILE, "rpi"), (SERVER_PROFILE, "server"), (NAS_PROFILE, "nas")):
            with self.subTest(profile=profile):
                result = register(self.database, {**self.device, "profile": profile}, self.api, apply=False)
                self.assertEqual(result["tags"], [tag])

    @patch("onboardrr.termix.stored_host_ipv4", return_value="192.0.2.20")
    @patch("onboardrr.host_resolution.socket.getaddrinfo")
    def test_uses_stored_ipv4_when_the_hostname_only_resolves_to_ipv6(self, resolve, stored):
        resolve.return_value = [(None, None, None, None, ("2001:db8::2", 0))]
        device = {**self.device, "host": "lab.local"}
        self.assertEqual(ssh_address(device, None), "192.0.2.20")
        stored.assert_called_once_with("lab")
        self.assertEqual(ssh_address(device, "override.example"), "2001:db8::2")
