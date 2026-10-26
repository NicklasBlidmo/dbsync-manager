"""
The operation steps against the real test database (design §13). Tagged
`db`; the project's database row points at the test database itself.
"""

import json
import tempfile
from pathlib import Path
from unittest import mock

from django.conf import settings
from django.db import connection
from django.test import TransactionTestCase, override_settings, tag
from django.utils import timezone

from registry.models import Database, Event, MerginAccount, Operation, SyncConnection, SyncProject, SyncStatus

from . import layout, operations
from .core import Supervisor
from .operations import StepFailed
from .tests import StubChild


@tag("db")
class SupervisorIOTests(TransactionTestCase):
    """TransactionTestCase, not TestCase: drop_schemas works on its own
    connection, which cannot see schemas created inside an uncommitted test
    transaction (the first server run of this test failed exactly so)."""

    def tearDown(self):
        with connection.cursor() as cursor:
            cursor.execute("DROP SCHEMA IF EXISTS io_s_a CASCADE; DROP SCHEMA IF EXISTS io_b_a CASCADE")

    def setUp(self):
        cfg = settings.DATABASES["default"]
        self.database = Database(name="test-db", host=cfg["HOST"], port=int(cfg["PORT"] or 5432),
                                 dbname=connection.settings_dict["NAME"], user=cfg["USER"])
        self.database.password = cfg["PASSWORD"]
        self.database.save()
        account = MerginAccount(name="svc-io", username="svc")
        account.secret = "x"
        account.save()
        self.project = SyncProject.objects.create(mergin_project="ak/io", mergin_account=account,
                                                  database=self.database, schema_prefix="io")
        SyncConnection.objects.create(project=self.project, sync_file="a.gpkg", modified_schema="io_s_a", base_schema="io_b_a")
        self.sup = Supervisor(1)
        self.cursor = connection.cursor()

    def test_drop_schemas_is_idempotent_and_reports(self):
        self.cursor.execute("CREATE SCHEMA io_s_a; CREATE TABLE io_s_a.t (id int); CREATE SCHEMA io_b_a")
        connection.commit()   # autocommit is off inside tests; the step uses its own connection
        op = Operation(project=self.project, kind="remove", params={"drop_schemas": True})
        self.assertEqual(self.sup.io.drop_schemas(op), "dropped io_b_a, io_s_a")
        self.cursor.execute("SELECT count(*) FROM pg_namespace WHERE nspname IN ('io_s_a', 'io_b_a')")
        self.assertEqual(self.cursor.fetchone()[0], 0)
        self.assertEqual(self.sup.io.drop_schemas(op), "dropped io_b_a, io_s_a")   # again: no error
        kept = Operation(project=self.project, kind="remove", params={"drop_schemas": False})
        self.assertIn("kept", self.sup.io.drop_schemas(kept))

    def test_mark_removed_and_reset(self):
        conn = self.project.connections.get()
        SyncStatus.objects.create(project=self.project, connection=conn, db_version=3)
        self.sup.io.mark_removed(Operation(project=self.project, kind="remove", params={}))
        self.project.refresh_from_db()
        self.assertIsNotNone(self.project.removed_at)
        self.assertEqual(self.project.desired_state, "removed")
        self.assertEqual(self.project.connections.count(), 0)
        self.assertEqual(SyncStatus.objects.filter(project=self.project).count(), 0)

        other = SyncProject.objects.create(mergin_project="ak/io2", mergin_account=self.project.mergin_account,
                                           database=self.database, schema_prefix="io2", desired_state="paused",
                                           post_init_ok_at=timezone.now(), health="behind", active_conditions={"x": "y"})
        self.sup.io.reset(Operation(project=other, kind="reinit", params={"init_from": "db"}))
        other.refresh_from_db()
        self.assertIsNone(other.post_init_ok_at)
        self.assertEqual((other.health, other.active_conditions, other.desired_state, other.init_from),
                         ("unknown", {}, "running", "db"))

    def test_snapshot_pending_refuses_without_confirmation(self):
        conn = self.project.connections.get()
        SyncStatus.objects.create(project=self.project, connection=conn, base_count={"t": 5}, modified_count={"t": 8})
        op = Operation(project=self.project, kind="reinit", params={})
        with self.assertRaisesRegex(StepFailed, "would be lost"):
            self.sup.io.snapshot_pending(op)
        op.params = {"confirm_pending_loss": True}
        self.assertIn('"a.gpkg:t": 3', self.sup.io.snapshot_pending(op))


@tag("db")
class MigrateCheckoutsDbTests(TransactionTestCase):
    """Slice 16, C10: two projects in the 2.3.0 layout are migrated one at a
    time (`wait_turn` reads the registry), the old tree goes, the marker
    comes, and `projects_needing_migration` empties."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        override = override_settings(DBSYNC_DATA_DIR=Path(self.tmp.name))
        override.enable()
        self.addCleanup(override.disable)
        database = Database(name="mig-db", host="db", dbname="ak", user="u")
        database.password = "pw"
        database.save()
        account = MerginAccount(name="svc-mig", username="svc")
        account.secret = "x"
        account.save()
        self.projects = []
        for name in ("ak/a", "ak/b"):
            project = SyncProject.objects.create(mergin_project=name, mergin_account=account, database=database,
                                                 schema_prefix=name[-1])
            SyncConnection.objects.create(project=project, sync_file="1-x.gpkg",
                                          modified_schema=f"{name[-1]}_s_x", base_schema=f"{name[-1]}_b_x")
            (layout.legacy_checkout_tree(project.work_dir, name) / f"{name[-1]}_s_x").mkdir(parents=True)
            self.projects.append(project)
        self.sup = Supervisor(1)

    @staticmethod
    def downloaded(project):
        conn = project.connections.get()
        (conn.checkout_dir / ".mergin").mkdir(parents=True)
        (conn.checkout_dir / ".mergin" / "mergin.json").write_text(json.dumps({"name": "x", "version": "v3", "files": []}))

    def test_migrate_runs_one_project_at_a_time_and_lifts_the_refusal(self):
        a, b = self.projects
        self.assertEqual([p.pk for p in layout.projects_needing_migration()], [a.pk, b.pk])
        created, skipped = layout.request_migrations(layout.projects_needing_migration(), None)
        self.assertEqual((len(created), skipped), (2, []))
        again, skipped = layout.request_migrations(layout.projects_needing_migration(), None)
        self.assertEqual(again, [])
        self.assertTrue(all("is open" in why for _, why in skipped))
        op_a, op_b = sorted(created, key=lambda o: o.pk)

        def start(project, rt, now):
            rt.child = StubChild()            # db-sync downloads the checkouts from here on

        with mock.patch.object(Supervisor, "_start", side_effect=start) as started:
            self.assertEqual(operations.advance(op_a, self.sup.io), "wait")
            self.assertEqual(op_a.step, "redownload")
            self.assertEqual(operations.advance(op_b, self.sup.io), "wait")
            self.assertEqual(op_b.step, "wait_turn")                  # one project at a time
            self.assertIn(f"operation {op_a.pk} on ak/a is migrating", op_b.steps[-1]["detail"])
            self.assertFalse(layout.legacy_checkout_tree(a.work_dir, "ak/a").exists())
            self.assertTrue(layout.marker_path(a.work_dir).exists())
            self.assertTrue(layout.legacy_checkout_tree(b.work_dir, "ak/b").exists())   # untouched so far
            started.assert_called_once()
            self.downloaded(a)
            self.assertEqual(operations.advance(op_a, self.sup.io), "done")
            self.assertEqual(operations.advance(op_b, self.sup.io), "wait")
            self.assertEqual(op_b.step, "redownload")
            self.downloaded(b)
            self.assertEqual(operations.advance(op_b, self.sup.io), "done")
        self.assertEqual(layout.projects_needing_migration(), [])
        for project in (a, b):
            self.assertTrue(Event.objects.filter(project=project, message__startswith="old-layout checkout").exists())
            self.assertTrue(Event.objects.filter(project=project, message__startswith="checkouts downloaded in").exists())
        self.assertEqual(set(Operation.objects.filter(status="done").values_list("kind", flat=True)), {"migrate"})
