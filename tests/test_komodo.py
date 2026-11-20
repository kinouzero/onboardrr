import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from onboardrr.http_json import HttpStatusError
from onboardrr.inventory import PROFILE, SERVER_PROFILE, Inventory
from onboardrr.komodo import Komodo, adopt_legacy_periphery, create_periphery_passkey, periphery_address, register_device
from onboardrr.remote.periphery_identity import identity
from onboardrr.runs import Ownership, RunStore


SERVER_ID = "0123456789abcdef01234567"
TAG_ID = "aaaaaaaaaaaaaaaaaaaaaaaa"
ADDRESS = "wss://lab.local:8120"
PASSKEY = "fixture-passkey"


class KomodoTest(unittest.TestCase):
    def setUp(self):
        self.komodo = Komodo("https://komodo.example:8443", "fixture-key", "fixture-api-secret")

    def test_legacy_adoption_reuses_the_server_and_resumes_without_staging(self):
        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory) / "inventory.sqlite3"
            device = Inventory(db).add("lab", "lab.local", "operator", 22, PROFILE)
            runs = RunStore(db)
            run_id = runs.open("lab")["id"]
            runs.complete_step(run_id, "ssh")
            self.komodo.version = Mock(return_value="2.3.4")
            verified = {"server_id": SERVER_ID, "address": "https://192.0.2.10:8120", "registration": "legacy-inbound"}
            with patch("onboardrr.komodo.legacy_periphery", return_value=verified), patch.object(
                self.komodo, "_request"
            ) as request:
                first = adopt_legacy_periphery(self.komodo, device, "2.3.4", 8120, None, None, None, runs)
                second = adopt_legacy_periphery(self.komodo, device, "2.3.4", 8120, None, None, None, runs)
            self.assertEqual(first, second)
            self.assertEqual(first["next_step"], "komodo-stacks")
            self.assertEqual([(item["kind"], item["ownership"]) for item in first["resources"]],
                             [("komodo-server", Ownership.REUSED)])
            request.assert_not_called()

    @patch("onboardrr.remote.periphery_identity.subprocess.check_output")
    def test_agent_probe_rejects_a_passkey_mismatch_and_returns_only_the_public_key(self, command):
        interfaces = json.dumps([{"addr_info": [{"local": "192.0.2.10"}]}])
        container = json.dumps([{
            "Id": "container-id", "State": {"Running": True},
            "Config": {"Image": "ghcr.io/moghtech/komodo-periphery:2.3.4", "Env": [
                "PERIPHERY_PRIVATE_KEY=file:/config/agent.pem", f"PERIPHERY_PASSKEYS={PASSKEY}",
            ]},
        }])
        command.side_effect = [interfaces, container, interfaces, container, b"public-key"]
        with self.assertRaises(ValueError):
            identity("different", "192.0.2.10")
        self.assertEqual(command.call_count, 2)
        self.assertEqual(identity(PASSKEY, "192.0.2.10"), {"public_key": "cHVibGljLWtleQ=="})
        self.assertIn("-pubout", command.call_args.args[0])

    def test_registration_creates_a_missing_passkey_only_for_a_new_server(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "inventory.sqlite3"
            device = Inventory(database).add("lab", "lab.local", "operator", 22, PROFILE)
            runs = RunStore(database)
            run = runs.open("lab")
            bao = Mock()
            missing = HttpStatusError("OpenBao", "KV read", 404)
            bao.read.side_effect = [missing, missing, (PASSKEY,), (PASSKEY,)]
            with (
                patch.object(self.komodo, "version", return_value="2.3.4"),
                patch.object(self.komodo, "existing_server", side_effect=[{"id": SERVER_ID}, None]),
                patch.object(self.komodo, "register", return_value=(SERVER_ID, Ownership.REUSED)) as register,
                patch("onboardrr.komodo.token_urlsafe", return_value=PASSKEY),
            ):
                with self.assertRaisesRegex(ValueError, "SSH enrollment"):
                    register_device(self.komodo, device, ADDRESS, "2.3.4", "", runs, openbao=bao)
                bao.read.assert_not_called()
                runs.complete_step(run["id"], "ssh")
                with self.assertRaisesRegex(ValueError, "Restore the existing Periphery passkey"):
                    register_device(self.komodo, device, ADDRESS, "2.3.4", "", runs, openbao=bao)
                bao.create.assert_not_called()
                register.assert_not_called()
                done = register_device(self.komodo, device, ADDRESS, "2.3.4", "", runs, openbao=bao)
                self.assertEqual(done["next_step"], "goku-files")
                register.assert_called_once_with("lab", ADDRESS, PASSKEY)
                self.assertEqual(register_device(self.komodo, device, ADDRESS, "2.3.4", "", runs, openbao=bao), done)
                bao.create.assert_called_once_with("deployments/periphery/lab", {"passkey": PASSKEY})

    def test_passkey_creation_uses_a_concurrently_created_record(self):
        bao = Mock()
        bao.create.side_effect = HttpStatusError("OpenBao", "KV create", 403)
        path = "deployments/periphery/lab"
        bao.read.return_value = (PASSKEY,)
        self.assertEqual(create_periphery_passkey(bao, path), PASSKEY)
        bao.read.side_effect = HttpStatusError("OpenBao", "KV read", 404)
        with self.assertRaisesRegex(ValueError, "needs OpenBao create permission"):
            create_periphery_passkey(bao, path)

    def test_rpc_uses_only_the_configured_https_origin_and_rejects_redirects(self):
        with patch("onboardrr.http_json.http.client.HTTPSConnection") as factory:
            connection = factory.return_value
            connection.getresponse.return_value.status = 302
            with self.assertRaisesRegex(ValueError, "HTTP 302"):
                self.komodo.version()
            factory.assert_called_once_with("komodo.example", 8443, timeout=10)
            request = connection.request.call_args
            self.assertEqual(request.args, ("POST", "/read"))
            self.assertEqual(json.loads(request.kwargs["body"]), {"type": "GetVersion", "params": {}})
            self.assertEqual(request.kwargs["headers"]["X-Api-Secret"], "fixture-api-secret")
            connection.close.assert_called_once()

    def test_reference_output_contains_no_configuration_secrets(self):
        with patch.object(self.komodo, "_request", side_effect=[
            {"name": "naruto", "config": {"passkey": PASSKEY, "address": ADDRESS, "insecure_tls": False}},
            [{"name": "docker", "id": "template", "template": True}, {"name": "live", "template": False}],
            {"version": "2.3.4"},
        ]):
            result = self.komodo.reference("naruto")
        self.assertTrue(result["server"]["has_passkey_override"])
        self.assertEqual(result["stack_templates"], [{"name": "docker", "id": "template"}])
        self.assertNotIn(PASSKEY, json.dumps(result))

    def test_registration_creates_a_disabled_server_then_reuses_the_matching_target(self):
        existing = {"id": SERVER_ID, "name": "lab", "info": {"address": ADDRESS}}
        with patch.object(self.komodo, "_request", side_effect=[
            [], {"_id": {"$oid": SERVER_ID}},
            [existing], {"config": {"address": ADDRESS, "insecure_tls": False, "passkey": PASSKEY}},
        ]) as request:
            self.assertEqual(self.komodo.register("lab", ADDRESS, PASSKEY), (SERVER_ID, Ownership.CREATED))
            self.assertEqual(self.komodo.register("lab", ADDRESS, PASSKEY), (SERVER_ID, Ownership.REUSED))
        writes = [call for call in request.call_args_list if call.args[0] == "/write"]
        self.assertEqual(len(writes), 1)
        self.assertEqual(writes[0].args[2]["config"], {
            "address": ADDRESS, "enabled": False, "insecure_tls": False, "passkey": PASSKEY,
        })

    def test_conflicting_addresses_and_unverified_passkeys_block_writes(self):
        other = {"id": SERVER_ID, "name": "other", "info": {"address": ADDRESS}}
        with patch.object(self.komodo, "_request", return_value=[other]) as request:
            with self.assertRaisesRegex(ValueError, "uses this Periphery address"):
                self.komodo.register("lab", ADDRESS, PASSKEY)
            request.assert_called_once()
        other["name"] = "lab"
        with patch.object(self.komodo, "_request", side_effect=[
            [other], {"config": {"address": ADDRESS, "insecure_tls": False, "passkey": "different"}},
        ]) as request:
            with self.assertRaisesRegex(ValueError, "Could not verify"):
                self.komodo.register("lab", ADDRESS, PASSKEY)
            self.assertTrue(all(call.args[0] == "/read" for call in request.call_args_list))

    def test_only_secure_origins_and_valid_periphery_endpoints_are_accepted(self):
        for origin in ("http://localhost", "https://user@localhost", "https://localhost/path"):
            with self.subTest(origin=origin), self.assertRaisesRegex(ValueError, "HTTPS origin"):
                Komodo(origin, "key", "secret")
        self.assertEqual(periphery_address("2001:db8::1", 8120), "wss://[2001:db8::1]:8120")
        with self.assertRaisesRegex(ValueError, "Periphery port"):
            periphery_address("localhost", 0)

    def test_checkpoint_requires_ssh_passkey_and_version_and_stores_only_server_reference(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "inventory.sqlite3"
            device = Inventory(database).add("lab", "lab.local", "operator", 22, PROFILE)
            runs = RunStore(database)
            run = runs.open("lab")
            with (
                patch.object(self.komodo, "version", return_value="2.3.4"),
                patch.object(self.komodo, "register", return_value=(SERVER_ID, Ownership.CREATED)) as register,
                patch.object(self.komodo, "_request", side_effect=[
                    [{"name": "rpi", "_id": {"$oid": TAG_ID}}],
                    {"tags": []},
                    {},
                    {"tags": [TAG_ID]},
                ]) as request,
            ):
                for version, passkey, message in [
                    ("2.3.4", "", "passkey"),
                    ("2.3.5", PASSKEY, "version"),
                    ("2.3.4", PASSKEY, "SSH enrollment"),
                ]:
                    with self.assertRaisesRegex(ValueError, message):
                        register_device(self.komodo, device, ADDRESS, version, passkey, runs)
                register.assert_not_called()
                runs.complete_step(run["id"], "ssh")
                completed = register_device(self.komodo, device, ADDRESS, "2.3.4", PASSKEY, runs)
                self.assertEqual(completed["next_step"], "goku-files")
                self.assertEqual(completed["resources"][0]["reference"], SERVER_ID)
                self.assertNotIn(PASSKEY, json.dumps(completed))
                repeated = register_device(self.komodo, device, ADDRESS, "2.3.4", PASSKEY, runs)
                self.assertEqual(repeated, completed)
                register.assert_called_with("lab", ADDRESS, PASSKEY, allow_create=False)
                self.assertEqual(request.call_count, 4)
                self.assertEqual(request.call_args_list[2].args, (
                    "/write", "UpdateResourceMeta",
                    {"target": {"type": "Server", "id": SERVER_ID}, "tags": [TAG_ID]},
                ))

    def test_registration_retries_missing_profile_tag_on_the_recorded_server(self):
        other_tag = "bbbbbbbbbbbbbbbbbbbbbbbb"
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "inventory.sqlite3"
            device = Inventory(database).add("lab", "lab.local", "operator", 22, SERVER_PROFILE)
            runs = RunStore(database)
            run_id = runs.open("lab")["id"]
            runs.complete_step(run_id, "ssh")
            with (
                patch.object(self.komodo, "version", return_value="2.3.4"),
                patch.object(self.komodo, "register", side_effect=[
                    (SERVER_ID, Ownership.CREATED), (SERVER_ID, Ownership.REUSED),
                ]),
                patch.object(self.komodo, "_request", side_effect=[
                    [], {"_id": {"$oid": TAG_ID}}, {"tags": [other_tag]},
                    ValueError("temporary tag update failure"),
                    [{"name": "server", "_id": {"$oid": TAG_ID}}],
                    {"tags": [other_tag]}, {}, {"tags": [other_tag, TAG_ID]},
                ]) as request,
            ):
                with self.assertRaisesRegex(ValueError, "temporary tag update failure"):
                    register_device(self.komodo, device, ADDRESS, "2.3.4", PASSKEY, runs)
                pending = runs.show(run_id)
                self.assertEqual(pending["next_step"], "komodo-registration")
                self.assertEqual(pending["resources"][0]["ownership"], Ownership.CREATED)
                completed = register_device(self.komodo, device, ADDRESS, "2.3.4", PASSKEY, runs)
                self.assertEqual(completed["next_step"], "goku-files")
                self.assertEqual(len(completed["resources"]), 1)
                request.assert_any_call("/write", "CreateTag", {"name": "server"})
                request.assert_any_call("/write", "UpdateResourceMeta", {
                    "target": {"type": "Server", "id": SERVER_ID}, "tags": [other_tag, TAG_ID],
                })
