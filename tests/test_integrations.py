import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from subprocess import CompletedProcess
from unittest.mock import patch
from uuid import uuid4

from onboardrr.__main__ import komodo_client, pangolin_client
from onboardrr.http_json import HttpStatusError
from onboardrr.inventory import PROFILE, Inventory
from onboardrr.pangolin import SshIntegration, reference_site
from onboardrr.retirement import retirement_plan
from onboardrr.runs import STEPS, Ownership, Resource, ResourceKind, RunStore
from onboardrr.runtime import load_controller_settings
from onboardrr.secrets import OpenBao, configured_openbao, integration_credentials


class SecretProviderTest(unittest.TestCase):
    def test_split_layout_routes_data_and_metadata_operations(self):
        bao = OpenBao("https://bao.example", "onboardrr", "fixture-token", layout="split")
        data = {"data": {"data": {"passkey": "fixture"}, "metadata": {"version": 1}}}
        with patch.object(bao.service, "request", return_value=data) as request:
            bao.create("deployments/periphery/fly", {"passkey": "fixture"})
            self.assertEqual(bao.read("deployments/periphery/fly", "passkey"), ("fixture",))
            bao.patch("deployments/compose/fly/config", {"variables": {}}, version=1)
            bao.secret_metadata("deployments/periphery/fly")
            bao.delete_secret("deployments/periphery/fly")
            self.assertEqual([call.args for call in request.call_args_list], [
                ("POST", "/v1/hosts/data/fly/periphery"),
                ("GET", "/v1/hosts/data/fly/periphery"),
                ("PATCH", "/v1/hosts/data/fly/compose/config"),
                ("GET", "/v1/hosts/metadata/fly/periphery"),
                ("DELETE", "/v1/hosts/metadata/fly/periphery"),
            ])
            with self.assertRaisesRegex(ValueError, "No dedicated OpenBao path"):
                bao.read("deployments/other", "value")

    @patch.dict(os.environ, {
        "ONBOARDRR_BAO_URL": "https://bao.example",
        "ONBOARDRR_BAO_LAYOUT": "split",
        "ONBOARDRR_BAO_TOKEN_FILE": "/private/token",
    }, clear=True)
    @patch("onboardrr.secrets.token_file", return_value="fixture-token")
    def test_split_controller_reads_host_and_service_records_and_keeps_explicit_mount(self, _token_file):
        with patch("onboardrr.secrets.JsonService.request") as request:
            request.return_value = {
                "data": {"data": {"api_key": "key", "api_secret": "secret", "api_token": "token", "value": "fixture"}}
            }
            self.assertEqual(integration_credentials("komodo"), ("key", "secret"))
            self.assertEqual(integration_credentials("pangolin"), ("token",))
            self.assertEqual(integration_credentials("wud"), ("token",))
            configured_openbao().read("deployments/wifi/2G", "value")
            configured_openbao().read("deployments/wifi/5G", "value")
            configured_openbao().read("deployments/goku", "value")
            configured_openbao().read("deployments/monitoring", "value")
            configured_openbao(mount="hosts").read("common/USER", "value")
            self.assertEqual([call.args[1] for call in request.call_args_list], [
                "/v1/services/data/komodo/onboardrr",
                "/v1/services/data/pangolin/onboardrr",
                "/v1/services/data/wud/onboardrr",
                "/v1/hosts/data/common/wifi/2G",
                "/v1/hosts/data/common/wifi/5G",
                "/v1/hosts/data/goku/smb",
                "/v1/services/data/beszel/onboardrr",
                "/v1/hosts/data/common/USER",
            ])

    def test_openbao_creation_requires_a_new_kv_record(self):
        bao = OpenBao("https://bao.example", "onboardrr", "fixture-token")
        with patch.object(bao.service, "request") as request:
            bao.create("deployments/periphery/fly", {"passkey": "fixture"})
        request.assert_called_once_with(
            "POST", "/v1/onboardrr/data/deployments/periphery/fly",
            headers={"X-Vault-Token": "fixture-token", "Content-Type": "application/json"}, operation="KV create",
            data={"options": {"cas": 0}, "data": {"passkey": "fixture"}},
        )

    def test_controller_settings_configure_integrations_without_shell_exports(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "onboardrr"
            config.mkdir()
            (config / "config.toml").write_text(
                'komodo_url = "https://komodo.example"\n'
                'openbao_url = "https://bao.example"\n'
                'openbao_mount = "onboardrr"\n'
                'openbao_layout = "split"\n'
                'openbao_token_file = "~/controller-token"\n'
                'komodo_openbao_token_file = "~/newt-token"\n'
                'pangolin_ssh_target = "core.example"\n'
                'pangolin_url = "https://pangolin.example"\n'
            )
            environment = {"XDG_CONFIG_HOME": directory, "ONBOARDRR_BAO_MOUNT": "overridden"}
            with (
                patch.dict(os.environ, environment, clear=True),
                patch("onboardrr.__main__.integration_credentials", return_value=("fixture-key", "fixture-secret")),
            ):
                load_controller_settings()
                self.assertEqual(komodo_client().service.host, "komodo.example")
                self.assertEqual(komodo_client("https://override.example").service.host, "override.example")
                self.assertEqual(os.environ["ONBOARDRR_BAO_TOKEN_FILE"], "~/controller-token")
                self.assertEqual(os.environ["ONBOARDRR_BAO_MOUNT"], "overridden")
                self.assertEqual(os.environ["ONBOARDRR_BAO_LAYOUT"], "split")
                self.assertEqual(os.environ["ONBOARDRR_KOMODO_BAO_TOKEN_FILE"], "~/newt-token")
                self.assertEqual(os.environ["ONBOARDRR_PANGOLIN_URL"], "https://pangolin.example")
                transport = pangolin_client("zero").service
                self.assertIsInstance(transport, SshIntegration)
                self.assertEqual(transport.target, "core.example")
                self.assertEqual(pangolin_client("zero", url="https://override.example").service.host,
                                 "override.example")

    def test_missing_pangolin_transport_names_the_configuration_before_requesting_secrets(self):
        with patch.dict(os.environ, {}, clear=True), patch("onboardrr.__main__.integration_credentials") as credentials:
            with self.assertRaisesRegex(ValueError, "pangolin_ssh_target or pangolin_url"):
                pangolin_client("zero")
            credentials.assert_not_called()

    def test_missing_komodo_url_names_the_configuration_before_requesting_secrets(self):
        with patch.dict(os.environ, {}, clear=True), patch("onboardrr.__main__.integration_credentials") as credentials:
            with self.assertRaisesRegex(ValueError, "komodo_url.*ONBOARDRR_KOMODO_URL"):
                komodo_client()
            credentials.assert_not_called()

    def test_reads_each_integration_from_its_own_secret_record(self):
        with patch.dict(os.environ, {}, clear=True), patch("onboardrr.secrets.configured_openbao") as configured:
            bao = configured.return_value
            bao.read.side_effect = [("fixture-key", "fixture-secret"), ("fixture-pangolin",), ("fixture-wud",)]
            self.assertEqual(integration_credentials("komodo"), ("fixture-key", "fixture-secret"))
            self.assertEqual(integration_credentials("pangolin"), ("fixture-pangolin",))
            self.assertEqual(integration_credentials("wud"), ("fixture-wud",))
            self.assertEqual([call.args for call in bao.read.call_args_list], [
                ("integrations/komodo", "api_key", "api_secret"),
                ("integrations/pangolin", "api_token"),
                ("integrations/wud", "api_token"),
            ])

    def test_respects_complete_komodo_overrides_and_refuses_mixing_sources(self):
        with (
            patch.dict(os.environ, {"ONBOARDRR_KOMODO_API_KEY": "override"}, clear=True),
            patch("onboardrr.secrets.configured_openbao") as configured,
        ):
            with self.assertRaisesRegex(ValueError, "complete komodo"):
                integration_credentials("komodo")
            os.environ["ONBOARDRR_KOMODO_API_SECRET"] = "override-secret"
            self.assertEqual(integration_credentials("komodo"), ("override", "override-secret"))
            configured.assert_not_called()

    def test_optional_wud_record_preserves_public_wud_and_reports_permission_errors(self):
        with patch.dict(os.environ, {}, clear=True), patch("onboardrr.secrets.configured_openbao") as configured:
            configured.return_value.read.side_effect = HttpStatusError("OpenBao", "KV read", 404)
            self.assertEqual(integration_credentials("wud", optional=True), ("",))
            configured.return_value.read.side_effect = HttpStatusError("OpenBao", "KV read", 403)
            with self.assertRaisesRegex(HttpStatusError, "HTTP 403"):
                integration_credentials("wud", optional=True)

    def test_reads_required_kv_fields_from_the_configured_mount(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "bao-token"
            path.write_text("fixture-bao-token\n")
            path.chmod(0o600)
            with (
                patch.dict(os.environ, {
                    "ONBOARDRR_BAO_URL": "https://bao.example",
                    "ONBOARDRR_BAO_MOUNT": "private",
                    "ONBOARDRR_BAO_TOKEN_FILE": str(path),
                }),
                patch("onboardrr.secrets.JsonService.request") as request,
            ):
                request.return_value = {"data": {"data": {"smb_username": "fixture-key", "smb_password": "fixture-secret"}}}
                bao = configured_openbao()
                self.assertEqual(bao.read("deployments/goku", "smb_username", "smb_password"), ("fixture-key", "fixture-secret"))
                request.assert_called_once_with(
                    "GET", "/v1/private/data/deployments/goku",
                    headers={"X-Vault-Token": "fixture-bao-token"}, operation="KV read",
                )
                with self.assertRaisesRegex(ValueError, "Invalid OpenBao secret path"):
                    bao.read("onboardrr/../other", "smb_username")

    def test_refuses_an_exposed_token_file(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "bao-token"
            path.write_text("fixture-token")
            path.chmod(0o644)
            with patch.dict(os.environ, {
                "ONBOARDRR_BAO_URL": "https://bao.example",
                "ONBOARDRR_BAO_TOKEN_FILE": str(path),
            }):
                with self.assertRaisesRegex(ValueError, "private file"):
                    configured_openbao()

    def test_pangolin_reference_returns_only_safe_fields(self):
        with patch("onboardrr.pangolin.JsonService.request") as request:
            request.return_value = {"data": {
                "siteId": 42, "niceId": "naruto", "type": "newt", "online": True,
                "newtId": "fixture-newt", "secret": "fixture-newt-secret",
            }}
            report = reference_site("https://pangolin.example", "fixture-key", "org-1", "naruto")
            request.assert_called_once_with(
                "GET", "/v1/org/org-1/site/naruto",
                headers={"Authorization": "Bearer fixture-key"}, operation="read reference site",
            )
            self.assertEqual(report["siteId"], 42)
            self.assertNotIn("fixture-newt", json.dumps(report))


class RetirementTest(unittest.TestCase):
    def test_keeps_created_ownership_across_runs_and_protects_data_by_default(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "inventory.sqlite3"
            device = Inventory(database).add("lab", "lab.local", "operator", 22, PROFILE)
            runs = RunStore(database)
            first = runs.open("lab")["id"]
            runs.record_resource(first, "ssh", Resource(ResourceKind.SSH_KEY, "operator-key", Ownership.CREATED))
            runs.record_resource(first, "ssh", Resource(ResourceKind.HOST_FILE, "/etc/fstab", Ownership.REUSED))
            for step in STEPS[:2]:
                runs.complete_step(first, step)
            runs.record_resource(first, "goku-files", Resource(ResourceKind.GOKU_DIRECTORY, "/data/lab", Ownership.CREATED))
            for step in STEPS[2:]:
                runs.complete_step(first, step)

            second = runs.open("lab")["id"]
            runs.record_resource(second, "ssh", Resource(ResourceKind.SSH_KEY, "operator-key", Ownership.REUSED))
            backup = uuid4()
            runs.record_resource(second, "ssh", Resource(ResourceKind.HOST_FILE, "/etc/fstab", Ownership.MODIFIED, backup))
            runs.complete_step(second, "ssh")
            runs.record_resource(second, "komodo-registration", Resource(ResourceKind.KOMODO_SERVER, "shared", Ownership.REUSED))

            plan = retirement_plan(database, "lab")
            self.assertEqual(plan["device_id"], device["id"])
            self.assertEqual([item["action"] for item in plan["actions"]], [
                "preserve", "preserve", "restore", "remove-after-fallback-ssh-check",
            ])
            self.assertEqual(plan["actions"][2]["backup_id"], str(backup))
            purge_plan = retirement_plan(database, "lab", purge=True)
            self.assertEqual(purge_plan["actions"][1]["action"], "remove")
            self.assertEqual(purge_plan["actions"][0]["action"], "preserve")


class LinuxPrepareTest(unittest.TestCase):
    def test_component_limits_ansible_to_its_tasks(self):
        from onboardrr.linux import prepare_linux

        device = {"name": "lab", "host": "lab.local", "ssh_port": 22, "ssh_user": "operator"}
        with tempfile.TemporaryDirectory() as directory:
            known_hosts = Path(directory) / "known_hosts"
            known_hosts.touch()
            with (
                patch("onboardrr.linux.shutil.which", return_value="/fixture/ansible-playbook"),
                patch("onboardrr.linux.ssh_destination", return_value=device["host"]),
                patch("onboardrr.linux.subprocess.run", return_value=CompletedProcess([], 0)) as run,
            ):
                prepare_linux(device, known_hosts, None, None, None, system_only=True, component="bluetooth")
        command = run.call_args.args[0]
        self.assertEqual(command[command.index("--tags") + 1], "bluetooth")

    def test_system_only_skips_mount_credentials_and_sources(self):
        from onboardrr.linux import prepare_linux

        device = {"name": "lab", "ssh_user": "operator"}
        with patch("onboardrr.linux.run_playbook") as run:
            prepare_linux(device, Path("known_hosts"), None, None, None, system_only=True)
        variables = run.call_args.args[3]
        self.assertTrue(variables["onboardrr_system_only"])
        self.assertNotIn("onboardrr_cifs_source", variables)
        self.assertNotIn("onboardrr_smb_password_b64", variables)

    def test_server_preparation_passes_selected_mounts_and_options_to_ansible(self):
        from unittest.mock import Mock

        from onboardrr.linux import prepare_linux

        device = {"name": "zero", "ssh_user": "operator", "profile": "server"}
        bao = Mock()
        bao.read.return_value = ("operator", "fixture")
        with patch("onboardrr.linux.run_playbook") as run:
            prepare_linux(
                device, Path("known_hosts"), "//goku/compose", "goku:/data/zero", bao,
                optional_mounts={"dev": None, "downloads": "//goku/downloads", "plex": "//goku/docker/plex"},
                inotify_instances=1024, swappiness=0, nvidia=True,
                nvidia_driver_package="nvidia-driver-580", vnc=True,
            )
        variables = run.call_args.args[3]
        self.assertEqual(variables["onboardrr_profile"], "server")
        self.assertEqual(variables["onboardrr_optional_mounts"], {
            "downloads": "//goku/downloads", "plex": "//goku/docker/plex",
        })
        self.assertEqual(variables["onboardrr_inotify_instances"], 1024)
        self.assertEqual(variables["onboardrr_swappiness"], 0)
        self.assertTrue(variables["onboardrr_nvidia"])
        self.assertEqual(variables["onboardrr_nvidia_driver_package"], "nvidia-driver-580")
        self.assertTrue(variables["onboardrr_vnc"])

    def test_invalid_server_settings_are_rejected_before_connecting(self):
        from onboardrr.linux import prepare_linux

        device = {"name": "zero", "ssh_user": "operator", "profile": "server"}
        options = (
            ({"swappiness": 201}, "Swappiness"),
            ({"nvidia_driver_package": "nvidia-driver-580"}, "with a valid NVIDIA"),
            ({"nvidia": True, "nvidia_driver_package": "nvidia-driver=*"}, "with a valid NVIDIA"),
        )
        for kwargs, message in options:
            with self.subTest(kwargs=kwargs), patch("onboardrr.linux.run_playbook") as run:
                with self.assertRaisesRegex(ValueError, message):
                    prepare_linux(device, Path("known_hosts"), None, None, None, system_only=True, **kwargs)
                run.assert_not_called()

    def test_uses_one_registered_target_and_passes_secrets_only_on_stdin(self):
        from base64 import b64decode
        from unittest.mock import Mock

        from onboardrr.linux import prepare_linux

        device = {"name": "lab", "host": "lab.local", "ssh_port": 2222, "ssh_user": "operator"}
        bao = Mock()
        bao.read.return_value = ("smb-fixture", "smb-{{ literal }}-secret")
        with tempfile.TemporaryDirectory() as directory:
            known_hosts = Path(directory) / "known hosts"
            known_hosts.touch()

            def run(command, **options):
                inventory = json.loads(Path(command[2]).read_text())
                hosts = inventory["all"]["hosts"]
                self.assertEqual(list(hosts), ["lab"])
                self.assertEqual(hosts["lab"]["ansible_host"], "192.0.2.20")
                self.assertEqual(hosts["lab"]["ansible_port"], 2222)
                self.assertIn("StrictHostKeyChecking=yes", hosts["lab"]["ansible_ssh_args"])
                self.assertIn("HostKeyAlias=[lab.local]:2222", hosts["lab"]["ansible_ssh_args"])
                self.assertIn("--check", command)
                self.assertIn("@/dev/stdin", command)
                self.assertNotIn("smb-", " ".join(command))
                variables = json.loads(options["input"])
                self.assertEqual(b64decode(variables["onboardrr_smb_password_b64"]).decode(), "smb-{{ literal }}-secret")
                self.assertEqual(b64decode(variables["onboardrr_sudo_b64"]).decode(), "sudo-fixture")
                self.assertEqual(variables["onboardrr_smb_credentials_file"], "/etc/onboardrr/lab-goku.smb")
                return CompletedProcess(command, 0)

            with (
                patch("onboardrr.linux.shutil.which", return_value="/fixture/bin/ansible-playbook") as lookup,
                patch("onboardrr.host_resolution.openssh_destination", return_value="192.0.2.20"),
                patch("onboardrr.host_resolution.dns_addresses", return_value=set()),
                patch("onboardrr.linux.subprocess.run", side_effect=run),
            ):
                self.assertEqual(
                    prepare_linux(device, known_hosts, "//goku/compose", "goku:/data/lab", bao,
                                  become_password="sudo-fixture"),
                    {"device": "lab", "mode": "check"},
                )
                self.assertEqual(lookup.call_args.args, ("ansible-playbook",))
                self.assertTrue(lookup.call_args.kwargs["path"].endswith(str(Path(sys.executable).parent)))
        bao.read.assert_called_once_with("deployments/goku", "smb_username", "smb_password")
