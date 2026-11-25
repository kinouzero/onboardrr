import unittest
from unittest.mock import patch

from onboardrr.remote.kuma import inspect_local, uptime_kuma_present


class KumaDetectionTest(unittest.TestCase):
    def test_daemon_lookup_identifies_kuma_without_matching_its_satellites(self):
        satellites = "louislam/uptime-kuma-mariadb:1\tuptime-kuma-mariadb\tuptime-kuma-mariadb\n"
        satellites += "ghcr.io/bigboot/autokuma:2\tautokuma\tautokuma\n"
        self.assertFalse(uptime_kuma_present(satellites))
        self.assertTrue(uptime_kuma_present(satellites + "mirror.test/kuma/uptime-kuma@sha256:abc\tmonitor\t\n"))
        self.assertTrue(uptime_kuma_present("private.registry/renamed:2\tmonitor\tuptime-kuma\n"))

    @patch("onboardrr.remote.kuma.subprocess.check_output", return_value="")
    def test_lookup_is_local_and_includes_stopped_containers(self, docker):
        self.assertFalse(inspect_local())
        self.assertEqual(docker.call_args.args[0][:5],
                         ["docker", "--host", "unix:///var/run/docker.sock", "ps", "--all"])
