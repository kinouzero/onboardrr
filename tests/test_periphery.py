import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import yaml

from onboardrr.goku_retirement import retire_goku_files
from onboardrr.inventory import Inventory, NAS_PROFILE, PROFILE
from onboardrr.periphery import AGENT_ENV, COMPOSE, LEGACY_AGENT_ENV, TEMPLATE, read_goku_files, render_files
from onboardrr.periphery import render_reference, stage_files, stage_goku_files
from onboardrr.primary_storage import StorageEndpoint
from onboardrr.reconcile import reconcile
from onboardrr.runs import Ownership, Resource, ResourceKind, RunStore


REFERENCE_COMPOSE = """services:
  komodo-agent:
    image: ghcr.io/moghtech/komodo-periphery:2.3.4
    environment:
      PERIPHERY_PASSKEYS: ${KOMODO_PASSKEY}
      PERIPHERY_ROOT_DIRECTORY: ${KOMODO_PERIPHERY_ROOT_DIRECTORY}
    labels:
      monitor: ${DOCKER_HOSTNAME}:${NARUTO_IP}:2380
volumes:
  komodo_data:
    driver_opts:
      device: :/docker/komodo/data/${DOCKER_HOSTNAME}
"""

REFERENCE_TEMPLATE = """DOCKER_HOSTNAME='naruto'
DOCKER_API_PORT=2380
{{__HOSTS}}
DOMAIN={{__DOMAIN}}
KOMODO_PERIPHERY_ROOT_DIRECTORY=/share/docker/komodo/data/${DOCKER_HOSTNAME}
{{_KOMODO_SECRETS}}
"""


class PeripheryRenderTest(unittest.TestCase):
    def test_nas_files_are_staged_on_primary_storage_and_only_secondary_has_an_export(self):
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        share = root / "compose"
        (share / "naruto").mkdir(parents=True)
        source = REFERENCE_COMPOSE.replace(
            "    labels:", "    volumes:\n      - type: volume\n        source: komodo_data\n"
            "        target: ${KOMODO_PERIPHERY_ROOT_DIRECTORY}\n        volume: {nocopy: true}\n    labels:",
        ).replace("    driver_opts:", "    driver_opts:\n      type: nfs")
        (share / "naruto" / COMPOSE).write_text(source)
        script = Path("onboardrr/remote/goku_periphery.sh").read_text()
        script = script.replace("/share/compose", str(share)).replace("/share/docker/komodo/data", str(root / "data"))
        host = StorageEndpoint("keeper.local", "192.168.1.20", "keeper", "operator")
        database = root / "inventory.sqlite3"
        inventory, runs = Inventory(database), RunStore(database)

        def shell(destination, name, action, payload=None):
            self.assertEqual(destination, host)
            result = subprocess.run(["/bin/sh", "-c", script, "--", action, name], input=payload or b"",
                                    capture_output=True, check=True)
            return result.stdout

        def prepare_local(storage, name, address, *, local=False):
            self.assertEqual(storage, host)
            self.assertEqual(local, name == "keeper")
            (root / "data" / name).mkdir(parents=True)
            return "created-directory"

        with (patch("onboardrr.periphery.goku_periphery", side_effect=shell),
              patch("onboardrr.goku_export.ensure_goku_export", side_effect=prepare_local) as prepare):
            for name, address in (("keeper", "192.168.1.20"), ("luffy", "192.168.1.21")):
                with self.subTest(name=name):
                    device = inventory.add(name, name + ".local", "operator", 22, NAS_PROFILE)
                    run_id = runs.open(name)["id"]
                    for step in ("ssh", "nas-prepare", "komodo-registration"):
                        runs.complete_step(run_id, step)
                    export = f"/onboardrr-{name}" if name != "keeper" else None
                    result = stage_goku_files(device, address, host, runs, nfs_export=export)
                    self.assertEqual(result.get("nfs_export"), export)
                    compose = yaml.safe_load((share / name / COMPOSE).read_text())
                    if export:
                        self.assertEqual(compose["volumes"]["komodo_data"]["driver_opts"]["device"], ":" + export)
                    else:
                        self.assertNotIn("volumes", compose)
                        mount, = compose["services"]["komodo-agent"]["volumes"]
                        self.assertEqual(mount["type"], "bind")
                        self.assertEqual(mount["target"], mount["source"])
                    report = reconcile(device, runs, None, compose_root=None, data_root=None, known_hosts=None,
                                       public_key=None, cifs_source=None, nfs_source=None, apply=True,
                                       goku_host=host, only="goku-files")
                    self.assertEqual(report["next_step"], "linux")
            self.assertEqual(prepare.call_count, 2)
        self.assertTrue((root / "data" / "luffy").is_dir())

    def test_remote_staging_uses_nas_paths_and_preserves_existing_files(self):
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        share = root / "compose"
        reference = share / "naruto"
        reference.mkdir(parents=True)
        source = (
            REFERENCE_COMPOSE.replace(
                "    environment:\n      PERIPHERY_PASSKEYS: ${KOMODO_PASSKEY}",
                f"    env_file:\n      - {LEGACY_AGENT_ENV}\n    environment:",
            ).replace("${NARUTO_IP}", "${HOST_IP}")
        )
        (reference / COMPOSE).write_text(source)
        (root / "data" / "lab").mkdir(parents=True)
        script = Path("onboardrr/remote/goku_periphery.sh").read_text()
        script = script.replace("/share/compose", str(share)).replace("/share/docker/komodo/data", str(root / "data"))
        database = root / "inventory.sqlite3"
        device = Inventory(database).add("lab", "lab.local", "operator", 22, PROFILE)
        runs = RunStore(database)
        run = runs.open("lab")["id"]
        runs.complete_step(run, "ssh")
        runs.complete_step(run, "komodo-registration")

        def shell(host, name, action, payload=None):
            result = subprocess.run(["/bin/sh", "-c", script, "--", action, name], input=payload or b"",
                                    capture_output=True)
            if result.returncode:
                raise ValueError("Fixture denied the stage.")
            return result.stdout

        with (
            patch("onboardrr.periphery.goku_periphery", side_effect=shell),
            patch("onboardrr.goku_export.ensure_goku_export", side_effect=("created-directory", "ready")) as export,
        ):
            staged = stage_goku_files(device, "192.0.2.8", "goku.local", runs, nfs_export="/onboardrr-lab")
            self.assertEqual(staged["files"], [f"/share/compose/lab/{COMPOSE}", f"/share/compose/lab/{TEMPLATE}"])
            files = read_goku_files("goku.local", "lab", "verify")
            self.assertIn(AGENT_ENV, files[COMPOSE])
            self.assertIn("${DOCKER_HOST_IP}", files[COMPOSE])
            self.assertIn(":/onboardrr-lab", files[COMPOSE])
            self.assertIn("HOSTNAME='lab'", files[TEMPLATE])
            self.assertEqual(stage_goku_files(device, "192.0.2.8", "goku.local", runs, nfs_export="/onboardrr-lab"), staged)
            self.assertEqual(staged["nfs_export"], "/onboardrr-lab")
            self.assertEqual(export.call_count, 2)
            export.assert_called_with("goku.local", "lab", "192.0.2.8")
            saved = runs.show(run)
            self.assertEqual(saved["next_step"], "goku-files")
            self.assertIn({"kind": "goku-directory", "reference": "/share/docker/komodo/data/lab",
                           "ownership": "created", "step": "goku-files", "backup_id": None}, saved["resources"])
            self.assertEqual(len(saved["resources"]), 3)
            (share / "lab" / TEMPLATE).write_text("operator edit")
            with self.assertRaisesRegex(ValueError, "denied"):
                stage_goku_files(device, "192.0.2.8", "goku.local", runs, nfs_export="/onboardrr-lab")
            self.assertEqual((share / "lab" / TEMPLATE).read_text(), "operator edit")
            self.assertEqual(export.call_count, 2)

    def test_staged_files_retire_by_digest_without_removing_other_files(self):
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        reference, share = root / "reference", root / "share"
        reference.mkdir()
        share.mkdir()
        (reference / COMPOSE).write_text(REFERENCE_COMPOSE)
        (reference / TEMPLATE).write_text(REFERENCE_TEMPLATE)
        database = root / "inventory.sqlite3"
        device = Inventory(database).add("lab", "192.0.2.8", "operator", 22, PROFILE)
        runs = RunStore(database)
        run = runs.open("lab")["id"]
        for step in ("ssh", "komodo-registration"):
            runs.complete_step(run, step)
        stage_files(device, "192.0.2.8", reference, share, runs)
        stage_files(device, "192.0.2.8", reference, share, runs)
        records = runs.show(run)["resources"]
        self.assertEqual(len(records), 2)
        self.assertTrue(all(item.get("content_sha256") and item["ownership"] == "created" for item in records))
        target = share / "lab"
        unrelated = target / ".env"
        unrelated.write_text("DOCKER_HOSTNAME=lab\n")
        rendered = (target / TEMPLATE).read_bytes()

        def retire(apply=False):
            return retire_goku_files(database, device, None, compose_root=share, known_hosts=None,
                                     public_key=Path("operator.pub"), apply=apply)["files"]

        self.assertEqual([item["status"] for item in retire()], ["eligible", "eligible"])
        with (
            patch("onboardrr.goku_retirement.ensure_stacks_detached") as detached,
            patch("onboardrr.goku_retirement.check_host_mounts", return_value=True),
        ):
            (target / TEMPLATE).write_bytes(rendered + b"# local edit\n")
            self.assertEqual([item["status"] for item in retire(True)], ["eligible", "changed"])
            detached.assert_not_called()
            (target / TEMPLATE).unlink()
            (target / TEMPLATE).symlink_to(unrelated)
            self.assertEqual([item["status"] for item in retire(True)], ["eligible", "changed"])
            detached.assert_not_called()
            (target / TEMPLATE).unlink()
            (target / TEMPLATE).write_bytes(rendered)
            self.assertEqual([item["status"] for item in retire(True)], ["removed", "removed"])
            self.assertEqual([item["status"] for item in retire(True)], ["absent", "absent"])
        self.assertEqual(unrelated.read_text(), "DOCKER_HOSTNAME=lab\n")
        self.assertTrue(target.is_dir())

    def test_staging_does_not_claim_existing_files_and_legacy_records_remain_unverified(self):
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        share = root / "share"
        share.mkdir()
        (root / COMPOSE).write_text(REFERENCE_COMPOSE)
        (root / TEMPLATE).write_text(REFERENCE_TEMPLATE)
        database = root / "inventory.sqlite3"
        device = Inventory(database).add("lab", "192.0.2.8", "operator", 22, PROFILE)
        runs = RunStore(database)
        run = runs.open("lab")["id"]
        for step in ("ssh", "komodo-registration"):
            runs.complete_step(run, step)
        render_files(device, "192.0.2.8", root, share / "lab")
        stage_files(device, "192.0.2.8", root, share, runs)
        self.assertEqual(runs.show(run)["resources"], [])
        runs.record_resource(run, "goku-files", Resource(
            ResourceKind.GOKU_FILE, f"/share/compose/lab/{COMPOSE}", Ownership.CREATED,
        ))
        report = retire_goku_files(database, device, None, compose_root=share, known_hosts=None,
                                  public_key=None, apply=True)
        self.assertEqual(report["files"][0]["status"], "unverified")
        self.assertTrue((share / "lab" / COMPOSE).is_file())

    def test_changes_identity_and_monitoring_variable_and_preserves_secret_markers(self):
        result = render_reference("ichigo", "192.0.2.9", REFERENCE_COMPOSE, REFERENCE_TEMPLATE)
        self.assertIn("${DOCKER_HOST_IP}", result[COMPOSE])
        self.assertNotIn("${NARUTO_IP}", result[COMPOSE])
        self.assertIn("HOSTNAME='ichigo'\nDOCKER_HOST_IP='192.0.2.9'", result[TEMPLATE])
        self.assertIn("{{_KOMODO_SECRETS}}", result[TEMPLATE])
        self.assertIn("/share/docker/komodo/data/${HOSTNAME}", result[TEMPLATE])
        self.assertNotIn("DOCKER_HOSTNAME", result[COMPOSE] + result[TEMPLATE])

    def test_rejects_expanded_credentials_and_non_ipv4_addresses(self):
        with self.assertRaisesRegex(ValueError, "unexpanded"):
            render_reference("lab", "192.0.2.8", REFERENCE_COMPOSE,
                             REFERENCE_TEMPLATE + "KOMODO_PASSKEY=fixture\n")
        with self.assertRaises(ValueError):
            render_reference("lab", "2001:db8::1", REFERENCE_COMPOSE, REFERENCE_TEMPLATE)

    def test_safe_rerun_and_rejection_of_conflicting_output(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / COMPOSE).write_text(REFERENCE_COMPOSE)
            (root / TEMPLATE).write_text(REFERENCE_TEMPLATE)
            destination = root / "out"
            first = render_files({"name": "lab"}, "192.0.2.8", root, destination)
            self.assertEqual(first, render_files({"name": "lab"}, "192.0.2.8", root, destination))
            self.assertEqual((destination / TEMPLATE).stat().st_mode & 0o077, 0)
            with self.assertRaisesRegex(ValueError, "different template.env"):
                render_files({"name": "lab"}, "192.0.2.9", root, destination)
            self.assertIn("192.0.2.8", (destination / TEMPLATE).read_text())

    def test_can_render_a_dedicated_export_without_changing_the_container_path(self):
        result = render_reference(
            "lab", "192.0.2.8", REFERENCE_COMPOSE, REFERENCE_TEMPLATE, nfs_export="/onboardrr-lab",
        )
        self.assertIn("device: :/onboardrr-lab", result[COMPOSE])
        self.assertIn("/share/docker/komodo/data/${HOSTNAME}", result[TEMPLATE])
        with self.assertRaisesRegex(ValueError, "absolute NFS"):
            render_reference("lab", "192.0.2.8", REFERENCE_COMPOSE, REFERENCE_TEMPLATE, nfs_export="/lab/../other")
