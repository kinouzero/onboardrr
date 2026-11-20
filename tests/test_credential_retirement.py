import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock

from onboardrr.credential_retirement import retire_credentials
from onboardrr.inventory import Inventory, PROFILE
from onboardrr.pangolin import newt_reference
from onboardrr.runs import Ownership, Resource, ResourceKind, RunStore


class CredentialRetirementTest(unittest.TestCase):
    def setUp(self):
        directory = Path(self.enterContext(TemporaryDirectory()))
        self.database = directory / "inventory.sqlite3"
        self.device = Inventory(self.database).add("lab", "192.168.1.55", "operator", 22, PROFILE)
        self.komodo, self.pangolin, self.bao = Mock(), Mock(), Mock()
        self.pangolin.site.return_value = None
        self.variables = {"LAB_HOSTNAME": "lab", "LAB_IP": "192.168.1.55", "SHARED": "fixture-shared"}
        self.fields = {"LAB_NEWT_ID": "fixture-id", "LAB_NEWT_SECRET": "fixture-secret"}
        self.references = {}
        self.komodo._request.side_effect = self.request
        self.bao.fields_with_version.side_effect = lambda *args: (self.fields.copy(), 6)
        self.bao.patch.side_effect = lambda path, patch, *, version: [self.fields.pop(k) for k in patch]
        runs = RunStore(self.database)
        run = runs.open("lab")
        for variable in self.variables:
            runs.record_resource(run["id"], "ssh", Resource(ResourceKind.KOMODO_VARIABLE, variable, Ownership.CREATED))
        fingerprint = newt_reference("fixture-id", "fixture-secret")
        runs.record_resource(run["id"], "ssh", Resource(ResourceKind.NEWT_CREDENTIALS, fingerprint, Ownership.CREATED))

    def request(self, route, action, params=None):
        if action == "ListStacks":
            return [{"id": "other", "info": {"server_id": "other"}}] if self.references else []
        if action == "GetStack":
            return {"config": self.references}
        if action == "ListVariables":
            return [{"name": key, "value": value} for key, value in self.variables.items()]
        if action == "DeleteVariable":
            name = params["name"]
            return {"name": name, "value": self.variables.pop(name)}
        if action.startswith("List"):
            return []
        self.fail(f"Unexpected Komodo action: {action}")

    def apply(self):
        return retire_credentials(self.database, self.device, self.komodo, self.pangolin, self.bao, apply=True)

    def test_removes_only_owned_values_and_reuses_absence(self):
        report = self.apply()
        self.assertEqual(report["newt"], "removed")
        self.assertEqual(set(report["variables"].values()), {"removed"})
        self.assertEqual(self.variables, {"SHARED": "fixture-shared"})
        self.assertEqual(self.fields, {})
        self.assertNotIn("fixture", str(report))
        self.bao.patch.assert_called_once_with(
            "variables", {"LAB_NEWT_ID": None, "LAB_NEWT_SECRET": None}, version=6,
        )
        repeated = self.apply()
        self.assertEqual(repeated["newt"], "absent")
        self.assertEqual(set(repeated["variables"].values()), {"absent"})

    def test_preserves_cross_resource_references_and_replaced_values(self):
        self.references = {"environment": "HOST=[[LAB_HOSTNAME]]"}
        self.fields["LAB_NEWT_SECRET"] = "replacement"
        self.variables["LAB_IP"] = "192.168.1.56"
        report = self.apply()
        self.assertEqual(report["variables"], {"LAB_HOSTNAME": "referenced", "LAB_IP": "value-changed"})
        self.assertEqual(report["newt"], "value-changed")
        self.bao.patch.assert_not_called()
        self.assertTrue(all(call.args[0] == "/read" for call in self.komodo._request.call_args_list))

    def test_dedicated_pair_uses_legacy_alias_for_dependency_check(self):
        self.bao.mount = "services"
        self.fields = {"ID": "fixture-id", "SECRET": "fixture-secret"}
        self.references = {"environment": "NEWT_SECRET=[[LAB_NEWT_SECRET]]"}
        self.assertEqual(self.apply()["newt"], "referenced")
        self.bao.patch.assert_not_called()
        self.references = {}
        self.assertEqual(self.apply()["newt"], "removed")
        self.bao.patch.assert_called_once_with(
            "pangolin/newt/lab", {"ID": None, "SECRET": None}, version=6,
        )
        self.assertEqual(self.apply()["newt"], "absent")
