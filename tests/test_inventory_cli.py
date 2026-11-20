import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path

from onboardrr.__main__ import main


class InventoryCliTest(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.database = Path(directory.name) / "state" / "inventory.sqlite3"

    def subprocess_command(self, *arguments):
        return subprocess.run(
            [sys.executable, "-m", "onboardrr", "--database", str(self.database), *arguments],
            capture_output=True,
            text=True,
            timeout=10,
            env={**os.environ, "XDG_CONFIG_HOME": str(self.database.parent)},
        )

    def command(self, *arguments):
        stdout, stderr = StringIO(), StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            try:
                status = main(["--database", str(self.database), *arguments])
            except SystemExit as error:
                status = error.code
        return subprocess.CompletedProcess(arguments, status, stdout.getvalue(), stderr.getvalue())

    def record(self, *arguments):
        result = self.command(*arguments)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def test_list_starts_empty(self):
        result = self.subprocess_command("list")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), [])

    def test_registration_persists_and_is_idempotent(self):
        original = self.record("add", "Lab", "LAB.local.", "--user", "operator")
        repeated = self.record("add", "lab", "lab.local", "--user", "operator")
        self.assertEqual(original, repeated)
        self.assertEqual(original["host"], "lab.local")
        self.assertEqual(original["ssh_port"], 22)
        self.assertEqual(self.record("list"), [original])
        self.assertEqual(stat.S_IMODE(self.database.stat().st_mode) & 0o077, 0)

    def test_conflicting_device_names_and_endpoints_are_rejected(self):
        original = self.record("add", "lab", "10.0.0.1", "--user", "operator")
        cases = [
            ("lab", "10.0.0.2", "Device name"),
            ("spare", "10.0.0.1", "SSH endpoint"),
        ]
        for name, host, message in cases:
            with self.subTest(name=name, host=host):
                result = self.command("add", name, host, "--user", "operator")
                self.assertEqual(result.returncode, 1)
                self.assertIn(message, result.stderr)
        self.assertEqual(self.record("list"), [original])

    def test_ipv6_and_nondefault_ssh_port_are_supported(self):
        result = self.record("add", "lab", "2001:0db8::1", "--user", "operator", "--port", "2222")
        self.assertEqual(result["host"], "2001:db8::1")
        self.assertEqual(result["ssh_port"], 2222)

    def test_platform_profiles_persist_and_select_their_own_run_steps(self):
        server = self.record("add", "zero", "zero.local", "--user", "operator", "--profile", "serveur")
        self.assertEqual(server["profile"], "server")
        self.assertEqual(server, self.record("add", "zero", "zero.local", "--user", "operator", "--profile", "server"))
        nas = self.record("add", "goku", "goku.local", "--user", "operator", "--profile", "nas")
        self.assertEqual(nas["profile"], "nas")
        for name, expected in (("zero", "komodo-registration"), ("goku", "nas-prepare")):
            run = self.record("run", "open", name)
            steps = self.record("run", "show", run["id"])["steps"]
            self.assertEqual([step["name"] for step in steps[:2]], ["ssh", expected])
        self.assertIn("Raspberry Pi profile", self.command("wifi", "zero").stderr)
        self.assertIn("--public-key", self.command("prepare", "goku").stderr)
        self.assertIn("require the server profile", self.command("prepare", "goku", "--vnc").stderr)
        self.assertIn("Use mount sources with full preparation", self.command(
            "prepare", "zero", "--system-only", "--downloads-source", "//goku/downloads",
        ).stderr)

    def test_invalid_inputs_do_not_create_a_database(self):
        cases = [
            ("lab", "https://lab.local", "--user", "operator"),
            ("lab", "999.1.1.1", "--user", "operator"),
            ("lab", "lab.local", "--user", "operator", "--port", "65536"),
            ("lab", "lab.local", "--user", "user with spaces"),
            ("lab device", "lab.local", "--user", "operator"),
            ("lab", "lab.local", "--user", "operator", "--profile", "unknown"),
        ]
        for arguments in cases:
            with self.subTest(arguments=arguments):
                self.assertNotEqual(self.command("add", *arguments).returncode, 0)
        self.assertFalse(self.database.exists())

    def test_set_profile_keeps_identity_and_linux_run_then_switches_nas_workflow(self):
        original = self.record("add", "lab", "lab.local", "--user", "operator", "--port", "2222", "--profile", "server")
        run = self.record("run", "open", "lab")
        self.assertEqual(self.record("set-profile", "LAB", "raspberry-pi"), {**original, "profile": "raspberry-pi"})
        self.assertEqual(self.record("run", "open", "lab"), run)
        self.assertEqual(self.record("set-profile", "lab", "nas"), {**original, "profile": "nas"})
        prior = self.record("run", "show", run["id"])
        self.assertEqual(prior["status"], "superseded")
        active = self.record("run", "open", "lab")
        self.assertNotEqual(active["id"], run["id"])
        self.assertEqual([step["name"] for step in self.record("run", "show", active["id"])["steps"]],
                         ["ssh", "nas-prepare", "komodo-registration", "goku-files", "linux",
                          "periphery", "komodo-stacks", "pangolin", "verify"])
        self.assertEqual(self.record("set-profile", "lab", "nas")["profile"], "nas")
        self.assertEqual(len(self.record("run", "list", "lab")), 2)

    def test_concurrent_registration_keeps_one_identity(self):
        arguments = ("add", "lab", "lab.local", "--user", "operator")
        with ThreadPoolExecutor(max_workers=4) as workers:
            results = list(workers.map(lambda _: self.subprocess_command(*arguments), range(4)))
        for result in results:
            self.assertEqual(result.returncode, 0, result.stderr)
        records = [json.loads(result.stdout) for result in results]
        self.assertEqual(self.record("list"), [records[0]])
        self.assertTrue(all(record == records[0] for record in records))
