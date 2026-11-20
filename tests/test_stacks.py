import json
import socket
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

from onboardrr.http_json import HttpStatusError
from onboardrr.runs import Ownership, Resource, ResourceKind
from onboardrr.stacks import (
    apply_baseline, baseline_address, deploy_stack, ensure_host_value, instance_config, preview_baseline,
    publish_hostname, publish_host_variable, wait_for_host_aliases,
    template_requirements,
)


class BaselineStacksTest(unittest.TestCase):
    def test_hostname_comes_from_the_stack_environment(self):
        template = {"file_contents": "name: ${HOSTNAME}\n", "environment": "HOSTNAME=[[_HOSTNAME]]\n"}
        result = instance_config("monitoring", template, "lab-pi", "server", {})
        self.assertEqual(result["environment"], "HOSTNAME=[[LAB_PI_HOSTNAME]]\n")
        self.assertEqual(result["compose_cmd_wrapper"], "env -u HOSTNAME [[COMPOSE_COMMAND]]")
        self.assertIn("config", result["compose_cmd_wrapper_include"])
        self.assertIn("up", result["compose_cmd_wrapper_include"])

    def test_preview_checks_readiness_and_references_without_exposing_values(self):
        api = Mock()
        api._request.side_effect = [
            [{"name": "lab-pi", "id": "server", "info": {"state": "Disabled"}}],
            [
                {"name": "lab-pi-docker", "info": {"server_id": "server"}},
                {"name": "stack-monitoring", "id": "monitoring-template", "template": True},
                {"name": "stack-network", "id": "network-template", "template": True},
            ],
            [{"name": "DOMAIN", "value": "fixture-secret-value"}],
            ["BESZEL_AGENT_KEY"],
            {"config": {"file_contents": "${HOST_IP} ${DOCKER_HOSTNAME} ${KEY} ${LOG_LEVEL:-info} $${ESCAPED}",
                        "environment": "DOCKER_HOSTNAME=[[_HOSTNAME]]\nKEY=[[BESZEL_AGENT_KEY]]"}},
            {"config": {"file_contents": "${NEWT_ID} ${NEWT_SECRET}",
                        "environment": "NEWT_ID=fixture\nNEWT_SECRET=fixture"}},
        ]
        result = preview_baseline(api, {"name": "lab-pi"}, primary_server="zero")
        docker, monitoring, network = result["stacks"]
        self.assertEqual(docker["action"], "reuse")
        self.assertEqual(monitoring["missing_environment_keys"], ["HOST_IP"])
        self.assertEqual(monitoring["missing_variables"], ["LAB_PI_HOSTNAME", "LAB_PI_IP"])
        self.assertEqual(network["missing_variables"], ["LAB_PI_NEWT_ID", "LAB_PI_NEWT_SECRET"])
        self.assertTrue(result["blockers"])
        self.assertNotIn("fixture-secret-value", json.dumps(result))
        self.assertTrue(all(call.args[0] == "/read" for call in api._request.call_args_list))

    def test_primary_server_skips_komodo(self):
        api = Mock()
        result = preview_baseline(api, {"name": "main"}, primary_server="main")
        self.assertEqual(result["status"], "skipped")
        api._request.assert_not_called()

    def test_conflicting_stack_is_not_reused(self):
        api = Mock()
        api._request.side_effect = [
            [{"name": "lab", "id": "expected", "info": {"state": "Ok"}}],
            [{"name": "lab-docker", "info": {"server_id": "other"}}],
            [],
            [],
        ]
        result = preview_baseline(api, {"name": "lab"}, primary_server="zero")
        self.assertEqual([s["action"] for s in result["stacks"]], ["conflict", "blocked", "blocked"])

    def test_rejects_templates_that_use_host_files(self):
        with self.assertRaisesRegex(ValueError, "inline Compose"):
            template_requirements({"file_contents": "services: {}", "files_on_host": True}, "LAB")

    def test_docker_requirements_use_the_device_ip_for_the_added_lan_binding(self):
        requirements = template_requirements({
            "file_contents": "ports:\n  - ${DOCKER_API_PORT}:${DOCKER_API_INTERNAL_PORT}\n",
            "environment": "HOST_IP=[[_IP]]\nDOCKER_API_PORT=[[PORT]]\nDOCKER_API_INTERNAL_PORT=2375\n",
        }, "LAB", kind="docker")
        self.assertEqual(requirements["variables"], ["LAB_IP", "PORT"])

    def test_hostname_publication_reuses_the_vault_entry_and_an_existing_komodo_variable(self):
        api, bao = Mock(), Mock()
        bao.read.return_value = ("lab",)
        self.assertFalse(publish_hostname(api, bao, "lab", {"LAB_HOSTNAME": "lab"}, set()))
        bao.read.assert_called_once_with("lab/HOSTNAME", "value")
        bao.create.assert_not_called()
        api._request.assert_not_called()

    def test_hostname_publication_refuses_a_mismatched_vault_entry(self):
        api, bao = Mock(), Mock()
        bao.read.return_value = ("other",)
        with self.assertRaisesRegex(ValueError, "must match the device's inventory name"):
            publish_hostname(api, bao, "lab", {}, set())
        bao.create.assert_not_called()
        api._request.assert_not_called()

    def test_core_ip_secret_is_validated_against_the_host_vault_entry(self):
        api, bao = Mock(), Mock()
        bao.read.return_value = ("192.168.1.4",)
        self.assertEqual(baseline_address({"name": "lab"}, bao, {}, {"LAB_IP"}), "192.168.1.4")
        bao.read.assert_called_once_with("lab/IP", "value")
        bao.create.assert_not_called()
        self.assertFalse(publish_host_variable(api, "LAB_IP", "192.168.1.4", {}, {"LAB_IP"}))
        api._request.assert_not_called()
        with self.assertRaisesRegex(ValueError, "differs from the stored OpenBao address"):
            baseline_address({"name": "lab"}, bao, {"LAB_IP": "192.168.1.8"}, set())

    @patch("onboardrr.stacks.sleep")
    def test_waits_for_both_host_aliases_without_writing_komodo_variables(self, _sleep):
        api = Mock()
        api._request.side_effect = [["LAB_PI_IP"], ["LAB_PI_IP", "LAB_PI_HOSTNAME"]]
        wait_for_host_aliases(api, "lab-pi", set())
        self.assertEqual([call.args for call in api._request.call_args_list], [
            ("/read", "ListSecrets"), ("/read", "ListSecrets"),
        ])

    def test_new_ip_comes_from_the_inventory_target_and_must_be_private(self):
        bao = Mock()
        bao.read.side_effect = HttpStatusError("OpenBao", "KV read", 404)
        self.assertEqual(baseline_address({"name": "lab", "host": "10.0.0.8"}, bao, {}, set()), "10.0.0.8")
        with self.assertRaisesRegex(ValueError, "private LAN IPv4"):
            baseline_address({"name": "lab", "host": "203.0.113.8"}, bao, {}, set())
        with self.assertRaisesRegex(ValueError, "validate the configured Komodo secret"):
            baseline_address({"name": "lab", "host": "10.0.0.8"}, bao, {}, {"LAB_IP"})

    def test_openbao_ip_write_rejects_a_hostname_or_a_number(self):
        bao = Mock()
        for value in ("lab.local", 167772168):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, "literal LAN IPv4"):
                ensure_host_value(bao, "lab", "IP", value)
        bao.create.assert_not_called()

    def test_legacy_host_ip_can_supply_the_canonical_address(self):
        bao = Mock()
        bao.read.side_effect = [HttpStatusError("OpenBao", "KV read", 404), ("10.0.0.8",)]
        self.assertEqual(baseline_address({"name": "lab"}, bao, {}, {"LAB_IP"}), "10.0.0.8")
        self.assertEqual([call.args for call in bao.read.call_args_list], [("lab/IP", "value"), ("lab/HOST_IP", "value")])

    @patch("onboardrr.image_versions.docker_hub_tags")
    @patch("onboardrr.host_resolution.socket.getaddrinfo")
    def test_apply_creates_missing_stack_and_resumes_running_instances(self, resolve, tags):
        tags.return_value = [
            {"name": "1.9.0", "images": [{"os": "linux", "architecture": "arm64"}]},
            {"name": "1.10.0", "images": [{"os": "linux", "architecture": "arm64"}]},
            {"name": "1.11.0", "images": [{"os": "linux", "architecture": "amd64"}]},
        ]
        device = {"name": "lab-pi", "id": "device", "host": "lab-pi.local"}
        resolve.return_value = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("192.168.1.146", 0))]
        server_id, docker_id, monitoring_id, network_id, update_id, docker_tag, shared_tag = (
            digit * 24 for digit in "1234567"
        )

        def listed(kind, identifier, state="running"):
            return {
                "name": f"lab-pi-{kind}", "id": identifier,
                "info": {"server_id": server_id, "state": state},
            }

        api = Mock()
        api._request.side_effect = [
            [{"name": "lab-pi", "id": server_id, "info": {"state": "Ok"}}],
            [
                listed("monitoring", monitoring_id),
                listed("network", network_id),
                {"name": "stack-docker", "template": True, "id": "template", "tags": [docker_tag, shared_tag]},
            ],
            [
                {"name": "DOCKER_API_PORT"},
                {"name": "DOCKER_API_INTERNAL_PORT"},
            ],
            [],
            {"config": {
                "file_contents": "services:\n  proxy:\n    image: alpine/socat:1.8.0\n"
                                 "    ports:\n      - ${DOCKER_API_PORT}:${DOCKER_API_INTERNAL_PORT}\n"
                                 "    hostname: ${DOCKER_HOSTNAME}\n"
                                 "    labels:\n      - 'wud.tag.include=^1\\.\\d+\\.\\d+$$'\n",
                "environment": "DOCKER_HOSTNAME=[[_HOSTNAME]]\n"
                               "DOCKER_API_PORT=[[DOCKER_API_PORT]]\n"
                               "DOCKER_API_INTERNAL_PORT=[[DOCKER_API_INTERNAL_PORT]]\n",
            }},
            {"cpu_arch": "aarch64"},
            {"name": "LAB_PI_HOSTNAME", "value": "lab-pi"},
            {"name": "LAB_PI_IP", "value": "192.168.1.146"},
            {"_id": {"$oid": docker_id}},
            {"tags": [shared_tag]},
            {},
            {"tags": [shared_tag, docker_tag]},
            {"_id": {"$oid": update_id}, "status": "Complete", "success": True},
            [listed("docker", docker_id)],
        ]
        with TemporaryDirectory() as tmp:
            runs = Mock(database=Path(tmp) / "inventory.sqlite3")
            runs.list.return_value = [{"id": "run", "next_step": "komodo-stacks"}]
            runs.show.return_value = {"id": "run", "resources": [
                {"kind": "komodo-server", "reference": server_id, "step": "komodo-registration"},
                {"kind": "komodo-stack", "reference": monitoring_id, "step": "komodo-stacks", "ownership": "created"},
            ]}
            bao = Mock()
            bao.read.side_effect = HttpStatusError("OpenBao", "KV read", 404)
            result = apply_baseline(api, device, primary_server="zero", runs=runs, host_bao=bao)

        self.assertEqual(result["status"], "applied")
        self.assertEqual([call.args for call in bao.create.call_args_list], [
            ("lab-pi/HOSTNAME", {"value": "lab-pi"}),
            ("lab-pi/IP", {"value": "192.168.1.146"}),
        ])
        runs.record_resource.assert_any_call(
            "run", "komodo-stacks", Resource(ResourceKind.KOMODO_VARIABLE, "LAB_PI_HOSTNAME", Ownership.CREATED),
        )
        runs.record_resource.assert_any_call(
            "run", "komodo-stacks", Resource(ResourceKind.KOMODO_VARIABLE, "LAB_PI_IP", Ownership.CREATED),
        )
        writes = [call.args for call in api._request.call_args_list if call.args[0] != "/read"]
        self.assertEqual(writes.pop(0), ("/write", "CreateVariable", {"name": "LAB_PI_HOSTNAME", "value": "lab-pi"}))
        self.assertEqual(writes.pop(0), ("/write", "CreateVariable", {"name": "LAB_PI_IP", "value": "192.168.1.146"}))
        self.assertEqual(len(writes), 3)
        self.assertEqual(writes[0][:2], ("/write", "CreateStack"))
        config = writes[0][2]["config"]
        self.assertIn('"${HOST_IP}:${DOCKER_API_PORT}:${DOCKER_API_INTERNAL_PORT}"', config["file_contents"])
        self.assertIn("image: alpine/socat:1.10.0", config["file_contents"])
        self.assertEqual(result["stacks"][0]["image_updates"]["proxy"]["to"], "alpine/socat:1.10.0")
        tags.assert_called_once_with("alpine/socat")
        self.assertIn("HOST_IP=[[LAB_PI_IP]]", config["environment"])
        self.assertIn("HOSTNAME=[[LAB_PI_HOSTNAME]]", config["environment"])
        self.assertNotIn("DOCKER_HOSTNAME", config["file_contents"] + config["environment"])
        self.assertEqual(writes[1], ("/write", "UpdateResourceMeta", {
            "target": {"type": "Stack", "id": docker_id}, "tags": [shared_tag, docker_tag],
        }))
        self.assertEqual(writes[2], ("/execute", "DeployStack", {"stack": docker_id}))
        for identifier, ownership in (
            (docker_id, Ownership.CREATED), (monitoring_id, Ownership.CREATED), (network_id, Ownership.REUSED)
        ):
            runs.record_resource.assert_any_call("run", "komodo-stacks", Resource(ResourceKind.KOMODO_STACK, identifier, ownership))
        runs.complete_step.assert_called_once_with("run", "komodo-stacks")

    def test_apply_rejects_newt_secret_stored_as_plain_variable_before_writes(self):
        device = {"name": "lab", "id": "device"}
        api = Mock()
        api._request.side_effect = [
            [{"name": "lab", "id": "server", "info": {"state": "Ok"}}],
            [
                {"name": "lab-docker", "info": {"server_id": "server", "state": "running"}},
                {"name": "lab-monitoring", "info": {"server_id": "server", "state": "running"}},
                {"name": "stack-network", "template": True, "id": "template"},
            ],
            [{"name": "LAB_NEWT_ID"}, {"name": "LAB_NEWT_SECRET"}],
            [],
            {"config": {"file_contents": "${NEWT_ID} ${NEWT_SECRET}"}},
        ]
        with TemporaryDirectory() as tmp:
            runs = Mock(database=Path(tmp) / "inventory.sqlite3")
            runs.list.return_value = [{"id": "run", "next_step": "komodo-stacks"}]
            runs.show.return_value = {"id": "run", "resources": [
                {"kind": "komodo-server", "reference": "server", "step": "komodo-registration"},
            ]}
            with self.assertRaisesRegex(ValueError, "Newt identity and secret"):
                apply_baseline(api, device, primary_server="zero", runs=runs)
        self.assertTrue(all(call.args[0] == "/read" for call in api._request.call_args_list))
        runs.complete_step.assert_not_called()

    def test_docker_proxy_can_be_applied_without_newt_credentials_or_advancing_other_stacks(self):
        device = {"name": "lab", "id": "device"}
        api = Mock()
        api._request.side_effect = [
            [{"name": "lab", "id": "server", "info": {"state": "Ok"}}],
            [{"name": "lab-docker", "id": "docker", "info": {"server_id": "server", "state": "running"}}],
            [], [],
        ]
        with TemporaryDirectory() as tmp:
            runs = Mock(database=Path(tmp) / "inventory.sqlite3")
            runs.list.return_value = [{"id": "run", "next_step": "komodo-stacks"}]
            runs.show.return_value = {"id": "run", "resources": [
                {"kind": "komodo-server", "reference": "server", "step": "komodo-registration"},
            ]}
            report = apply_baseline(api, device, primary_server="zero", runs=runs, kinds=("docker",))
        self.assertEqual([item["name"] for item in report["stacks"]], ["lab-docker"])
        runs.record_resource.assert_called_once_with("run", "komodo-stacks",
                                                     Resource(ResourceKind.KOMODO_STACK, "docker", Ownership.REUSED))
        runs.complete_step.assert_not_called()

    def test_apply_recovers_tags_on_a_recorded_stack_without_recreating_or_redeploying_it(self):
        device = {"name": "lab", "id": "device"}
        docker_tag = "6" * 24
        api = Mock()
        api._request.side_effect = [
            [{"name": "lab", "id": "server", "info": {"state": "Ok"}}],
            [
                {"name": f"lab-{kind}", "id": kind, "info": {"server_id": "server", "state": "running"}}
                for kind in ("docker", "monitoring", "network")
            ] + [{"name": "stack-docker", "template": True, "tags": [docker_tag]}],
            [], [], {"tags": []}, {}, {"tags": [docker_tag]},
        ]
        with TemporaryDirectory() as tmp:
            runs = Mock(database=Path(tmp) / "inventory.sqlite3")
            runs.list.return_value = [{"id": "run", "next_step": "komodo-stacks"}]
            runs.show.return_value = {"id": "run", "resources": [
                {"kind": "komodo-server", "reference": "server"},
                {"kind": "komodo-stack", "reference": "docker", "step": "komodo-stacks", "ownership": "created"},
            ]}
            result = apply_baseline(api, device, primary_server="zero", runs=runs)
        self.assertEqual(result["status"], "applied")
        self.assertEqual([call.args for call in api._request.call_args_list if call.args[0] != "/read"], [
            ("/write", "UpdateResourceMeta", {"target": {"type": "Stack", "id": "docker"}, "tags": [docker_tag]}),
        ])
        runs.complete_step.assert_called_once_with("run", "komodo-stacks")

    def test_resumes_a_deployment_already_in_progress_without_redeploying(self):
        api = Mock()
        api._request.return_value = [{"id": "stack", "info": {"state": "running"}}]
        deploy_stack(api, "stack", "deploying")
        api._request.assert_called_once_with("/read", "ListStacks", {"limit": 0})
