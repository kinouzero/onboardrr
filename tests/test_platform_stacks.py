import hashlib
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import yaml

from onboardrr.automatic import choices
from onboardrr.inventory import NAS_PROFILE, PROFILE, SERVER_PROFILE
from onboardrr.platform_stacks import install_stack, merge_openbao, stack_payload, use_ferretdb
from onboardrr.remote.platform_stack import deploy, komodo_backend, stage_manifest


class PlatformStackTest(unittest.TestCase):
    def test_komodo_core_uses_the_rendered_config_in_its_single_manifest(self):
        model = {"name": "komodo", "services": {"komodo": {"environment": {"EXISTING": "${KOMODO_SECRET}",
                                                                           "CHECK": "${EMAIL:?Set the SMTP username}"},
                                                           "volumes": ["/srv/keys:/config/keys"]},
                                                "komodo-agent": {}, "komodo-mongodb": {}}}
        request = stack_payload(model, "komodo", "lab", True)
        self.assertIn("watch-core.sh", request["compose"])
        self.assertIn("core.config.toml", request["compose"])
        self.assertIn("/srv/keys:/config/keys", request["compose"])
        self.assertIn("KOMODO_SECRET", request["required"])
        self.assertIn("EMAIL", request["required"])
        self.assertIn("ONBOARDRR_KOMODO_RENDERED", request["required"])

    def test_platform_compose_formats_services_without_changing_network_modes(self):
        model = {
            "name": "pangolin",
            "networks": {"default": {"external": True, "name": "${HOSTNAME}"}},
            "services": {
                "proxy": {"network_mode": "service:gateway", "image": "proxy:1"},
                "gateway": {
                    "restart": "unless-stopped", "image": "gateway:1",
                    "labels": {"kuma.__docker": "previous connection"},
                    "environment": ["Z=value", "A=a=b"],
                    "volumes": ["./config:/config"],
                    "healthcheck": {"test": ["CMD", "true"]},
                },
            },
        }
        tcp = stack_payload(model, "pangolin", "zero", False, kuma_local=False)
        compose = tcp["compose"]
        self.assertIn("HOST_IP", tcp["required"])
        socket = stack_payload(model, "pangolin", "lab", False, kuma_local=True)
        self.assertNotIn("DOCKER_API_PORT", socket["required"])
        formatted = yaml.safe_load(compose)
        self.assertEqual(list(formatted["services"]), ["gateway", "proxy"])
        gateway = formatted["services"]["gateway"]
        self.assertEqual(list(gateway), sorted(gateway))
        self.assertEqual(gateway["environment"], ["A=a=b", "Z=value"])
        self.assertEqual(gateway["restart"], "always")
        self.assertEqual(gateway["labels"]["kuma.__docker"], '"${HOSTNAME}", "tcp", "http://${HOST_IP}:${DOCKER_API_PORT}"')
        self.assertIn('"socket", "/var/run/docker.sock"', socket["compose"])
        self.assertEqual(gateway["networks"], ["default"])
        self.assertEqual(formatted["networks"], model["networks"])
        self.assertEqual(formatted["services"]["proxy"]["network_mode"], "service:gateway")
        self.assertNotIn("networks", formatted["services"]["proxy"])
        self.assertIn("\n\n  proxy:", compose)
        self.assertIn("\n      test: [CMD, 'true']", compose)
        self.assertIn("\n    volumes:\n      - ./config:/config", compose)

    def test_pangolin_published_ports_use_aliases_and_keep_reference_defaults(self):
        model = {"name": "pangolin", "services": {"pangolin-gerbil": {
            "ports": ["8080:80", "8443:443", "21820:21820/udp", "51820:51820/udp"],
            "healthcheck": {"test": ["CMD", "curl", "http://127.0.0.1:3003"]},
        }}}
        request = stack_payload(model, "pangolin", "lab", False)
        gerbil = yaml.safe_load(request["compose"])["services"]["pangolin-gerbil"]
        self.assertEqual(gerbil["ports"], [
            "${PANGOLIN_HTTP_PORT:-${ONBOARDRR_PANGOLIN_HTTP_PORT}}:80",
            "${PANGOLIN_HTTPS_PORT:-${ONBOARDRR_PANGOLIN_HTTPS_PORT}}:443",
            "${PANGOLIN_EXTRA_UDP_PORT:-${ONBOARDRR_PANGOLIN_EXTRA_UDP_PORT}}:21820/udp",
            "${PANGOLIN_WIREGUARD_PORT:-${ONBOARDRR_PANGOLIN_WIREGUARD_PORT}}:51820/udp",
        ])
        self.assertEqual(set(request["port_defaults"].values()), {"8080", "8443", "21820", "51820"})
        self.assertEqual(gerbil["healthcheck"]["test"][-1], "http://127.0.0.1:${GERBIL_PORT}")

    def test_preview_reports_missing_variable_names_without_writing_files(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(Path, "home", return_value=Path(directory)):
            request = {"stack": "authentik", "name": "lab", "required": ["POSTGRES_PASSWORD"],
                       "compose": "name: authentik\n", "apply": False}
            with patch("onboardrr.remote.platform_stack.subprocess.run",
                       return_value=subprocess.CompletedProcess([], 0, "OTHER=fixture-value\n", "")) as runner:
                report = deploy(request)
            self.assertEqual(report["blockers"], ["The OpenBao Compose environment needs: POSTGRES_PASSWORD."])
            self.assertNotIn("fixture-value", str(report))
            self.assertFalse(Path(report["compose"]).exists())
            runner.assert_called_once()

    def test_full_komodo_preserves_the_existing_generated_periphery_manifest(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "compose.komodo.yml"
            original = json.dumps({"name": "komodo", "services": {"komodo-agent": {
                "environment": {"PERIPHERY_CONNECT_AS": "lab"},
            }}})
            target.write_text(original)
            target.chmod(0o600)
            stage_manifest(target, "name: komodo\nservices: {}\n", "komodo", "lab")
            self.assertEqual((target.parent / ".compose.komodo.periphery.previous").read_text(), original)
            self.assertEqual(target.read_text(), "name: komodo\nservices: {}\n")


    def test_openbao_combines_the_server_with_existing_agents_and_keeps_version_updates(self):
        reference = {
            "name": "secrets", "services": {"openbao": {"image": "openbao:2"}, "openbao-postgres": {}},
            "networks": {"default": {"external": True, "name": "${DOCKER_HOSTNAME}"}},
        }
        existing = {
            "name": "secrets",
            "services": {
                "openbao-agent-compose": {"image": "openbao:3", "networks": ["openbao"]},
                "openbao-agent-komodo": {"image": "openbao:3", "networks": ["openbao"]},
            },
            "networks": {"openbao": {"external": True, "name": "lab"}},
        }
        merged = merge_openbao(reference, json.dumps(existing))
        self.assertEqual(set(merged["services"]), set(existing["services"]) | set(reference["services"]))
        self.assertEqual(set(merged["networks"]), {"openbao", "default"})
        self.assertEqual(merged["services"]["openbao-agent-compose"], existing["services"]["openbao-agent-compose"])
        merged["services"]["openbao"]["image"] = "openbao:4"
        self.assertEqual(merge_openbao(reference, json.dumps(merged))["services"]["openbao"]["image"], "openbao:4")
        with self.assertRaisesRegex(ValueError, "Review the existing"):
            merge_openbao(reference, '{"name":"secrets","services":{"openbao":{}}}')

    def test_openbao_starts_from_local_bootstrap_files_and_reports_a_sealed_server(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(Path, "home", return_value=Path(directory)):
            home = Path(directory)
            root = home / ".config/docker/openbao"
            request = {
                "stack": "openbao", "name": "lab", "required": ["DOCKER_HOSTNAME"],
                "compose": "name: secrets\nservices: {}\n", "apply": True,
                "previous_digest": hashlib.sha256(b"").hexdigest(),
            }
            self.assertIn("Restore the local OpenBao bootstrap files", deploy(request)["blockers"][0])
            config = root / "config"
            config.mkdir(parents=True)
            (config / "openbao.hcl").write_text("configuration")
            bootstrap = root / "bootstrap"
            bootstrap.mkdir()
            for filename in ("openbao.env", "openbao-postgres.env"):
                path = bootstrap / filename
                path.write_text("FIXTURE=value\n")
                path.chmod(0o600)
            with patch("onboardrr.remote.platform_stack.subprocess.run", side_effect=[
                subprocess.CompletedProcess([], 0, "DOCKER_HOSTNAME=lab\n", ""),
                subprocess.CompletedProcess([], 0, "", ""),
                subprocess.CompletedProcess([], 0, "lab\n", ""),
                subprocess.CompletedProcess([], 0, "", ""),
                subprocess.CompletedProcess([], 2, '{"initialized":true,"sealed":true}', ""),
            ]) as runner:
                result = deploy(request)
            command = runner.call_args_list[-2].args[0]
            self.assertEqual(command[command.index("--project-name") + 1], "secrets")
            self.assertEqual(command[-2:], ["openbao-postgres", "openbao"])
            self.assertTrue(result["sealed"])
            self.assertEqual(result["next_action"], "Unseal OpenBao before deploying its consumers.")
            self.assertEqual(Path(result["compose"]).read_text(), request["compose"])
            self.assertTrue((root / "compose.env").is_file())

    def test_openbao_merge_refuses_to_replace_a_manifest_changed_after_reading(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "compose.openbao.yml"
            target.write_text("new contents")
            target.chmod(0o600)
            with self.assertRaisesRegex(ValueError, "changed during installation"):
                stage_manifest(target, "replacement", "openbao", "lab",
                               previous_digest=hashlib.sha256(b"old contents").hexdigest())
            self.assertEqual(target.read_text(), "new contents")


    def test_critical_platform_actions_match_the_profile_in_the_wizard(self):
        expected = {
            PROFILE: {"openbao", "authentik", "komodo", "pangolin"},
            NAS_PROFILE: {"authentik", "komodo", "pangolin"},
            SERVER_PROFILE: {"openbao", "authentik", "komodo", "pangolin"},
        }
        for profile, allowed in expected.items():
            with self.subTest(profile=profile):
                options = choices("auto-install", {"profile": profile})
                self.assertEqual({name for name in {"openbao", "authentik", "komodo", "pangolin"}
                                  if f"{name}-server" in options}, allowed)
                self.assertIn("openbao-agent", options)

    def test_direct_install_rejects_openbao_server_on_nas_before_accessing_storage(self):
        with patch("onboardrr.platform_stacks.storage_endpoint") as storage:
            with self.assertRaisesRegex(ValueError, "Installing openbao is unavailable"):
                install_stack(None, {"profile": NAS_PROFILE}, "openbao", None, None, apply=True, prepare_agent=None)
            storage.assert_not_called()

    def test_fresh_komodo_can_use_documentdb_without_reusing_mongodb_data(self):
        model = {"name": "komodo", "services": {
            "komodo": {"depends_on": {"komodo-mongodb": {}},
                       "environment": {"KOMODO_DATABASE_PASSWORD": "${MONGODB_PASSWORD}"}},
            "komodo-mongodb": {"networks": ["default"], "labels": {"docker.stack": "database"}},
        }}
        use_ferretdb(model)
        services = model["services"]
        self.assertNotIn("komodo-mongodb", services)
        self.assertEqual(services["komodo"]["environment"]["KOMODO_DATABASE_ADDRESS"], "komodo-ferretdb:27017")
        self.assertIn("/komodo/db/documentdb:", services["komodo-postgres"]["volumes"][0])
        self.assertEqual(services["komodo"]["environment"]["KOMODO_DATABASE_PASSWORD"], "${POSTGRES_PASSWORD}")
        self.assertEqual(services["komodo-ferretdb"]["environment"]["PGPASSWORD"], "${POSTGRES_PASSWORD}")
        self.assertEqual(services["komodo-postgres"]["environment"]["POSTGRES_PASSWORD"], "${POSTGRES_PASSWORD}")
        postgres_labels = services["komodo-postgres"]["labels"]
        ferret_labels = services["komodo-ferretdb"]["labels"]
        self.assertEqual(postgres_labels["wud.watch"], "true")
        self.assertEqual(ferret_labels["wud.watch"], "true")
        self.assertIn("wud.tag.transform", postgres_labels)
        self.assertEqual(postgres_labels["homepage.icon"], "sh-postgresql")
        self.assertEqual(ferret_labels["homepage.icon"], "sh-ferretdb")
        self.assertIn("kuma.__docker", ferret_labels)
        self.assertEqual(ferret_labels["docker.stack"], "database")

    def test_komodo_preserves_existing_mongodb_when_cpu_lacks_avx(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(Path, "read_text", return_value="flags: sse2"):
            home = Path(directory)
            with (patch("onboardrr.remote.platform_stack.platform.machine", return_value="x86_64"),
                  patch("onboardrr.remote.platform_stack.subprocess.check_output", return_value="")):
                self.assertEqual(komodo_backend(home, ["docker"], {}), {"backend": "ferretdb"})
                data = home / ".config/docker/komodo/db/data"
                data.mkdir(parents=True)
                (data / "WiredTiger").touch()
                self.assertIn("blockers", komodo_backend(home, ["docker"], {}))


    def test_komodo_prefers_ferretdb_on_a_fresh_raspberry_pi_with_mongodb_capable_cpu(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(Path, "read_text", return_value="Features: asimddp"):
            home = Path(directory)
            with (patch("onboardrr.remote.platform_stack.platform.machine", return_value="aarch64"),
                  patch("onboardrr.remote.platform_stack.subprocess.check_output", return_value="")):
                self.assertEqual(komodo_backend(home, ["docker"], {}), {"backend": "mongodb"})
                self.assertEqual(komodo_backend(home, ["docker"], {}, prefer_ferretdb=True), {"backend": "ferretdb"})
                data = home / ".config/docker/komodo/db/data"
                data.mkdir(parents=True)
                (data / "WiredTiger").touch()
                self.assertEqual(komodo_backend(home, ["docker"], {}, prefer_ferretdb=True), {"backend": "mongodb"})


    @patch("onboardrr.remote.platform_stack.subprocess.check_output", return_value="komodo-postgres\nkomodo-ferretdb\n")
    def test_backend_probe_preserves_the_running_ferretdb_deployment(self, docker):
        with patch.object(Path, "read_text", return_value="flags: avx"), patch.object(Path, "iterdir", side_effect=PermissionError):
            self.assertEqual(komodo_backend(Path("/home/operator"), ["docker"], {}), {"backend": "ferretdb"})
        self.assertIn("label=com.docker.compose.project=komodo", docker.call_args.args[0])
