import getpass
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import yaml


ANSIBLE = shutil.which("ansible-playbook", path=f"{Path(sys.executable).parent}{os.pathsep}{os.defpath}")
MOUNT_TASKS = Path(__file__).resolve().parents[1] / "provisioning/roles/linux/tasks/mount.yml"


@unittest.skipUnless(ANSIBLE, "Ansible is required to exercise the mount playbook")
class MountWorkspaceTest(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.destination = self.root / "compose"
        self.workspace = self.destination / "lab"
        self.workspace.mkdir(parents=True)
        tools = self.root / "bin"
        tools.mkdir()
        findmnt = tools / "findmnt"
        findmnt.write_text("#!/bin/sh\nexit 1\n")
        findmnt.chmod(0o700)
        self.environment = {
            **os.environ, "PATH": f"{tools}{os.pathsep}{os.environ.get('PATH', os.defpath)}",
        }
        tasks = yaml.safe_load(MOUNT_TASKS.read_text())
        before_mount = next(index for index, task in enumerate(tasks) if "ansible.posix.mount" in task)
        self.playbook = self.root / "fixture.yml"
        self.playbook.write_text(yaml.safe_dump([{
            "hosts": "localhost", "connection": "local", "gather_facts": False,
            "vars": {
                "ansible_python_interpreter": sys.executable,
                "onboardrr_mount": {
                    "path": str(self.destination), "local_workspace": str(self.workspace),
                    "source": "//fixture/compose", "fstype": "cifs",
                },
                "onboardrr_name": "lab", "onboardrr_user": getpass.getuser(),
                "onboardrr_account": ["lab", str(os.getuid())],
            },
            "tasks": tasks[:before_mount],
        }]))

    def run_guard(self, *flags):
        return subprocess.run([ANSIBLE, "-i", "localhost,", str(self.playbook), *flags],
                              cwd=self.root, env=self.environment, capture_output=True, text=True, timeout=35)

    def test_empty_system_workspace_is_only_removed_when_applying(self):
        preview = self.run_guard("--check")
        self.assertEqual(preview.returncode, 0, preview.stdout + preview.stderr)
        self.assertTrue(self.workspace.is_dir())
        applied = self.run_guard()
        self.assertEqual(applied.returncode, 0, applied.stdout + applied.stderr)
        self.assertFalse(self.workspace.exists())
        self.assertTrue(self.destination.is_dir())

    def test_hidden_files_in_the_workspace_or_next_to_it_are_preserved(self):
        for directory in (self.workspace, self.destination):
            with self.subTest(directory=directory):
                file = directory / ".local"
                file.write_text("fixture file\n")
                applied = self.run_guard()
                self.assertNotEqual(applied.returncode, 0)
                self.assertIn("contains local data", applied.stdout)
                self.assertEqual(file.read_text(), "fixture file\n")
                file.unlink()
