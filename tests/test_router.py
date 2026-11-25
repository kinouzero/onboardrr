import hashlib
import hmac
import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from ipaddress import IPv4Address
from pathlib import Path
from unittest.mock import patch

from dns import exception, flags, message, rrset

from onboardrr.__main__ import main
from onboardrr.dns_discovery import answers_dns
from onboardrr.inventory import PROFILE, Inventory
from onboardrr.router import Freebox, dns_servers, maintain


class RouterMaintenanceTest(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.database = Path(directory.name) / "inventory.sqlite3"
        self.state = self.database.parent / "routers"
        self.dns_path = "/api/latest/dhcp/config/"

    def test_router_identity_changes_the_token_path_and_requires_pairing(self):
        origin = "https://mafreebox.freebox.fr"
        with patch.object(Freebox, "request", side_effect=[{"uid": "old"}, {"uid": "new"}]) as request:
            old = Freebox(origin, self.state)
            new = Freebox(origin, self.state)
            self.state.mkdir()
            old.credential.touch(mode=0o600)
            self.assertNotEqual(old.credential, new.credential)
            with self.assertRaisesRegex(ValueError, "Pair Onboardrr"):
                new.configure_dns(None, False)
            self.assertEqual(request.call_count, 2)

    def test_pair_polls_and_stores_only_a_private_app_token(self):
        responses = [{"uid": "router"}, {"track_id": 13, "app_token": "private-test-token"},
                     {"status": "pending"}, {"status": "granted"}]
        with (patch.object(Freebox, "request", side_effect=responses), patch("onboardrr.router.sleep"),
              redirect_stderr(io.StringIO())):
            router = Freebox(Freebox.DEFAULT_URL, self.state)
            result = router.pair()
        self.assertEqual(result["status"], "paired")
        self.assertNotIn("private-test-token", str(result))
        self.assertEqual(router.credential.read_text(), "private-test-token\n")
        self.assertEqual(router.credential.stat().st_mode & 0o777, 0o600)

    def test_preview_apply_readback_and_retry_preserve_other_dhcp_settings(self):
        state = {"dns": ["192.168.1.39", "", ""], "enabled": True, "options": [{"id": "domain_search"}]}
        updates = []

        def request(path, method="GET", *, payload=None, session=None):
            if path == "/api_version":
                return {"uid": "router"}
            if path == "/api/latest/login/":
                return {"challenge": "test-challenge"}
            if path == "/api/latest/login/session/":
                proof = hmac.new(b"test-token", b"test-challenge", hashlib.sha1).hexdigest()
                self.assertEqual(payload, {"app_id": Freebox.APP_ID, "password": proof})
                return {"session_token": "test-session"}
            self.assertEqual(session, "test-session")
            if path == self.dns_path:
                if method == "PUT":
                    updates.append(payload)
                    state.update(payload)
                    return {}
                return state.copy()
            self.assertEqual(path, "/api/latest/login/logout/")
            return {}

        with patch.object(Freebox, "request", side_effect=request):
            router = Freebox(Freebox.DEFAULT_URL, self.state)
            self.state.mkdir()
            router.credential.touch(mode=0o600)
            router.credential.write_text("test-token\n")
            self.assertEqual(router.configure_dns(None, False)["current_dns"], ["192.168.1.39"])
            desired = ["192.168.1.17", "192.168.1.4"]
            self.assertEqual(router.configure_dns(desired, False)["proposed_dns"], desired)
            self.assertEqual(updates, [])
            self.assertEqual(router.configure_dns(desired, True)["status"], "applied")
            self.assertEqual(router.configure_dns(desired, True)["status"], "unchanged")
        self.assertEqual(updates, [{"dns": [*desired, ""]}])
        self.assertEqual(state["options"], [{"id": "domain_search"}])
        self.assertTrue(state["enabled"])

    def test_apply_requires_ipv4_addresses_and_an_implemented_provider(self):
        for entry in ("", "2001:db8::1", "127.0.0.1", "192.168.1.4,192.168.1.4"):
            with self.subTest(entry=entry), self.assertRaises(ValueError):
                dns_servers(entry)
        with self.assertRaisesRegex(ValueError, "Specify"):
            maintain(self.database, apply=True)
        with patch.dict(os.environ, {"ONBOARDRR_ROUTER_PROVIDER": "other"}):
            with self.assertRaisesRegex(ValueError, "Unsupported router provider"):
                maintain(self.database)

    def test_cli_preview_apply_and_pair_are_inventory_independent(self):
        base = ["--database", str(self.database)]
        with patch("onboardrr.router.maintain", return_value={"status": "preview"}) as operation:
            with redirect_stdout(io.StringIO()):
                self.assertEqual(main([*base, "router-dns"]), 0)
                operation.assert_called_once_with(self.database, servers=None, apply=False)
                operation.reset_mock()
                self.assertEqual(main([*base, "router-dns", "--servers", "192.168.1.4", "--apply"]), 0)
                operation.assert_called_once_with(self.database, servers="192.168.1.4", apply=True)
                operation.reset_mock()
                self.assertEqual(main([*base, "router-pair"]), 0)
                operation.assert_called_once_with(self.database, pair=True)

    def test_detection_prefers_openbao_falls_back_to_inventory_and_never_contacts_router(self):
        inventory = Inventory(self.database)
        for name in ("fly", "naruto", "alias", "offline", "public"):
            inventory.add(name, f"{name}.local", "operator", 22, PROFILE)
        recorded = {"fly": "192.168.1.4", "naruto": "192.168.1.39", "alias": "192.168.1.4"}
        resolved = {"fly.local": "192.168.1.159", "naruto.local": "192.168.1.17",
                    "offline.local": "192.168.1.90", "public.local": "8.8.8.8"}

        def probe(address):
            return address in {"192.168.1.4", "192.168.1.17"}

        def resolve(host, family):
            return {IPv4Address(resolved[host])} if host in resolved else set()

        output = io.StringIO()
        with (patch("onboardrr.dns_discovery.stored_host_ipv4", side_effect=recorded.get),
              patch("onboardrr.dns_discovery.resolve_addresses", side_effect=resolve) as resolve_addresses,
              patch("onboardrr.dns_discovery.answers_dns", side_effect=probe) as probe_dns,
              patch("onboardrr.router.maintain") as router, redirect_stdout(output)):
            self.assertEqual(main(["--database", str(self.database), "router-dns", "--detect"]), 0)
        self.assertEqual(json.loads(output.getvalue()), {
            "status": "detected",
            "servers": [{"host": "alias", "address": "192.168.1.4"},
                        {"host": "naruto", "address": "192.168.1.17"}],
        })
        self.assertNotIn("fly.local", [call.args[0] for call in resolve_addresses.call_args_list])
        self.assertNotIn("8.8.8.8", [call.args[0] for call in probe_dns.call_args_list])
        router.assert_not_called()

    def test_detection_rejects_write_options(self):
        for options in (["--apply"], ["--servers", "192.168.1.4"]):
            with self.subTest(options=options), redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                main(["--database", str(self.database), "router-dns", "--detect", *options])

    def test_detection_requires_recursive_answers_over_udp_and_tcp(self):
        def answer(request, address, *, timeout):
            response = message.make_response(request, recursion_available=True)
            response.answer.append(rrset.from_text("example.org.", 60, "IN", "A", "192.0.2.1"))
            return message.from_wire(response.to_wire())

        def authoritative(request, address, *, timeout):
            response = answer(request, address, timeout=timeout)
            response.flags &= ~flags.RA
            return response

        with patch("onboardrr.dns_discovery.query.udp", side_effect=answer):
            with patch("onboardrr.dns_discovery.query.tcp", side_effect=answer):
                self.assertTrue(answers_dns("192.168.1.4"))
            with patch("onboardrr.dns_discovery.query.tcp", side_effect=exception.Timeout):
                self.assertFalse(answers_dns("192.168.1.4"))
            with patch("onboardrr.dns_discovery.query.tcp", side_effect=authoritative):
                self.assertFalse(answers_dns("192.168.1.4"))
