import json
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
from uuid import uuid4

from onboardrr.inventory import NAS_PROFILE, PROFILE, Inventory
from onboardrr.runs import STEPS, Ownership, Resource, ResourceKind, RunStore
from onboardrr.storage import MIGRATIONS


class RunStoreTest(unittest.TestCase):
    def test_existing_completed_nas_run_retains_identity_and_reopens_for_services(self):
        self.inventory.add("nas", "nas.local", "operator", 22, NAS_PROFILE)
        run_id = self.store.open("nas")["id"]
        self.store.complete_step(run_id, "ssh")
        self.store.complete_step(run_id, "nas-prepare")
        with closing(sqlite3.connect(self.database)) as connection, connection:
            connection.execute("DELETE FROM run_steps WHERE run_id = ? AND position BETWEEN 2 AND 7", (run_id,))
            connection.execute("UPDATE run_steps SET position = 2, completed_at = 'legacy' WHERE run_id = ? AND name = 'verify'",
                               (run_id,))
            connection.execute("UPDATE runs SET finished_at = 'legacy' WHERE id = ?", (run_id,))
            connection.execute("DROP TABLE primary_storage")
            connection.execute("DROP TABLE host_ip_stack_deployments")
            connection.execute("DROP TABLE portabase_dashboard_sources")
            connection.execute("PRAGMA user_version = 7")
        upgraded = self.store.open("nas")
        self.assertEqual((upgraded["id"], upgraded["next_step"]), (run_id, "komodo-registration"))
        steps = self.store.show(run_id)["steps"]
        self.assertEqual([step["name"] for step in steps], ["ssh", "nas-prepare", *STEPS[1:]])
        self.assertTrue(all(step["completed_at"] for step in steps[:2]))
        self.assertFalse(any(step["completed_at"] for step in steps[2:]))

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.database = Path(directory.name) / "inventory.sqlite3"
        self.inventory = Inventory(self.database)
        self.device = self.inventory.add("lab", "lab.local", "operator", 22, PROFILE)
        self.store = RunStore(self.database)

    def cli(self, *arguments, expected_code=0):
        result = subprocess.run(
            [sys.executable, "-m", "onboardrr", "--database", str(self.database), *arguments],
            capture_output=True,
            text=True,
            timeout=10,
        )
        self.assertEqual(result.returncode, expected_code, result.stderr)
        if expected_code:
            return result.stderr
        return json.loads(result.stdout)

    def test_cli_shows_pending_run_and_reopening_preserves_identity(self):
        self.assertEqual(self.cli("run", "list", "lab"), [])
        opened = self.cli("run", "open", "lab")
        self.assertEqual(opened["device_id"], self.device["id"])
        self.assertEqual(opened["status"], "pending")
        self.assertEqual(self.cli("run", "open", "LAB"), opened)
        self.assertEqual(self.cli("run", "list", "lab"), [opened])
        details = self.cli("run", "show", opened["id"])
        self.assertEqual(details["next_step"], STEPS[0])
        self.assertEqual([step["name"] for step in details["steps"]], list(STEPS))
        self.assertIn("Unknown device", self.cli("run", "open", "missing", expected_code=1))
        self.assertIn("Unknown run", self.cli("run", "show", "missing", expected_code=1))

    def test_cli_defaults_to_mdns_and_changes_an_address_without_losing_the_run(self):
        new_device = self.cli("add", "spare", "--user", "operator")
        self.assertEqual(new_device["host"], "spare.local")
        opened = self.cli("run", "open", "lab")
        self.assertIn("already registered", self.cli("set-host", "lab", "spare.local", expected_code=1))
        updated = self.cli("set-host", "lab", "lab-wifi.local")
        self.assertEqual(updated, {**self.device, "host": "lab-wifi.local"})
        self.assertEqual(self.cli("run", "list", "lab"), [opened])

    def test_failure_and_partial_resource_observations_survive_reopening(self):
        opened = self.store.open("lab")
        run_id = opened["id"]
        step = opened["next_step"]
        backup = uuid4()
        resources = (
            Resource(ResourceKind.SSH_KEY, "SHA256:operator-key", Ownership.CREATED),
            Resource(ResourceKind.SSH_KEY, "SHA256:existing-key", Ownership.REUSED),
            Resource(ResourceKind.HOST_FILE, "/home/operator/.ssh/authorized_keys", Ownership.MODIFIED, backup),
        )
        for resource in resources:
            self.store.record_resource(run_id, step, resource)
        self.store.mark_failed(run_id, step)

        resumed = RunStore(self.database)
        self.assertEqual(resumed.open("lab")["id"], run_id)
        details = self.cli("run", "show", run_id)
        self.assertEqual(details["status"], "failed")
        self.assertEqual(details["next_step"], step)
        self.assertIsNotNone(details["steps"][0]["last_failed_at"])
        self.assertEqual(
            {item["ownership"]: item["backup_id"] for item in details["resources"]},
            {"reused": None, "created": None, "modified": str(backup)},
        )
        self.assertEqual(self.store.list("lab")[0]["status"], "failed")
        resumed.record_resource(run_id, step, resources[0])
        with self.assertRaisesRegex(ValueError, "different metadata"):
            resumed.record_resource(
                run_id, step, Resource(resources[0].kind, resources[0].reference, Ownership.REUSED)
            )
        resumed.complete_step(run_id, step)
        completed = resumed.show(run_id)
        self.assertEqual(completed["next_step"], STEPS[1])
        self.assertEqual(completed["status"], "pending")
        self.assertIsNotNone(completed["steps"][0]["completed_at"])
        self.assertIsNotNone(completed["steps"][0]["last_failed_at"])
        self.assertEqual(len(completed["resources"]), 3)

    def test_ordered_completion_keeps_history_and_allows_a_new_run(self):
        first = self.store.open("lab")
        with self.assertRaisesRegex(ValueError, "no longer pending"):
            self.store.complete_step(first["id"], STEPS[1])
        for step in STEPS:
            self.store.complete_step(first["id"], step)
        with self.assertRaisesRegex(ValueError, "no longer pending"):
            self.store.mark_failed(first["id"], STEPS[-1])

        second = self.store.open("lab")
        self.assertNotEqual(second["id"], first["id"])
        current, finished = self.store.list("lab")
        self.assertEqual(current, second)
        self.assertEqual(finished["id"], first["id"])
        self.assertEqual(finished["status"], "completed")
        self.assertIsNotNone(finished["finished_at"])
        self.assertIsNone(finished["next_step"])

    def test_concurrent_opens_keep_one_unfinished_run_per_device(self):
        with ThreadPoolExecutor(max_workers=4) as workers:
            results = list(workers.map(self.store.open, ["lab"] * 4))
        self.assertTrue(all(run == results[0] for run in results))
        self.assertEqual(self.store.list("lab"), [results[0]])
        self.inventory.add("spare", "spare.local", "operator", 22, PROFILE)
        self.assertNotEqual(self.store.open("spare")["id"], results[0]["id"])

    def test_resource_fields_require_references_and_backup_ids(self):
        with self.assertRaisesRegex(ValueError, "non-secret resource"):
            Resource(ResourceKind.PANGOLIN_SITE, "https://site/?key=secret", Ownership.CREATED)
        with self.assertRaisesRegex(ValueError, "requires a backup ID"):
            Resource(ResourceKind.HOST_FILE, "/etc/fstab", Ownership.MODIFIED)
        with self.assertRaisesRegex(ValueError, "Only a modified resource"):
            Resource(ResourceKind.HOST_FILE, "/etc/fstab", Ownership.REUSED, uuid4())


class StateMigrationTest(unittest.TestCase):
    def test_existing_unversioned_inventory_is_preserved(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "legacy.sqlite3"
            device_id = str(uuid4())
            with closing(sqlite3.connect(database)) as connection, connection:
                connection.execute("""
                    CREATE TABLE devices (
                        id TEXT PRIMARY KEY NOT NULL,
                        name TEXT NOT NULL UNIQUE,
                        host TEXT NOT NULL,
                        ssh_user TEXT NOT NULL,
                        ssh_port INTEGER NOT NULL CHECK (ssh_port BETWEEN 1 AND 65535),
                        profile TEXT NOT NULL,
                        UNIQUE (host, ssh_port)
                    )
                    """)
                connection.execute(
                    "INSERT INTO devices VALUES (?, ?, ?, ?, ?, ?)",
                    (device_id, "old-pi", "old-pi.local", "operator", 22, PROFILE),
                )
            self.assertEqual(Inventory(database).list()[0]["id"], device_id)
            self.assertEqual(RunStore(database).open("old-pi")["device_id"], device_id)
            with closing(sqlite3.connect(database)) as connection:
                self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], len(MIGRATIONS))
                self.assertEqual(connection.execute("SELECT COUNT(*) FROM monitoring_resources").fetchone()[0], 0)
