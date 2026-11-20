import io
import json
import os
import tempfile
import unittest
from argparse import ArgumentParser
from contextlib import nullcontext, redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import Mock, patch

from onboardrr.automatic import MONITORING, Automation, add_parsers, choices, defaults, run
from onboardrr.__main__ import main, sudo_password
from onboardrr.http_json import HttpStatusError
from onboardrr.inventory import NAS_PROFILE, PROFILE, SERVER_PROFILE, Inventory
from onboardrr.runs import Ownership, Resource, ResourceKind, RunStore
from onboardrr.sudo_input import password_pipe


class AutomaticTest(unittest.TestCase):
    def setUp(self):
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.database = root / "inventory.sqlite3"
        self.inventory = Inventory(self.database)
        self.runs = RunStore(self.database)
        self.device = self.inventory.add("lab", "lab.local", "operator", 22, PROFILE)
        self.run_id = self.runs.open("lab")["id"]
        self.runs.complete_step(self.run_id, "ssh")
        self.parser = ArgumentParser()
        add_parsers(self.parser.add_subparsers(dest="command"))
        self.calls = []
        self.enterContext(redirect_stderr(io.StringIO()))
        self.enterContext(patch.dict(os.environ, {}, clear=True))
        self.komodo = Mock()
        self.komodo._request.side_effect = [
            HttpStatusError("Komodo", "ListSecrets", 503),
            ["LAB_NEWT_ID", "LAB_NEWT_SECRET"],
            [{"name": "lab", "info": {"state": "Ok"}}],
        ]
        self.fail_stage = False
        self.online_checks = 0

    def execute(self, *argv, device=None, dispatch=None):
        device = device or self.device
        args = self.parser.parse_args([argv[0], device["name"], *argv[1:]])
        return run(args, device, self.runs, dispatch or self.dispatch, lambda: self.komodo)

    def dispatch(self, arguments):
        self.assertEqual(arguments[:2], ["--database", str(self.database)])
        command = arguments[2:]
        self.calls.append(command)
        action = command[0]
        report = {}
        if action == "komodo" and command[1] == "register":
            if "--apply" in command:
                self.runs.complete_step(self.run_id, "komodo-registration")
            else:
                report = {"core_version": "2.3.4"}
        if action == "periphery" and self.fail_stage:
            return 1
        if action == "reconcile":
            scope = "--only" if "--only" in command else "--through"
            step = command[command.index(scope) + 1]
            if "--apply" not in command:
                self.assertEqual(step, "periphery")
                print('{"status": "incomplete", "checks": {"periphery": false}}')
                return 1
            self.runs.complete_step(self.run_id, step)
        if action == "periphery-recover":
            self.runs.complete_step(self.run_id, "periphery")
        if action == "komodo" and command[1] == "baseline" and "network" in command:
            self.runs.complete_step(self.run_id, "komodo-stacks")
        if action == "pangolin-enroll" and "--site-only" in command:
            step = self.runs.show(self.run_id)["next_step"]
            self.runs.record_resource(self.run_id, step, Resource(ResourceKind.PANGOLIN_SITE, "1", Ownership.REUSED))
        if action == "pangolin-enroll" and "--site-only" not in command and self.runs.show(self.run_id)["next_step"] == "pangolin":
            self.online_checks += 1
            if self.online_checks == 2:
                self.runs.complete_step(self.run_id, "pangolin")
        if action == "verify":
            self.runs.complete_step(self.run_id, "verify")
        print(json.dumps(report))
        return 0

    def test_profile_presets_and_preview_do_not_apply_any_operation(self):
        self.assertEqual(defaults("auto-install", self.device), (
            "ssh", "system", "docker", "bluetooth", "komodo", "goku-files", "goku-mounts", "periphery",
            "pangolin", "docker-proxy", "monitoring-agents", "newt", "pangolin-routes", "verify",
        ))
        self.assertEqual(defaults("auto-install", {"name": "zero", "profile": SERVER_PROFILE}),
                         ("ssh", "system", "docker", "bluetooth"))
        self.assertEqual(defaults("auto-install", {"name": "nas", "profile": NAS_PROFILE}),
                         tuple(key for key in defaults("auto-install", self.device) if key != "bluetooth"))
        self.assertEqual(defaults("auto-uninstall", {"name": "nas", "profile": NAS_PROFILE}), MONITORING)
        report = self.execute("auto-install", "--public-key", "/fixture.pub")
        self.assertEqual((report["status"], report["next_step"]), ("preview", "komodo-registration"))
        self.assertEqual(self.calls, [])

    def test_install_blocks_then_resumes_in_order_after_core_and_newt_reconnect(self):
        flags = ("auto-install", "--public-key", "/fixture.pub", "--apply")
        self.fail_stage = True
        result = self.execute(*flags)
        self.assertEqual((result["status"], result["next_step"]), ("blocked", "goku-files"))
        self.assertEqual(self.calls[-1], ["periphery", "lab", "--nfs-export", "/onboardrr-lab"])
        self.assertEqual([args[4] for args in self.calls[:3]], ["system", "docker", "bluetooth"])
        self.assertEqual(len(self.calls), 6)
        self.calls.clear()
        self.fail_stage = False

        def configure_mounts(job):
            job.prepare("goku-mounts")
            self.runs.complete_step(self.run_id, "linux")

        with (
            patch.object(Automation, "prepare_owned_mounts", configure_mounts),
            patch("onboardrr.automatic.sleep") as wait,
        ):
            result = self.execute(*flags)
        self.assertEqual((result["status"], result["next_step"]), ("applied", None))
        self.assertEqual([args[0] for args in self.calls], [
            "periphery", "reconcile", "prepare", "reconcile", "periphery-recover",
            "pangolin-enroll", "komodo", "komodo", "komodo", "pangolin-enroll", "pangolin-enroll", "verify",
        ])
        self.assertIn("--skip-openbao-agent", self.calls[2])
        self.assertEqual(wait.call_count, 2)
        self.calls.clear()
        self.assertEqual(self.execute(*flags)["status"], "applied")
        self.assertEqual(self.calls, [])

    def test_sudo_password_is_shared_by_auto_steps_but_never_forwarded_to_goku(self):
        passwords = []

        def prepare(args, *_):
            passwords.append(sudo_password(args))
            return {}

        def dispatch_steps(_args, _device, _runs, dispatch, _komodo):
            prefix = ["--database", str(self.database)]
            for _ in range(2):
                self.assertEqual(dispatch([*prefix, "prepare", "lab", "--component", "system",
                                           "--ask-become-pass", "--apply"]), 0)
            self.assertEqual(dispatch([*prefix, "retire-export", "lab", "--goku-ssh", "goku.local",
                                       "--ask-become-pass", "--apply"]), 0)
            return {"status": "applied"}

        secret = " fixture sudo é "
        with (
            password_pipe(secret) as descriptor,
            patch("onboardrr.__main__.automatic.run", side_effect=dispatch_steps),
            patch("onboardrr.__main__.prepare_command", side_effect=prepare),
            patch("onboardrr.__main__.retire_goku_export", return_value={}) as export,
            patch("onboardrr.sudo_input.getpass.getpass") as prompt,
            redirect_stdout(io.StringIO()) as output,
        ):
            code = main(["--database", str(self.database), "--sudo-password-fd", str(os.dup(descriptor)),
                         "auto-uninstall", "lab", "--ask-become-pass", "--apply"])
        self.assertEqual(code, 0)
        self.assertEqual(passwords, [secret, secret])
        self.assertIsNone(export.call_args.kwargs["password"])
        self.assertNotIn(secret, output.getvalue())
        prompt.assert_not_called()

    def test_service_selection_requires_periphery_before_apply(self):
        report = self.execute("auto-install", "--select", "workloads", "--public-key", "/fixture.pub", "--apply")
        self.assertEqual(report["status"], "blocked")
        self.assertIn("Komodo and Periphery", report["blockers"][0])
        self.assertFalse(self.calls)

    def test_explicit_choices_run_only_selected_components_in_workflow_order(self):
        with (patch.dict(os.environ, {"ONBOARDRR_MONITORING_HOST": "core"}),
              patch("onboardrr.automatic.emit") as progress):
            report = self.execute("auto-install", "--select", "beszel", "--select", "docker",
                                  "--select", "system", "--select", "wud", "--apply")
        self.assertEqual(report["status"], "applied")
        self.assertEqual(report["selected"], ["system", "docker", "wud", "beszel"])
        self.assertEqual([event.kwargs for event in progress.call_args_list if event.args == ("overall",)],
                         [{"completed": count, "total": 4} for count in range(5)])
        self.assertEqual([event.kwargs["done"] for event in progress.call_args_list if event.args == ("step",)],
                         [False, True] * 4)
        self.assertEqual([(command[0], command[command.index("--component") + 1]) for command in self.calls], [
            ("prepare", "system"), ("prepare", "docker"), ("monitoring", "wud"), ("monitoring", "beszel"),
        ])
        self.assertLess(list(choices("auto-install", self.device)).index("goku-mounts"),
                        list(choices("auto-install", self.device)).index("periphery"))

    def test_komodo_registration_does_not_stage_or_mount_goku(self):
        report = self.execute("auto-install", "--select", "komodo", "--public-key", "/fixture.pub", "--apply")
        self.assertEqual((report["status"], report["next_step"]), ("applied", "goku-files"))
        self.assertEqual([command[:2] for command in self.calls], [["komodo", "register"], ["komodo", "register"]])

    def test_existing_goku_shares_can_be_mounted_without_selecting_komodo(self):
        with patch.object(Automation, "prepare_owned_mounts", lambda job: job.prepare("goku-mounts")):
            report = self.execute("auto-install", "--select", "goku-mounts", "--select", "system",
                                  "--public-key", "/fixture.pub", "--apply")
        self.assertEqual((report["status"], report["next_step"]), ("applied", "komodo-registration"))
        self.assertEqual([command[4] for command in self.calls], ["system", "goku-mounts"])

    def test_grouped_goku_requires_registration_before_any_storage_changes(self):
        for mode in ((), ("--apply",)):
            report = self.execute("auto-install", "--select", "goku-files", "--select", "goku-mounts",
                                  "--public-key", "/fixture.pub", *mode)
            self.assertEqual(report["status"], "blocked")
            self.assertIn("Komodo registration", report["blockers"][0])
        self.assertEqual(self.calls, [])

    def test_goku_mount_check_records_ownership_and_advances_once(self):
        for step in ("komodo-registration", "goku-files"):
            self.runs.complete_step(self.run_id, step)
        compose = "/home/operator/.docker/compose"
        data = "/share/docker/komodo/data/lab"
        mounts = [Resource(ResourceKind.MOUNT, path, Ownership.REUSED) for path in (compose, data)]
        with (
            patch("onboardrr.ssh.load_public_key"),
            patch("onboardrr.ssh.agent_identity", return_value=nullcontext()),
            patch("onboardrr.ssh.connect", return_value=nullcontext()),
            patch("onboardrr.ssh.script", return_value=json.dumps({
                "compose_mount": compose, "detached": {compose: True, data: False},
            })),
            patch("onboardrr.automatic.host_resources", return_value=mounts) as verify,
        ):
            report = self.execute("auto-install", "--select", "goku-files", "--select", "goku-mounts",
                                  "--public-key", "/fixture.pub", "--apply")
        verify.assert_called_once()
        self.assertEqual([command[0] for command in self.calls], ["prepare"])
        self.assertEqual((report["status"], report["next_step"]), ("applied", "periphery"))
        self.assertEqual({item["reference"]: item["ownership"] for item in self.runs.show(self.run_id)["resources"]},
                         {compose: Ownership.CREATED, data: Ownership.REUSED})

    def test_termix_is_optional_and_uses_the_auto_install_and_remove_dispatch(self):
        with patch.dict(os.environ, {"ONBOARDRR_TERMIX_URL": "https://termix.example"}):
            self.assertIn("termix", defaults("auto-install", self.device))
            self.assertIn("termix", defaults("auto-uninstall", self.device))
            self.assertEqual(self.execute("auto-install", "--select", "termix", "--apply")["status"], "applied")
            self.assertEqual(self.execute("auto-uninstall", "--select", "termix", "--apply")["status"], "applied")
        self.assertEqual(self.calls, [["termix", "lab", "--apply"], ["termix", "lab", "--remove", "--apply"]])

    def test_existing_legacy_periphery_resumes_without_fresh_agent_setup(self):
        def dispatch(argv):
            command = argv[2:]
            self.calls.append(command)
            if command[:2] == ["komodo", "register"]:
                self.assertIn("--public-key", command)
                if "--apply" in command:
                    for step in ("komodo-registration", "goku-files", "linux", "periphery"):
                        self.runs.complete_step(self.run_id, step)
                print('{"core_version":"2.3.4","registration":"legacy-inbound"}')
            else:
                print("{}")
            return 0

        result = self.execute("auto-install", "--select", "system", "--select", "komodo",
                              "--public-key", "/fixture.pub", "--apply", dispatch=dispatch)
        self.assertEqual((result["status"], result["next_step"]), ("applied", "komodo-stacks"))
        self.assertEqual([command[0] for command in self.calls], ["prepare", "komodo", "komodo"])
        self.assertEqual(self.calls[0][3:5], ["--component", "system"])
        self.assertIn("--expect-legacy", self.calls[2])

    def test_auto_agent_uses_controller_settings_without_a_separate_token_file(self):
        with patch.dict(os.environ, {"ONBOARDRR_BAO_URL": "https://bao.example"}):
            self.assertIn("openbao-agent", defaults("auto-install", self.device))
            result = self.execute("auto-install", "--select", "system", "--select", "openbao-agent", "--apply")
        self.assertEqual(result["status"], "applied")
        self.assertNotIn("--ensure-openbao-agent", self.calls[0])
        self.assertIn("--ensure-openbao-agent", self.calls[1])
        self.assertNotIn("--skip-openbao-agent", self.calls[1])

    def test_qnap_system_preset_uses_entware_preparation_without_linux_operations(self):
        nas = self.inventory.add("nas", None, "operator", 22, NAS_PROFILE)
        identifier = self.runs.open("nas")["id"]
        self.runs.complete_step(identifier, "ssh")
        result = self.execute("auto-install", "--select", "system", "--select", "docker",
                              "--public-key", "/fixture.pub", "--apply", device=nas)
        self.assertEqual(result["status"], "applied")
        self.assertEqual(len(self.calls), 2)
        self.assertEqual([command[4] for command in self.calls], ["system", "docker"])
        self.assertTrue(all("--skip-monitoring" in command for command in self.calls))

    def test_qnap_runs_full_installation_and_reaches_the_verified_checkpoint(self):
        nas = self.inventory.add("nas", "nas.local", "operator", 22, NAS_PROFILE)
        self.run_id = self.runs.open("nas")["id"]
        self.runs.complete_step(self.run_id, "ssh")
        self.komodo._request.side_effect = [
            ["NAS_NEWT_ID", "NAS_NEWT_SECRET"], [{"name": "nas", "info": {"state": "Ok"}}],
        ]

        def dispatch(argv):
            if argv[2:3] == ["prepare"] and "docker" in argv:
                self.runs.complete_step(self.run_id, "nas-prepare")
            return self.dispatch(argv)

        with (patch.dict(os.environ, {"ONBOARDRR_BAO_URL": "https://bao.example"}),
              patch("onboardrr.automatic.sleep")):
            result = self.execute("auto-install", "--public-key", "/fixture.pub", "--apply",
                                  device=nas, dispatch=dispatch)
        self.assertEqual((result["status"], result["next_step"]), ("applied", None))
        self.assertEqual([command[4] for command in self.calls if command[0] == "prepare"],
                         ["system", "docker", "docker-auth", "openbao-agent", "goku-mounts"])
        self.assertIn("Verify NAS storage and Container Station", result["finished"])
        self.assertIn(["periphery", "nas", "--nfs-export", "/onboardrr-nas"], self.calls)
        mounts = next(call for call in self.calls if call[0] == "prepare" and "goku-mounts" in call)
        self.assertIn("//goku.local/compose", mounts)
        self.assertIn("goku.local:/onboardrr-nas", mounts)
        self.assertEqual([command[-2] for command in self.calls if command[:2] == ["komodo", "baseline"]],
                         ["docker", "monitoring", "network"])
        self.assertNotIn("wifi", result["selected"])
        self.assertNotIn("bluetooth", result["selected"])

    def test_monitoring_only_retirement_never_removes_stacks_or_asks_for_ssh(self):
        stack = {"kind": ResourceKind.KOMODO_STACK, "reference": "1" * 24, "action": "remove"}
        with patch("onboardrr.automatic.retirement_plan", return_value={"actions": [stack], "monitoring": {}}):
            def dispatch(argv):
                self.calls.append(argv[2:])
                print('{"monitoring": {"status": "removed"}}')
                return 0
            result = self.execute("auto-uninstall", "--select", "wud", "--apply", dispatch=dispatch)
        self.assertEqual(result["status"], "applied")
        self.assertEqual(self.calls, [["monitoring", "lab", "--component", "wud", "--remove", "--apply"]])

    def test_failed_workload_removal_blocks_later_operations(self):
        self.runs.record_resource(self.run_id, "komodo-registration", Resource(
            ResourceKind.KOMODO_STACK, "1" * 24, Ownership.CREATED,
        ))

        def dispatch(argv):
            self.calls.append(argv[2:])
            print(json.dumps({"actions": [{"kind": "komodo-stack", "reference": "1" * 24,
                                           "action": "remove", "status": "pending", "reason": "compose-project-shared"}]}))
            return 0

        result = self.execute("auto-uninstall", "--select", "workloads", "--select", "host",
                              "--public-key", "/fixture.pub", "--apply", dispatch=dispatch)
        self.assertEqual(result["status"], "blocked")
        self.assertIn("compose-project-shared", result["blockers"][0])
        self.assertEqual(self.calls, [["retire", "lab", "--apply", "--skip-monitoring", "--stack-kind", "network"]])
