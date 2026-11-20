import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

from onboardrr.goku_retirement import DATA_ROOT
from onboardrr.inventory import Inventory, PROFILE
from onboardrr.pangolin import enroll_site
from onboardrr.periphery import COMPOSE, TEMPLATE, GokuPeripheryError
from onboardrr.reconcile import reconcile
from onboardrr.retirement import retirement_plan
from onboardrr.runs import Ownership, Resource, ResourceKind, RunStore
from onboardrr.stacks import apply_baseline
from onboardrr.verification import verify_run


class ReconcileTest(unittest.TestCase):
    def setUp(self):
        root = Path(self.enterContext(TemporaryDirectory()))
        database = root / "inventory.sqlite3"
        self.device = Inventory(database).add("lab", "192.168.1.55", "operator", 22, PROFILE)
        self.runs = RunStore(database)
        run = self.runs.open("lab")
        self.run_id = run["id"]
        self.server = "1" * 24
        self.runs.complete_step(self.run_id, "ssh")
        self.runs.record_resource(self.run_id, "komodo-registration", Resource(
            ResourceKind.KOMODO_SERVER, self.server, Ownership.CREATED,
        ))
        self.runs.complete_step(self.run_id, "komodo-registration")
        self.compose_root, self.data_root = root / "compose", root / "data"
        for parent in (self.compose_root, self.data_root):
            (parent / "lab").mkdir(parents=True)
        (self.compose_root / "lab" / COMPOSE).write_text("name: ${DOCKER_HOSTNAME}\n")
        (self.compose_root / "lab" / TEMPLATE).write_text("DOCKER_HOSTNAME='lab'\n{{_KOMODO_SECRETS}}\n")
        self.data = self.data_root / "lab/persisted.txt"
        self.data.write_text("disposable")
        self.komodo = Mock()
        self.live = {"name": "lab", "id": self.server, "info": {"state": "Ok"}}
        self.komodo._request.return_value = [self.live]
        self.cifs = "/home/operator/.docker/compose"
        self.nfs = f"{DATA_ROOT}/lab"
        self.mounts = [Resource(ResourceKind.MOUNT, path, Ownership.REUSED) for path in (self.cifs, self.nfs)]

    def reconcile(self, *, apply, **scope):
        return reconcile(
            self.device, self.runs, self.komodo, compose_root=self.compose_root, data_root=self.data_root,
            known_hosts=None, public_key=None, cifs_source=None, nfs_source=None, apply=apply, **scope,
        )

    @patch("onboardrr.reconcile.host_resources")
    def test_single_checkpoint_does_not_run_earlier_or_later_checks(self, host):
        result = self.reconcile(apply=True, only="linux")
        self.assertEqual((result["status"], result["next_step"]), ("incomplete", "goku-files"))
        host.assert_not_called()
        self.reconcile(apply=True, only="goku-files")
        self.reconcile(apply=True, only="goku-files")
        host.assert_not_called()
        host.return_value = self.mounts
        result = self.reconcile(apply=True, only="linux")
        self.assertEqual((result["checks"], result["next_step"]), ({"linux": True}, "periphery"))
        self.assertEqual(host.call_args.kwargs, {"stage": "linux", "mounts_only": True})
        self.komodo._request.assert_not_called()

    @patch("onboardrr.pangolin.configure_private_network")
    @patch("onboardrr.verification.host_snapshot")
    @patch("onboardrr.reconcile.host_resources")
    def test_reconciles_interrupted_preparation_then_reuses_stacks_and_completes(self, host, snapshot, network):
        preview = self.reconcile(apply=False)
        self.assertEqual(preview["checks"], {"goku-files": True})
        self.assertEqual(self.runs.show(self.run_id)["next_step"], "goku-files")
        host.side_effect = [ValueError("fixture-sensitive"), self.mounts, []]
        stalled = self.reconcile(apply=True)
        self.assertEqual(stalled["next_step"], "linux")
        self.assertNotIn("fixture-sensitive", str(stalled))
        self.assertEqual(self.runs.show(self.run_id)["status"], "failed")
        ready = self.reconcile(apply=True)
        self.assertEqual(ready["next_step"], "komodo-stacks")
        self.assertEqual(ready["checks"], {"linux": True, "periphery": True})

        kinds = ("docker", "monitoring", "network")
        stacks = [
            {"id": str(index) * 24, "name": f"lab-{kind}",
             "info": {"server_id": self.server, "state": "running"}}
            for index, kind in enumerate(kinds, start=2)
        ]
        self.komodo._request.side_effect = [
            [self.live], stacks, [], [],
            [self.live], stacks, [], [],
            [self.live], stacks, *({"config": {"project_name": kind}} for kind in kinds),
        ]
        pangolin, bao = Mock(), Mock()
        pangolin.site.return_value = {
            "siteId": 9, "niceId": "lab", "type": "newt", "online": True, "newtId": "fixture-id",
        }
        bao.fields_with_version.return_value = ({"LAB_NEWT_ID": "fixture-id", "LAB_NEWT_SECRET": "fixture-secret"}, 1)
        observed = enroll_site(pangolin, self.device, runs=self.runs, bao=bao, apply=True)
        self.assertEqual(observed["action"], "reuse")
        for _ in range(2):
            report = apply_baseline(self.komodo, self.device, primary_server="zero", runs=self.runs)
            self.assertEqual([entry["action"] for entry in report["stacks"]], ["reuse"] * 3)
        enroll_site(pangolin, self.device, runs=self.runs, bao=bao, apply=True)
        network.assert_called_once()
        bao.patch.assert_not_called()
        snapshot.return_value = {"docker": "26.1.5", "compose": "2.26.1",
                                 "mounts": {self.cifs: "cifs", self.nfs: "nfs4"},
                                 "projects": dict.fromkeys(kinds, True)}
        final = verify_run(self.device, self.runs, self.komodo, pangolin,
                           known_hosts=None, public_key_path=None, apply=True)
        self.assertEqual(final["status"], "ready")
        self.assertEqual(self.runs.show(self.run_id)["status"], "completed")
        self.assertTrue(all(call.args[0] == "/read" for call in self.komodo._request.call_args_list))

        plan = retirement_plan(self.runs.database, "lab", purge=True)
        preserved = {item["reference"] for item in plan["actions"] if item["action"] == "preserve"}
        self.assertTrue({self.nfs, self.cifs, "9", *(stack["id"] for stack in stacks)} <= preserved)
        self.assertEqual(self.data.read_text(), "disposable")

    def test_wrong_device_template_cannot_advance_the_run(self):
        (self.compose_root / "lab" / TEMPLATE).write_text("DOCKER_HOSTNAME=other\n{{_KOMODO_SECRETS}}\n")
        result = self.reconcile(apply=True)
        self.assertEqual(result["status"], "incomplete")
        self.assertEqual(self.runs.show(self.run_id)["next_step"], "goku-files")
        self.komodo._request.assert_not_called()

    def test_reconcile_reports_a_verified_goku_directory_blocker(self):
        message = "Create /share/docker/komodo/data/lab on Goku and configure the device's NFS export."
        with patch("onboardrr.reconcile.read_goku_files", side_effect=GokuPeripheryError(message)) as read:
            result = reconcile(
                self.device, self.runs, self.komodo, compose_root=None, data_root=None, known_hosts=None,
                public_key=None, cifs_source=None, nfs_source=None, apply=True, goku_host="goku.local",
            )
        read.assert_called_once_with("goku.local", "lab", "verify")
        self.assertEqual(result["blockers"], [message])
        self.assertEqual(self.runs.show(self.run_id)["next_step"], "goku-files")
