"""
Database-backed tests (design §13). Tagged `db`; run them with the compose
profile `test` (a throwaway PostGIS and a non-superuser role):

    docker compose --profile test run --rm test

and skip them locally with `manage.py test --exclude-tag=db`.
"""

from django.conf import settings
from django.db import connection
from django.test import TestCase, tag

from registry.models import Database, MerginAccount, SyncConnection, SyncProject


@tag("db")
class SchemaBootstrapTests(TestCase):
    """Every manager table lives in the registry schema (CLAUDE.md invariant)."""

    MANAGER_PREFIXES = ("django_", "auth_", "registry_")

    def test_registry_schema_holds_all_manager_tables(self):
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT table_schema, table_name FROM information_schema.tables "
                "WHERE table_schema IN (%s, 'public')",
                [settings.REGISTRY_SCHEMA],
            )
            rows = cursor.fetchall()
        in_public = [name for schema, name in rows if schema == "public" and name.startswith(self.MANAGER_PREFIXES)]
        in_registry = [name for schema, name in rows if schema == settings.REGISTRY_SCHEMA]
        self.assertEqual(in_public, [], "manager tables created in public")
        self.assertIn("django_migrations", in_registry)
        self.assertIn("registry_syncproject", in_registry)

    def test_registry_role_is_not_a_superuser(self):
        with connection.cursor() as cursor:
            cursor.execute("SELECT rolsuper FROM pg_roles WHERE rolname = current_user")
            (superuser,) = cursor.fetchone()
        self.assertFalse(superuser, "the test role must not be a superuser; it hides permission bugs")


@tag("db")
class ConstraintTests(TestCase):
    def setUp(self):
        self.account = MerginAccount(name="svc", username="svc")
        self.account.secret = "pw"
        self.account.save()
        self.database = Database(name="gis", host="db", dbname="ak", user="u")
        self.database.password = "pw"
        self.database.save()
        self.project = SyncProject.objects.create(
            mergin_project="ak/a", mergin_account=self.account, database=self.database, schema_prefix="a",
        )

    def test_target_schema_is_unique_per_database(self):
        SyncConnection.objects.create(project=self.project, sync_file="a.gpkg", modified_schema="a_s", base_schema="a_b")
        other = SyncProject.objects.create(
            mergin_project="ak/b", mergin_account=self.account, database=self.database, schema_prefix="b",
        )
        from django.db import IntegrityError, transaction

        with self.assertRaises(IntegrityError), transaction.atomic():
            SyncConnection.objects.create(project=other, sync_file="b.gpkg", modified_schema="a_s", base_schema="b_b")

    def test_one_open_operation_per_project(self):
        from django.db import IntegrityError, transaction

        from registry.models import Operation

        Operation.objects.create(project=self.project, kind="remove", status="running")
        with self.assertRaises(IntegrityError), transaction.atomic():
            Operation.objects.create(project=self.project, kind="reinit", status="pending")
        # A closed one does not block.
        Operation.objects.filter(project=self.project).update(status="done")
        Operation.objects.create(project=self.project, kind="reinit", status="pending")


@tag("db")
class WritersQueryTests(TestCase):
    """db_writers against real catalogue state (field finding 2026-09-22b):
    a table without any grant has relacl NULL, the normal case after init."""

    def setUp(self):
        from django.db import connection

        self.cursor = connection.cursor()
        self.cursor.execute("CREATE SCHEMA writers_test")
        self.cursor.execute("CREATE TABLE writers_test.t (id int)")
        self.addCleanup(lambda: self.cursor.execute("DROP SCHEMA writers_test CASCADE"))
        self.cursor.execute("SELECT current_user")
        self.me = self.cursor.fetchone()[0]

    def writers(self, sync_role):
        from supervisor.check import db_writers

        return db_writers(self.cursor, "writers_test", sync_role)

    def test_table_without_grants_has_no_other_writers(self):
        self.assertEqual(self.writers(self.me), [])

    def test_owner_counts_as_a_writer_when_not_the_sync_role(self):
        self.assertEqual(self.writers("someone_else"), [self.me])

    def test_public_grant_is_reported(self):
        self.cursor.execute("GRANT INSERT ON writers_test.t TO PUBLIC")
        self.assertEqual(self.writers(self.me), ["PUBLIC"])
        self.cursor.execute("REVOKE INSERT ON writers_test.t FROM PUBLIC")
        self.cursor.execute("GRANT SELECT ON writers_test.t TO PUBLIC")
        self.assertEqual(self.writers(self.me), [])
