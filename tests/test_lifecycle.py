import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

from onboardrr.goku_retirement import DATA_ROOT, mount_points, purge_goku
from onboardrr.http_json import HttpStatusError
from onboardrr.inventory import Inventory, PROFILE
from onboardrr.retirement import retire_baseline, retire_komodo_registration, retire_komodo_server, retirement_plan
from onboardrr.runs import Ownership, Resource, ResourceKind, RunStore
from onboardrr.verification import verify_run


class MountTableTest(unittest.TestCase):
    @unittest.skipUnless(sys.platform == "darwin", "macOS mount table")
    @patch("subprocess.check_output", side_effect=AssertionError("Do not spawn mount"))
    def test_reads_the_cached_mount_table_without_spawning_mount(self, command):
        self.assertIn(Path("/"), mount_points())
        command.assert_not_called()


class LifecycleTest(unittest.TestCase):
    def setUp(self):
        directory = Path(self.enterContext(TemporaryDirectory()))
        self.database = directory / "inventory.sqlite3"
        self.device = Inventory(self.database).add("lab", "lab.local", "operator", 22, PROFILE)
        self.runs = RunStore(self.database)
        self.run_id = self.runs.open("lab")["id"]
        self.server = "1" * 24
        self.stacks = {"docker": "2" * 24, "monitoring": "3" * 24, "network": "4" * 24}
        self.cifs = "/home/operator/.docker/compose"
        self.nfs = str(directory / "lab")
        data = Path(self.nfs)
        data.mkdir()
        self.data_file = data / "persisted.txt"
        self.data_file.write_text("disposable data")

        self.runs.complete_step(self.run_id, "ssh")
        self.record("komodo-registration", ResourceKind.KOMODO_SERVER, self.server)
        self.runs.complete_step(self.run_id, "komodo-registration")
        self.record("goku-files", ResourceKind.GOKU_DIRECTORY, f"{DATA_ROOT}/lab")
        self.runs.complete_step(self.run_id, "goku-files")
        for mount in (self.cifs, self.nfs):
            self.record("linux", ResourceKind.MOUNT, mount)
        for step in ("linux", "periphery"):
            self.runs.complete_step(self.run_id, step)
        for kind, identifier in self.stacks.items():
            owner = Ownership.REUSED if kind == "monitoring" else Ownership.CREATED
            self.record("komodo-stacks", ResourceKind.KOMODO_STACK, identifier, owner)
        self.runs.complete_step(self.run_id, "komodo-stacks")
        self.record("pangolin", ResourceKind.PANGOLIN_SITE, "9")
        self.runs.complete_step(self.run_id, "pangolin")

    def record(self, step, kind, reference, ownership=Ownership.CREATED):
        self.runs.record_resource(self.run_id, step, Resource(kind, reference, ownership))

    def server_list(self, state="Ok"):
        return [{"id": self.server, "name": "lab", "info": {"state": state}}]

    def stack_list(self, state="running"):
        return [{"id": identifier, "name": f"lab-{kind}", "info": {"server_id": self.server, "state": state}}
                for kind, identifier in self.stacks.items()]

    @patch("onboardrr.verification.host_snapshot")
    def test_verification_keeps_failed_checkpoint_and_resumes_after_ssh_recovers(self, host):
        komodo, pangolin = Mock(), Mock()
        core = [self.server_list(), self.stack_list()] + [
            {"config": {"project_name": kind}} for kind in self.stacks
        ]
        komodo._request.side_effect = core * 2
        pangolin.site.return_value = {"siteId": 9, "niceId": "lab", "type": "newt", "online": True}
        host.side_effect = [
            ValueError("fixture-credential"),
            {"docker": "26.1.5", "compose": "2.26.1", "mounts": {self.cifs: "cifs", self.nfs: "nfs4"},
             "projects": dict.fromkeys(self.stacks, True)},
        ]
        options = {"known_hosts": None, "public_key_path": None, "apply": True}
        failed = verify_run(self.device, self.runs, komodo, pangolin, **options)
        self.assertEqual(failed["status"], "incomplete")
        self.assertNotIn("fixture-credential", str(failed))
        self.assertEqual(self.runs.show(self.run_id)["status"], "failed")
        recovered = verify_run(self.device, self.runs, komodo, pangolin, **options)
        self.assertEqual(recovered["status"], "ready")
        self.assertEqual(self.runs.show(self.run_id)["status"], "completed")

    @patch("onboardrr.verification.host_snapshot")
    def test_final_apply_enrolls_monitoring_before_completing_the_run(self, host):
        komodo, pangolin = Mock(), Mock()
        core = [self.server_list(), self.stack_list()] + [
            {"config": {"project_name": kind}} for kind in self.stacks
        ]
        komodo._request.side_effect = core * 2
        pangolin.site.return_value = {"siteId": 9, "niceId": "lab", "type": "newt", "online": True}
        host.return_value = {
            "docker": "26.1.5", "compose": "2.26.1", "mounts": {self.cifs: "cifs", self.nfs: "nfs4"},
            "projects": dict.fromkeys(self.stacks, True),
        }
        monitoring = Mock(side_effect=[ValueError("temporary enrollment error"), {"checks": {"beszel": True}}])
        options = {"known_hosts": None, "public_key_path": None, "apply": True, "monitoring": monitoring}
        failed = verify_run(self.device, self.runs, komodo, pangolin, **options)
        self.assertFalse(failed["checks"]["monitoring"])
        self.assertEqual(self.runs.show(self.run_id)["next_step"], "verify")
        recovered = verify_run(self.device, self.runs, komodo, pangolin, **options)
        self.assertEqual(recovered["status"], "ready")
        monitoring.assert_called_with(self.device, apply=True)
        self.assertEqual(self.runs.show(self.run_id)["status"], "completed")

    @patch("onboardrr.verification.host_snapshot")
    def test_verifies_reused_https_agent_with_existing_storage_and_site(self, host):
        device = Inventory(self.database).add("old", "old.local", "operator", 22, PROFILE)
        run_id = self.runs.open("old")["id"]
        self.runs.complete_step(run_id, "ssh")
        self.runs.record_resource(run_id, "komodo-registration", Resource(
            ResourceKind.KOMODO_SERVER, self.server, Ownership.REUSED,
        ))
        for step in ("komodo-registration", "goku-files", "linux", "periphery"):
            self.runs.complete_step(run_id, step)
        for stack in self.stacks.values():
            self.runs.record_resource(run_id, "komodo-stacks", Resource(ResourceKind.KOMODO_STACK, stack, Ownership.REUSED))
        self.runs.complete_step(run_id, "komodo-stacks")
        self.runs.record_resource(run_id, "pangolin", Resource(ResourceKind.PANGOLIN_SITE, "9", Ownership.REUSED))
        self.runs.complete_step(run_id, "pangolin")
        komodo, pangolin = Mock(), Mock()
        komodo._request.side_effect = [
            [{"id": self.server, "name": "old", "info": {"state": "Ok", "address": "https://192.0.2.1:8120"}}],
            [{**entry, "name": entry["name"].replace("lab-", "old-")} for entry in self.stack_list()],
            *({"config": {"project_name": kind}} for kind in self.stacks),
        ]
        pangolin.site.return_value = None
        pangolin.site_by_id.return_value = {
            "siteId": 9, "name": "old", "niceId": "generated-site-name", "type": "newt", "online": True,
        }
        host.return_value = {"docker": "26.1.5", "compose": "2.26.1", "mounts": {},
                             "projects": dict.fromkeys(self.stacks, True)}
        output = verify_run(device, self.runs, komodo, pangolin, known_hosts=None, public_key_path=None, apply=True)
        self.assertEqual(output["status"], "ready")
        self.assertEqual(self.runs.show(run_id)["status"], "completed")

    def test_retirement_resumes_and_preserves_reused_stacks_and_disposable_data(self):
        komodo = Mock()
        entries = {entry["id"]: entry for entry in self.stack_list()}
        online = False
        operations = []

        def request(route, action, params):
            if action == "ListServers":
                return self.server_list("Ok" if online else "NotOk")
            if action == "ListStacks":
                return list(entries.values())
            if action == "GetStack":
                return {"config": {"project_name": entries[params["stack"]]["name"]}}
            operations.append((route, action, params))
            if action == "DestroyStack":
                entries[params["stack"]]["info"]["state"] = "down"
                return {"_id": {"$oid": "5" * 24}, "status": "Complete", "success": True}
            if action == "DeleteStack":
                identifier = params["id"]
                del entries[identifier]
                return {"_id": {"$oid": identifier}}
            self.fail(f"Unexpected operation: {action}")

        komodo._request.side_effect = request
        offline = retire_baseline(self.database, self.device, komodo)
        self.assertEqual({item.get("reason") for item in offline["actions"] if "reason" in item},
                         {"periphery-unavailable"})
        self.assertEqual(operations, [])
        online = True
        retire_baseline(self.database, self.device, komodo, stack_kind="network")
        self.assertEqual(set(entries), {self.stacks["docker"], self.stacks["monitoring"]})
        removed = [params["id"] for _, action, params in operations if action == "DeleteStack"]
        self.assertEqual(removed, [self.stacks["network"]])
        retire_baseline(self.database, self.device, komodo)
        self.assertEqual(set(entries), {self.stacks["monitoring"]})
        previous = operations.copy()
        retire_baseline(self.database, self.device, komodo)
        self.assertEqual(operations, previous)
        for _, action, params in operations:
            if action == "DestroyStack":
                self.assertEqual(set(params), {"stack", "remove_orphans"})
                self.assertFalse(params["remove_orphans"])
        self.assertEqual(self.data_file.read_text(), "disposable data")
        preview = retirement_plan(self.database, self.device["name"], purge=True)
        self.assertEqual(next(item["action"] for item in preview["actions"] if item["kind"] == "goku-directory"),
                         "remove")
        self.assertTrue(self.data_file.is_file())

    def test_retirement_leaves_shared_compose_projects_pending(self):
        komodo = Mock()
        komodo._request.side_effect = [
            self.server_list(), self.stack_list(),
            {"config": {"project_name": "shared"}},
            {"config": {"project_name": "shared"}},
            {"config": {"project_name": "shared"}},
        ]
        result = retire_baseline(self.database, self.device, komodo)
        self.assertEqual(sum(item.get("reason") == "compose-project-shared" for item in result["actions"]), 2)
        self.assertTrue(all(call.args[0] == "/read" for call in komodo._request.call_args_list))

    def test_unregister_keeps_server_used_by_a_builder(self):
        komodo = Mock()
        komodo._request.side_effect = [
            self.server_list("NotOk"), [], [], [], [{"id": "builder"}],
            {"config": {"type": "Server", "params": {"server_ids": [self.server]}}},
        ]
        actions = retirement_plan(self.database, "lab")["actions"]
        retire_komodo_server(self.device, komodo, actions)
        server = next(item for item in actions if item["kind"] == ResourceKind.KOMODO_SERVER)
        self.assertEqual(server["reason"], "komodo-server-has-dependencies")
        self.assertTrue(all(call.args[0] == "/read" for call in komodo._request.call_args_list))

    def test_unregister_removes_recorded_offline_server_only_once(self):
        komodo = Mock()
        komodo._request.side_effect = [
            self.server_list("NotOk"), [], [], [], [], [],
            {"_id": {"$oid": self.server}}, [],
        ]
        actions = retirement_plan(self.database, "lab")["actions"]
        retire_komodo_server(self.device, komodo, actions)
        server = next(item for item in actions if item["kind"] == ResourceKind.KOMODO_SERVER)
        self.assertEqual(server["status"], "removed")
        retire_komodo_server(self.device, komodo, actions)
        self.assertEqual(server["status"], "absent")
        writes = [call.args for call in komodo._request.call_args_list if call.args[0] == "/write"]
        self.assertEqual(writes, [("/write", "DeleteServer", {"id": self.server})])

    def test_unregister_retains_passkey_and_reopens_checkpoint_then_allows_passkey_removal(self):
        komodo, bao = Mock(), Mock()
        attached = self.server_list("Disabled")

        def request(route, action, params):
            if action == "ListServers":
                return list(attached)
            if action == "DeleteServer":
                attached.clear()
                return {"_id": {"$oid": self.server}}
            return []

        komodo._request.side_effect = request
        preview = retire_komodo_registration(self.database, self.device, komodo, bao=bao)
        self.assertEqual(preview["passkey"], "pending")
        self.assertTrue(attached)
        applied = retire_komodo_registration(self.database, self.device, komodo, apply=True)
        self.assertEqual(applied["passkey"], "preserved")
        run = self.runs.show(self.run_id)
        self.assertEqual(run["next_step"], "komodo-registration")
        self.assertNotIn(self.server, [resource["reference"] for resource in run["resources"]])
        self.assertFalse(attached)
        bao.delete_secret.assert_not_called()
        missing = HttpStatusError("OpenBao", "KV metadata read", 404)
        bao.secret_metadata.side_effect = [{}, missing, missing]
        complete = retire_komodo_registration(self.database, self.device, komodo, bao=bao, apply=True)
        self.assertEqual(complete["passkey"], "removed")
        self.assertEqual(retire_komodo_registration(self.database, self.device, komodo, bao=bao, apply=True)["passkey"], "absent")
        bao.delete_secret.assert_called_once_with("deployments/periphery/lab")

    def test_combined_unregister_removes_the_server_before_deleting_the_passkey(self):
        komodo, bao = Mock(), Mock()
        servers = self.server_list("Disabled")

        def request(route, action, params):
            if action == "ListServers":
                return list(servers)
            if action == "DeleteServer":
                servers.clear()
                return {"_id": {"$oid": self.server}}
            return []

        def delete_secret(path):
            self.assertFalse(servers)
            self.assertEqual(path, "deployments/periphery/lab")

        komodo._request.side_effect = request
        bao.delete_secret.side_effect = delete_secret
        bao.secret_metadata.side_effect = [{}, HttpStatusError("OpenBao", "KV metadata read", 404)]
        outcome = retire_komodo_registration(self.database, self.device, komodo, apply=True, bao=bao)
        self.assertEqual(outcome["actions"][0]["status"], "removed")
        self.assertEqual(outcome["passkey"], "removed")
        bao.delete_secret.assert_called_once()

    def test_unregister_blocks_passkey_removal_on_connected_komodo_server(self):
        komodo, bao = Mock(), Mock()
        komodo._request.return_value = self.server_list("Ok")
        result = retire_komodo_registration(self.database, self.device, komodo, bao=bao, apply=True)
        self.assertEqual(result["blockers"], ["periphery-still-connected"])
        self.assertEqual(result["passkey"], "preserved")
        bao.delete_secret.assert_not_called()
        self.assertTrue(all(call.args[0] == "/read" for call in komodo._request.call_args_list))

    @patch("onboardrr.goku_retirement.check_host_mounts")
    def test_purge_requires_detachment_and_removes_only_the_owned_directory(self, detached):
        root = self.database.parent
        sibling = root / "shared"
        sibling.mkdir()
        kept = sibling / "keep.txt"
        kept.write_text("shared data")
        (Path(self.nfs) / "shared-link").symlink_to(sibling, target_is_directory=True)
        komodo, pangolin = Mock(), Mock()
        komodo._request.return_value = []
        pangolin.site.return_value = None
        pangolin.site_by_id.return_value = None
        args = dict(roots={DATA_ROOT: root}, known_hosts=None, public_key=None)

        detached.return_value = False
        with self.assertRaisesRegex(ValueError, "recorded mounts"):
            purge_goku(self.database, self.device, komodo, pangolin, **args)
        self.assertTrue(self.data_file.exists())
        detached.return_value = True
        first = purge_goku(self.database, self.device, komodo, pangolin, **args)
        self.assertFalse(Path(self.nfs).exists())
        self.assertEqual(kept.read_text(), "shared data")
        repeated = purge_goku(self.database, self.device, komodo, pangolin, **args)
        for result, status in ((first, "removed"), (repeated, "absent")):
            entry = next(item for item in result["actions"] if item["kind"] == "goku-directory")
            self.assertEqual(entry["status"], status)
