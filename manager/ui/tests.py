"""
UI tests (tagged `db`: they need users, groups and rows). Run with the
compose profile `test`.
"""

from django.contrib.auth.models import Group, User
from django.test import TestCase, tag
from django.urls import reverse

from registry.models import Database, Event, MerginAccount, SyncConnection, SyncProject, SyncStatus
from registry.permissions import ensure_groups


@tag("db")
class PagesTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        ensure_groups()
        cls.viewer = User.objects.create_user("viewer", password="pw")
        cls.viewer.groups.add(Group.objects.get(name="viewer"))
        cls.operator = User.objects.create_user("operator", password="pw")
        cls.operator.groups.add(Group.objects.get(name="operator"))
        account = MerginAccount(name="svc", username="svc")
        account.secret = "pw"
        account.save()
        database = Database(name="gis", host="db", dbname="ak", user="u")
        database.password = "pw"
        database.save()
        cls.project = SyncProject.objects.create(
            mergin_project="ak/bonsta", mergin_account=account, database=database, schema_prefix="p3922_test",
            health=SyncProject.Health.BEHIND, active_conditions={"db_behind:1-punkter.gpkg": "2026-09-22T12:00:00"},
        )
        conn = SyncConnection.objects.create(
            project=cls.project, sync_file="1-punkter.gpkg", modified_schema="p3922_test_s_punkter",
            base_schema="p3922_test_b_punkter",
        )
        SyncStatus.objects.create(
            project=cls.project, connection=conn, server_version=11, local_version=11, db_version=10,
            gpkg_count={"punkter": 5}, base_count={"punkter": 5}, modified_count={"punkter": 6},
            mergin_permission="reader", direction="one_way",
        )

    def test_anonymous_is_sent_to_login(self):
        response = self.client.get(reverse("home"))
        self.assertRedirects(response, f"{reverse('login')}?next=/")

    def test_home_shows_three_badges_and_versions(self):
        self.client.login(username="viewer", password="pw")
        response = self.client.get(reverse("home"))
        self.assertContains(response, "ak/bonsta")
        self.assertContains(response, "1 alert")
        for text in ("running", "stopped", "behind", "v11", "v10"):
            self.assertContains(response, text)
        self.assertNotContains(response, "Pause")

    def test_home_filter(self):
        self.client.login(username="viewer", password="pw")
        response = self.client.get(reverse("projects_table"), {"health": "ok"})
        self.assertContains(response, "No projects match")

    def test_project_page_shows_interrupted_pull_and_events(self):
        self.client.login(username="operator", password="pw")
        response = self.client.get(reverse("project", args=[self.project.pk]))
        self.assertContains(response, "database behind checkout")
        self.assertContains(response, "interrupted pull, not user edits")
        self.assertContains(response, "db_behind (1-punkter.gpkg)")
        self.assertContains(response, "Pause")
        self.assertContains(response, "(no log yet)")

    def test_viewer_cannot_pause(self):
        self.client.login(username="viewer", password="pw")
        response = self.client.post(reverse("project_action", args=[self.project.pk, "pause"]))
        self.assertEqual(response.status_code, 403)
        self.project.refresh_from_db()
        self.assertEqual(self.project.desired_state, "running")

    def test_operator_pause_writes_desired_state_and_audit_event(self):
        self.client.login(username="operator", password="pw")
        response = self.client.post(reverse("project_action", args=[self.project.pk, "pause"]))
        self.assertRedirects(response, reverse("project", args=[self.project.pk]))
        self.project.refresh_from_db()
        self.assertEqual(self.project.desired_state, "paused")
        event = Event.objects.get(project=self.project, source="user")
        self.assertEqual(event.user, self.operator)
        self.assertIn("paused", event.message)
        # Pausing again is a no-op without a second event.
        self.client.post(reverse("project_action", args=[self.project.pk, "pause"]))
        self.assertEqual(Event.objects.filter(project=self.project, source="user").count(), 1)

    def test_get_on_action_is_refused(self):
        self.client.login(username="operator", password="pw")
        response = self.client.get(reverse("project_action", args=[self.project.pk, "pause"]))
        self.assertEqual(response.status_code, 405)
