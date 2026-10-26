"""
The JSON API (slice 15, E; tagged `db`): a token is required, a viewer
can read but not verify, an operator's verify creates the operation, and
the shapes match docs/api.md.
"""

import json
import tempfile
from datetime import time as dtime
from pathlib import Path

from django.contrib.auth.models import Group, User
from django.test import TestCase, override_settings, tag
from django.urls import reverse
from django.utils import timezone

from registry.models import ApiToken, Database, MerginAccount, Operation, SyncConnection, SyncProject, SyncStatus
from registry.permissions import ensure_groups
from supervisor import facts


@tag("db")
class ApiTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        ensure_groups()
        cls.viewer = User.objects.create_user("api-viewer", password="pw")
        cls.viewer.groups.add(Group.objects.get(name="viewer"))
        cls.operator = User.objects.create_user("api-operator", password="pw")
        cls.operator.groups.add(Group.objects.get(name="operator"))
        cls.viewer_token = ApiToken.generate()
        ApiToken.objects.create(name="MerginQA", user=cls.viewer, token_hash=ApiToken.hash(cls.viewer_token),
                                prefix=cls.viewer_token[:8])
        cls.operator_token = ApiToken.generate()
        ApiToken.objects.create(name="akdb", user=cls.operator, token_hash=ApiToken.hash(cls.operator_token),
                                prefix=cls.operator_token[:8])
        cls.revoked_token = ApiToken.generate()
        ApiToken.objects.create(name="old", user=cls.operator, token_hash=ApiToken.hash(cls.revoked_token),
                                prefix=cls.revoked_token[:8], revoked=True)
        account = MerginAccount(name="svc", username="svc")
        account.secret = "pw"
        account.save()
        database = Database(name="gis", host="db", dbname="ak", user="u")
        database.password = "pw"
        database.save()
        cls.project = SyncProject.objects.create(
            mergin_project="ak/bonsta", mergin_account=account, database=database, schema_prefix="p3922",
            health=SyncProject.Health.OK, active_conditions={"db_behind:1-punkter.gpkg": "2026-10-21T08:00:00+00:00"},
        )
        conn = SyncConnection.objects.create(
            project=cls.project, sync_file="1-punkter.gpkg", modified_schema="p3922_s_punkter",
            base_schema="p3922_b_punkter", skip_tables=["skipped"],
        )
        SyncStatus.objects.create(
            project=cls.project, connection=conn, server_version=12, local_version=12, db_version=11,
            checked_at="2026-10-21T08:05:00+00:00", direction="two_way", mergin_permission="writer",
            gpkg_count={"punkter": 5, "skipped": 1}, base_count={"punkter": 5}, modified_count={"punkter": 5},
            tables=[{"name": "punkter", "geometry_column": "geom"}, {"name": "skipped", "geometry_column": None}],
            content_diff={"punkter": {"only_base": 0, "only_modified": 0, "changed": 1,
                                      "sample": [{"key": "3", "uuid": None, "kind": "changed", "columns": ["name"]}]}},
        )

    def get(self, url, token=None):
        headers = {"HTTP_AUTHORIZATION": f"Bearer {token}"} if token else {}
        return self.client.get(url, **headers)

    def post(self, url, token=None):
        headers = {"HTTP_AUTHORIZATION": f"Bearer {token}"} if token else {}
        return self.client.post(url, **headers)

    def test_token_required_and_revoked_refused(self):
        response = self.get(reverse("api_projects"))
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response["WWW-Authenticate"], 'Bearer realm="dbsync-manager"')
        self.assertIn("error", response.json())
        self.assertEqual(self.get(reverse("api_projects"), "not-a-token").status_code, 401)
        self.assertEqual(self.get(reverse("api_projects"), self.revoked_token).status_code, 401)
        # A browser session is not enough: no cookies, tokens only.
        self.client.login(username="api-operator", password="pw")
        self.assertEqual(self.client.get(reverse("api_projects")).status_code, 401)

    def test_viewer_reads_the_documented_shapes(self):
        response = self.get(reverse("api_projects"), self.viewer_token)
        self.assertEqual(response.status_code, 200)
        projects = response.json()["projects"]
        self.assertEqual(len(projects), 1)
        state = projects[0]
        for key in ("id", "mergin_project", "mergin_account", "database", "schema_prefix", "desired_state",
                    "process_state", "process_reason", "health", "health_since", "direction", "versions",
                    "last_pull_confirmed_at", "last_attempt_at", "last_check_at", "active_conditions",
                    "count_divergence", "content_divergence", "last_verification", "conflicts", "connections"):
            self.assertIn(key, state, key)
        self.assertEqual(state["conflicts"], {"last_at": None, "count_30d": 0})
        self.assertEqual(state["versions"], {"server": 12, "checkout": 12, "database": 11})
        self.assertEqual(state["direction"], "two_way")
        self.assertEqual(state["count_divergence"], ["db_behind:1-punkter.gpkg"])
        self.assertEqual(state["content_divergence"]["1-punkter.gpkg"]["punkter"]["changed"], 1)
        self.assertIsNone(state["last_verification"])
        connection = state["connections"][0]
        self.assertEqual(connection["sync_file"], "1-punkter.gpkg")
        self.assertEqual(connection["db_version"], 11)

        response = self.get(reverse("api_project", args=[self.project.pk]), self.viewer_token)
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["project"]["id"], self.project.pk)
        tables = body["tables"]
        self.assertEqual(tables[0]["sync_file"], "1-punkter.gpkg")
        layers = {layer["name"]: layer for layer in tables[0]["layers"]}
        self.assertEqual(layers["punkter"]["geometry_column"], "geom")
        self.assertTrue(layers["punkter"]["synced"])
        self.assertEqual(layers["skipped"]["skipped_by"], "skip_tables")
        self.assertFalse(layers["skipped"]["synced"])
        self.assertEqual(layers["punkter"]["modified_schema"], "p3922_s_punkter")
        self.assertEqual(self.get(reverse("api_project", args=[999]), self.viewer_token).status_code, 404)
        self.assertIsNotNone(ApiToken.objects.get(name="MerginQA").last_used_at)

    def test_conflicts_list_and_file(self):
        with tempfile.TemporaryDirectory() as tmp, override_settings(DBSYNC_DATA_DIR=Path(tmp)):
            directory = self.project.work_dir / "conflicts"
            directory.mkdir(parents=True)
            stamp = timezone.localtime().strftime("%Y%m%dT%H%M%S")
            name = f"{stamp}-1-punkter.gpkg.json"
            (directory / name).write_text(json.dumps({"geodiff": [
                {"table": "punkter", "type": "conflict", "fid": "17",
                 "changes": [{"column": 3, "base": "a", "old": "field", "new": "db"}]}]}))
            state = self.get(reverse("api_project", args=[self.project.pk]), self.viewer_token).json()["project"]
            self.assertEqual(state["conflicts"]["count_30d"], 1)
            self.assertTrue(state["conflicts"]["last_at"].startswith(timezone.localdate().isoformat()))
            response = self.get(reverse("api_project_conflicts", args=[self.project.pk]), self.viewer_token)
            self.assertEqual(response.status_code, 200)
            entry = response.json()["conflicts"][0]
            self.assertEqual((entry["name"], entry["connection"], entry["count"]), (name, "1-punkter.gpkg", 1))
            self.assertEqual(entry["tables"]["punkter"]["fids"], ["17"])
            response = self.get(reverse("api_project_conflict_file", args=[self.project.pk, name]), self.viewer_token)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()["geodiff"][0]["changes"][0]["old"], "field")
            self.assertEqual(self.get(reverse("api_project_conflict_file", args=[self.project.pk, "nope.json"]),
                                      self.viewer_token).status_code, 404)
            self.assertEqual(self.get(reverse("api_project_conflicts", args=[self.project.pk])).status_code, 401)

    def test_verify_is_for_operators_and_creates_one_operation(self):
        url = reverse("api_project_verify", args=[self.project.pk])
        self.assertEqual(self.post(url, self.viewer_token).status_code, 403)
        self.assertEqual(self.get(url, self.operator_token).status_code, 405)
        response = self.post(url, self.operator_token)
        self.assertEqual(response.status_code, 202)
        operation = response.json()["operation"]
        self.assertEqual((operation["kind"], operation["status"]), ("verify", "pending"))
        self.assertEqual(operation["requested_by"], "api-operator")
        row = Operation.objects.get(pk=operation["id"])
        self.assertEqual(row.kind, "verify")
        # A second request while the first is open: 409 with the open operation.
        response = self.post(url, self.operator_token)
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["operation"]["id"], row.pk)
        # The operation endpoint reports it, with the result once there is one.
        row.status = Operation.Status.DONE
        row.params = {"result": {"ok": True, "connections": {"1-punkter.gpkg": {"ok": True, "tables": [], "error": ""}}}}
        row.save()
        response = self.get(reverse("api_operation", args=[row.pk]), self.viewer_token)
        self.assertEqual(response.json()["operation"]["result"]["ok"], True)
        self.assertNotIn("result", response.json()["operation"]["params"])
        state = self.get(reverse("api_project", args=[self.project.pk]), self.viewer_token).json()["project"]
        self.assertEqual(state["last_verification"]["ok"], True)
        self.assertEqual(self.get(reverse("api_operation", args=[999]), self.viewer_token).status_code, 404)

    def test_state_matches_facts_directly(self):
        # The page and the API read the same structure.
        self.assertEqual(
            json.loads(json.dumps(facts.state_for(self.project)))["versions"],
            {"server": 12, "checkout": 12, "database": 11},
        )
