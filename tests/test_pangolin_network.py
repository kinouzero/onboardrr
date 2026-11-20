import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlsplit

from onboardrr.http_json import HttpStatusError
from onboardrr.pangolin import Pangolin
from onboardrr.pangolin_network import configure_private_network, destination_ipv4


class PangolinPrivateNetworkTest(unittest.TestCase):
    def setUp(self):
        directory = self.enterContext(tempfile.TemporaryDirectory())
        self.database = Path(directory) / "state.sqlite"
        self.device = {"name": "ichigo", "host": "192.168.1.146"}
        self.sites = [
            {"siteId": 1, "name": "Goku", "type": "newt"},
            {"siteId": 2, "name": "Naruto", "type": "newt"},
            {"siteId": 3, "name": "local", "type": "local"},
            {"siteId": 9, "name": "Ichigo", "type": "newt"},
        ]
        self.resources = {
            5: {"siteResourceId": 5, "name": "goku", "alias": "goku.pang", "mode": "host",
                "siteIds": [1, 88], "roleIds": [42], "destination": "192.168.1.189", "enabled": True},
            6: {"siteResourceId": 6, "name": "naruto", "alias": "naruto.pang", "mode": "host",
                "siteIds": [1, 2], "enabled": True},
            7: {"siteResourceId": 7, "name": "monitoring", "alias": "monitoring.pang", "mode": "host",
                "siteIds": [2], "enabled": True},
        }
        self.api = Pangolin(Mock(), "fixture-token", "zero")
        self.api.service.request.side_effect = self.respond
        self.writes = []
        self.fail_update = None

    def respond(self, method, path, *, operation, headers, data=None):
        parts = urlsplit(path)
        if method == "GET":
            field, records = (("sites", self.sites) if parts.path.endswith("/sites")
                              else ("siteResources", list(self.resources.values())))
            page = int(parse_qs(parts.query)["page"][0])
            return {"data": {field: records[page - 1:page],
                             "pagination": {"total": len(records), "pageSize": 1, "page": page}}}
        self.writes.append((method, parts.path, data))
        if method == "PUT":
            self.resources[10] = {"siteResourceId": 10, "enabled": True, **data}
            return {"data": {"siteResourceId": 10}}
        identifier = int(parts.path.rsplit("/", 1)[1])
        if identifier == self.fail_update:
            raise HttpStatusError("Pangolin", operation, 429)
        self.assertEqual(data.get("name"), self.resources[identifier]["name"])
        self.resources[identifier].update(data)
        return {"data": {"siteResourceId": identifier}}

    def reconcile(self, apply=False):
        return configure_private_network(self.api, self.device, self.sites[-1], database=self.database, apply=apply)

    def test_preview_apply_and_repeat_maintain_routes_and_grants(self):
        preview = self.reconcile()
        self.assertEqual(preview["alias"], "ichigo.pang")
        self.assertEqual(preview["site_ids"], [1, 2, 9])
        self.assertEqual(preview["peer_resources_to_update"], [5, 6])
        self.assertEqual(self.writes, [])
        self.assertEqual(self.reconcile(apply=True), preview)
        self.assertEqual(self.writes[0][0:2], ("PUT", "/v1/org/zero/private-resource"))
        self.assertEqual(self.resources[10]["destination"], "192.168.1.146")
        self.assertEqual(self.resources[10]["roleIds"], [])
        self.assertEqual(self.resources[5]["siteIds"], [1, 9, 88])
        self.assertEqual(self.resources[5]["roleIds"], [42])
        self.assertEqual(self.resources[7]["siteIds"], [2])
        self.writes.clear()
        self.assertEqual(self.reconcile(apply=True)["action"], "reuse")
        self.assertEqual(self.writes, [])

    def test_retry_resumes_only_the_missing_assignment(self):
        self.fail_update = 6
        created = Mock()
        with self.assertRaises(HttpStatusError):
            configure_private_network(self.api, self.device, self.sites[-1], database=self.database,
                                      apply=True, record_created=created)
        created.assert_called_once_with("zero/9/10/192.168.1.146")
        self.fail_update = None
        self.writes.clear()
        result = configure_private_network(self.api, self.device, self.sites[-1], database=self.database,
                                           apply=True, record_created=created)
        self.assertEqual(result["action"], "reuse")
        created.assert_called_once()
        self.assertEqual(self.writes, [("POST", "/v1/private-resource/6", {"name": "naruto", "siteIds": [1, 2, 9]})])

    def test_existing_destination_conflict_blocks_all_writes(self):
        self.resources[5].update(name="ichigo", alias="ICHIGO.PANG")
        with self.assertRaisesRegex(ValueError, "conflicting resource"):
            self.reconcile(apply=True)
        self.assertEqual(self.writes, [])

    def test_existing_target_adds_missing_newt_sites_without_replacing_assignments(self):
        self.reconcile(apply=True)
        self.resources[10]["siteIds"] = [9, 88]
        self.writes.clear()
        self.assertEqual(self.reconcile(apply=True)["action"], "update")
        self.assertEqual(self.writes, [("POST", "/v1/private-resource/10", {"name": "ichigo", "siteIds": [1, 2, 9, 88]})])

    def test_hostname_with_multiple_ipv4_addresses_requires_override(self):
        results = [(None, None, None, None, (ip, 0)) for ip in ("192.168.1.10", "192.168.1.11")]
        with patch("onboardrr.host_resolution.socket.getaddrinfo", return_value=results):
            with self.assertRaisesRegex(ValueError, "--ipv4"):
                destination_ipv4({"host": "ichigo.local"}, None)
        self.assertEqual(destination_ipv4({"host": "ichigo.local"}, "192.168.1.146"), "192.168.1.146")

    def test_unresolved_host_uses_the_openssh_destination_for_the_private_route(self):
        records = [(None, None, None, None, ("192.0.2.14", 0))]
        with (patch("onboardrr.host_resolution.socket.getaddrinfo", side_effect=[OSError(), records]),
              patch("onboardrr.host_resolution.openssh_destination", return_value="nas.example") as alias,
              patch("onboardrr.pangolin_network.stored_host_ipv4") as stored):
            self.assertEqual(destination_ipv4({"host": "secondary.local", "name": "secondary"}, None), "192.0.2.14")
            alias.assert_called_once_with("secondary.local")
            stored.assert_not_called()
