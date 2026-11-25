import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from onboardrr.inventory import Inventory, PROFILE
from onboardrr.stack_ip_refresh import refresh_komodo_host_ip, redeploy_host_stacks, uses_host_ip, wait_for_core_ip


def stack(number, *, contents="ports: [${LISTEN_IP}]", template=False):
    return {"_id": {"$oid": str(number) * 24}, "name": f"stack-{number}", "template": template,
            "config": {"file_contents": contents, "environment": "LISTEN_IP=[[LAB_IP]]"},
            "info": {"deployed_config": "previous deployment"}}


class StackIpRefreshTest(unittest.TestCase):
    def test_periphery_address_is_updated_before_refreshing_stack_states(self):
        api, inventory, device = Mock(), Mock(), {"name": "lab"}
        address, known_hosts = "192.0.2.20", Path("known_hosts")
        api.sync_periphery_ip.return_value = {"komodo_periphery_address": f"https://{address}:8120"}

        def deploy(*args):
            api.sync_periphery_ip.assert_called_once_with("lab", address, apply=True)
            raise ValueError("Komodo stack list unavailable")

        with patch("onboardrr.stack_ip_refresh.redeploy_host_stacks", side_effect=deploy):
            result = refresh_komodo_host_ip(api, inventory, device, known_hosts, address)
        self.assertEqual(result["komodo_periphery_address"], f"https://{address}:8120")
        self.assertIn("Komodo stack list unavailable", result["blockers"][0])

    def test_detects_exact_and_indirect_references_in_compose(self):
        self.assertTrue(uses_host_ip(stack(1), "LAB_IP"))
        indirect = stack(1, contents="address: ${URL}")
        indirect["config"]["environment"] += "\nURL=http://$LISTEN_IP:8080"
        self.assertTrue(uses_host_ip(indirect, "LAB_IP"))
        self.assertFalse(uses_host_ip(stack(1, contents="# old ${LISTEN_IP}\n${LISTEN_IP_OTHER}"), "LAB_IP"))
        self.assertFalse(uses_host_ip(stack(1), "LAB_IP_OTHER"))

    def test_only_running_dependents_deploy_and_retry_resumes_the_failed_stack(self):
        with tempfile.TemporaryDirectory() as directory:
            inventory = Inventory(Path(directory) / "inventory.sqlite3")
            device = inventory.add("lab", "lab.local", "operator", 22, PROFILE)
            stacks = [stack(1), stack(2), stack(3), stack(4, contents="services: {}"),
                      stack(5, template=True), stack(6), stack(7), stack(8)]
            states = ["running", "unknown", "down", "running", "running", "running", "stopped", "paused"]
            summary = [{"id": row["_id"]["$oid"], "info": {"state": state}} for row, state in zip(stacks, states)]
            deployed = []
            fail = {"6" * 24}

            def request(route, action, arguments=None):
                if action == "ListStacks":
                    return summary
                if action == "ListFullStacks":
                    return stacks
                self.assertEqual((route, action), ("/execute", "DeployStack"))
                identifier = arguments["stack"]
                deployed.append(identifier)
                if identifier in fail:
                    raise ValueError("temporary failure")
                return {"_id": {"$oid": "d" * 24}, "status": "Complete", "success": True}

            api = Mock()
            api._request.side_effect = request
            hosts = Path("known_hosts")
            with patch("onboardrr.stack_ip_refresh.wait_for_core_ip") as ready:
                first = redeploy_host_stacks(api, inventory, device, hosts, "192.0.2.10")
                ready.assert_called_once_with(api, inventory, hosts, "LAB_IP", "192.0.2.10")
                fail.clear()
                second = redeploy_host_stacks(api, inventory, device, hosts, "192.0.2.10")
                third = redeploy_host_stacks(api, inventory, device, hosts, "192.0.2.11")
            self.assertEqual(first["redeployed_stacks"], ["stack-1"])
            self.assertEqual(second["redeployed_stacks"], ["stack-6"])
            self.assertEqual(third["redeployed_stacks"], ["stack-1", "stack-6"])
            self.assertEqual(deployed, ["1" * 24, "6" * 24, "6" * 24, "1" * 24, "6" * 24])
            self.assertIn("stack-2", first["blockers"][0])
            self.assertIn("temporary failure", first["blockers"][1])
            self.assertEqual(len(first["blockers"]), 2)

    def test_waits_for_the_loaded_core_secret_before_deploying(self):
        api, inventory = Mock(), Mock()
        api._request.return_value = ["LAB_IP"]
        with (patch("onboardrr.stack_ip_refresh.run_ssh_probe", side_effect=[{"ready": False}, {"ready": True}]) as probe,
              patch("onboardrr.stack_ip_refresh.sleep") as pause):
            wait_for_core_ip(api, inventory, Path("known_hosts"), "LAB_IP", "192.0.2.10")
        self.assertEqual(probe.call_count, 2)
        pause.assert_called_once()
        api.version.assert_called_once()
