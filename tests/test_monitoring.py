import json
import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

import yaml

from onboardrr.inventory import PROFILE, Inventory
from onboardrr.monitoring import (
    Beszel, Monitoring, compose_source, configured_monitoring, monitoring_registration, monitoring_variables, retire_compose,
    variable_bindings, watcher_prefix,
)
from onboardrr.nas_monitoring import compose_payload
from onboardrr.remote.monitoring import PROMETHEUS_PATH, edit_prometheus, execute, prometheus_source, validate_target
from onboardrr.retirement import retire_baseline, retirement_plan


PORTS = (2380, 9100, 45876)
DEVICE = {"name": "lab-pi", "host": "192.168.1.42"}
ADDRESS = DEVICE["host"]
ADDRESS_KEY = "LAB_PI_IP"
VARIABLES = [
    {"name": ADDRESS_KEY, "value": ADDRESS},
    {"name": "DOCKER_API_PORT", "value": "2380"},
    {"name": "NODE_EXPORTER_PORT", "value": "9100"},
    {"name": "BESZEL_AGENT_PORT", "value": "45876"},
]
ENVIRONMENT = "DOCKER_API_PORT=[[DOCKER_API_PORT]]\n"
BOUND_ENVIRONMENT = ENVIRONMENT + "LAB_PI_IP=[[LAB_PI_IP]]\n"
REQUEST = {"name": DEVICE["name"], "address": ADDRESS, "ports": PORTS, "watcher": watcher_prefix(DEVICE["name"])}
COMPOSE = """
services:
  dozzle:
    environment:
      DOZZLE_REMOTE_HOST: tcp://${PEER_IP}:2380|peer # keep existing hosts
  wud:
    environment:
      WUD_WATCHER_PEER_HOST: ${PEER_IP}
      WUD_WATCHER_PEER_PORT: "2380"
    restart: always
  prometheus:
    image: prom/prometheus:v3
"""
PROMETHEUS = """global:
  scrape_interval: 15s
scrape_configs:
  - job_name: peer
    static_configs:
      - targets: ['192.168.1.90:9100'] # peer target
rule_files: []
"""
READY_HOST = {"beszel_agent": True, "docker_api": True, "node_exporter": True}
NEW = {**READY_HOST, "wud": False, "dozzle": False, "prometheus": False}
REGISTERED = dict.fromkeys(NEW, True)
IDENTIFIER = "1" * 24


def central_stack(contents, environment=ENVIRONMENT):
    return {"_id": {"$oid": IDENTIFIER},
            "config": {"file_contents": contents, "project_name": "monitoring", "environment": environment}}


class MonitoringTest(unittest.TestCase):
    def test_failed_prometheus_reload_restores_the_original_configuration(self):
        root = Path(self.enterContext(TemporaryDirectory()))
        original = "scrape_configs: []\n"
        desired = "scrape_configs: [{job_name: lab}]\n"
        temp = "/tmp/onboardrr-prometheus.fixture"
        writes = {}

        def docker(*args, data=None):
            if "mktemp" in args:
                return temp.encode()
            if "cat" in args:
                return original.encode()
            if data:
                writes.setdefault(args[-1], []).append(data.decode())
            return b""

        with (patch("onboardrr.remote.monitoring.private_state", return_value=root),
              patch("onboardrr.remote.monitoring.docker", side_effect=docker) as command,
              patch("onboardrr.remote.monitoring.reload_prometheus",
                    side_effect=[ValueError("Reload failed."), None]) as reload):
            with self.assertRaisesRegex(ValueError, "Reload failed"):
                edit_prometheus("hub", "lab", "192.168.1.42:9100", original, desired)
        self.assertEqual(writes, {temp: [desired], PROMETHEUS_PATH: [desired, original]})
        self.assertEqual([call.kwargs["present"] for call in reload.call_args_list], [True, False])
        self.assertIn("rm", command.call_args.args)
        backup, = root.glob("prometheus-*.yml")
        self.assertEqual(backup.read_text(), original)
        self.assertEqual(backup.stat().st_mode & 0o777, 0o600)

    def test_remote_wud_and_dozzle_read_their_deployed_endpoints_without_touching_prometheus(self):
        records = [
            {"Config": {"Env": [f"{REQUEST['watcher']}_HOST={ADDRESS}", f"{REQUEST['watcher']}_PORT=2380",
                                f"{REQUEST['watcher']}_CRON=0 * * * *"]}},
            {"Config": {"Env": [f"DOZZLE_REMOTE_HOST=tcp://{ADDRESS}:2380|lab-pi"]}},
        ]
        request = {**REQUEST, "project": "monitoring", "components": ["wud", "dozzle"]}
        with (patch("onboardrr.remote.monitoring.inspect_container", side_effect=records),
              patch("onboardrr.remote.monitoring.health", return_value=True) as health,
              patch("onboardrr.remote.monitoring.reconcile_prometheus") as prometheus):
            self.assertEqual(execute(request), {"docker_api": True, "wud": True, "dozzle": True})
        health.assert_called_once_with(ADDRESS, 2380, "/_ping", b"OK")
        prometheus.assert_not_called()

    @patch.dict(os.environ, {"ONBOARDRR_MONITORING_HOST": "core", "ONBOARDRR_BESZEL_URL": "https://beszel.example"})
    @patch("onboardrr.monitoring.Monitoring")
    @patch("onboardrr.monitoring.configured_openbao")
    @patch("onboardrr.monitoring.Beszel")
    def test_new_nas_uses_canonical_beszel_password_and_agent_key(self, beszel, openbao, monitoring):
        controller, services = Mock(), Mock()
        controller.fields_with_version.return_value = ({"beszel_identity": "test@example.org"}, 1)
        services.read.side_effect = [("fixture-password",), ("ssh-ed25519 AAAA",)]
        openbao.side_effect = [controller, services, services]
        monitoring.return_value.run.side_effect = (
            lambda _device, **kwargs: kwargs["provision"](ADDRESS, PORTS, ())
        )
        provision = Mock()
        with TemporaryDirectory() as directory:
            enroll = configured_monitoring(Mock(), Path(directory) / "inventory")
            enroll(DEVICE, provision=provision)
        self.assertEqual([call.args for call in services.read.call_args_list], [
            ("beszel/PASSWORD", "value"), ("beszel/AGENT_KEY", "value"),
        ])
        beszel.assert_called_once_with("https://beszel.example", "test@example.org", "fixture-password")
        payload, _ = provision.call_args.args
        self.assertEqual(json.loads(payload)["services"]["beszel-agent"]["environment"]["KEY"], "ssh-ed25519 AAAA")

    def test_compose_preserves_peers_and_comments_and_repeats_without_edits(self):
        desired, changed = compose_source(COMPOSE, REQUEST, ADDRESS_KEY)
        data = yaml.safe_load(desired)["services"]
        self.assertIn("# keep existing hosts", desired)
        self.assertEqual(data["wud"]["environment"]["WUD_WATCHER_PEER_HOST"], "${PEER_IP}")
        self.assertEqual(data["wud"]["environment"][REQUEST["watcher"] + "_HOST"], "${LAB_PI_IP}")
        self.assertEqual(data["wud"]["environment"][REQUEST["watcher"] + "_PORT"], "${DOCKER_API_PORT}")
        self.assertEqual(data["wud"]["environment"][REQUEST["watcher"] + "_CRON"], "0 */6 * * *")
        self.assertIn(REQUEST["watcher"] + "_CRON: 0 */6 * * *\n", desired)
        self.assertIn(REQUEST["watcher"] + "_HOST: ${LAB_PI_IP}\n", desired)
        self.assertIn("DOZZLE_REMOTE_HOST: tcp://${PEER_IP}:2380|peer,", desired)
        self.assertIn("tcp://${LAB_PI_IP}:${DOCKER_API_PORT}|lab-pi", data["dozzle"]["environment"]["DOZZLE_REMOTE_HOST"])
        self.assertEqual(data["prometheus"], yaml.safe_load(COMPOSE)["services"]["prometheus"])
        self.assertEqual(changed, {"wud", "dozzle"})
        self.assertEqual(compose_source(desired, REQUEST, ADDRESS_KEY), (desired, set()))
        with self.assertRaisesRegex(ValueError, "conflicts"):
            compose_source(desired, REQUEST, "OTHER_IP")

    def test_existing_wud_watcher_gets_a_schedule_without_changing_dozzle(self):
        desired, _ = compose_source(COMPOSE, REQUEST, ADDRESS_KEY)
        cron = f"      {REQUEST['watcher']}_CRON: 0 */6 * * *\n"
        self.assertEqual(compose_source(desired.replace(cron, ""), REQUEST, ADDRESS_KEY), (desired, {"wud"}))
        custom = desired.replace(cron, cron.replace("*/6", "*/12"))
        self.assertEqual(compose_source(custom, REQUEST, ADDRESS_KEY), (custom, set()))

    def test_retirement_checks_only_the_owned_wud_schedule_at_runtime(self):
        prefix = REQUEST["watcher"]
        env = [f"{prefix}_HOST={ADDRESS}", f"{prefix}_PORT=2380"]
        request = {**REQUEST, "project": "monitoring", "retire": ["wud-cron"]}
        runtime = {"Config": {"Env": env}}
        with patch("onboardrr.remote.monitoring.inspect_container", return_value=runtime):
            self.assertFalse(execute(request)["wud"])
            env.append(f"{prefix}_CRON=0 */6 * * *")
            self.assertTrue(execute(request)["wud"])

    def test_unquotes_existing_variables_without_redeploying_unchanged_services(self):
        desired, _ = compose_source(COMPOSE, REQUEST, ADDRESS_KEY)
        quoted = desired.replace(
            "_HOST: ${LAB_PI_IP}", '_HOST: "${LAB_PI_IP}"'
        ).replace(
            "_PORT: ${DOCKER_API_PORT}", '_PORT: "${DOCKER_API_PORT}"'
        ).replace(
            "DOZZLE_REMOTE_HOST: tcp", 'DOZZLE_REMOTE_HOST: "tcp'
        ).replace("|lab-pi #", '|lab-pi" #')
        self.assertEqual(compose_source(quoted, REQUEST, ADDRESS_KEY), (desired, set()))

    def test_live_literal_entries_migrate_and_unrelated_values_are_preserved(self):
        desired, _ = compose_source(COMPOSE, REQUEST, ADDRESS_KEY)
        current = desired.replace("${LAB_PI_IP}", ADDRESS).replace("${DOCKER_API_PORT}", "2380")
        self.assertEqual(compose_source(current, REQUEST, ADDRESS_KEY), (desired, {"wud", "dozzle"}))
        with self.assertRaisesRegex(ValueError, "conflicts"):
            compose_source(current, {**REQUEST, "address": "192.168.1.43"}, ADDRESS_KEY)

    def test_komodo_variables_supply_monitoring_endpoint_and_bindings(self):
        komodo = Mock()
        komodo._request.return_value = VARIABLES
        self.assertEqual(monitoring_variables(komodo, DEVICE["name"], None), (ADDRESS_KEY, ADDRESS, PORTS))
        with self.assertRaisesRegex(ValueError, "does not match"):
            monitoring_variables(komodo, DEVICE["name"], "192.168.1.43")
        komodo._request.return_value = VARIABLES[1:]
        with self.assertRaisesRegex(ValueError, "LAB_PI_IP"):
            monitoring_variables(komodo, DEVICE["name"], None)
        self.assertEqual(variable_bindings(ENVIRONMENT, ADDRESS_KEY, "DOCKER_API_PORT"), BOUND_ENVIRONMENT)
        with self.assertRaisesRegex(ValueError, "Bind LAB_PI_IP"):
            variable_bindings("LAB_PI_IP=192.168.1.43", ADDRESS_KEY)

    def test_monitoring_resolves_openbao_address_and_ports_for_published_aliases(self):
        komodo = Mock()
        hosts, services = Mock(), Mock()
        hosts.read.return_value = (ADDRESS,)
        services.read.side_effect = [("27350",), ("9100",), ("45876",)]
        komodo._request.side_effect = [[], [ADDRESS_KEY, *(item["name"] for item in VARIABLES[1:])]]
        with (
            patch.dict("os.environ", {"ONBOARDRR_BAO_URL": "https://bao.example"}),
            patch("onboardrr.monitoring.configured_openbao", side_effect=[hosts, services]),
        ):
            self.assertEqual(monitoring_variables(komodo, DEVICE["name"], None, device=DEVICE),
                             (ADDRESS_KEY, ADDRESS, (27350, 9100, 45876)))
        hosts.read.assert_called_once_with("lab-pi/IP", "value")
        self.assertEqual([call.args for call in services.read.call_args_list], [
            ("docker/API_PORT", "value"), ("node-exporter/PORT", "value"), ("beszel/AGENT_PORT", "value"),
        ])

    def test_monitoring_requires_a_komodo_alias_for_the_openbao_port(self):
        komodo = Mock()
        komodo._request.side_effect = [[VARIABLES[0], *VARIABLES[2:]], []]
        with (
            patch.dict("os.environ", {"ONBOARDRR_BAO_URL": "https://bao.example"}),
            patch("onboardrr.monitoring.configured_openbao") as openbao,
        ):
            with self.assertRaisesRegex(ValueError, "DOCKER_API_PORT"):
                monitoring_variables(komodo, DEVICE["name"], None, device=DEVICE)
        openbao.assert_not_called()

    def test_prometheus_adds_only_the_selected_job_and_refuses_an_existing_conflict(self):
        result = prometheus_source(PROMETHEUS, "lab-pi", f"{ADDRESS}:9100")
        self.assertIn("# peer target", result)
        data = yaml.safe_load(result)
        self.assertEqual(data["scrape_configs"][-1]["job_name"], "lab-pi")
        self.assertEqual(data["rule_files"], [])
        self.assertEqual(prometheus_source(result, "lab-pi", f"{ADDRESS}:9100"), result)
        with self.assertRaisesRegex(ValueError, "different targets"):
            prometheus_source(result, "lab-pi", "192.168.1.43:9100")

    def test_prometheus_removal_preserves_peers_and_detects_an_edited_job(self):
        target = f"{ADDRESS}:9100"
        desired = prometheus_source(PROMETHEUS, "lab-pi", target)
        self.assertEqual(prometheus_source(desired, "lab-pi", target, remove=True), PROMETHEUS)
        edited = desired.replace(f"- {target}", f"- {target}\n      - 192.168.1.99:9100")
        with self.assertRaisesRegex(ValueError, "differs"):
            prometheus_source(edited, "lab-pi", target, remove=True)
        empty = "scrape_configs: []\n"
        single = prometheus_source(empty, "lab-pi", target)
        self.assertEqual(yaml.safe_load(prometheus_source(single, "lab-pi", target, remove=True)),
                         yaml.safe_load(empty))

    def test_retirement_removes_only_owned_entries_and_preserves_a_binding_used_elsewhere(self):
        contents, _ = compose_source(COMPOSE, REQUEST, ADDRESS_KEY)
        contents += '  another:\n    environment:\n      HOST: ${LAB_PI_IP}\n'
        retired, environment, changed = retire_compose(
            contents, BOUND_ENVIRONMENT, REQUEST, ADDRESS_KEY, {"dozzle": True, "wud-host": True, "binding": True},
        )
        data = yaml.safe_load(retired)["services"]
        self.assertIn(REQUEST["watcher"] + "_PORT", data["wud"]["environment"])
        self.assertEqual(data["dozzle"]["environment"]["DOZZLE_REMOTE_HOST"], "tcp://${PEER_IP}:2380|peer")
        self.assertIn("# keep existing hosts", retired)
        self.assertEqual(environment, BOUND_ENVIRONMENT)
        self.assertEqual(changed, {"wud", "dozzle"})

    def test_only_explicit_legacy_retirement_removes_exact_literal_endpoints(self):
        contents, _ = compose_source(COMPOSE, REQUEST, ADDRESS_KEY)
        contents = contents.replace("${LAB_PI_IP}", ADDRESS).replace("${DOCKER_API_PORT}", "2380")
        owns = {"dozzle", "wud-host", "wud-port", "wud-cron"}
        with self.assertRaisesRegex(ValueError, "differs"):
            retire_compose(contents, ENVIRONMENT, REQUEST, ADDRESS_KEY, owns)
        retired, _, _ = retire_compose(contents, ENVIRONMENT, {**REQUEST, "legacy": True}, ADDRESS_KEY, owns)
        self.assertEqual(retired, COMPOSE)

    def test_enrollment_ownership_drives_retirement_even_when_the_device_is_offline(self):
        with TemporaryDirectory() as directory:
            database = Path(directory) / "inventory.sqlite3"
            device = Inventory(database).add("lab-pi", ADDRESS, "operator", 22, PROFILE)
            komodo, beszel = Mock(), Mock()
            beszel.service.host, beszel.service.port = "beszel.example", None
            beszel.enroll.return_value = "fixture"
            beszel.system.side_effect = [None, {"id": "fixture"}]
            monitoring = Monitoring(komodo, "hub.example", "zero-monitoring", beszel, database)
            desired, _ = compose_source(COMPOSE, REQUEST, ADDRESS_KEY)
            deployed = central_stack(desired, BOUND_ENVIRONMENT)
            cleaned, _, _ = retire_compose(desired, BOUND_ENVIRONMENT, REQUEST, ADDRESS_KEY,
                                           {"wud-host", "wud-port", "wud-cron", "binding", "dozzle"})
            komodo._request.side_effect = [
                VARIABLES, central_stack(COMPOSE), central_stack(COMPOSE), deployed, {"success": True},
                deployed, deployed, central_stack(cleaned), {"success": True},
            ]
            live = {**NEW, "prometheus_configured": False}
            retirement_before = {"wud": True, "dozzle": True, "prometheus": False}
            retirement_after = {"wud": False, "dozzle": False, "prometheus": True}
            with (
                patch.object(monitoring, "hub", side_effect=[live, REGISTERED, retirement_before, retirement_after]),
                patch("onboardrr.monitoring.wait_update"),
            ):
                monitoring.run(device, apply=True)
                receipt = monitoring_registration(database, device["id"])
                self.assertEqual(receipt["owned"], {"binding": True, "wud-host": True, "wud-port": True, "dozzle": True,
                                                   "wud-cron": True, "beszel": "fixture", "prometheus": True})
                self.assertEqual(retirement_plan(database, "lab-pi")["monitoring"]["status"], "pending")
                outcome = retire_baseline(database, device, komodo, monitoring=monitoring.retire)
                self.assertEqual(outcome["monitoring"]["status"], "removed")
                self.assertIsNone(monitoring_registration(database, device["id"]))
                self.assertNotIn("monitoring", retire_baseline(database, device, komodo, monitoring=monitoring.retire))
            beszel.retire.assert_called_once_with("fixture", "lab-pi", ADDRESS, 45876)

    def test_retirement_retries_after_compose_was_saved_but_reload_failed(self):
        with TemporaryDirectory() as directory:
            database = Path(directory) / "inventory.sqlite3"
            device = Inventory(database).add("lab-pi", ADDRESS, "operator", 22, PROFILE)
            komodo, beszel = Mock(), Mock()
            beszel.service.host, beszel.service.port = "beszel.example", None
            monitoring = Monitoring(komodo, "hub.example", "zero-monitoring", beszel, database)
            stack = central_stack(COMPOSE)
            receipt = {"scope": monitoring.scope(stack), "address": ADDRESS, "ports": list(PORTS),
                       "owned": {"wud-host": True, "wud-port": True, "dozzle": True}}
            monitoring.save(device, receipt)
            komodo._request.side_effect = [stack, {"success": True}, stack, {"success": True}]
            active = {"wud": True, "dozzle": True}
            stopped = {"wud": False, "dozzle": False}
            with (
                patch.object(monitoring, "hub", side_effect=[active, active, stopped]),
                patch("onboardrr.monitoring.wait_update", side_effect=[ValueError("deployment failed"), None]),
            ):
                with self.assertRaisesRegex(ValueError, "deployment failed"):
                    retire_baseline(database, device, komodo, monitoring=monitoring.retire)
                self.assertEqual(monitoring_registration(database, device["id"]), receipt)
                self.assertEqual(retire_baseline(database, device, komodo, monitoring=monitoring.retire)
                                 ["monitoring"]["status"], "removed")
            self.assertTrue(all(call.args[0] != "/write" for call in komodo._request.call_args_list))

    def test_apply_updates_only_wud_and_dozzle_and_reuses_an_existing_registration(self):
        komodo = Mock()
        beszel = Mock()
        monitoring = Monitoring(komodo, "hub.example", "zero-monitoring", beszel)
        desired, _ = compose_source(COMPOSE, REQUEST, ADDRESS_KEY)
        komodo._request.side_effect = [
            VARIABLES, central_stack(COMPOSE), central_stack(COMPOSE),
            central_stack(desired, BOUND_ENVIRONMENT), {"success": True},
            VARIABLES, central_stack(desired, BOUND_ENVIRONMENT),
        ]
        beszel.system.side_effect = [None, {"id": "record"}, {"id": "record"}, {"id": "record"}]
        with (
            patch.object(monitoring, "hub", side_effect=[NEW, REGISTERED, REGISTERED, REGISTERED]) as hub,
            patch("onboardrr.monitoring.wait_update") as wait,
        ):
            self.assertEqual(monitoring.run(DEVICE, apply=True)["status"], "enrolled")
            self.assertEqual(monitoring.run(DEVICE, apply=True)["checks"], {**REGISTERED, "beszel": True})
        calls = [call.args for call in komodo._request.call_args_list if call.args[0] != "/read"]
        self.assertEqual(calls, [
            ("/write", "UpdateStack",
             {"id": IDENTIFIER, "config": {"file_contents": desired, "environment": BOUND_ENVIRONMENT}}),
            ("/execute", "DeployStack", {"stack": IDENTIFIER, "services": ["wud", "dozzle"]}),
        ])
        self.assertTrue(hub.call_args_list[1].kwargs["apply"])
        self.assertFalse(hub.call_args_list[-1].kwargs["apply"])
        beszel.enroll.assert_called_once_with("lab-pi", ADDRESS, 45876)
        wait.assert_called_once()

    def test_wud_can_enroll_and_retire_independently_of_beszel_and_dozzle(self):
        with TemporaryDirectory() as directory:
            database = Path(directory) / "inventory.sqlite3"
            device = Inventory(database).add("lab-pi", ADDRESS, "operator", 22, PROFILE)
            komodo = Mock()
            monitoring = Monitoring(komodo, "hub.example", "zero-monitoring", None, database)
            wud_only = COMPOSE[COMPOSE.index("  wud:"):COMPOSE.index("  prometheus:")]
            contents = "services:\n" + wud_only
            desired, _ = compose_source(contents, REQUEST, ADDRESS_KEY, ("wud",))
            stack = central_stack(contents)
            applied = central_stack(desired, BOUND_ENVIRONMENT)
            full = central_stack(compose_source(COMPOSE, REQUEST, ADDRESS_KEY)[0], BOUND_ENVIRONMENT)
            retired = central_stack(compose_source(COMPOSE, REQUEST, ADDRESS_KEY, ("dozzle",))[0], BOUND_ENVIRONMENT)
            komodo._request.side_effect = [
                VARIABLES[:2], stack, stack, applied, {"success": True},
                full, full, retired, {"success": True},
            ]
            with (
                patch.object(monitoring, "hub", side_effect=[
                    {"docker_api": True, "wud": False}, {"docker_api": True, "wud": True},
                    {"wud": True}, {"wud": False},
                ]) as hub,
                patch("onboardrr.monitoring.wait_update"),
            ):
                self.assertEqual(monitoring.run(device, apply=True, component="wud")["checks"],
                                 {"docker_api": True, "wud": True})
                receipt = monitoring_registration(database, device["id"])
                self.assertEqual(receipt["ports"], [2380, None, None])
                receipt["owned"]["dozzle"] = True
                monitoring.save(device, receipt)
                monitoring.retire(device, receipt, component="wud")
            self.assertEqual(monitoring_registration(database, device["id"])["owned"],
                             {"dozzle": True, "binding": True})
            self.assertTrue(all(call.args[0]["components"] == ["wud"] for call in hub.call_args_list))
            self.assertEqual([call.args[2]["services"] for call in komodo._request.call_args_list
                              if call.args[:2] == ("/execute", "DeployStack")], [["wud"], ["wud"]])

    def test_previews_are_read_only_and_missing_host_services_block_apply(self):
        komodo = Mock()
        beszel = Mock()
        monitoring = Monitoring(komodo, "hub.example", "zero-monitoring", beszel)
        komodo._request.side_effect = [VARIABLES, central_stack(COMPOSE)] * 2
        beszel.system.return_value = None
        with patch.object(monitoring, "hub", return_value={**NEW, "docker_api": False}) as hub:
            self.assertFalse(monitoring.run(DEVICE)["endpoints_ready"])
            with self.assertRaisesRegex(ValueError, "cannot reach"):
                monitoring.run(DEVICE, apply=True)
        self.assertTrue(all(call.args[0] == "/read" for call in komodo._request.call_args_list))
        self.assertTrue(all(not call.kwargs for call in hub.call_args_list))
        beszel.enroll.assert_not_called()

    def test_nas_compose_limits_docker_to_read_operations_and_binds_the_lan_address(self):
        model = json.loads(compose_payload(ADDRESS, PORTS, "ssh-ed25519 AAAATEST fixture"))
        proxy = model["services"]["docker-telemetry"]
        self.assertEqual(proxy["ports"], [f"{ADDRESS}:2380:2375"])
        self.assertEqual(proxy["environment"]["POST"], "0")
        self.assertEqual(model["services"]["beszel-agent"]["environment"]["PORT"], "45876")
        with self.assertRaisesRegex(ValueError, "private LAN"):
            validate_target("lab-pi", "8.8.8.8", PORTS)

    def test_beszel_registration_sends_ownership_and_checks_the_exact_record(self):
        record = {"id": "fixture", "name": "lab-pi", "host": ADDRESS, "port": "45876", "users": ["operator"]}
        with patch("onboardrr.monitoring.JsonService") as api:
            request = api.return_value.request
            request.side_effect = [
                {"record": {"id": "operator"}, "token": "fixture"},
                {"id": "fixture"},
                {"items": [record], "totalItems": 1},
            ]
            Beszel("https://beszel.example", "operator", "fixture-password").enroll("lab-pi", ADDRESS, 45876)
        self.assertEqual(request.call_args_list[1].kwargs["data"]["users"], ["operator"])

    def test_beszel_retirement_does_not_delete_a_shared_record(self):
        with patch("onboardrr.monitoring.JsonService") as api:
            request = api.return_value.request
            request.return_value = {"record": {"id": "operator"}, "token": "fixture"}
            beszel = Beszel("https://beszel.example", "operator", "fixture-password")
            with patch.object(beszel, "system", return_value={"id": "fixture", "users": ["operator", "peer"]}):
                with self.assertRaisesRegex(ValueError, "shared"):
                    beszel.retire("fixture", "lab-pi", ADDRESS, 45876)
            with patch.object(beszel, "system", side_effect=[{"id": "fixture", "users": ["operator"]}, None]):
                beszel.retire("fixture", "lab-pi", ADDRESS, 45876)
            self.assertEqual([call.args[:2] for call in request.call_args_list if call.args[0] == "DELETE"],
                             [("DELETE", "/api/collections/systems/records/fixture")])
