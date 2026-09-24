"""
The tick after an operation finishes (field finding 2026-09-24): a
project that mark_removed just removed must not be started from the stale
object loaded at the beginning of the tick. Tagged `db`.
"""

from unittest import mock

from django.test import TransactionTestCase, tag
from django.utils import timezone

from registry.models import Database, MerginAccount, Operation, SyncConnection, SyncProject

from .core import Runtime, Supervisor


@tag("db")
class TickAfterOperationTests(TransactionTestCase):
    def setUp(self):
        account = MerginAccount(name="svc-tick", username="svc")
        account.secret = "x"
        account.save()
        database = Database(name="tick-db", host="db", dbname="ak", user="u")
        database.password = "pw"
        database.save()
        self.project = SyncProject.objects.create(mergin_project="ak/tick", mergin_account=account,
                                                  database=database, schema_prefix="t")
        SyncConnection.objects.create(project=self.project, sync_file="a.gpkg", modified_schema="t_s_a", base_schema="t_b_a")

    def test_removed_project_is_not_started_in_the_same_tick(self):
        Operation.objects.create(project=self.project, kind="remove", params={"drop_schemas": False})
        sup = Supervisor(1)
        sup._lock_held = True
        with mock.patch("supervisor.core.processes.spawn") as spawn, \
             mock.patch("supervisor.core.write_config"), \
             mock.patch.object(sup.io, "delete_workdir", return_value="skipped in test"):
            sup.tick()
            spawn.assert_not_called()
        self.project.refresh_from_db()
        self.assertIsNotNone(self.project.removed_at)
        self.assertEqual(Operation.objects.get().status, "done")
        # And the next tick, reading the registry afresh, does not start it either.
        with mock.patch("supervisor.core.processes.spawn") as spawn:
            sup.tick()
            spawn.assert_not_called()

    def test_start_re_reads_the_registry(self):
        sup = Supervisor(1)
        stale = SyncProject.objects.get(pk=self.project.pk)   # says desired running, not removed
        SyncProject.objects.filter(pk=self.project.pk).update(desired_state="paused")
        with mock.patch("supervisor.core.processes.spawn") as spawn, mock.patch("supervisor.core.write_config"):
            sup._start(stale, Runtime(), 0.0)
            spawn.assert_not_called()
        SyncProject.objects.filter(pk=self.project.pk).update(desired_state="running", removed_at=timezone.now())
        with mock.patch("supervisor.core.processes.spawn") as spawn, mock.patch("supervisor.core.write_config"):
            sup._start(stale, Runtime(), 0.0)
            spawn.assert_not_called()
