import json
import unittest
import subprocess
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch
from uuid import UUID

from onboardrr.http_json import HttpStatusError
from onboardrr.portabase import catalog_actions, configured_portabase
from onboardrr.remote.portabase_sync import discover, project_hints, sync
from onboardrr.tui import command_args


def container(name, image, *, running=True, networks=("main",), env=(), labels=None):
    return {
        "Id": name, "Name": "/" + name,
        "Config": {"Image": image, "Env": env, "Labels": labels or {}},
        "State": {"Running": running},
        "NetworkSettings": {"Networks": dict.fromkeys(networks, {})},
    }


class PortabaseTest(unittest.TestCase):
    def test_discovers_without_deleting_stopped_or_unreachable_sources(self):
        agent = container("portabase-agent", "portabase/agent:1.26.3")
        postgres = container("sample-db", "registry.example:5000/library/postgres:17",
                             env=["POSTGRES_USER=owner", "POSTGRES_PASSWORD=fixture", "POSTGRES_DB=app"],
                             labels={"com.docker.compose.project": "sample", "com.docker.compose.service": "db"})
        redis = container("cache", "valkey/valkey:8", running=False)
        unavailable = container("unavailable", "mysql:8", networks=("elsewhere",),
                                env=["MYSQL_DATABASE=app", "MYSQL_USER=user", "MYSQL_PASSWORD=fixture"])
        targets, known, skipped = discover([agent, postgres, redis, unavailable], agent)
        self.assertEqual(set(targets), {"sample/db"})
        self.assertEqual(targets["sample/db"]["type"], "postgresql")
        self.assertEqual(known, {"portabase-agent", "sample/db", "cache", "unavailable"})
        self.assertEqual({entry["container"] for entry in skipped}, {"cache", "unavailable"})

    def test_file_apply_and_retirement_preserve_manual_sources_and_ids(self):
        with TemporaryDirectory() as directory:
            folder = Path(directory)
            config = folder / "config.json"
            manual = {"generated_id": "manual", "type": "mysql", "host": "external", "database": "app"}
            config.write_text(json.dumps({"databases": [manual]}))
            edge = json.dumps({"agentId": "00000000-0000-4000-8000-000000000001",
                               "serverUrl": "https://portabase.example", "masterKeyB64": "fixture-edge-key"})
            agent = container("portabase-agent", "portabase/agent:1.26.3", env=["EDGE_KEY=" + edge])
            agent["Mounts"] = [{"Source": str(folder), "Destination": "/config", "Type": "bind"}]
            database = container("app-postgres", "postgres:17",
                                 env=["POSTGRES_USER=owner", "POSTGRES_PASSWORD=fixture", "POSTGRES_DB=app"],
                                 labels={"com.docker.compose.project": "app", "com.docker.compose.service": "db"})
            snapshot = ["agent database", json.dumps([agent, database]), "agent database"]
            with patch("onboardrr.remote.portabase_sync.docker", side_effect=snapshot[:2]):
                preview = sync({"apply": False})
            self.assertEqual(preview["added"], ["app-postgres"])
            self.assertNotIn("fixture", json.dumps(preview))
            self.assertEqual(list(folder.iterdir()), [config])
            with patch("onboardrr.remote.portabase_sync.docker", side_effect=[
                *snapshot, subprocess.CalledProcessError(1, "docker"), "agent",
            ]):
                with self.assertRaisesRegex(ValueError, "previous configuration was restored"):
                    sync({"apply": True})
            self.assertEqual(json.loads(config.read_text())["databases"], [manual])
            with patch("onboardrr.remote.portabase_sync.docker", side_effect=[*snapshot, "agent", "true"]):
                applied = sync({"apply": True})
            database_id, = applied["managed"]
            self.assertEqual(config.stat().st_mode & 0o777, 0o600)
            with patch("onboardrr.remote.portabase_sync.docker", side_effect=snapshot):
                self.assertEqual(sync({"apply": True})["updated"], [])
            stopped = {**database, "State": {"Running": False}}
            with patch("onboardrr.remote.portabase_sync.docker", side_effect=["agent database", json.dumps([agent, stopped])]):
                self.assertFalse(sync({"apply": False})["retired"])
            with patch("onboardrr.remote.portabase_sync.docker", side_effect=[
                "agent", json.dumps([agent]), "agent", "agent", "true",
            ]):
                removed = sync({"apply": True})
            self.assertEqual(removed["retired"], {"app/db": database_id})
            self.assertEqual(json.loads(config.read_text())["databases"], [manual])

    @patch.dict("os.environ", {"PORTABASE_API_KEY": "fixture"}, clear=True)
    def test_public_api_accepts_an_explicit_environment_token(self):
        with patch("onboardrr.portabase.configured_openbao") as bao:
            api = configured_portabase("https://portabase.example")
        self.assertEqual(api.headers["x-api-key"], "fixture")
        bao.assert_not_called()

    @patch.dict("os.environ", {}, clear=True)
    def test_public_api_accepts_only_a_private_controller_token_file(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "portabase.token"
            path.write_text("fixture\n")
            with patch.dict("os.environ", {"ONBOARDRR_PORTABASE_API_TOKEN_FILE": str(path)}):
                path.chmod(0o640)
                with self.assertRaisesRegex(ValueError, "Portabase token must be a private file"):
                    configured_portabase("https://portabase.example")
                path.chmod(0o600)
                self.assertEqual(configured_portabase("https://portabase.example").headers["x-api-key"], "fixture")

    def test_projects_follow_compose_then_existing_application_then_stack(self):
        def identifier(number):
            return str(UUID(int=number))

        stacks = {
            "authentik-postgres": ("authentik", "authentik"),
            "authentik-valkey": ("authentik", "authentik"),
            "komodo-mongo": ("komodo", "komodo"),
            "komodo-postgres": ("komodo", "komodo"),
            "plane-valkey": ("projects", "track"),
            "jobs-postgres": ("", "track"),
            "missing-mysql": ("unknown", "unknown"),
            "ambiguous-postgres": ("duplicates", "unknown"),
            "existing-mysql": ("projects", "track"),
        }
        sources = {name: identifier(index) for index, name in enumerate(stacks, 1)}
        containers = [
            container(name, "postgres", running=name != "komodo-mongo",
                      labels={"docker.stack": stack, "com.docker.compose.project": compose})
            for name, (stack, compose) in stacks.items()
        ]
        hints = project_hints({"databases": [
            {"generated_id": source, "host": name} for name, source in sources.items()
        ]}, containers)
        names = {identifier(10): "auth", identifier(11): "monitoring", identifier(12): "projects",
                 identifier(13): "track", identifier(14): "duplicates", identifier(15): "Duplicates"}
        linked = {"authentik-postgres": identifier(10), "komodo-mongo": identifier(11),
                  "existing-mysql": identifier(13)}
        agent, organization = identifier(16), identifier(17)
        rows = [{"id": identifier(index), "agentDatabaseId": source, "agentId": agent,
                 "projectId": linked.get(name)} for index, (name, source) in enumerate(sources.items(), 20)]
        api = Mock()
        api.rows.side_effect = {"/databases": rows, "/organizations": [{"id": organization}],
                                f"/organizations/{organization}/projects": [
                                    {"id": project, "name": name} for project, name in names.items()
                                ]}.__getitem__
        report = {"managed": [source for name, source in sources.items() if name != "jobs-postgres"],
                  "retired": {}, "agent_id": agent, "project_hints": hints}
        preview = catalog_actions(api, report, project=None, purge=False, apply=False)
        api.request.assert_not_called()
        self.assertEqual(preview["projects_unmatched"], ["ambiguous-postgres", "missing-mysql"])
        self.assertEqual({item["source"]: item["project"] for item in preview["project_assignments"]}, {
            "authentik-valkey": "auth", "komodo-postgres": "monitoring", "plane-valkey": "track",
            "jobs-postgres": "track",
        })
        result = catalog_actions(api, report, project=None, purge=False, apply=True)
        self.assertEqual(result["projects_linked"], 4)
        self.assertEqual(api.request.call_count, 4)
        api.request.assert_any_call("PATCH", f"/databases/{identifier(24)}", {"projectId": identifier(13)})
        self.assertNotIn(f"/databases/{identifier(28)}", [call.args[1] for call in api.request.call_args_list])

    @patch("onboardrr.portabase.sleep")
    def test_apply_waits_for_new_source_to_register(self, sleep_mock):
        api = Mock()
        agent, identifier, source, project = (str(UUID(int=number)) for number in range(1, 5))
        row = {"id": source, "agentDatabaseId": identifier, "agentId": agent}
        report = {"managed": [identifier], "retired": {}, "agent_id": agent, "added": ["app-postgres"]}
        api.rows.side_effect = [[], [row]]
        result = catalog_actions(api, report, project=project, purge=False, apply=True)
        self.assertEqual(result["projects_linked"], 1)
        self.assertEqual(result["awaiting_agent_report"], 0)
        sleep_mock.assert_called_once()
        api.request.assert_called_once_with("PATCH", f"/databases/{source}", {"projectId": project})

    def project_policy_fixture(self):
        agent, organization, reference, target, source, database, storage, notification = (
            str(UUID(int=number)) for number in range(1, 9)
        )
        org = f"/organizations/{organization}"
        target_path = f"/projects/{target}"
        report = {
            "agent_id": agent, "managed": [source], "retired": {},
            "project_hints": {source: {"container": "service-db", "application": "service",
                                      "compose": "new-stack", "stack": "medias"}},
        }
        database_row = {"id": database, "agentDatabaseId": source, "agentId": agent, "projectId": None}
        policy_row = {"backupPolicy": "0 0 * * *", "retentionPolicy": {
            "type": "gfs", "count": 7, "days": 30, "gfsHourly": 24, "gfsDaily": 7,
            "gfsWeekly": 4, "gfsMonthly": 12, "gfsYearly": 3,
        }}
        storage_policy = {"storageChannelId": storage, "enabled": True}
        alert_policy = {"notificationChannelId": notification, "enabled": True, "eventKinds": ["error_backup"]}
        rows = {"/databases": [database_row], "/organizations": [{"id": organization}],
                org + "/agents": [{"id": agent}], org + "/projects": [{"id": reference, "name": "medias"}],
                org + "/storage-channels": [{"id": storage}], org + "/notification-channels": [{"id": notification}]}
        for path in (f"/projects/{reference}", target_path):
            rows[path + "/storage-policy"] = [storage_policy]
            rows[path + "/alert-policy"] = [alert_policy]
        api = Mock()
        api.rows.side_effect = rows.__getitem__
        api.resource.side_effect = lambda method, path, data=None: (
            {"id": target, "name": "new-stack", "organizationId": organization} if method == "POST" else policy_row
        )
        return api, rows, report, org, target_path, f"/databases/{database}", target

    def test_creates_and_verifies_project_policies_before_linking(self):
        api, rows, report, org, target_path, database_path, target = self.project_policy_fixture()
        preview = catalog_actions(api, report, project=None, purge=False, apply=False, policy_project="medias")
        self.assertEqual(preview["projects_to_create"], ["new-stack"])
        self.assertEqual(preview["project_assignments"], [{"source": "service-db", "project": "new-stack"}])
        api.request.assert_not_called()
        self.assertTrue(all(call.args[0] == "GET" for call in api.resource.call_args_list))

        api.reset_mock()
        result = catalog_actions(api, report, project=None, purge=False, apply=True, policy_project="medias")
        self.assertEqual(result["projects_created"], ["new-stack"])
        self.assertEqual(result["projects_linked"], 1)
        api.resource.assert_any_call("POST", org + "/projects", {"name": "new-stack"})
        policy_writes = [call.args[:2] for call in api.request.call_args_list]
        self.assertEqual(policy_writes, [("PUT", target_path + f"/{name}-policy")
                                        for name in ("storage", "alert", "backup", "retention")]
                         + [("PATCH", database_path)])
        self.assertEqual(api.request.call_args.args[2], {"projectId": target})
        trace = [call.args[:2] for call in api.mock_calls if call[0] in {"resource", "request"}]
        self.assertLess(trace.index(("GET", target_path)), trace.index(("PATCH", database_path)))

        rows[org + "/projects"].append({"id": target, "name": "new-stack"})
        rows["/databases"][0]["projectId"] = target
        api.reset_mock()
        repeat = catalog_actions(api, report, project=None, purge=False, apply=True, policy_project="medias")
        self.assertFalse(repeat["projects_created"] or repeat["projects_linked"])
        api.resource.assert_not_called()
        api.request.assert_not_called()

    def test_project_policy_preflight_rejects_unavailable_storage(self):
        api, rows, report, org, _, _, _ = self.project_policy_fixture()
        rows[org + "/storage-channels"] = []
        with self.assertRaisesRegex(ValueError, "storage channels available"):
            catalog_actions(api, report, project=None, purge=False, apply=True, policy_project="medias")
        self.assertTrue(all(call.args[0] == "GET" for call in api.resource.call_args_list))
        api.request.assert_not_called()

    def test_failed_project_policy_archives_only_new_empty_project(self):
        api, _, report, _, target_path, _, _ = self.project_policy_fixture()
        api.request.side_effect = [None, HttpStatusError("Portabase", "save alerts", 422), None]
        with self.assertRaisesRegex(ValueError, "Could not configure backup policies"):
            catalog_actions(api, report, project=None, purge=False, apply=True, policy_project="medias")
        api.request.assert_called_with("DELETE", target_path)
        self.assertFalse(any(call.args[0] == "PATCH" for call in api.request.call_args_list))

    def test_api_cleanup_respects_scope_opt_in_and_active_jobs(self):
        api = Mock()
        agent = "00000000-0000-4000-8000-000000000001"
        identifier = "00000000-0000-4000-8000-000000000002"
        source = "00000000-0000-4000-8000-000000000003"
        backup = "00000000-0000-4000-8000-000000000004"
        report = {"agent_id": agent, "managed": [], "retired": {"app/db": identifier}}
        owned = {"id": source, "agentDatabaseId": identifier, "agentId": agent, "projectId": "project"}
        api.rows.side_effect = [[owned]]
        catalog_actions(api, report, project=None, purge=False, apply=True)
        api.request.assert_not_called()
        api.rows.side_effect = [[{**owned, "agentId": "another-agent"}]]
        self.assertEqual(catalog_actions(api, report, project=None, purge=True, apply=True)["backups_deleted"], 0)
        api.request.assert_not_called()
        api.rows.side_effect = [[owned], [{"id": backup, "status": "ongoing"}]]
        self.assertEqual(catalog_actions(api, report, project=None, purge=True, apply=True)["backup_jobs_active"], ["app/db"])
        api.request.assert_not_called()
        api.rows.side_effect = [[owned], [{"id": backup, "status": "success"}]]
        self.assertEqual(catalog_actions(api, report, project=None, purge=True, apply=True)["backups_deleted"], 1)
        api.request.assert_called_once_with("DELETE", f"/databases/{source}/backup/{backup}")

    def test_tui_action_is_a_preview_until_explicitly_applied(self):
        host = {"name": "zero", "profile": "server"}
        options = command_args("portabase-sync", host, "--purge-backups", public_key="id.pub")
        self.assertEqual(options, ["portabase-sync", "zero", "--public-key", "id.pub", "--purge-backups"])
        self.assertIn("--apply", command_args("portabase-sync", host, "", public_key="id.pub", apply=True))
