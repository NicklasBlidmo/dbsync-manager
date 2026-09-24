"""
Remove and re-init from the project page (design §6, §9). Tagged `db`.
"""

from django.contrib.auth.models import Group, User
from django.test import TestCase, tag
from django.urls import reverse
from django.utils import timezone

from registry.models import Database, MerginAccount, Operation, SyncConnection, SyncProject, SyncStatus
from registry.permissions import ensure_groups


@tag("db")
class OperationPageTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        ensure_groups()
        cls.operator = User.objects.create_user("op3", password="pw")
        cls.operator.groups.add(Group.objects.get(name="operator"))
        account = MerginAccount(name="svc3", username="svc")
        account.secret = "pw"
        account.save()
        database = Database(name="gis3", host="db", dbname="ak", user="u")
        database.password = "pw"
        database.save()
        cls.project = SyncProject.objects.create(
            mergin_project="ak/ops", mergin_account=account, database=database, schema_prefix="o",
        )
        conn = SyncConnection.objects.create(project=cls.project, sync_file="1-punkter.gpkg",
                                             modified_schema="o_s_punkter", base_schema="o_b_punkter")
        now = timezone.now()
        SyncStatus.objects.create(project=cls.project, connection=conn, server_version=6, local_version=6, db_version=6,
                                  gpkg_count={"punkter": 5}, base_count={"punkter": 5}, modified_count={"punkter": 7},
                                  checked_at=now, last_ok_check_at=now)

    def setUp(self):
        self.client.login(username="op3", password="pw")

    def test_remove_form_shows_pending_changes_and_requires_the_name(self):
        url = reverse("project_operation", args=[self.project.pk, "remove"])
        page = self.client.get(url)
        self.assertContains(page, "would be lost")
        self.assertContains(page, "1-punkter.gpkg:punkter (2 rows)")
        self.assertContains(page, "Type the project name")
        response = self.client.post(url, {"drop_schemas": "1", "confirm_name": "wrong"})
        self.assertContains(response, "type the project name exactly")
        self.assertFalse(Operation.objects.exists())
        response = self.client.post(url, {"drop_schemas": "1", "confirm_name": "ak/ops"})
        self.assertRedirects(response, reverse("project", args=[self.project.pk]))
        op = Operation.objects.get(project=self.project)
        self.assertEqual((op.kind, op.status, op.params), ("remove", "pending", {"drop_schemas": True}))
        self.assertEqual(op.requested_by, self.operator)
        # While it is open: pause is refused, a second operation is refused, the page shows the panel.
        self.assertEqual(self.client.post(reverse("project_action", args=[self.project.pk, "pause"])).status_code, 409)
        self.assertEqual(self.client.post(url, {"drop_schemas": "1", "confirm_name": "ak/ops"}).status_code, 409)
        page = self.client.get(reverse("project", args=[self.project.pk]))
        self.assertContains(page, "actions disabled while an operation is open")
        self.assertContains(page, "Waiting for the supervisor")

    def test_reinit_records_confirmed_pending_loss_and_init_from(self):
        url = reverse("project_operation", args=[self.project.pk, "reinit"])
        response = self.client.post(url, {"init_from": "db", "confirm_name": "ak/ops"})
        self.assertEqual(response.status_code, 302)
        op = Operation.objects.get(project=self.project)
        self.assertEqual(op.params, {"init_from": "db", "confirm_pending_loss": True})

    def test_retry_and_cancel(self):
        op = Operation.objects.create(
            project=self.project, kind="remove", status="failed", step="pre_remove_hook", error="hook failed",
            steps=[{"name": "block", "ok": True}, {"name": "pre_remove_hook", "ok": False}],
        )
        page = self.client.get(reverse("project", args=[self.project.pk]))
        self.assertContains(page, "Retry step")
        self.assertContains(page, "Cancel")
        self.client.post(reverse("operation_retry", args=[op.pk]))
        op.refresh_from_db()
        self.assertEqual(op.status, "pending")
        op.status = "failed"
        op.steps.append({"name": "drop_schemas", "ok": False})
        op.save()
        self.assertEqual(self.client.post(reverse("operation_cancel", args=[op.pk])).status_code, 409)
        op.steps = op.steps[:2]
        op.save()
        self.client.post(reverse("operation_cancel", args=[op.pk]))
        op.refresh_from_db()
        self.assertEqual(op.status, "cancelled")
