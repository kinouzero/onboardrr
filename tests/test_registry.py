import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from onboardrr.http_json import HttpStatusError
from onboardrr.linux import PROVISIONING
from onboardrr.registry import docker_hub_credentials


class DockerRegistryTest(unittest.TestCase):
    @patch("onboardrr.registry.configured_openbao")
    def test_gets_the_optional_docker_hub_credentials(self, configured):
        bao = Mock()
        configured.return_value = bao
        bao.read.side_effect = [("docker-user",), ("fixture-token",)]
        self.assertEqual(docker_hub_credentials(), {"username": "docker-user", "token": "fixture-token"})
        configured.assert_called_once_with(mount="services")
        self.assertEqual([call.args for call in bao.read.call_args_list],
                         [("dockerhub/USERNAME", "value"), ("dockerhub/TOKEN", "value")])
        missing = HttpStatusError("OpenBao", "KV read", 404)
        bao.read.side_effect = [missing, missing]
        self.assertIsNone(docker_hub_credentials())
        bao.read.side_effect = [("docker-user",), missing]
        with self.assertRaisesRegex(ValueError, "Configure valid Docker Hub"):
            docker_hub_credentials()

    def test_login_uses_standard_input_and_restricts_the_config(self):
        script = PROVISIONING / "roles/linux/files/docker-login.sh"
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            docker = home / "docker"
            docker.write_text(
                '#!/bin/sh\n'
                'set -eu\n'
                '[ "$1" = --host ] && [ "$2" = unix:///var/run/docker.sock ] && [ "$3" = --config ]\n'
                '[ "$5" = login ] && [ "$6" = --username ] && [ "$7" = fixture-user ]\n'
                '[ "$8" = --password-stdin ] && [ "$#" = 8 ]\n'
                'IFS= read -r token\n'
                '[ "$token" = fixture-token ]\n'
                'printf \'{"auths":{}}\' > "$4/config.json"\n'
            )
            docker.chmod(0o700)
            invocation = ["/bin/sh", str(script), str(docker), str(home), "fixture-user"]
            result = subprocess.run(invocation, input="fixture-token\n", text=True, capture_output=True)
            self.assertEqual((result.returncode, result.stdout, result.stderr), (0, "", ""))
            config = home / ".docker/config.json"
            self.assertEqual(json.loads(config.read_text()), {"auths": {}})
            self.assertEqual(config.stat().st_mode & 0o777, 0o600)
            original = home / "other.json"
            config.rename(original)
            config.symlink_to(original)
            result = subprocess.run(invocation, input="fixture-token\n", text=True, capture_output=True)
            self.assertEqual(result.returncode, 9)
