import hashlib
import json
import os
import shlex
import subprocess
import tempfile
import unittest
from base64 import b64encode
from contextlib import nullcontext
from pathlib import Path
from unittest.mock import Mock, patch

from onboardrr import nas, ssh
from onboardrr.inventory import NAS_PROFILE, Inventory
from onboardrr.primary_storage import StorageEndpoint
from onboardrr.remote import nas_mount, nas_openbao, reconcile
from onboardrr.runs import RunStore


class NasControllerTest(unittest.TestCase):
    def setUp(self):
        directory = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.database = directory / "inventory.sqlite3"
        self.device = Inventory(self.database).add("luffy", "192.168.1.30", "operator", 22, NAS_PROFILE)
        self.endpoint = StorageEndpoint("goku.local", "192.168.1.20", "goku", "operator")
        self.facts = {"entware_state": "missing", "python": "no", "container_station": "TRUE",
                      "docker": "27.1.2", "compose": "2.29.1", "uid": "1000"}
        self.client = Mock()
        self.key = self.enterContext(patch("onboardrr.nas.ssh.load_public_key")).return_value
        self.enterContext(patch("onboardrr.nas.ssh.agent_identity", return_value=nullcontext("agent")))
        self.enterContext(patch("onboardrr.nas.ssh.connect", return_value=nullcontext(self.client)))
        self.storage_password = Mock(return_value="fixture-sudo")

    def test_prepare_checks_before_installing_dependencies_and_advancing_the_run(self):
        runs = RunStore(self.database)
        run_id = runs.open("luffy")["id"]
        runs.complete_step(run_id, "ssh")
        self.key.get_name.return_value = "ssh-ed25519"
        self.key.asbytes.return_value = b"fixture"
        replies = [self.facts.copy(), self.facts.copy(), {"changed": "yes"},
                   {"changed": "yes"}, {"changed": "no"}, {}]
        with (patch("onboardrr.nas.run", side_effect=replies) as call,
              patch("onboardrr.nas.ssh.sudo_access", return_value="passwordless")):
            preview = nas.prepare(self.database, self.device, None, None, None, False)
            self.assertEqual(preview["mode"], "check")
            self.assertEqual(runs.show(run_id)["next_step"], "nas-prepare")
            self.assertEqual([item.args[1] for item in call.call_args_list], ["inspect"])
            result = nas.prepare(self.database, self.device, None, None, None, True,
                                 docker_auth={"username": "operator", "token": "fixture-token"})
        self.assertTrue(result["changed"])
        self.assertEqual(runs.show(run_id)["next_step"], "komodo-registration")
        self.assertEqual([item.args[1] for item in call.call_args_list],
                         ["inspect", "inspect", "install-entware", "install-python", "apply", "docker-login"])
        self.assertEqual(call.call_args.kwargs["payload"], b"fixture-token\n")

    def test_secondary_mount_checks_sudo_and_passes_openbao_credentials_in_the_payload(self):
        secret = Mock()
        secret.read.return_value = ("smb-user", "fixture-smb")
        cifs, nfs = self.endpoint.mount_sources("luffy")
        with (patch("onboardrr.nas.run", side_effect=[
                  {"uid": "1000", "gid": "100", "home": "/homes/operator"}, {"ready": "yes"},
              ]) as call,
              patch("onboardrr.nas.ssh.sudo_access", side_effect=["unavailable", "password"]) as sudo,
              patch("onboardrr.secrets.configured_openbao", return_value=secret),
              patch("onboardrr.goku_export.ensure_goku_export") as export):
            result = nas.prepare_storage(self.database, self.device, None, None, self.endpoint, cifs, nfs,
                                         apply=True, password_provider=self.storage_password)
        self.assertEqual((result["compose"], result["periphery"]), (cifs, nfs))
        self.assertEqual([item.args[2] for item in sudo.call_args_list], [None, "fixture-sudo"])
        export.assert_called_once_with(self.endpoint, "luffy", "192.168.1.30")
        self.assertEqual(call.call_args.args, (self.client, "compose-mount"))
        payload = json.loads(call.call_args.kwargs["payload"])
        self.assertEqual(payload["config"]["password"], "fixture-smb")
        self.assertNotIn("fixture-smb", str(call.call_args.kwargs["arguments"]))

    def test_primary_and_storage_preview_require_no_smb_access(self):
        for name, apply, expected in (("luffy", False, None), ("goku", True, "compose-local")):
            with self.subTest(name=name, apply=apply):
                device = {**self.device, "name": name}
                with (patch("onboardrr.nas.run") as call, patch("onboardrr.secrets.configured_openbao") as bao):
                    report = nas.prepare_storage(self.database, device, None, None, self.endpoint, "//host/compose",
                                                 "host:/data", apply=apply, password_provider=self.storage_password)
                    if expected:
                        call.assert_called_once_with(self.client, expected)
                        self.assertEqual(report["compose"], "/share/compose")
                    else:
                        call.assert_not_called()
                    bao.assert_not_called()
        self.storage_password.assert_not_called()

    def test_secondary_stops_before_reading_secrets_when_the_operator_cannot_use_sudo(self):
        with (patch("onboardrr.nas.run", return_value={"uid": "1000", "gid": "100", "home": "/homes/operator"}),
              patch("onboardrr.nas.ssh.sudo_access", return_value="unavailable"),
              patch("onboardrr.secrets.configured_openbao") as bao):
            with self.assertRaisesRegex(ValueError, "requires QTS sudo"):
                nas.prepare_storage(self.database, self.device, None, None, self.endpoint, "//host/compose",
                                    "host:/data", apply=True, password_provider=self.storage_password)
        bao.assert_not_called()

    def test_qts_wire_command_keeps_sudo_password_and_credentials_in_stdin(self):
        with patch("onboardrr.nas.ssh.command", return_value=(0, b"ready=yes\nstatus=mounted\n")) as send:
            result = nas.run(self.client, "compose-mount", sudo="password", password="fixture-sudo",
                             payload=b"fixture-smb", arguments=("runner",))
        self.assertEqual(result["ready"], "yes")
        self.assertTrue(send.call_args.args[1].startswith("sudo -S"))
        self.assertNotIn("fixture-", send.call_args.args[1])
        self.assertEqual(send.call_args.args[2], b"fixture-sudo\nfixture-smb")


class NasTest(unittest.TestCase):
    def test_qts_mount_configuration_persists_with_one_cron_entry(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cron = root / "crontab"
            cron.write_text("0 4 * * * qnap-maintenance\n")
            runner, config = root / "restore-mounts.py", root / "mounts.json"
            schedule = f"* * * * * python3 {runner} --restore"
            result = subprocess.CompletedProcess([], 0, stdout=schedule + "\n")
            with (patch.object(nas_mount, "CRONTAB", cron), patch.object(nas_mount, "RUNNER", runner),
                  patch.object(nas_mount, "CONFIG", config), patch.object(nas_mount, "SCHEDULE", schedule),
                  patch.object(nas_mount.shutil, "which", return_value="/usr/bin/crontab"),
                  patch.object(nas_mount, "mount_shares"),
                  patch.object(nas_mount.subprocess, "run", return_value=result)):
                nas_mount.configure({"device": "luffy"}, "# mount fixture\n")
                initial = {path: path.stat().st_ino for path in (config, runner, cron)}
                nas_mount.configure({"device": "luffy"}, "# mount fixture\n")
            self.assertEqual({path: path.stat().st_ino for path in (config, runner, cron)}, initial)
            self.assertEqual(cron.read_text().splitlines(), ["0 4 * * * qnap-maintenance", schedule])
            self.assertEqual(config.stat().st_mode & 0o777, 0o600)
            self.assertEqual(json.loads(config.read_text()), {"device": "luffy"})

    def test_qts_reload_uses_the_vendor_crontab_and_checks_activation(self):
        for active_after_reload in (True, False):
            with self.subTest(active_after_reload=active_after_reload), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                cron = root / "crontab"
                runner = root / "restore-mounts.py"
                schedule = nas_mount.SCHEDULE.replace(str(nas_mount.RUNNER), str(runner))
                original = "0 4 * * * qnap-maintenance\n"
                cron.write_text(original + schedule + "\n")
                inactive = subprocess.CompletedProcess([], 1, stdout="")
                active = subprocess.CompletedProcess([], 0, stdout=schedule + "\n")
                with (patch.object(nas_mount, "CRONTAB", cron),
                      patch.object(nas_mount, "RUNNER", runner),
                      patch.object(nas_mount, "SCHEDULE", schedule),
                      patch.object(nas_mount, "CONFIG", root / "mounts.json"),
                      patch.object(nas_mount.shutil, "which", return_value="/usr/bin/crontab"),
                      patch.object(nas_mount, "mount_shares"),
                      patch.object(nas_mount, "run") as reload,
                      patch.object(nas_mount.subprocess, "run",
                                   side_effect=[inactive, active if active_after_reload else inactive]) as list_cron):
                    if active_after_reload:
                        nas_mount.configure({"device": "luffy"}, "# mount fixture\n")
                    else:
                        with self.assertRaisesRegex(ValueError, "did not load"):
                            nas_mount.configure({"device": "luffy"}, "# mount fixture\n")
                self.assertEqual([call.args[0] for call in reload.call_args_list],
                                 [["/usr/bin/crontab", str(cron)], ["/etc/init.d/crond.sh", "restart"]])
                self.assertEqual([call.args[0] for call in list_cron.call_args_list], [["/usr/bin/crontab", "-l"]] * 2)
                self.assertEqual(cron.read_text(), original + schedule + "\n")

    def test_secondary_mounts_the_two_primary_shares_without_hiding_existing_data(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            home = root / "home"
            home.mkdir()
            docker = root / "docker"
            docker.mkdir()
            storage = {"device": "luffy", "home": str(home), "uid": os.getuid(), "gid": os.getgid(),
                       "cifs": "//192.168.1.20/compose", "nfs": "192.168.1.20:/onboardrr-luffy",
                       "username": "fixture", "password": "fixture-password"}
            compose = home / ".docker/compose"
            data = docker / "komodo/data/luffy"
            data.mkdir(parents=True)
            existing_file = data / "state"
            existing_file.touch()
            mounts, calls, credential_paths = {}, [], []

            def run(command):
                calls.append(command[0])
                if command[0] == "mount.cifs":
                    credential = Path(command[-1].partition(",")[0].split("=", 1)[1])
                    credential_paths.append(credential)
                    self.assertEqual(credential.stat().st_mode & 0o777, 0o600)
                    self.assertIn("password=fixture-password", credential.read_text())
                    self.assertFalse(any("fixture-password" in argument for argument in command))
                    mounts[compose.resolve()] = ("cifs", storage["cifs"])
                elif command[0] == "mount":
                    mounts[data.resolve()] = ("nfs4", storage["nfs"])

            with (patch.object(nas_mount, "ROOT", root),
                  patch.object(nas_mount, "mount_table", return_value=mounts),
                  patch.object(nas_mount, "run", side_effect=run)):
                with self.assertRaisesRegex(ValueError, "hide existing content"):
                    nas_mount.mount_shares(storage, docker=docker)
                self.assertEqual(calls, [])
                existing_file.unlink()
                nas_mount.mount_shares(storage, docker=docker)
                nas_mount.mount_shares(storage, docker=docker)
            self.assertEqual(calls, ["mount.cifs", "mount"])
            self.assertEqual(len(credential_paths), 1)
            self.assertFalse(credential_paths[0].exists())

    def test_restore_rejects_credentials_without_private_permissions_and_logs_state_changes(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "mounts.json"
            config.write_text('{"password":"fixture-secret"}')
            config.chmod(0o644)
            state = config.with_name("restore-status")
            with (patch.object(nas_mount, "CONFIG", config), patch.object(nas_mount, "RESTORE_STATE", state),
                  patch.object(nas_mount.shutil, "which", return_value="/usr/bin/logger"),
                  patch.object(nas_mount, "mount_shares") as mount,
                  patch.object(nas_mount.subprocess, "run") as logger):
                for _ in range(2):
                    with self.assertRaisesRegex(ValueError, "permissions 0600"):
                        nas_mount.restore()
                mount.assert_not_called()
                self.assertEqual(logger.call_count, 1)
                self.assertNotIn("fixture-secret", str(logger.call_args))
                config.chmod(0o600)
                nas_mount.restore()
                self.assertEqual(logger.call_count, 2)
                self.assertIn("Primary NAS mounts verified.", state.read_text())

    def test_openbao_uses_container_station_project_and_renders_private_cache(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            root = home / ".config/docker/openbao"
            calls = []

            def docker(*arguments):
                calls.append(arguments)
                if "up" in arguments:
                    cache = root / "compose/cache/config.env"
                    cache.write_text("VALUE=fixture\n")
                    cache.chmod(0o600)
                if "--services" in arguments:
                    return "openbao-agent-compose"
                return ""

            request = {"device": "nas", "address": "192.0.2.12", "port": 2380,
                       "image": "ghcr.io/openbao/openbao:2.7.3", "agent": {
                           "projects": ["config"], "url": "https://bao.example",
                           "role_id": "fixture-role", "secret_id": "fixture-secret",
                           "config_b64": b64encode(b"{}").decode(),
                       }}
            with (patch.object(Path, "home", return_value=home),
                  patch.object(nas_openbao, "docker", side_effect=docker)):
                self.assertEqual(nas_openbao.install(request)["status"], "ready")
                compose = json.loads((root / "compose.yaml").read_text())
                self.assertEqual(compose["name"], "secrets")
                service = compose["services"]["openbao-agent-compose"]
                self.assertEqual(service["labels"]["docker.stack"], "secrets")
                self.assertEqual(service["volumes"][1]["source"], str(root / "compose/cache"))
                self.assertIn("DOCKER_API_PORT=2380", (root / "compose.env").read_text())
                self.assertEqual((root / "compose/agent/secret-id").stat().st_mode & 0o777, 0o600)
                self.assertIn("--force-recreate", next(args for args in calls if "up" in args))
                calls.clear()
                self.assertEqual(nas_openbao.install(request)["status"], "ready")
                self.assertNotIn("--force-recreate", next(args for args in calls if "up" in args))

    def test_openbao_migrates_only_the_recognized_legacy_agent_and_restores_it_on_failure(self):
        name = "openbao-agent-compose"
        request = {
            "device": "luffy", "address": "192.0.2.12", "port": 2380, "image": "ghcr.io/openbao/openbao:2.7.3",
            "agent": {"projects": ["periphery"], "url": "https://bao.example", "role_id": "fixture-role",
                      "secret_id": "fixture-secret", "config_b64": b64encode(b"{}").decode()},
        }
        for scenario in ("migration", "failed-render", "different-mount", "additional-service"):
            with self.subTest(scenario=scenario), tempfile.TemporaryDirectory() as directory:
                home = Path(directory)
                root = home / ".config/docker/openbao"
                legacy = root.with_name("compose-openbao")
                legacy.mkdir(parents=True)
                old_compose = legacy / "compose.yaml"
                old_env = legacy / "agent-compose.env"
                for file in (old_compose, old_env):
                    file.write_text("original\n")
                    file.chmod(0o600)
                details = {
                    "labels": {"com.docker.compose.project": "secrets", "com.docker.compose.service": name,
                               "com.docker.compose.project.config_files": str(old_compose),
                               "com.docker.compose.project.environment_file": str(old_env),
                               "docker.stack": "secrets"},
                    "mounts": [
                        {"Source": str(legacy / "agent"), "Destination": "/agent", "Type": "bind", "RW": False},
                        {"Source": str(legacy / "cache"), "Destination": "/cache", "Type": "bind", "RW": True},
                    ],
                    "running": True,
                }
                if scenario == "different-mount":
                    details["mounts"][1]["Source"] = "/share/unrelated"
                cache = root / "compose/cache/periphery.env"
                if scenario == "failed-render":
                    cache.parent.mkdir(parents=True)
                    cache.write_text("VALUE=stale\n")
                    cache.chmod(0o600)
                starts = []

                def docker(*arguments):
                    if arguments[:2] == ("ps", "-aq"):
                        return "fixture-id"
                    if arguments[0] == "inspect":
                        return json.dumps(details)
                    if "config" in arguments:
                        return f"{name}\nunrelated" if scenario == "additional-service" else name
                    if "up" in arguments:
                        starts.append(arguments)
                        if scenario == "migration":
                            cache.write_text("VALUE=fresh\n")
                            cache.chmod(0o600)
                    if "--status" in arguments:
                        return name
                    return ""

                with (patch.object(Path, "home", return_value=home),
                      patch.object(nas_openbao, "docker", side_effect=docker),
                      patch.object(nas_openbao.time, "sleep")):
                    if scenario == "migration":
                        self.assertTrue(nas_openbao.install(request)["migrated"])
                        self.assertEqual(len(starts), 1)
                        service = json.loads((root / "compose.yaml").read_text())["services"][name]
                        self.assertEqual(service["volumes"][1]["source"], str(cache.parent))
                        self.assertIn(str(root / "compose.yaml"), starts[0])
                        self.assertEqual(starts[0][-1], name)
                    elif scenario == "failed-render":
                        with self.assertRaisesRegex(ValueError, "previous agent was restored"):
                            nas_openbao.install(request)
                        self.assertEqual(len(starts), 2)
                        self.assertIn(str(old_compose), starts[1])
                        self.assertIn(str(old_env), starts[1])
                    else:
                        with self.assertRaisesRegex(ValueError, "Cannot migrate"):
                            nas_openbao.install(request)
                        self.assertEqual(starts, [])
                        self.assertFalse(root.exists())
                self.assertEqual(old_compose.read_text(), "original\n")
                self.assertEqual(old_env.read_text(), "original\n")

    def test_nas_storage_is_local_and_preview_leaves_the_share_unchanged(self):
        with tempfile.TemporaryDirectory() as directory:
            share = Path(directory)
            for name in ("goku", "luffy"):
                expected = share / "komodo" / "data" / name
                self.assertEqual(reconcile.nas_storage({"device": name}, share=share), expected)
                self.assertFalse(expected.exists())
                self.assertEqual(reconcile.nas_storage({"device": name}, share=share, create=True), expected)
                self.assertTrue(expected.is_dir())
            (share / "komodo/data/other").symlink_to(share)
            with self.assertRaisesRegex(ValueError, "not a real directory"):
                reconcile.nas_storage({"device": "other"}, share=share, create=True)

    def test_native_monitoring_preserves_partial_and_existing_qts_projects(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            home = root / "home"
            home.mkdir()
            station = root / "station"
            (station / "bin").mkdir(parents=True)
            (root / "qpkg.conf").touch()
            getcfg = root / "getcfg"
            getcfg.write_text(f"""#!/bin/sh
case "$2" in
    Version) echo 5.2.11 ;;
    Install_Path) echo {shlex.quote(str(station))} ;;
esac
""")
            getcfg.chmod(0o700)
            docker = station / "bin" / "docker"
            docker.write_text("""#!/bin/sh
case "$3" in
    ps) printf '%s' "$FIXTURE_CONTAINER" ;;
    inspect) printf '%s' "$FIXTURE_OWNER" ;;
    compose) printf '%s\n' "$*" > "$FIXTURE_DOCKER_ARGS" ;;
esac
""")
            docker.chmod(0o700)
            script = (ssh.REMOTE / "nas.sh").read_text().replace("/sbin/getcfg", str(getcfg)).replace(
                "/etc/config/qpkg.conf", str(root / "qpkg.conf"),
            )
            scope = {**os.environ, "HOME": str(home), "FIXTURE_DOCKER_ARGS": str(root / "calls"),
                     "FIXTURE_CONTAINER": "", "FIXTURE_OWNER": ""}
            data = '{"services":{"node-exporter":{"image":"fixture"}}}\n'

            def invoke(action, argument, content=""):
                return subprocess.run(
                    ["/bin/sh", "-c", script, "onboardrr-nas", action, argument],
                    env=scope, input=content, text=True, capture_output=True,
                )

            self.assertEqual(invoke("stage-monitoring", "existing", data).returncode, 8)
            staged = invoke("stage-monitoring", "new", data)
            self.assertEqual(staged.returncode, 0, staged.stderr)
            source = Path(staged.stdout.strip().removeprefix("compose="))
            self.assertEqual(source.stat().st_mode & 0o777, 0o600)
            self.assertEqual(invoke("stage-monitoring", "existing", data).returncode, 0)
            self.assertEqual(invoke("stage-monitoring", "existing", data + "\n").returncode, 8)

            scope.update(FIXTURE_CONTAINER="fixture-id", FIXTURE_OWNER="/someone-elses-compose.json")
            self.assertEqual(invoke("start-monitoring", str(source)).returncode, 8)
            self.assertFalse((root / "calls").exists())
            scope["FIXTURE_OWNER"] = str(source)
            self.assertEqual(invoke("start-monitoring", str(source)).returncode, 0)
            self.assertIn("up --detach --no-recreate", (root / "calls").read_text())

    def test_nas_shell_probes_native_docker_and_prepares_the_home_idempotently(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            home = root / "home"
            home.mkdir()
            (root / "qpkg.conf").touch()
            profile = home / ".profile"
            profile.write_text("export EDITOR=vi\n")

            def executable(path, shell):
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("#!/bin/sh\n" + shell)
                path.chmod(0o700)

            executable(root / "getcfg", f"""
if [ "$1" = Entware ]; then
    if [ -x {shlex.quote(str(root / 'bin' / 'opkg'))} ]; then
        case "$2" in
            Enable) echo TRUE ;;
            Install_Path) echo {shlex.quote(str(root / 'entware'))} ;;
        esac
    fi
    exit 0
fi
case "$2" in
    Version) echo 5.2.11 ;;
esac
if [ "$1" = container-station ]; then
    case "$2" in
        Enable) echo TRUE ;;
        Install_Path) echo {shlex.quote(str(root / 'station'))} ;;
    esac
fi
""")
            executable(root / "station" / "bin" / "docker", """
case "$1" in
    --host) echo 27.1.2-qnap8 ;;
    compose) echo 2.29.1-qnap2 ;;
esac
""")
            source = root / "fixture.qpkg"
            expected = b"disposable QPKG fixture"
            tools = root / "tools"
            executable(tools / "curl", """
while [ "$1" != --output ]; do shift; done
cp "$FIXTURE_PACKAGE" "$2"
""")
            executable(root / "qpkg_cli", """
[ "$1" = --manually ] && [ -f "$2" ] || exit 1
echo queued >> "$FIXTURE_ROOT/qpkg-calls"
mkdir -p "$FIXTURE_ROOT/bin"
printf '#!/bin/sh\nexit 0\n' > "$FIXTURE_ROOT/bin/opkg"
chmod 700 "$FIXTURE_ROOT/bin/opkg"
""")
            script = (ssh.REMOTE / "nas.sh").read_text()
            for original, fixture in (
                ("/sbin/getcfg", "getcfg"),
                ("/sbin/qpkg_cli", "qpkg_cli"),
                ("/etc/config/qpkg.conf", "qpkg.conf"),
                ("/tmp/onboardrr-entware.", "onboardrr-entware."),
            ):
                script = script.replace(original, str(root / fixture))
            script = script.replace("/opt/bin", str(root / "bin")).replace("/opt", str(root / "opt"))
            script = script.replace("$(uname -m)", "x86_64").replace("$(id -u)", "0")
            script = script.replace(
                "c6361eeb0d23d77bf35fa190901c3477b1451a1ae290ef06dec518b894c38ba8",
                hashlib.sha256(expected).hexdigest(),
            )

            def invoke(action, *arguments, check=True):
                shell = ["/bin/sh", "-c", script, "onboardrr-nas", action]
                if action == "apply":
                    shell.append("lab-nas")
                shell.extend(arguments)
                return subprocess.run(
                    shell, input="# test launcher\n" if action == "apply" else "",
                    env={**os.environ, "HOME": str(home), "PATH": f"{tools}:{os.environ['PATH']}",
                         "FIXTURE_PACKAGE": str(source), "FIXTURE_ROOT": str(root)},
                    text=True, capture_output=True, check=check,
                )

            preview = invoke("inspect").stdout
            self.assertIn("docker=27.1.2-qnap8\ncompose=2.29.1-qnap2", preview)
            self.assertIn("entware_state=missing", preview)
            self.assertNotIn("docker=", invoke("inspect", "system").stdout)
            selected = invoke("inspect", "docker").stdout
            self.assertIn("docker=27.1.2-qnap8", selected)
            self.assertNotIn("entware_state=", selected)
            self.assertFalse((home / ".docker").exists())
            with self.subTest("preserve occupied opt"):
                opt = root / "opt"
                opt.symlink_to(home)
                self.assertIn("entware_state=conflict", invoke("inspect").stdout)
                self.assertEqual(invoke("install-entware", check=False).returncode, 5)
                self.assertTrue(opt.is_symlink())
                opt.unlink()
            source.write_bytes(b"incorrect digest")
            self.assertEqual(invoke("install-entware", check=False).returncode, 6)
            self.assertFalse((root / "qpkg-calls").exists())
            source.write_bytes(expected)
            self.assertEqual(invoke("install-entware").stdout, "changed=yes\n")
            self.assertEqual(invoke("install-entware").stdout, "changed=no\n")
            self.assertEqual((root / "qpkg-calls").read_text(), "queued\n")
            self.assertFalse(list(root.glob("onboardrr-entware.*")))
            self.assertIn("entware_state=ready", invoke("inspect").stdout)
            self.assertEqual(invoke("apply").stdout, "changed=yes\n")
            self.assertEqual(invoke("apply").stdout, "changed=no\n")
            self.assertTrue((home / ".docker" / "compose").is_dir())
            text = profile.read_text()
            self.assertTrue(text.startswith(f"export EDITOR=vi\n\nalias opkg={root}/bin/opkg\n"))
            self.assertEqual(text.count("alias dcu="), 1)
            self.assertEqual(text.count("alias cdcompose="), 1)
            executable(root / "station" / "bin" / "docker", """
if [ "$1" = compose ] && [ -e "$HOME/standalone" ]; then exit 1; fi
printf 'plugin %s %s\n' "$DOCKER_HOST" "$*"
""")
            executable(root / "station" / "bin" / "docker-compose", """
printf 'standalone %s %s\n' "$DOCKER_HOST" "$*"
""")
            env = {**os.environ, "HOME": str(home), "DOCKER_HOST": "tcp://example.invalid:2375"}

            def cli(command):
                return subprocess.check_output(
                    ["/bin/sh", "-c", '. "$HOME/.profile" && ' + command], env=env, text=True,
                )

            self.assertEqual(cli("docker ps"), "plugin unix:///var/run/docker.sock ps\n")
            self.assertEqual(cli("docker compose ps"), "plugin unix:///var/run/docker.sock compose ps\n")
            (home / "standalone").touch()
            self.assertEqual(cli("docker compose ps"), "standalone unix:///var/run/docker.sock ps\n")

    def test_ssh_bootstrap_installs_python_even_when_entware_is_already_ready(self):
        facts = {"entware_state": "ready", "python": "no"}
        with (
            patch.object(nas, "inspect", return_value={"facts": facts, "blockers": [], "sudo": "password"}),
            patch.object(nas, "run", return_value={"changed": "yes"}) as run,
        ):
            nas.prepare_bootstrap(None, {}, "fixture", None)
            run.assert_called_once_with(None, "install-python", sudo="password", password="fixture")
        self.assertEqual(facts["python"], "yes")

    def test_disabled_container_station_has_an_actionable_blocker(self):
        device = {"name": "goku", "profile": "nas", "ssh_user": "operator"}
        with patch.object(nas, "run", return_value={"container_station": "FALSE"}):
            result = nas.inspect(None, device, None, None)
        self.assertEqual(result["blockers"], ["Install and enable Container Station through QNAP App Center."])

    def test_working_docker_still_requires_admin_when_entware_is_missing(self):
        device = {"name": "goku", "profile": "nas", "ssh_user": "operator"}
        facts = {"container_station": "TRUE", "docker": "27.1.2", "compose": "2.29.1",
                 "entware_state": "missing", "uid": "1000"}
        with (
            patch.object(nas, "run", return_value=facts) as run,
            patch.object(ssh, "sudo_access", return_value="unavailable") as sudo,
        ):
            preview = nas.inspect(None, device, None, None)
        sudo.assert_called_once_with(None, 1000, None)
        run.assert_called_once_with(None, "inspect")
        self.assertEqual(preview["blockers"], ["Use --ask-become-pass with a QTS administrator to install Entware."])
