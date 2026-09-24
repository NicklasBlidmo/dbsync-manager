"""
The operation steps against the real test database (design §13). Tagged
`db`; the project's database row points at the test database itself.
"""

from django.conf import settings
from django.db import connection
from django.test import TransactionTestCase, tag
from django.utils import timezone

from registry.models import Database, MerginAccount, Operation, SyncConnection, SyncProject, SyncStatus

from .core import Supervisor
from .operations import StepFailed


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
