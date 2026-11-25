import base64
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import yaml

from onboardrr.compose_agent import BOOTSTRAP_FIELDS, MANAGED_AUTH, MANAGED_POLICY, configured_compose_agent
from onboardrr.http_json import HttpStatusError
from onboardrr.linux import prepare_linux


class ComposeAgentTest(unittest.TestCase):
    def test_agent_compose_preserves_labels_and_selects_the_host_transport(self):
        try:
            import jinja2
        except ImportError:
            self.skipTest("Install the provisioning requirements to render the Ansible template.")

        template_path = (Path(__file__).resolve().parents[1] / "provisioning/roles/linux/templates"
                         / "openbao-agent.compose.yaml.j2")
        environment = jinja2.Environment(undefined=jinja2.StrictUndefined)
        environment.filters["to_json"] = json.dumps
        environment.filters["from_json"] = json.loads
        environment.globals.update(
            onboardrr_compose_agent={"url": "https://bao.example"},
            onboardrr_openbao_host_ip="192.0.2.12",
            onboardrr_openbao_docker_api_port=2381,
        )
        template = environment.from_string(template_path.read_text())
        endpoints = yaml.safe_load((template_path.parent.parent / "defaults/main.yml").read_text())[
            "onboardrr_openbao_internal_endpoints"
        ]
        unified = yaml.safe_load(template.render(
            onboardrr_name="zero", onboardrr_openbao_internal_endpoint=endpoints["zero"],
            onboardrr_kuma={"stdout": '{"local":true}'},
            onboardrr_account=["", "1001", "1002"], onboardrr_agent_specs=[
                {"variant": "compose", "directory": "/home/operator/compose", "output": "cache"},
                {"variant": "komodo", "directory": "/home/operator/komodo", "output": "rendered"},
            ],
        ))
        self.assertEqual(set(unified["services"]), {"openbao-agent-compose", "openbao-agent-komodo"})
        self.assertEqual(unified["services"]["openbao-agent-komodo"]["volumes"][1]["source"],
                         "/home/operator/komodo/rendered")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for output in ("agent", "cache", "rendered"):
                (root / output).mkdir()
            config = root / "agent/agent.hcl"
            for variant, configuration, paths in (
                ("compose", json.dumps({"template": [
                    {"destination": "/cache/config.env"},
                    {"destination": "/cache/project.env"},
                ]}, indent=2), ("cache/config.env", "cache/project.env")),
                ("komodo", 'template {\n  destination = "/rendered/core.config.toml"\n}\n',
                 ("rendered/core.config.toml",)),
            ):
                with self.subTest(healthcheck=variant):
                    kind, script = unified["services"][f"openbao-agent-{variant}"]["healthcheck"]["test"]
                    self.assertEqual(kind, "CMD-SHELL")
                    for prefix in ("/agent/", "/cache/", "/rendered/"):
                        script = script.replace(prefix, str(root) + prefix)
                        configuration = configuration.replace(prefix, str(root) + prefix)
                    config.write_text(configuration)
                    command = ["sh", "-c", script.replace("$$", "$")]
                    self.assertEqual(subprocess.run(command).returncode, 1)
                    for path in paths:
                        (root / path).write_text("fixture")
                    self.assertEqual(subprocess.run(command).returncode, 0)
                    (root / paths[-1]).write_text("")
                    self.assertEqual(subprocess.run(command).returncode, 1)
        for host, kuma_local in (("zero", False), ("lab", True)):
            for variant, output, releases in (
                ("compose", "cache", "moghtech/komodo"),
                ("komodo", "rendered", "openbao/openbao"),
            ):
                with self.subTest(host=host, variant=variant):
                    name = f"openbao-agent-{variant}"
                    compose = yaml.safe_load(template.render(
                        onboardrr_name=host, onboardrr_agent_variant=variant,
                        onboardrr_kuma={"stdout": json.dumps({"local": kuma_local})},
                        onboardrr_openbao_internal_endpoint=endpoints.get(host, {}),
                        onboardrr_account=["", "1001", "1002"], onboardrr_agent_output=output,
                        onboardrr_agent_directory=f"/home/operator/.config/docker/openbao/{variant}",
                    ))
                    self.assertEqual(compose["name"], "secrets")
                    self.assertEqual(list(compose["services"]), [name])
                    service = compose["services"][name]
                    self.assertEqual(service["container_name"], name)
                    self.assertEqual(service["user"], "1001:1002")
                    self.assertEqual(service["volumes"][1]["target"], f"/{output}")
                    if host == "zero":
                        self.assertEqual(service["networks"], ["openbao"])
                        self.assertEqual(compose["networks"], {"openbao": {"external": True, "name": "zero"}})
                        self.assertNotIn("network_mode", service)
                    else:
                        self.assertNotIn("networks", compose)
                        self.assertEqual(service.get("network_mode"), "bridge" if variant == "compose" else None)
                    labels = service["labels"]
                    endpoint = ('"socket", "/var/run/docker.sock"' if kuma_local
                                else '"tcp", "http://192.0.2.12:2381"')
                    self.assertEqual(labels, {
                        "docker.stack": "secrets",
                        "homepage.href": "https://bao.example",
                        "homepage.group": f"Docker {host}",
                        "homepage.icon": "sh-openbao",
                        "homepage.name": name,
                        "komodo.skip": None,
                        "kuma.__docker": f'"{host}", {endpoint}',
                        "wud.display.icon": "sh-openbao",
                        "wud.display.name": name,
                        "wud.link.template": f"https://github.com/{releases}/releases",
                        "wud.tag.include": r"^\d+\.\d+\.\d+$$",
                    })

    def test_disabled_without_the_bootstrap_token(self):
        with patch.dict(os.environ, {}, clear=True), patch("onboardrr.compose_agent.OpenBao") as bao:
            self.assertIsNone(configured_compose_agent("lab"))
        bao.assert_not_called()

    @patch.dict(os.environ, {"ONBOARDRR_BAO_URL": "https://bao.example"}, clear=True)
    def test_managed_periphery_cache_uses_the_shared_user(self):
        record = {"role_id": "fixture-role", "secret_id": "fixture-secret", "projects": "periphery",
                  "auth_mount": "onboardrr-compose", "periphery_cache": True}
        client = Mock()
        client.fields_with_version.return_value = record, 1
        with patch("onboardrr.compose_agent.configured_openbao", return_value=client):
            agent = configured_compose_agent("lab", ensure=True)
        template, = json.loads(base64.b64decode(agent["config_b64"]))["template"]
        self.assertEqual(template["destination"], "/cache/periphery.env")
        self.assertIn('secret "hosts/data/common/USER"', template["contents"])
        self.assertIn("PERIPHERY_PASSKEYS=", template["contents"])

    @patch.dict(os.environ, {"ONBOARDRR_BAO_URL": "https://bao.example"}, clear=True)
    def test_legacy_projects_preserve_their_source_and_approle(self):
        source = '{{ with secret "services/data/docker/API_PORT" }}{{ .Data.data.value }}{{ end }}'
        record = {"role_id": "legacy-role", "secret_id": "legacy-secret",
                  "projects": "periphery,config", "auth_mount": "compose",
                  "legacy_templates": {"config": source}, "periphery_cache": True,
                  "compose_config_variables": True}
        client = Mock()
        with patch("onboardrr.compose_agent.configured_openbao", return_value=client):
            client.fields_with_version.return_value = record, 1
            agent = configured_compose_agent("lab", ensure=True)
            config = json.loads(base64.b64decode(agent["config_b64"]))
            templates = {item["destination"]: item["contents"] for item in config["template"]}
            self.assertTrue(templates["/cache/config.env"].startswith(source))
            self.assertIn("deployments/compose/lab/config", templates["/cache/config.env"])
            self.assertIn(".Data.data.variables", templates["/cache/config.env"])
            self.assertIn("deployments/periphery/lab", templates["/cache/periphery.env"])
            self.assertEqual(config["auto_auth"]["method"][0]["mount_path"], "auth/compose")
            client.create.assert_not_called()
            for invalid in ("not an object", {"../config": source}, {"config": 123}, {"config": " "}):
                record["legacy_templates"] = invalid
                with self.subTest(invalid=invalid), self.assertRaisesRegex(ValueError, "invalid legacy templates"):
                    configured_compose_agent("lab", ensure=True)

    def test_reads_only_host_bootstrap_and_builds_scoped_templates(self):
        environment = {
            "ONBOARDRR_BAO_AGENT_TOKEN_FILE": "/private/token",
            "ONBOARDRR_BAO_AGENT_AUTH_MOUNT": "compose-approle",
            "ONBOARDRR_BAO_URL": "https://bao.example/",
            "ONBOARDRR_BAO_MOUNT": "onboardrr",
        }
        with (
            patch.dict(os.environ, environment, clear=True),
            patch("onboardrr.compose_agent.token_file", return_value="controller-token"),
            patch("onboardrr.compose_agent.OpenBao") as bao,
        ):
            client = bao.return_value
            client.fields_with_version.return_value = ({
                "role_id": "host-role", "secret_id": "host-secret-id", "projects": "web, db,web",
                "docker_api_port": "2380", "host_ip": "192.0.2.10",
            }, 1)
            agent = configured_compose_agent("lab")
            client.fields_with_version.assert_called_once_with(
                "deployments/compose-agent/lab", *BOOTSTRAP_FIELDS,
            )
        rendered = base64.b64decode(agent["config_b64"]).decode()
        config = json.loads(rendered)
        self.assertNotIn("controller-token", str(agent))
        self.assertNotIn("host-secret-id", rendered)
        self.assertEqual(agent["projects"], ["db", "web"])
        self.assertNotIn("komodo", agent)
        self.assertEqual((agent["url"], agent["host_ip"], agent["docker_api_port"]),
                         ("https://bao.example", "192.0.2.10", 2380))
        self.assertEqual(config["auto_auth"]["method"][0]["mount_path"], "auth/compose-approle")
        self.assertEqual([item["destination"] for item in config["template"]], ["/cache/db.env", "/cache/web.env"])
        for key, item in zip(agent["projects"], config["template"], strict=True):
            self.assertIn(f"onboardrr/data/deployments/compose/lab/{key}", item["contents"])
            self.assertEqual(item["perms"], "0600")
            self.assertFalse(item["backup"])

    @patch.dict(os.environ, {"ONBOARDRR_BAO_URL": "https://bao.example", "ONBOARDRR_BAO_LAYOUT": "split"}, clear=True)
    def test_split_agent_renders_host_compose_and_periphery_paths(self):
        with patch("onboardrr.compose_agent.configured_openbao") as bao:
            bao.return_value.fields_with_version.return_value = ({
                "role_id": "host-role", "secret_id": "host-secret", "projects": "config,periphery",
                "periphery_cache": True,
            }, 1)
            agent = configured_compose_agent("lab", ensure=True)
        templates = {item["destination"]: item["contents"]
                     for item in json.loads(base64.b64decode(agent["config_b64"]))["template"]}
        self.assertIn("hosts/data/lab/compose/config", templates["/cache/config.env"])
        self.assertIn("hosts/data/lab/periphery", templates["/cache/periphery.env"])

    def test_refuses_missing_or_invalid_bootstrap(self):
        environment = {"ONBOARDRR_BAO_AGENT_TOKEN_FILE": "/private/token",
                       "ONBOARDRR_BAO_URL": "https://bao.example/"}
        with (
            patch.dict(os.environ, environment, clear=True),
            patch("onboardrr.compose_agent.token_file", return_value="controller-token"),
            patch("onboardrr.compose_agent.OpenBao") as bao,
        ):
            client = bao.return_value
            client.fields_with_version.side_effect = HttpStatusError("OpenBao", "KV read", 404)
            with self.assertRaisesRegex(ValueError, "bootstrap record for lab"):
                configured_compose_agent("lab")
            client.fields_with_version.side_effect = None
            for invalid in ("", "../other", "a,,b", "onboardrr-openbao-agent"):
                client.fields_with_version.return_value = ({"role_id": "role", "secret_id": "secret", "projects": invalid}, 1)
                with self.subTest(projects=invalid), self.assertRaisesRegex(ValueError, "Compose project"):
                    configured_compose_agent("lab")

    def test_leaves_network_defaults_to_ansible_and_preserves_port_overrides(self):
        environment = {"ONBOARDRR_BAO_AGENT_TOKEN_FILE": "/private/token", "ONBOARDRR_BAO_URL": "https://bao.example"}
        with (
            patch.dict(os.environ, environment, clear=True),
            patch("onboardrr.compose_agent.token_file", return_value="controller-token"),
            patch("onboardrr.compose_agent.OpenBao") as bao,
        ):
            record = {"role_id": "role", "secret_id": "secret", "projects": "web"}
            bao.return_value.fields_with_version.return_value = record, 1
            agent = configured_compose_agent("lab")
            self.assertIsNone(agent["host_ip"])
            self.assertIsNone(agent["docker_api_port"])
            with patch.dict(os.environ, {"ONBOARDRR_DOCKER_API_PORT": "2381"}):
                self.assertEqual(configured_compose_agent("lab")["docker_api_port"], 2381)
                record["docker_api_port"] = "2382"
                self.assertEqual(configured_compose_agent("lab")["docker_api_port"], 2382)
            record["docker_api_port"] = "0"
            with self.assertRaisesRegex(ValueError, "valid Docker API port"):
                configured_compose_agent("lab")

    @patch.dict(os.environ, {"ONBOARDRR_BAO_URL": "https://bao.example", "ONBOARDRR_BAO_MOUNT": "onboardrr"}, clear=True)
    def test_automatic_agent_provisions_and_reuses_a_scoped_identity(self):
        client = Mock(mount="onboardrr", headers={"X-Vault-Token": "controller-token"})
        missing = HttpStatusError("OpenBao", "read", 404)
        client.fields_with_version.side_effect = missing
        client.read.return_value = ("fixture-passkey",)
        client.service.request.side_effect = [
            {"data": {"keys": ["custom"]}},
            missing,
            None,
            {"data": {"role_id": "new-role"}},
            {"data": {"secret_id": "host-secret", "secret_id_accessor": "accessor"}},
            {"auth": {"policies": [MANAGED_POLICY], "metadata": {"role_name": "lab"}, "client_token": "test-token"}},
            None,
        ]
        with patch("onboardrr.compose_agent.configured_openbao", return_value=client):
            agent = configured_compose_agent("lab", ensure=True)
            path, record = client.create.call_args.args
            self.assertEqual(path, "deployments/compose-agent/lab")
            client.create.assert_any_call("deployments/compose/lab/config", {"variables": {}})
            self.assertEqual(record["auth_mount"], MANAGED_AUTH)
            self.assertNotIn("fixture-passkey", str(record))
            client.fields_with_version.side_effect = None
            client.fields_with_version.return_value = (record, 1)
            client.service.request.reset_mock()
            self.assertEqual(configured_compose_agent("lab", ensure=True), agent)
            client.service.request.assert_not_called()
        configuration = json.loads(base64.b64decode(agent["config_b64"]))
        templates = {template["destination"]: template["contents"] for template in configuration["template"]}
        self.assertIn("deployments/compose/lab/custom", templates["/cache/custom.env"])
        self.assertIn(".Data.data.variables", templates["/cache/config.env"])
        self.assertIn("deployments/periphery/lab", templates["/cache/periphery.env"])
        self.assertIn("PERIPHERY_PASSKEYS=", templates["/cache/periphery.env"])

    def test_zero_renders_core_with_a_separate_approle(self):
        with (
            patch.dict(os.environ, {"ONBOARDRR_BAO_AGENT_TOKEN_FILE": "/private/token"}, clear=True),
            patch("onboardrr.compose_agent.token_file"),
            patch("onboardrr.compose_agent.OpenBao") as bao,
        ):
            record = {"role_id": "compose-role", "secret_id": "compose-secret", "projects": "web",
                      "komodo_role_id": "core-role", "komodo_common_mount": "compose"}
            bao.return_value.fields_with_version.return_value = (record, 1)
            with self.assertRaisesRegex(ValueError, "AppRole IDs"):
                configured_compose_agent("zero")
            record["komodo_secret_id"] = "core-secret"
            agent = configured_compose_agent("zero")
            record["komodo_newt_mount"] = "services"
            dedicated = configured_compose_agent("zero")
        self.assertIsNone(agent["docker_api_port"])
        self.assertEqual(agent["komodo"]["role_id"], "core-role")
        core_template, = json.loads(base64.b64decode(agent["komodo"]["config_b64"]))["template"]
        self.assertEqual(core_template["destination"], "/rendered/core.config.toml")
        self.assertEqual(core_template["perms"], "0600")
        self.assertIn('secret "komodo/data/variables"', core_template["contents"])
        self.assertIn('secret "compose/data/config/common/STORAGE_IP"', core_template["contents"])
        dedicated_template, = json.loads(base64.b64decode(dedicated["komodo"]["config_b64"]))["template"]
        self.assertNotIn('services/metadata/pangolin/newt/', core_template["contents"])
        self.assertIn('secrets "services/metadata/pangolin/newt/"', dedicated_template["contents"])

    def test_prepare_passes_agent_configuration_to_ansible(self):
        agent = {"role_id": "role", "secret_id": "secret", "projects": ["web"]}
        device = {"name": "lab", "ssh_user": "operator"}
        with patch("onboardrr.linux.run_playbook") as playbook:
            prepare_linux(device, Path("known_hosts"), None, None, None,
                          system_only=True, compose_agent=agent)
        self.assertTrue(playbook.call_args.args[3]["onboardrr_system_only"])
        self.assertEqual(playbook.call_args.args[3]["onboardrr_compose_agent"], agent)

    @patch.dict(os.environ, {"ONBOARDRR_BAO_URL": "https://bao.example"}, clear=True)
    def test_existing_komodo_agent_uses_its_own_auth_mount_and_template(self):
        source = '{{ with secret "komodo/data/variables" }}{{ .Data.data }}{{ end }}'
        client = Mock()
        record = {"role_id": "compose-role", "secret_id": "compose-secret", "projects": "config",
                  "auth_mount": "compose", "komodo_role_id": "core-role",
                  "komodo_secret_id": "core-secret", "komodo_auth_mount": "komodo",
                  "komodo_template": source}
        with patch("onboardrr.compose_agent.configured_openbao", return_value=client):
            client.fields_with_version.return_value = record, 1
            agent = configured_compose_agent("zero", ensure=True)
            core = json.loads(base64.b64decode(agent["komodo"]["config_b64"]))
            self.assertEqual(core["auto_auth"]["method"][0]["mount_path"], "auth/komodo")
            self.assertEqual(core["template"][0]["contents"], source)
            self.assertEqual(core["template"][0]["destination"], "/rendered/core.config.toml")
            self.assertTrue(agent["komodo"]["uses_legacy_template"])
            record["komodo_template"] = []
            with self.assertRaisesRegex(ValueError, "invalid Komodo template"):
                configured_compose_agent("zero", ensure=True)
