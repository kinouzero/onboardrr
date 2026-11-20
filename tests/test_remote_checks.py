import io
import json
import runpy
import subprocess
import unittest
from contextlib import nullcontext, redirect_stdout
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from onboardrr.ssh import REMOTE


def run_script(script, request):
    stdout = io.StringIO()
    with patch("sys.stdin", io.StringIO(json.dumps(request))), redirect_stdout(stdout):
        try:
            runpy.run_path(str(REMOTE / script), run_name="__main__")
        except SystemExit as error:
            if error.code:
                raise
    return json.loads(stdout.getvalue())


class RemoteChecksTest(unittest.TestCase):
    def test_mount_detachment_requires_both_active_and_persistent_checks(self):
        with (patch("pathlib.Path.home", return_value=Path("/home/operator")),
              patch("subprocess.run", side_effect=[
                  subprocess.CompletedProcess([], 1, stderr=b""),
                  subprocess.CompletedProcess([], 0, stderr=b""),
                  subprocess.CompletedProcess([], 1, stderr=b"findmnt error"),
                  subprocess.CompletedProcess([], 1, stderr=b""),
              ]) as command):
            report = run_script("verify.py", {"mount_check_only": True, "include_compose": True,
                                               "mounts": ["/share/docker/data"]})
        self.assertEqual(report["detached"], {"/share/docker/data": False, "/home/operator/.docker/compose": False})
        self.assertEqual(sum("--fstab" in entry.args[0] for entry in command.call_args_list), 2)

    def test_verification_reports_unhealthy_services_and_the_correct_nas_storage(self):
        home = Path(self.enterContext(TemporaryDirectory()))
        workspace = home / ".docker/compose"
        workspace.parent.mkdir()
        workspace.symlink_to("/share/compose")
        destination = "/share/docker/komodo/data/nas"
        for local in (True, False):
            with self.subTest(local=local):
                mount = {"Type": "bind", "Source": destination} if local else {
                    "Type": "volume", "Name": "komodo_onboardrr-periphery-data"
                }
                outputs = ["27.1.2-qnap8", "2.29.1-qnap2", "healthy-id", "running healthy",
                           "unhealthy-id", "running unhealthy",
                           json.dumps([{**mount, "Destination": destination, "RW": True}])]
                proc = (f"//host/compose {workspace.resolve()} cifs rw 0 0\n"
                        f"host:/data {Path(destination).resolve()} nfs4 rw 0 0\n")
                with (patch("pathlib.Path.home", return_value=home),
                      patch("pathlib.Path.read_text", return_value=proc),
                      patch("subprocess.check_output", side_effect=outputs)):
                    result = run_script("verify.py", {"mounts": [], "projects": ["healthy", "unhealthy"],
                                                      "nas_device": "nas", "local_storage": local})
                self.assertEqual(result["projects"], {"healthy": True, "unhealthy": False})
                self.assertTrue(result["nas_storage"])
                self.assertTrue(result["nas_shares"])

    def test_preflight_distinguishes_unconfigured_endpoints_from_unreachable_ports(self):
        home = Path(self.enterContext(TemporaryDirectory()))
        (home / ".docker" / "compose").mkdir(parents=True)
        with (patch("pathlib.Path.home", return_value=home),
              patch("pathlib.Path.read_text", return_value='ID=debian\nNAME="ignored"\nVERSION_ID="13"\n'),
              patch("shutil.which", return_value=None),
              patch("socket.create_connection", side_effect=[nullcontext(), OSError("fixture")]) as connect):
            result = run_script("preflight.py", {"goku": ["192.0.2.10", [445, 2049]], "pangolin": [None, [443]]})
        self.assertEqual(result["os"], {"ID": "debian", "VERSION_ID": "13"})
        self.assertEqual(result["services"], {"goku": {"445": True, "2049": False}, "pangolin": None})
        self.assertFalse(result["docker_present"])
        self.assertEqual(connect.call_count, 2)

    def test_pangolin_relay_uses_only_one_private_container_address_and_preserves_http_status(self):
        request = {"method": "POST", "path": "/integration", "headers": {"Authorization": "fixture"}, "data": {"id": 1}}
        with (patch("subprocess.check_output", return_value=b'{"network":{"IPAddress":"8.8.8.8"}}'),
              patch("http.client.HTTPConnection") as connect):
            with self.assertRaises(SystemExit):
                run_script("pangolin_api.py", request)
            connect.assert_not_called()
        for status, body, expected in ((201, b'{"ok":true}', {"ok": True}), (403, b"fixture", None)):
            with self.subTest(status=status):
                networks = b'{"network":{"IPAddress":"172.18.0.20"}}'
                with (patch("subprocess.check_output", return_value=networks),
                      patch("http.client.HTTPConnection") as connect):
                    connection = connect.return_value
                    response = connection.getresponse.return_value
                    response.status = status
                    response.read.return_value = body
                    self.assertEqual(run_script("pangolin_api.py", request), {"status": status, "response": expected})
                    connect.assert_called_once_with("172.18.0.20", 3003, timeout=10)
                    connection.close.assert_called_once()
