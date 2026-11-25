import json
import shutil
import tempfile
import unittest
from base64 import b64encode
from contextlib import nullcontext
from pathlib import Path
from unittest.mock import Mock, patch

from onboardrr.inventory import Inventory, PROFILE
from onboardrr.outbound import recover_outbound
from onboardrr.remote import reconcile as worker
from onboardrr.runs import Ownership, Resource, ResourceKind, RunStore


def fixture_key(byte):
    return b64encode(bytes.fromhex("302a300506032b656e032100") + bytes([byte]) * 32).decode()


class OutboundRecoveryTest(unittest.TestCase):
    def setUp(self):
        directory = Path(self.enterContext(tempfile.TemporaryDirectory()))
        database = directory / "onboardrr.sqlite"
        self.device = Inventory(database).add("lab", "lab.local", "operator", 22, PROFILE)
        self.runs = RunStore(database)
        self.run = self.runs.open("lab")["id"]
        self.runs.complete_step(self.run, "ssh")
        self.identifier = "a" * 24
        self.core = Mock()
        self.core.version.return_value = "2.3.4"
        self.core.service.host = "komodo.example.org"
        self.core.service.port = None
        self.key = fixture_key(2)
        self.configured_key = fixture_key(1)
        self.server_config = {"address": "", "enabled": True, "insecure_tls": False}
        self.running = False
        self.local_key = None
        self.owned_network = None
        self.requests = []
        self.fail_start = False
        self.core._request.side_effect = self.request
        self.enterContext(patch("onboardrr.ssh.load_public_key"))
        self.enterContext(patch("onboardrr.ssh.agent_identity", side_effect=lambda key: nullcontext()))
        self.enterContext(patch("onboardrr.ssh.connect", side_effect=lambda *a, **k: nullcontext()))
        self.enterContext(patch("onboardrr.ssh.script", side_effect=self.remote))
        self.enterContext(patch("onboardrr.outbound.kuma_on_host", return_value=False))
        self.enterContext(patch("onboardrr.outbound.kuma_tcp_values", return_value={
            "HOST_IP": "192.0.2.10", "DOCKER_API_PORT": "2380",
        }))

    def request(self, route, action, params=None):
        self.requests.append((route, action, params))
        if action == "GetServer":
            return {"_id": {"$oid": self.identifier}, "name": "lab", "template": False,
                    "config": self.server_config.copy(),
                    "info": {"public_key": self.configured_key}}
        if action == "GetCoreInfo":
            return {"public_key": fixture_key(3)}
        if action == "GetServerState":
            return {"status": "Ok" if self.running else "NotOk"}
        if action == "GetPeripheryInformation":
            return {"public_key": self.local_key, "version": "2.3.4"}
        if action == "UpdateServerPublicKey":
            self.assertEqual(params, {"server": self.identifier, "public_key": self.key})
            self.configured_key = params["public_key"]
            return {}
        if action == "UpdateServer":
            self.assertEqual(params, {"id": self.identifier, "config": {"address": "", "enabled": True}})
            self.server_config.update(params["config"])
            return {}
        self.fail(action)

    def remote(self, client, script, payload, *, timeout):
        message = json.loads(payload)
        action = message["action"]
        self.requests.append(("ssh", action, None))
        if action == "stage":
            self.local_key = self.key
        if action == "start":
            if self.fail_start:
                raise ValueError("Fixture failed to start")
            self.assertEqual(message["agent_public_key"], self.configured_key)
            self.running = True
        return json.dumps({"public_key": self.local_key, "running": self.running,
                           "compose_mount": "/home/operator/.docker/compose", "owned_network": self.owned_network})

    def recover(self, **options):
        return recover_outbound(self.device, self.runs, self.core, server_id=self.identifier,
                                known_hosts=None, public_key=None, expected_version="2.3.4",
                                cifs_source="//goku/compose", nfs_source="goku:/lab", **options)

    def test_missing_key_requires_approval_then_resumes_after_start_failure(self):
        self.assertTrue(self.recover()["requires_key_replacement"])
        with self.assertRaisesRegex(ValueError, "--replace-agent-key"):
            self.recover(apply=True)
        self.assertNotIn(("ssh", "stage", None), self.requests)
        self.fail_start = True
        with self.assertRaisesRegex(ValueError, "Fixture failed to start"):
            self.recover(apply=True, replace_agent_key=True)
        self.assertEqual(self.runs.show(self.run)["next_step"], "komodo-registration")
        self.fail_start = False
        self.assertEqual(self.recover(apply=True)["next_step"], "komodo-stacks")
        self.assertEqual(self.recover(apply=True)["next_step"], "komodo-stacks")
        self.assertEqual(sum(route == "/write" for route, _, _ in self.requests), 1)
        resources = self.runs.show(self.run)["resources"]
        self.assertIn(self.identifier, {item["reference"] for item in resources})
        self.assertTrue(all(item["ownership"] == "reused" for item in resources))

    def test_historical_registration_is_reused_without_restarting_active_agent(self):
        self.runs.record_resource(self.run, "komodo-registration", Resource(
            ResourceKind.KOMODO_SERVER, self.identifier, Ownership.CREATED,
        ))
        self.running = True
        self.local_key = self.configured_key
        report = recover_outbound(self.device, self.runs, self.core, apply=True,
                                  known_hosts=None, public_key=None, expected_version="2.3.4",
                                  cifs_source="//goku/compose", nfs_source="goku:/lab")
        self.assertEqual(report["next_step"], "komodo-stacks")
        self.assertEqual([action for route, action, _ in self.requests if route == "ssh"], ["inspect"])
        self.assertFalse(any(route == "/write" for route, _, _ in self.requests))

    def test_key_cannot_be_replaced_on_online_registration(self):
        self.running = True
        self.local_key = self.key
        with self.assertRaisesRegex(ValueError, "offline"):
            self.recover(apply=True, replace_agent_key=True)
        self.assertNotIn(("ssh", "stage", None), self.requests)
        self.assertFalse(any(route == "/write" for route, _, _ in self.requests))

    def test_recovery_records_verified_network_by_full_id(self):
        self.running = True
        self.local_key = self.configured_key
        self.owned_network = "a" * 64
        self.recover(apply=True)
        network, = (item for item in self.runs.show(self.run)["resources"] if item["kind"] == "docker-network")
        self.assertEqual((network["reference"], network["ownership"]), (f"lab/{self.owned_network}", "created"))

    def prepare_bootstrap(self, ownership=Ownership.CREATED):
        self.runs.record_resource(self.run, "komodo-registration", Resource(
            ResourceKind.KOMODO_SERVER, self.identifier, ownership,
        ))
        for stage in ("komodo-registration", "goku-files", "linux"):
            self.runs.complete_step(self.run, stage)
        self.server_config.update(address="wss://lab.local:8120", enabled=False)
        self.configured_key = None

    def test_bootstrap_pins_key_before_enabling_and_resumes_without_rotating(self):
        self.prepare_bootstrap()
        self.assertFalse(self.recover(bootstrap=True)["requires_key_replacement"])
        self.assertFalse(any(route == "/write" for route, _, _ in self.requests))
        self.fail_start = True
        with self.assertRaisesRegex(ValueError, "Fixture failed to start"):
            self.recover(bootstrap=True, apply=True)
        self.assertEqual(self.runs.show(self.run)["next_step"], "periphery")
        self.fail_start = False
        self.assertEqual(self.recover(bootstrap=True, apply=True)["next_step"], "komodo-stacks")
        self.assertEqual([action for route, action, _ in self.requests if route == "/write"],
                         ["UpdateServerPublicKey", "UpdateServer"])

    def test_bootstrap_refuses_reused_registration(self):
        self.prepare_bootstrap(Ownership.REUSED)
        with self.assertRaisesRegex(ValueError, "server created by this run"):
            self.recover(bootstrap=True, apply=True)
        self.assertFalse(any(route == "/write" for route, _, _ in self.requests))

    def test_bootstrap_does_not_replace_an_existing_mismatched_key(self):
        self.prepare_bootstrap()
        self.configured_key = fixture_key(1)
        with self.assertRaisesRegex(ValueError, "does not match the device"):
            self.recover(bootstrap=True, apply=True)
        self.assertFalse(any(route == "/write" for route, _, _ in self.requests))


class OutboundWorkerTest(unittest.TestCase):
    @unittest.skipUnless(shutil.which("openssl"), "OpenSSL is required to test X25519 key generation")
    def test_worker_stages_private_key_once_and_refuses_untrusted_mounts(self):
        home = Path(self.enterContext(tempfile.TemporaryDirectory()))
        request = {"action": "stage", "device": "lab", "version": "2.3.4",
                   "device_id": "fixture-device-id",
                   "core_url": "https://komodo.example.org", "core_public_key": fixture_key(3),
                   "docker_endpoint": "http://192.0.2.10:2380",
                   "cifs_source": "//goku/compose", "nfs_source": "goku:/lab"}
        with patch.object(worker.Path, "home", return_value=home), patch.object(worker, "mount_matches") as mount:
            mount.return_value = False
            with self.assertRaisesRegex(ValueError, "active and persistent"):
                worker.recover_outbound(request)
            mount.return_value = True
            with patch.object(worker, "output", return_value=""):
                first = worker.recover_outbound(request)
                second = worker.recover_outbound(request)
                worker.recover_outbound({**request, "kuma_local": True})
        self.assertEqual(first["public_key"], second["public_key"])
        root = home / ".config" / "onboardrr" / "periphery"
        self.assertEqual((root / "keys" / "periphery.key").stat().st_mode & 0o777, 0o600)
        compose = home / ".docker/compose/compose.komodo.yml"
        self.assertEqual(compose.stat().st_mode & 0o777, 0o600)
        self.assertFalse((root / "compose.yaml").exists())
        labels = json.loads(compose.read_text())["services"]["komodo-agent"]["labels"]
        self.assertEqual(labels["docker.stack"], "komodo")
        self.assertEqual(labels["homepage.group"], "Docker lab")
        self.assertEqual(labels["kuma.__docker"], '"lab", "socket", "/var/run/docker.sock"')
        self.assertEqual(worker.periphery_labels("zero", request["core_url"], request["docker_endpoint"])["kuma.__docker"],
                         '"zero", "tcp", "http://192.0.2.10:2380"')
        self.assertEqual(labels["wud.tag.include"], r"^\d+\.\d+\.\d+$$")
