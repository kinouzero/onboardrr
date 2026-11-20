import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from onboardrr.http_json import HttpStatusError
from onboardrr.inventory import Inventory, PROFILE
from onboardrr.pangolin import Pangolin, enroll_site
from onboardrr.runs import Ownership, Resource, ResourceKind, RunStore
from onboardrr.secrets import OpenBao


class NewtEnrollmentTest(unittest.TestCase):
    def setUp(self):
        directory = self.enterContext(tempfile.TemporaryDirectory())
        database = Path(directory) / "inventory.sqlite3"
        self.device = Inventory(database).add("lab-pi", "lab-pi.local", "operator", 22, PROFILE)
        self.runs = RunStore(database)
        run = self.runs.open("lab-pi")
        self.run_id = run["id"]
        for step in ("ssh", "komodo-registration", "goku-files", "linux", "periphery"):
            self.runs.complete_step(self.run_id, step)
        self.transport = Mock()
        self.api = Pangolin(self.transport, "fixture-token", "zero")
        self.bao = Mock()
        self.fields = {"LAB_PI_NEWT_ID": "fixture-id", "LAB_PI_NEWT_SECRET": "fixture-secret"}
        self.site = {"siteId": 9, "niceId": "lab-pi", "type": "newt", "online": True, "newtId": "fixture-id"}
        self.not_found = HttpStatusError("Pangolin", "read site", 404)
        self.empty_sites = {"data": {"sites": [], "pagination": {"page": 1, "pageSize": 100, "total": 0}}}

    def test_create_persists_credentials_then_reuses_site_and_verifies_connection(self):
        events = Mock()
        events.attach_mock(self.bao, "bao")
        events.attach_mock(self.transport, "pangolin")
        self.transport.request.side_effect = [
            self.not_found,
            self.empty_sites,
            {"data": {"newtId": "fixture-id", "newtSecret": "fixture-secret"}},
            {"data": {"siteId": 9}},
            {"data": self.site},
        ]
        self.bao.fields_with_version.return_value = ({}, 4)
        self.bao.read.return_value = ("fixture-id", "fixture-secret")
        created = enroll_site(self.api, self.device, runs=self.runs, bao=self.bao, apply=True)
        operations = [call[0] if call[0] != "pangolin.request" else call.args[0] for call in events.mock_calls]
        self.assertEqual(operations, ["GET", "GET", "bao.fields_with_version", "GET", "bao.patch", "bao.read", "PUT", "GET"])
        self.bao.patch.assert_called_once_with("variables", self.fields, version=4)
        self.assertEqual(created["action"], "create")
        self.assertNotIn("fixture-", json.dumps(created))

        self.runs.complete_step(self.run_id, "komodo-stacks")
        with patch("onboardrr.pangolin.configure_private_network") as network:
            def interrupt_after_creation(*args, record_created, **kwargs):
                record_created("zero/9/5/192.168.1.42")
                raise ValueError("private host assignment failed")

            network.side_effect = interrupt_after_creation
            self.transport.request.side_effect = None
            self.transport.request.return_value = {"data": self.site}
            self.bao.fields_with_version.return_value = (self.fields, 5)
            with self.assertRaisesRegex(ValueError, "private host assignment failed"):
                enroll_site(self.api, self.device, runs=self.runs, bao=self.bao, apply=True)
            self.assertEqual(self.runs.show(self.run_id)["next_step"], "pangolin")
            network.side_effect = None
            network.return_value = {"alias": "lab-pi.pang"}
            reused = enroll_site(self.api, self.device, runs=self.runs, bao=self.bao, apply=True)
            self.assertEqual(reused["private_network"], {"alias": "lab-pi.pang"})
        self.assertEqual(reused["action"], "reuse")
        self.assertEqual(self.runs.show(self.run_id)["next_step"], "verify")
        resources = self.runs.show(self.run_id)["resources"]
        self.assertEqual(
            [(entry["reference"], entry["ownership"]) for entry in resources if entry["kind"] == "pangolin-site"],
            [("9", "created")],
        )
        self.assertEqual(sum(entry["kind"] == "newt-credentials" for entry in resources), 1)
        self.assertEqual([(entry["reference"], entry["ownership"]) for entry in resources
                          if entry["kind"] == "pangolin-private-route"], [("zero/9/5/192.168.1.42", "created")])
        self.assertEqual(sum(call.args[0] == "PUT" for call in self.transport.request.call_args_list), 1)

    def test_retry_after_interruption_reuses_saved_credential_pair(self):
        self.bao.fields_with_version.return_value = (self.fields, 5)
        self.transport.request.side_effect = [
            self.not_found, self.empty_sites, {"data": {"siteId": 9}}, {"data": self.site},
        ]
        enroll_site(self.api, self.device, runs=self.runs, bao=self.bao, apply=True)
        self.bao.patch.assert_not_called()
        put = self.transport.request.call_args_list[2]
        self.assertEqual(put.kwargs["data"]["secret"], "fixture-secret")

    def test_dedicated_storage_creates_both_fields_before_contacting_pangolin(self):
        self.bao.mount = "services"
        self.bao.fields_with_version.side_effect = HttpStatusError("OpenBao", "KV read", 404)
        self.bao.read.return_value = ("fixture-id", "fixture-secret")
        self.transport.request.side_effect = [
            self.not_found,
            self.empty_sites,
            {"data": {"newtId": "fixture-id", "newtSecret": "fixture-secret"}},
            {"data": {"siteId": 9}}, {"data": self.site},
        ]
        events = Mock()
        events.attach_mock(self.bao, "bao")
        events.attach_mock(self.transport, "pangolin")
        enroll_site(self.api, self.device, runs=self.runs, bao=self.bao, apply=True)
        self.bao.create.assert_called_once_with(
            "pangolin/newt/lab-pi", {"ID": "fixture-id", "SECRET": "fixture-secret"},
        )
        self.bao.patch.assert_not_called()
        self.assertEqual([call[0] for call in events.mock_calls], [
            "pangolin.request", "pangolin.request", "bao.fields_with_version", "pangolin.request", "bao.create",
            "bao.read", "pangolin.request", "pangolin.request",
        ])

    def test_dedicated_storage_rejects_partial_pair(self):
        self.bao.mount = "services"
        self.bao.fields_with_version.return_value = ({"ID": "fixture-id"}, 3)
        self.transport.request.side_effect = [self.not_found, self.empty_sites]
        with self.assertRaisesRegex(ValueError, "incomplete or invalid"):
            enroll_site(self.api, self.device, runs=self.runs, bao=self.bao, apply=True)
        self.bao.create.assert_not_called()
        self.bao.patch.assert_not_called()

    def test_existing_site_with_a_different_identity_is_never_modified(self):
        self.bao.fields_with_version.return_value = (self.fields, 5)
        self.transport.request.return_value = {"data": {**self.site, "newtId": "different"}}
        with self.assertRaisesRegex(ValueError, "does not match"):
            enroll_site(self.api, self.device, runs=self.runs, bao=self.bao, apply=True)
        self.bao.patch.assert_not_called()
        self.assertEqual(self.transport.request.call_count, 1)
        self.assertEqual(self.runs.show(self.run_id)["resources"], [])

    def test_resumes_existing_site_with_generated_identifier(self):
        legacy = {**self.site, "name": "Lab-Pi", "niceId": "generated-site-name"}
        listing = {"data": {"sites": [legacy], "pagination": {"page": 1, "pageSize": 100, "total": 1}}}
        self.transport.request.side_effect = [
            self.not_found, listing, {"data": legacy}, self.not_found, {"data": legacy},
        ]
        self.bao.fields_with_version.return_value = (self.fields, 5)
        created = enroll_site(self.api, self.device, runs=self.runs, bao=self.bao, apply=True)
        self.assertEqual(created["action"], "reuse")
        self.runs.complete_step(self.run_id, "komodo-stacks")
        with patch("onboardrr.pangolin.configure_private_network", return_value={}):
            enroll_site(self.api, self.device, runs=self.runs, bao=self.bao, apply=True)
        run = self.runs.show(self.run_id)
        self.assertEqual(run["next_step"], "verify")
        self.assertEqual([(entry["reference"], entry["ownership"]) for entry in run["resources"]], [("9", "reused")])
        self.assertTrue(all(call.args[0] == "GET" for call in self.transport.request.call_args_list))
        self.bao.patch.assert_not_called()

    def test_missing_recorded_site_is_not_recreated(self):
        self.runs.record_resource(self.run_id, "komodo-stacks", Resource(
            ResourceKind.PANGOLIN_SITE, "9", Ownership.CREATED,
        ))
        self.transport.request.side_effect = self.not_found
        with self.assertRaisesRegex(ValueError, "recorded by the onboarding run"):
            enroll_site(self.api, self.device, runs=self.runs, bao=self.bao, apply=True)
        self.assertEqual(self.transport.request.call_count, 1)
        self.bao.fields_with_version.assert_not_called()

    def test_site_only_can_run_before_komodo_and_routes_do_not_close_other_stacks(self):
        device = Inventory(self.runs.database).add("early", "early.local", "operator", 22, PROFILE)
        run_id = self.runs.open("early")["id"]
        self.runs.complete_step(run_id, "ssh")
        self.bao.fields_with_version.return_value = (
            {"EARLY_NEWT_ID": "fixture-id", "EARLY_NEWT_SECRET": "fixture-secret"}, 1,
        )
        self.transport.request.return_value = {"data": {**self.site, "niceId": "early"}}
        with patch("onboardrr.pangolin.configure_private_network", return_value={}) as routes:
            enroll_site(self.api, device, runs=self.runs, bao=self.bao, apply=True, site_only=True)
            self.assertEqual(self.runs.show(run_id)["next_step"], "komodo-registration")
            routes.assert_not_called()
            for step in ("komodo-registration", "goku-files", "linux", "periphery"):
                self.runs.complete_step(run_id, step)
            enroll_site(self.api, device, runs=self.runs, bao=self.bao, apply=True, sync_routes=True)
            routes.assert_called_once()
        self.assertEqual(self.runs.show(run_id)["next_step"], "komodo-stacks")

    def test_kv_version_conflict_prevents_site_creation(self):
        self.transport.request.side_effect = [
            self.not_found, self.empty_sites, {"data": {"newtId": "fixture-id", "newtSecret": "fixture-secret"}},
        ]
        self.bao.fields_with_version.return_value = ({}, 4)
        self.bao.patch.side_effect = HttpStatusError("OpenBao", "KV patch", 400)
        with self.assertRaisesRegex(ValueError, "KV patch failed"):
            enroll_site(self.api, self.device, runs=self.runs, bao=self.bao, apply=True)
        self.assertTrue(all(call.args[0] == "GET" for call in self.transport.request.call_args_list))

    def test_openbao_patch_supplies_current_version_and_only_the_new_fields(self):
        bao = OpenBao("https://bao.example", "komodo", "fixture-token")
        bao.service = Mock()
        bao.patch("variables", self.fields, version=4)
        bao.service.request.assert_called_once_with(
            "PATCH", "/v1/komodo/data/variables",
            headers={"X-Vault-Token": "fixture-token", "Content-Type": "application/merge-patch+json"},
            operation="KV patch", data={"options": {"cas": 4}, "data": self.fields},
        )

    def test_retirement_preserves_site_with_dependencies(self):
        self.transport.request.side_effect = [
            {"data": {**self.site, "online": False}},
            {"data": {"sites": [{"siteId": 9, "resourceCount": "1"}]}},
        ]
        self.assertEqual(self.api.retire_site("lab-pi", "9"), "site-has-dependencies")
        self.assertTrue(all(request.args[0] == "GET" for request in self.transport.request.call_args_list))

    def test_retirement_deletes_only_the_offline_site_without_cascading(self):
        self.transport.request.side_effect = [
            {"data": {**self.site, "online": False}},
            {"data": {"sites": [{"siteId": 9, "resourceCount": "0"}]}},
            {"success": True, "data": None},
            self.not_found,
            self.not_found,
        ]
        self.assertEqual(self.api.retire_site("lab-pi", "9"), "removed")
        self.assertEqual(self.api.retire_site("lab-pi", "9"), "absent")
        delete, = (request for request in self.transport.request.call_args_list if request.args[0] == "DELETE")
        self.assertEqual(delete.args[1], "/v1/site/9?deleteResources=false")

    def test_retirement_can_detach_from_shared_routes_without_deleting_them(self):
        offline = {"data": {**self.site, "online": False}}
        resource = {"siteResourceId": 5, "name": "goku", "alias": "goku.pang", "mode": "host", "siteIds": [3, 9]}
        def private_page(entry):
            return {"data": {"siteResources": [entry], "pagination": {"total": 1, "pageSize": 100, "page": 1}}}
        self.transport.request.side_effect = [
            offline, {"data": {"sites": [{"siteId": 9, "resourceCount": "1"}]}},
            private_page(resource), {"data": {"siteResourceId": 5}},
            private_page({**resource, "siteIds": [3]}), offline,
            {"data": {"sites": [{"siteId": 9, "resourceCount": 0}]}},
            {"success": True, "data": None},
        ]
        self.assertEqual(self.api.retire_site("lab-pi", "9", detach_private_routes=True), "removed")
        writes = [request for request in self.transport.request.call_args_list if request.args[0] != "GET"]
        self.assertEqual([request.args for request in writes],
                         [("POST", "/v1/private-resource/5"), ("DELETE", "/v1/site/9?deleteResources=false")])
        self.assertEqual(writes[0].kwargs["data"], {"name": "goku", "siteIds": [3]})

    def test_retirement_leaves_routes_untouched_if_any_dependency_cannot_be_detached(self):
        shared = {"siteResourceId": 5, "name": "goku", "alias": "goku.pang", "mode": "host", "siteIds": [3, 9]}
        for other in (None, {**shared, "siteResourceId": 6, "mode": "ssh"},
                      {**shared, "siteResourceId": 6, "siteIds": [9]}):
            with self.subTest(other=other):
                self.transport.request.reset_mock()
                resources = [shared, other] if other else [shared]
                self.transport.request.side_effect = [
                    {"data": {**self.site, "online": False}},
                    {"data": {"sites": [{"siteId": 9, "resourceCount": 2}]}},
                    {"data": {"siteResources": resources, "pagination": {
                        "total": len(resources), "pageSize": 100, "page": 1,
                    }}},
                ]
                self.assertEqual(self.api.retire_site("lab-pi", "9", detach_private_routes=True),
                                 "site-has-dependencies")
                self.assertTrue(all(request.args[0] == "GET" for request in self.transport.request.call_args_list))

    def test_retirement_deletes_only_the_recorded_single_site_route(self):
        offline = {"data": {**self.site, "online": False}}
        dependent = {"data": {"sites": [{"siteId": 9, "resourceCount": 1}]}}
        resource = {"siteResourceId": 5, "name": "lab-pi", "alias": "lab-pi.pang", "mode": "host",
                    "destination": "192.168.1.42", "siteIds": [9]}

        def private_page(records):
            return {"data": {"siteResources": records,
                             "pagination": {"total": len(records), "pageSize": 100, "page": 1}}}

        wrong = {"zero/9/5/192.168.1.43": {"status": "pending"}}
        self.transport.request.side_effect = [offline, dependent, private_page([resource])]
        self.assertEqual(self.api.retire_site("lab-pi", "9", detach_private_routes=True, owned_routes=wrong),
                         "site-has-dependencies")
        self.assertTrue(all(request.args[0] == "GET" for request in self.transport.request.call_args_list))

        owned = {"zero/9/5/192.168.1.42": {"status": "pending"}}
        self.transport.request.side_effect = [
            offline, dependent, private_page([resource]), {"success": True}, private_page([]), offline,
            {"data": {"sites": [{"siteId": 9, "resourceCount": 0}]}}, {"success": True},
        ]
        self.assertEqual(self.api.retire_site("lab-pi", "9", detach_private_routes=True, owned_routes=owned), "removed")
        self.assertEqual(owned["zero/9/5/192.168.1.42"]["status"], "removed")
        deletes = [request.args[1] for request in self.transport.request.call_args_list if request.args[0] == "DELETE"]
        self.assertEqual(deletes, ["/v1/private-resource/5", "/v1/site/9?deleteResources=false"])

        owned["zero/9/5/192.168.1.42"]["status"] = "pending"
        self.transport.request.side_effect = [self.not_found, self.not_found, private_page([])]
        self.assertEqual(self.api.retire_site("lab-pi", "9", detach_private_routes=True, owned_routes=owned), "absent")
        self.assertEqual(owned["zero/9/5/192.168.1.42"]["status"], "absent")
