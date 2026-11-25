import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from onboardrr.remote import compose_update
from onboardrr.stack_update import manage_compose_files, plan_compose_file, update_stack, wud_versions


def observation(identifier, current, offered):
    return {
        "id": identifier, "updateAvailable": True,
        "image": {"tag": {"value": current}}, "result": {"tag": offered},
    }


KOMODO_PAIR = {
    "komodo-postgres": "ghcr.io/ferretdb/postgres-documentdb:17-0.107.0-ferretdb-2.7.0",
    "komodo-ferretdb": "ghcr.io/ferretdb/ferretdb:2.7.0",
}
KOMODO_UPDATE = {
    "komodo-postgres": "ghcr.io/ferretdb/postgres-documentdb:17-0.108.0-ferretdb-2.8.0",
    "komodo-ferretdb": "ghcr.io/ferretdb/ferretdb:2.8.0",
}


class WudVersionsTest(unittest.TestCase):
    def test_host_update_deploys_only_matched_projects_and_reports_independent_failures(self):
        device = {"name": "nas", "profile": "nas"}
        api = Mock()
        api.request.return_value = [observation("match", "1.2", "1.3")]
        snapshot = {"containers": [{"id": "match", "service": "web", "image": "app:1.2"}],
                    "images": {"web": "app:1.2"}}
        answers = [{"projects": ["live", "inaccessible", "current"]}, snapshot, {"status": "updated"},
                   {"error": "Compose path unavailable."}, {"containers": [], "images": {"app": "existing:1"}}]
        with (patch("onboardrr.stack_update.device_lock"), patch("onboardrr.ssh.load_public_key"),
              patch("onboardrr.ssh.agent_identity"), patch("onboardrr.ssh.connect"),
              patch("onboardrr.stack_update.configured_wud", return_value=(api, {})),
              patch("onboardrr.ssh.script", side_effect=map(json.dumps, answers)) as remote):
            result = update_stack(None, device, None, None, None, apply=True)
        self.assertEqual(result["status"], "partial")
        self.assertEqual({key: item["status"] for key, item in result["projects"].items()},
                         {"live": "updated", "inaccessible": "failed", "current": "no-updates"})
        calls = [json.loads(call.args[2]) for call in remote.call_args_list]
        self.assertEqual([item for item in calls if item.get("apply")], [{
            "nas": True, "project": "live", "wud": True, "apply": True, "expected": snapshot,
            "versions": {"web": "1.3"}, "files_only": False,
        }])
        self.assertTrue(all(call.kwargs["entware"] for call in remote.call_args_list))

    def test_manual_refresh_reuses_the_snapshot_and_does_not_consult_wud(self):
        device = {"name": "lab", "profile": "raspberry-pi"}
        snapshot = {"containers": [], "images": {"web": "app:1.2"}}
        with (patch("onboardrr.stack_update.device_lock"), patch("onboardrr.ssh.load_public_key"),
              patch("onboardrr.ssh.agent_identity"), patch("onboardrr.ssh.connect"),
              patch("onboardrr.stack_update.configured_wud") as wud,
              patch("onboardrr.ssh.script", side_effect=[json.dumps(snapshot), '{"status":"updated"}']) as remote):
            result = update_stack(None, device, "live", None, None, apply=True, refresh=True)
        wud.assert_not_called()
        self.assertEqual(result["status"], "updated")
        self.assertEqual(json.loads(remote.call_args.args[2]), {
            "nas": False, "project": "live", "wud": False, "apply": True,
            "expected": snapshot, "versions": {}, "files_only": False,
        })

    def test_invalid_selected_file_is_reported_as_incomplete(self):
        snapshot = {"workspace": "/srv/compose", "architecture": "x86_64", "files": {
            "compose.yaml": {"text": "services: [", "digest": "fixture"},
        }}
        service = Mock()
        service.request.return_value = []
        host = {"name": "lab", "profile": "raspberry-pi"}
        with (patch("onboardrr.stack_update.device_lock"), patch("onboardrr.ssh.load_public_key"),
              patch("onboardrr.ssh.agent_identity"), patch("onboardrr.ssh.connect"),
              patch("onboardrr.stack_update.configured_wud", return_value=(service, {})),
              patch("onboardrr.ssh.script", return_value=json.dumps(snapshot)) as remote):
            result = manage_compose_files(None, host, None, None, files=["compose.yaml"], apply=True, deploy=True)
        self.assertEqual(result["status"], "deployment-incomplete")
        self.assertEqual(result["blockers"], ["compose.yaml: Invalid Compose YAML."])
        remote.assert_called_once()

    def test_controller_passes_wud_updates_to_deployment_only_on_apply(self):
        item = observation("fixture-id", "1.2", "1.3")
        item["image"].update({"name": "app/core", "registry": {"url": "https://ghcr.io/v2"},
                              "os": "linux", "architecture": "amd64"})
        snapshot = {"workspace": "/srv/compose", "architecture": "x86_64", "files": {
            "compose.yaml": {"text": "services: {core: {image: ghcr.io/app/core:1.2}}\n", "digest": "fixture"},
        }}
        service = Mock()
        service.request.return_value = [item]
        host = {"name": "lab", "profile": "raspberry-pi"}
        with (patch("onboardrr.stack_update.device_lock"), patch("onboardrr.ssh.load_public_key"),
              patch("onboardrr.ssh.agent_identity"), patch("onboardrr.ssh.connect"),
              patch("onboardrr.stack_update.configured_wud", return_value=(service, {})),
              patch("onboardrr.ssh.script", side_effect=[
                  json.dumps(snapshot), json.dumps(snapshot), '{"status":"deployed"}',
              ]) as remote):
            def run(*, apply):
                return manage_compose_files(None, host, None, None, files=["compose.yaml"], apply=apply, deploy=True)

            self.assertEqual(run(apply=False)["status"], "preview")
            remote.assert_called_once()
            self.assertEqual(run(apply=True)["status"], "deployed")
        request = json.loads(remote.call_args.args[2])
        self.assertEqual(request["changes"], {"compose.yaml": snapshot["files"]["compose.yaml"]["text"].replace("1.2", "1.3")})
        self.assertEqual(request["deployment"], {
            "images": {"compose.yaml": {"core": "ghcr.io/app/core:1.3"}},
            "updated": {"compose.yaml": ["core"]}, "wud_tags": [["fixture-id", "1.2", "1.3"]],
        })

    def test_selected_file_edits_only_the_matching_literal_image(self):
        original = (
            '# App stack\r\nservices:\r\n'
            '  core:\r\n    image: "ghcr.io/app/core:1.2" # keep this\r\n'
            '    labels: {wud.tag.include: "1\\\\..*"}\r\n'
            '  other:\r\n    image: ${OTHER_IMAGE}\r\n'
        )
        item = observation("a", "1.2", "1.3")
        item["image"].update({"name": "app/core", "registry": {"url": "https://ghcr.io/v2"},
                              "os": "linux", "architecture": "arm64"})
        item["includeTags"] = r"1\..*"
        wrong = {**item, "result": {"tag": "9.0"}, "image": {**item["image"], "architecture": "amd64"}}
        updated, report = plan_compose_file(original, "aarch64", "lab", [item, wrong])
        self.assertEqual(updated, original.replace("app/core:1.2", "app/core:1.3"))
        self.assertEqual(report["services"], ["core", "other"])
        self.assertEqual(list(report["updates"]), ["core"])

    def test_file_preview_waits_for_matching_komodo_releases_and_preserves_postgres_major(self):
        original = "services:\n" + "".join(f"  {name}:\n    image: {image}\n" for name, image in KOMODO_PAIR.items())
        offered = []
        for service, image in KOMODO_PAIR.items():
            name, old = image.removeprefix("ghcr.io/").split(":")
            item = observation(service, old, KOMODO_UPDATE[service].split(":")[1])
            item["image"].update({"name": name, "registry": {"url": "https://ghcr.io/v2"},
                                  "os": "linux", "architecture": "amd64"})
            offered.append(item)
        updated, report = plan_compose_file(original, "x86_64", "zero", offered[1:])
        self.assertEqual(updated, original)
        self.assertIn("matching", report["skipped"]["komodo-ferretdb"])
        updated, report = plan_compose_file(original, "x86_64", "zero", offered)
        self.assertEqual({name: item["to"] for name, item in report["updates"].items()}, KOMODO_UPDATE)
        self.assertTrue(all(image in updated for image in KOMODO_UPDATE.values()))
        self.assertEqual(plan_compose_file(original, "x86_64", "zero", offered, resume_database=True)[0], original)
        offered[0]["result"]["tag"] = offered[0]["result"]["tag"].replace("17-", "18-", 1)
        updated, report = plan_compose_file(original, "x86_64", "zero", offered)
        self.assertEqual(updated, original)
        self.assertIn("major upgrade", report["skipped"]["komodo-postgres"])

    def test_uses_exact_container_and_deployed_tag(self):
        snapshot = {
            "containers": [{"id": "selected", "service": "web", "image": "repo:1.2"}],
            "images": {"web": "repo:1.2"},
        }
        entries = [
            observation("unrelated", "1.2", "3.0"),
            observation("selected", "1.1", "4.0"),
            observation("selected", "1.2", "$INVALID"),
            observation("selected", "1.2", "1.3"),
        ]
        self.assertEqual(wud_versions(snapshot, entries), {"web": "1.3"})
        snapshot["images"]["web"] = "repo:2.0"
        self.assertEqual(wud_versions(snapshot, entries), {})

    def test_refuses_conflicting_versions_between_replicas(self):
        snapshot = {
            "containers": [{"id": name, "service": "web", "image": "repo:1.2"} for name in ("a", "b")],
            "images": {"web": "repo:1.2"},
        }
        with self.assertRaisesRegex(ValueError, "conflicting versions"):
            wud_versions(snapshot, [observation("a", "1.2", "1.3"), observation("b", "1.2", "1.4")])

    def test_matches_a_tag_already_selected_in_the_source(self):
        snapshot = {"containers": [{"id": "a", "service": "web", "image": "repo:1.2"}],
                    "images": {"web": "repo:1.3"}}
        self.assertEqual(wud_versions(snapshot, [observation("a", "1.2", "1.3")]), {"web": "1.3"})
        snapshot["containers"].append({"id": "b", "service": "web", "image": "repo:1.4"})
        self.assertEqual(wud_versions(snapshot, [observation("a", "1.2", "1.3")]), {})


class ComposeUpdateTest(unittest.TestCase):
    def test_komodo_database_upgrade_verifies_extension_before_starting_clients_and_resumes(self):
        directory = self.directory
        desired = {**KOMODO_UPDATE, "web": "repo:2"}
        command = ["compose", "--project-name", "komodo"]
        plan = [("komodo", directory, command, desired)]
        checkpoint = compose_update.komodo_database_checkpoint(directory)
        fail_extension = True

        def docker(arguments, stage, **options):
            if stage == "DocumentDB extension upgrade and verification":
                self.assertIn("ALTER EXTENSION documentdb UPDATE", options["payload"])
                if fail_extension:
                    raise ValueError("DocumentDB extension upgrade failed.")

        with (patch.object(compose_update, "configured_images", return_value=desired),
              patch.object(compose_update, "docker", side_effect=docker) as runner):
            self.assertEqual(compose_update.deploy_selected(plan)[0]["status"], "failed")
            self.assertEqual(json.loads(checkpoint.read_text()), KOMODO_UPDATE)
            self.assertEqual([call.args[0][-1] for call in runner.call_args_list if "up" in call.args[0]],
                             ["komodo-postgres"])
            fail_extension = False
            runner.reset_mock()
            self.assertEqual(compose_update.deploy_selected(plan)[0]["status"], "deployed")
            self.assertEqual([call.args[0][-1] for call in runner.call_args_list if "up" in call.args[0]],
                             ["komodo-postgres", "komodo-ferretdb", "komodo", "web"])
            stages = [call.args[1] for call in runner.call_args_list]
            self.assertLess(stages.index("pause Komodo database clients"),
                            stages.index("DocumentDB extension upgrade and verification"))
            self.assertFalse(checkpoint.exists())

    def test_komodo_checkpoint_matches_stopped_database_services_when_wud_has_no_updates(self):
        directory = self.directory.resolve()
        path = directory / "compose.komodo.yml"
        path.touch()
        compose_update.komodo_database_checkpoint(directory).write_text(json.dumps(KOMODO_UPDATE))
        rows = [
            [name + "-id", image, name, str(directory), str(path), self.environment_label]
            for name, image in {**KOMODO_PAIR, "komodo-postgres": KOMODO_UPDATE["komodo-postgres"]}.items()
        ]
        deployment = {"images": {path.name: KOMODO_UPDATE}, "updated": {}, "wud_tags": []}
        with (patch.object(compose_update, "inspect_compose_containers",
                           side_effect=[[[*rows[0], "komodo"]], rows]),
              patch.object(compose_update, "source_environment", return_value=({}, {})),
              patch.object(compose_update, "AGENT_STATE", self.directory)):
            (_, _, _, targets), = compose_update.deployment_plan({path.name: (path,)}, deployment)
        self.assertEqual(targets, KOMODO_UPDATE)

    def test_deploys_only_changed_services_using_recorded_files_and_can_retry(self):
        base = (self.directory / ".docker/compose").resolve()
        base.mkdir(parents=True)
        target = base / "compose.yaml"
        companion = base / "docker-compose.extra.yml"
        original = "services:\n  web:\n    image: repo:1.2\n"
        updated = original.replace("repo:1.2", "repo:1.3")
        target.write_text(original)
        companion.write_text("services: {sidecar: {image: sidecar:1}}\n")
        files = f"{target},{companion}"
        running = {"web": "repo:1.2"}
        fail_pull = True

        def docker(command, stage, **options):
            self.calls.append((command, stage, options))
            if stage == "project discovery":
                return "web-id\nsidecar-id\n"
            if stage == "container inspection":
                return "\n".join(json.dumps(row) for row in [
                    ["web-id", running["web"], "web", str(base), files, str(self.environment), "lab-project"],
                    ["sidecar-id", "sidecar:1", "sidecar", str(base), files, str(self.environment), "lab-project"],
                ])
            if stage == "Compose validation":
                return json.dumps({"services": {"web": {"image": target.read_text().split()[-1]}}})
            if stage == "image pull" and fail_pull:
                raise ValueError("Docker failed during image pull (exit 1).")
            if stage == "Compose deployment and health verification":
                running["web"] = "repo:1.3"
            return ""

        request = {"host": "lab", "files": [target.name]}
        with (patch.object(Path, "home", return_value=self.directory),
              patch.object(compose_update, "docker", side_effect=docker)):
            def apply(changes, edited):
                snapshot = compose_update.compose_files({**request, "action": "read-files"})
                return compose_update.compose_files({
                    **request, "action": "write-files", "changes": changes,
                    "expected": {target.name: snapshot["files"][target.name]["digest"]},
                    "deployment": {"images": {target.name: {"web": "repo:1.3"}}, "updated": edited,
                                   "wud_tags": [["web-id", "1.2", "1.3"]]},
                })

            result = apply({target.name: updated}, {target.name: ["web"]})
            self.assertEqual(result["status"], "deployment-incomplete")
            self.assertIn("image pull", result["blockers"][0])
            self.assertEqual(target.read_text(), updated)
            self.assertFalse(any("up" in command for command, _, _ in self.calls))

            fail_pull = False
            result = apply({}, {})
            self.assertEqual(result["deployments"], [
                {"project": "lab-project", "services": ["web"], "status": "deployed"},
            ])
            command = self.calls[-1][0]
            self.assertIn("lab-project", command)
            self.assertIn(str(self.environment), command)
            self.assertIn(str(companion), command)
            self.assertIn("--no-deps", command)
            self.assertIn("--wait", command)
            self.assertEqual(command[-2:], ["--", "web"])
            self.assertEqual(list(base.glob(".onboardrr-*.images.yml")), [])
            self.assertEqual(apply({}, {})["status"], "no-updates")

    def test_deploy_blocks_before_writing_when_the_local_stack_cannot_be_matched(self):
        base = (self.directory / ".docker/compose").resolve()
        base.mkdir(parents=True)
        target = base / "compose.yaml"
        original = "services:\n  web:\n    image: repo:1.2\n"
        target.write_text(original)
        request = {"host": "lab", "files": [target.name]}
        with (patch.object(Path, "home", return_value=self.directory),
              patch.object(compose_update, "docker", return_value="")):
            snapshot = compose_update.compose_files({**request, "action": "read-files"})
            with self.assertRaisesRegex(ValueError, "No running local Compose service"):
                compose_update.compose_files({
                    **request, "action": "write-files", "changes": {target.name: original.replace("1.2", "1.3")},
                    "expected": {target.name: snapshot["files"][target.name]["digest"]},
                    "deployment": {"images": {target.name: {"web": "repo:1.3"}},
                                   "updated": {target.name: ["web"]}, "wud_tags": []},
                })
        self.assertEqual(target.read_text(), original)

    def test_selected_workspace_updates_original_files_and_guards_the_snapshot(self):
        base = self.directory / ".docker/compose"
        base.mkdir(parents=True)
        (base / "lab").mkdir()
        (base / "lab/compose.komodo.yml").touch()
        target = base / "compose.yaml"
        target.write_text("services: {}\n")
        second = base / "docker-compose.worker.yml"
        second.write_text("services: {}\n")
        (base / "notes.yml").touch()
        (base / "linked-compose.yml").symlink_to(target)
        with patch.object(Path, "home", return_value=self.directory):
            selection = compose_update.compose_files({"action": "list-files", "host": "lab"})
            self.assertEqual(selection["workspace"], str(base.resolve()))
            self.assertEqual(selection["files"], [target.name, second.name])
            request = {"host": "lab", "workspace": selection["workspace"], "files": selection["files"]}
            snapshot = compose_update.compose_files({**request, "action": "read-files"})
            write = {**request, "action": "write-files",
                     "changes": {target.name: "services: {app: {}}\n", second.name: "services: {worker: {}}\n"},
                     "expected": {name: data["digest"] for name, data in snapshot["files"].items()}}
            compose_update.compose_files(write)
            self.assertEqual(target.read_text(), write["changes"][target.name])
            self.assertEqual(second.read_text(), write["changes"][second.name])
            self.assertEqual(list(base.glob(".onboardrr-*.images.yml")), [])
            with self.assertRaisesRegex(ValueError, "changed"):
                compose_update.compose_files(write)
            with self.assertRaisesRegex(ValueError, "regular Compose"):
                compose_update.compose_files({**request, "action": "read-files", "files": ["linked-compose.yml"]})
            with self.assertRaisesRegex(ValueError, "directly"):
                compose_update.compose_files({**request, "action": "read-files", "files": ["lab/compose.komodo.yml"]})

    def test_platform_wud_deploy_uses_the_flat_manifest_as_its_source(self):
        target = self.directory.resolve() / "compose.openbao.yml"
        target.touch()
        name = "openbao-agent-compose"
        old_directory = self.directory / "old"
        row = ["container", "openbao:1", name, str(old_directory), str(old_directory / "compose.yaml"),
               self.environment_label, "secrets"]
        deployment = {"images": {target.name: {name: "openbao:2"}}, "updated": {target.name: [name]},
                      "wud_tags": []}
        with (patch.object(compose_update, "inspect_compose_containers", return_value=[row]),
              patch.object(compose_update, "source_environment", return_value=({}, {})),
              patch.object(compose_update, "AGENT_STATE", self.directory)):
            (project, directory, command, targets), = compose_update.deployment_plan({target.name: (target,)}, deployment)
        self.assertEqual((project, directory, targets), ("secrets", target.parent, {name: "openbao:2"}))
        self.assertIn(str(target), command)
        self.assertNotIn(str(old_directory / "compose.yaml"), command)

    def test_shell_hostname_does_not_override_compose_configuration(self):
        with patch.dict(os.environ, {"HOSTNAME": "shell-host"}):
            self.assertNotIn("HOSTNAME", compose_update.docker_environment())

    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.directory = Path(folder.name)
        self.compose = self.directory / "compose.yml"
        self.compose.write_text("services: {}\n")
        self.environment = self.directory / ".env"
        self.environment.write_text("VALUE=fixture\n")
        self.environment_label = str(self.environment)
        self.override = self.directory / ".onboardrr-lab.images.yml"
        self.calls = []
        self.image = "registry.example:5000/repo:1.2"
        self.container = "fixture-id"
        self.files = str(self.compose)

    def docker(self, command, stage, **options):
        self.calls.append((command, stage, options))
        if stage == "project discovery":
            return self.container + "\n" if self.container else ""
        if stage == "container inspection":
            return json.dumps([self.container, self.image, "web", str(self.directory),
                               self.files, self.environment_label]) + "\n"
        if stage == "Compose validation":
            return json.dumps({"services": {"web": {"image": self.image}}})
        return ""

    def test_preview_and_apply_reuse_the_project_paths_and_validate_overrides(self):
        with patch.object(compose_update, "docker", side_effect=self.docker):
            preview = compose_update.update({"project": "lab"})
            self.assertEqual([call[1] for call in self.calls], [
                "project discovery", "container inspection", "Compose validation",
            ])
            self.assertFalse(self.override.exists())
            result = compose_update.update({
                "project": "lab", "apply": True, "expected": preview,
                "versions": {"web": "1.3"},
            })
        self.assertEqual(result["images"]["web"], "registry.example:5000/repo:1.3")
        self.assertEqual(json.loads(self.override.read_text())["services"], {
            "web": {"image": "registry.example:5000/repo:1.3"},
        })
        self.assertEqual(self.compose.read_text(), "services: {}\n")
        command = self.calls[-1][0]
        self.assertIn(str(self.environment), command)
        self.assertIn(str(self.override), command)
        self.assertIn("--wait", command)
        self.assertNotIn("--remove-orphans", command)

    def test_explicit_base_manifest_change_discards_the_previous_pin(self):
        self.override.write_text(json.dumps({
            "x-onboardrr-project": "lab", "x-onboardrr-base-images": {"web": self.image},
            "services": {"web": {"image": "registry.example:5000/repo:1.3"}},
        }))
        self.image = "registry.example:5000/repo:2.0"
        with patch.object(compose_update, "docker", side_effect=self.docker):
            preview = compose_update.update({"project": "lab"})
            self.assertEqual(preview["images"]["web"], self.image)
            result = compose_update.update({"project": "lab", "apply": True, "expected": preview})
        self.assertEqual(result["images"]["web"], self.image)
        self.assertEqual(json.loads(self.override.read_text())["services"], {})

    def test_file_only_writes_separate_overrides_and_skips_unavailable_sources(self):
        second = self.directory / "second.yaml"
        second.write_text("services: {}\n")
        missing = self.directory / "missing.yaml"
        rows = [
            ["a", "one:1", "web", str(self.directory), str(self.compose), self.environment_label],
            ["b", "two:1", "worker", str(self.directory), str(second), self.environment_label],
            ["c", "three:1", "unavailable", str(self.directory), str(missing), self.environment_label],
        ]

        def docker(command, stage, **options):
            self.calls.append((command, stage, options))
            if stage == "project discovery":
                return "a\nb\nc\n"
            if stage == "container inspection":
                return "\n".join(json.dumps(row) for row in rows)
            if stage == "Compose validation":
                name, image = ("web", "one:1") if str(self.compose) in command else ("worker", "two:1")
                return json.dumps({"services": {name: {"image": image}}})
            self.assertEqual(stage, "version override validation")
            return ""

        request = {"project": "lab", "wud": True, "files_only": True}
        with patch.object(compose_update, "docker", side_effect=docker):
            preview = compose_update.update(request)
            self.assertEqual(set(preview["skipped"]), {"unavailable"})
            self.assertEqual(list(self.directory.glob(".onboardrr-*.images.yml")), [])
            result = compose_update.update({**request, "apply": True, "expected": preview,
                                            "versions": {"web": "2", "worker": "2"}})

        self.assertEqual(result["status"], "files-updated")
        self.assertEqual(result["images"], {"web": "one:2", "worker": "two:2"})
        self.assertEqual(len(result["overrides"]), 2)
        services = [json.loads(Path(path).read_text())["services"] for path in result["overrides"]]
        self.assertCountEqual(services, [{"web": {"image": "one:2"}}, {"worker": {"image": "two:2"}}])
        self.assertEqual(self.compose.read_text(), "services: {}\n")
        self.assertEqual(second.read_text(), "services: {}\n")

    def test_agent_environment_follows_original_env_files_and_is_not_duplicated(self):
        state = self.directory / "agent"
        (state / "compose-secrets").mkdir(parents=True)
        manifest = state / "compose-agent-projects.json"
        manifest.write_text('["lab"]')
        managed = state / "compose-secrets" / "lab.env"
        managed.write_text("PASSWORD=fixture\n")
        managed.chmod(0o640)
        lstat = Path.lstat

        def root_stat(path):
            info = lstat(path)
            return SimpleNamespace(st_mode=info.st_mode, st_size=info.st_size,
                                   st_uid=0 if path in {manifest, managed} else os.getuid())

        with patch.object(compose_update, "AGENT_STATE", state), patch.object(Path, "lstat", root_stat):
            for files in ([], [str(self.environment)], [str(managed), str(self.environment)]):
                self.assertEqual(compose_update.agent_environment("lab", self.directory, files),
                                 [str(self.environment), str(managed)])
            self.assertEqual(compose_update.agent_environment("other", self.directory, []), [])
            managed.chmod(0o644)
            with self.assertRaisesRegex(ValueError, "not private"):
                compose_update.agent_environment("lab", self.directory, [])
            managed.unlink()
            with self.assertRaisesRegex(ValueError, "has not rendered"):
                compose_update.agent_environment("lab", self.directory, [])
            manifest.write_text("[]")
            with self.assertRaisesRegex(ValueError, "no longer manages"):
                compose_update.agent_environment("lab", self.directory, [str(managed)])

    def test_agent_environment_replaces_the_legacy_file_with_the_host_cache(self):
        state = self.directory / "agent-state"
        state.mkdir()
        cache = self.directory / "operator" / ".config" / "docker" / "openbao" / "cache"
        cache.mkdir(parents=True)
        manifest = state / "compose-agent-projects.json"
        manifest.write_text(json.dumps({"projects": ["lab"], "cache": str(cache), "uid": os.getuid()}))
        legacy = state / "compose-secrets" / "lab.env"
        managed = cache / "lab.env"
        managed.write_text("PASSWORD=fixture\n")
        managed.chmod(0o600)
        lstat = Path.lstat

        def root_manifest(path):
            info = lstat(path)
            return SimpleNamespace(st_mode=info.st_mode, st_size=info.st_size,
                                   st_uid=0 if path == manifest else info.st_uid)

        with patch.object(compose_update, "AGENT_STATE", state), patch.object(Path, "lstat", root_manifest):
            self.assertEqual(compose_update.agent_environment("lab", self.directory, [str(self.environment), str(legacy)]),
                             [str(self.environment), str(managed)])
            new_cache = cache.parent / "compose" / "cache"
            new_cache.mkdir(parents=True)
            new_managed = new_cache / managed.name
            managed.rename(new_managed)
            manifest.write_text(json.dumps({"projects": ["lab"], "cache": str(new_cache), "uid": os.getuid()}))
            self.assertEqual(
                compose_update.agent_environment("lab", self.directory, [str(self.environment), str(legacy), str(managed)]),
                [str(self.environment), str(new_managed)],
            )
            new_managed.chmod(0o644)
            with self.assertRaisesRegex(ValueError, "not private"):
                compose_update.agent_environment("lab", self.directory, [])

    def test_dcu_uses_one_managed_env_file_before_starting_compose(self):
        manifest = self.directory / "compose-agent-projects.json"
        manifest.touch()
        env_files = [str(self.environment), "/var/lib/onboardrr/compose-secrets/lab.env"]
        with (
            patch.object(compose_update, "AGENT_STATE", self.directory),
            patch.object(compose_update, "agent_environment", return_value=env_files) as agent,
            patch.object(compose_update, "docker", return_value='{"name": "lab"}') as docker,
        ):
            result = compose_update.dcu_command(["--pull", "always"], self.directory)
        agent.assert_called_once_with("lab", self.directory, [])
        self.assertIn("--no-interpolate", docker.call_args_list[0].args[0])
        self.assertNotIn("--no-interpolate", docker.call_args_list[1].args[0])
        self.assertEqual(result, [
            "compose", "--project-name", "lab", "--env-file", env_files[0], "--env-file", env_files[1],
            "up", "--detach", "--pull", "always",
        ])

    def test_dcu_can_launch_without_agent_and_uses_explicit_project_when_set(self):
        with patch.object(compose_update, "AGENT_STATE", self.directory), patch.object(compose_update, "docker") as docker:
            self.assertEqual(compose_update.dcu_command([], self.directory), ["compose", "up", "--detach"])
            (self.directory / "compose-agent-projects.json").touch()
            with patch.object(compose_update, "agent_environment", side_effect=ValueError("agent file missing")):
                with self.assertRaisesRegex(ValueError, "agent file missing"):
                    compose_update.dcu_command(["-p", "lab"], self.directory)
            docker.assert_not_called()

    def test_dcu_named_komodo_selects_the_file_and_layers_managed_environments(self):
        (self.directory / "compose-agent-projects.json").touch()
        periphery = self.directory / "compose.komodo.yml"
        periphery.touch()
        config = str(self.directory / "cache" / "config.env")
        passkeys = str(self.directory / "cache" / "periphery.env")
        with (
            patch.object(compose_update, "AGENT_STATE", self.directory),
            patch.object(compose_update, "agent_environment", side_effect=[[config], [config, passkeys],
                                                                           [config, passkeys]]) as agent,
            patch.object(compose_update, "docker") as docker,
        ):
            result = compose_update.dcu_command(["komodo", "--dry-run", "--no-deps"], self.directory)
        self.assertEqual([operation.args[0] for operation in agent.call_args_list], ["config", "periphery", "komodo"])
        docker.assert_not_called()
        self.assertEqual(result, ["compose", "--dry-run", "--file", str(periphery.resolve()), "--project-name", "komodo",
                                  "--env-file", config, "--env-file", passkeys, "up", "--detach", "--no-deps"])

    def test_dcu_keeps_a_service_argument_when_no_named_file_matches(self):
        with patch.object(compose_update, "AGENT_STATE", self.directory):
            self.assertEqual(compose_update.dcu_command(["web"], self.directory),
                             ["compose", "up", "--detach", "web"])

    def test_dcu_uses_the_installed_periphery_environment_with_the_flat_manifest(self):
        installed = (self.directory / ".docker/compose").resolve()
        installed.mkdir(parents=True)
        compose = installed / "compose.komodo.yml"
        compose.touch()
        rows = [["id", "image", "komodo-agent", str(installed), str(compose), ""]]
        with (patch.object(compose_update.Path, "home", return_value=self.directory),
              patch.object(compose_update, "inspect_compose_containers", return_value=rows),
              patch.object(compose_update, "agent_environment") as agent):
            result = compose_update.dcu_command(["--dry-run", "komodo"], installed)
        agent.assert_not_called()
        self.assertEqual(result, ["compose", "--dry-run", "--project-directory", str(installed),
                                  "--file", str(compose), "--project-name", "komodo", "up", "--detach"])

    def test_nas_dcu_uses_the_private_rendered_environment_with_container_station(self):
        cache = self.directory / ".config/docker/openbao/compose/cache"
        cache.mkdir(parents=True)
        rendered = cache / "lab.env"
        rendered.write_text("FROM_AGENT=fixture\n")
        rendered.chmod(0o600)
        with (
            patch.object(compose_update.Path, "home", return_value=self.directory),
            patch.object(compose_update, "AGENT_STATE", self.directory),
            patch.object(compose_update, "NAS_DOCKER", True),
            patch.object(compose_update, "COMPOSE_CLI", "/station/bin/docker-compose"),
        ):
            args = compose_update.dcu_command(["-p", "lab"], self.directory)
            self.assertEqual(compose_update.docker_command(args), [
                "/station/bin/docker-compose", "--project-name", "lab", "--env-file", str(self.environment),
                "--env-file", str(rendered),
                "up", "--detach",
            ])
            self.assertEqual(compose_update.docker_environment()["DOCKER_HOST"], "unix:///var/run/docker.sock")
            with patch.dict(compose_update.os.environ, {}, clear=True):
                self.assertEqual(compose_update.docker_environment()["DOCKER_CONFIG"], str(self.directory / ".docker"))
            rendered.chmod(0o644)
            with self.assertRaisesRegex(ValueError, "not private"):
                compose_update.dcu_command(["-p", "lab"], self.directory)

    def test_dcu_rejects_a_project_name_changed_by_interpolation(self):
        (self.directory / "compose-agent-projects.json").touch()
        with (
            patch.object(compose_update, "AGENT_STATE", self.directory),
            patch.object(compose_update, "agent_environment", return_value=["/var/lib/onboardrr/compose-secrets/lab.env"]),
            patch.object(compose_update, "docker", side_effect=['{"name": "lab"}', '{"name": "other"}']),
        ):
            with self.assertRaisesRegex(ValueError, "project name"):
                compose_update.dcu_command([], self.directory)

    def test_refuses_missing_or_changed_project_before_apply(self):
        with patch.object(compose_update, "docker", side_effect=self.docker):
            preview = compose_update.update({"project": "lab"})
            self.container = "changed"
            with self.assertRaisesRegex(ValueError, "changed"):
                compose_update.update({"project": "lab", "apply": True, "expected": preview})
            self.environment.unlink()
            with self.assertRaisesRegex(ValueError, "missing"):
                compose_update.update({"project": "lab"})
        self.assertFalse(any(call[1] == "Compose update and health verification" for call in self.calls))

    def test_refuses_to_create_a_new_project(self):
        self.container = ""
        with patch.object(compose_update, "docker", side_effect=self.docker):
            with self.assertRaisesRegex(ValueError, "no existing containers"):
                compose_update.update({"project": "lab", "apply": True})
        self.assertEqual(len(self.calls), 1)

    def test_project_without_an_explicit_env_file_can_be_updated(self):
        self.environment_label = None
        self.environment.unlink()
        with patch.object(compose_update, "docker", side_effect=self.docker):
            preview = compose_update.update({"project": "lab"})
            result = compose_update.update({"project": "lab", "apply": True, "expected": preview})
        self.assertEqual(result["status"], "updated")
        self.assertNotIn("--env-file", self.calls[-1][0])
