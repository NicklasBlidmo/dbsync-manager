"""
The checkout layout migration (slice 16, C10) without a database: the
supervisor refuses to start a project in the old layout, through
`_reconcile`; the operation's steps remove the tree, start the daemon
under the operation and wait for db-sync's download. `wait_turn` queries
the registry and is covered in tests_db_operations.
"""

import json
import tempfile
from pathlib import Path
from unittest import mock

from django.test import SimpleTestCase, override_settings

from registry.models import Database, MerginAccount, Operation, SyncConnection

from . import layout, operations
from .core import PS, Runtime, Supervisor
from .tests import StubChild, running_project


def legacy_tree(data_dir, project, *schemas):
    for schema in schemas:
        (layout.legacy_checkout_tree(project.work_dir, project.mergin_project) / schema).mkdir(parents=True, exist_ok=True)


def downloaded(connection):
    meta = connection.checkout_dir / ".mergin"
    meta.mkdir(parents=True, exist_ok=True)
    (meta / "mergin.json").write_text(json.dumps({"name": "y", "version": "v5", "files": []}), encoding="utf-8")
    (connection.checkout_dir / connection.sync_file).write_bytes(b"")


class RefuseToStartTests(SimpleTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.data_dir = Path(self.tmp.name)
        for target in (mock.patch.object(Supervisor, "_maybe_check"),
                       mock.patch.object(Supervisor, "_still_wanted", return_value=True),
                       mock.patch("supervisor.core.hooks.pending_requests", return_value=[])):
            target.start()
            self.addCleanup(target.stop)

    @mock.patch("supervisor.core.processes.spawn")
    @mock.patch("supervisor.core.events")
    @mock.patch.object(Supervisor, "_set_state")
    def test_project_with_the_old_tree_is_blocked_until_migrated(self, set_state, events_mod, spawn):
        with override_settings(DBSYNC_DATA_DIR=self.data_dir):
            project = running_project()
            legacy_tree(self.data_dir, project, "y_s_a")
            supervisor = Supervisor(tick_seconds=1)
            supervisor._reconcile(project)
            supervisor._reconcile(project)
            spawn.assert_not_called()
            state, reason = set_state.call_args.args[1:]
            self.assertEqual(state, PS.BLOCKED)
            self.assertIn("Migrate checkouts", reason)
            self.assertIn("migrate_checkouts", reason)
            events_mod.warning.assert_called_once()           # one event, not one per tick
            # The marker ends the refusal: the next tick reaches the config renderer.
            layout.mark_migrated(project.work_dir)
            project.mergin_account = MerginAccount(name="svc", username="svc")
            project.database = Database(name="d", host="h", dbname="x", user="u")
            with mock.patch("supervisor.core.render_config", side_effect=AssertionError("reached the start path")):
                with self.assertRaisesRegex(AssertionError, "reached the start path"):
                    supervisor._reconcile(project)


class MigrateStepsTests(SimpleTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.settings_override = override_settings(DBSYNC_DATA_DIR=Path(self.tmp.name))
        self.settings_override.enable()
        self.addCleanup(self.settings_override.disable)
        self.project = running_project()
        self.conns = [
            SyncConnection(pk=1, project=self.project, sync_file="1-a.gpkg", modified_schema="y_s_a", base_schema="y_b_a"),
            SyncConnection(pk=2, project=self.project, sync_file="2-b.gpkg", modified_schema="y_s_b", base_schema="y_b_b"),
        ]
        self.sup = Supervisor(tick_seconds=1)
        self.io = self.sup.io
        self.op = Operation(pk=31, project=self.project, kind="migrate", params={}, status="running", step="")
        for target in (mock.patch.object(type(self.io), "_connections", return_value=self.conns),
                       mock.patch.object(type(self.io), "_wants_running", return_value=True)):
            target.start()
            self.addCleanup(target.stop)
        self.events = mock.patch("supervisor.core.events").start()
        self.addCleanup(mock.patch.stopall)

    def test_remove_old_checkout_removes_the_tree_and_writes_the_marker(self):
        legacy_tree(Path(self.tmp.name), self.project, "y_s_a", "y_s_b")
        old = layout.legacy_checkout_tree(self.project.work_dir, self.project.mergin_project)
        (old / "y_s_a" / "1-a.gpkg").write_bytes(b"x")
        self.assertTrue(layout.needs_migration(self.project.work_dir, self.project.mergin_project))
        detail = self.io.remove_old_checkout(self.op)
        self.assertFalse(old.exists())
        self.assertTrue(layout.marker_path(self.project.work_dir).exists())
        self.assertIn("removed", detail)
        self.assertFalse(layout.needs_migration(self.project.work_dir, self.project.mergin_project))
        self.events.info.assert_called_once()
        self.assertIn("old-layout checkout", self.events.info.call_args.args[1])
        # Idempotent: the second run finds nothing and keeps the marker.
        self.assertIn("no old-layout checkout", self.io.remove_old_checkout(self.op))

    def test_redownload_starts_the_daemon_waits_then_reports_the_time(self):
        rt = self.sup.runtimes.setdefault(self.project.pk, Runtime())

        def start(project, rt_, now):
            rt_.child = StubChild()

        with mock.patch.object(Supervisor, "_start", side_effect=start) as started:
            result = self.io.redownload(self.op)
            self.assertIsInstance(result, operations.Wait)
            self.assertIn("db-sync started (pid 4242)", result.detail)
            self.assertIn("2 checkout(s)", result.detail)
            started.assert_called_once()
            self.assertTrue(self.op.params["started"])
            self.assertTrue(self.op.params["download_started_at"])
            # Next tick: one of two downloaded.
            downloaded(self.conns[0])
            result = self.io.redownload(self.op)
            self.assertIsInstance(result, operations.Wait)
            self.assertIn("waiting for 1 checkout(s): 2-b.gpkg", result.detail)
            started.assert_called_once()          # not started again while the child lives
            # Both downloaded: done, with an event that says how long it took.
            downloaded(self.conns[1])
            detail = self.io.redownload(self.op)
        self.assertIn("2 checkout(s) present in", detail)
        self.assertIn("y-y_s_a, y-y_s_b", detail)
        self.events.info.assert_called_once()
        self.assertIn("checkouts downloaded in", self.events.info.call_args.args[1])
        self.assertIs(rt.child.alive, True)

    def test_redownload_fails_when_the_daemon_exits_and_retry_starts_it_again(self):
        rt = self.sup.runtimes.setdefault(self.project.pk, Runtime())
        self.op.params = {"started": True, "download_started_at": "2026-10-26T10:00:00+00:00"}
        rt.child = StubChild()
        rt.child.alive = False
        rt.child.exit_code = 1
        with mock.patch.object(Supervisor, "_handle_exit", side_effect=lambda p, r, n: setattr(r, "child", None)) as handled:
            with self.assertRaisesRegex(operations.StepFailed, "exited before the download finished"):
                self.io.redownload(self.op)
            handled.assert_called_once()           # the exit is recorded although _reconcile is skipped
        self.assertFalse(self.op.params["started"])  # so retry starts the daemon again
        with mock.patch.object(Supervisor, "_start", side_effect=lambda p, r, n: setattr(r, "child", StubChild())):
            result = self.io.redownload(self.op)
        self.assertIsInstance(result, operations.Wait)
        self.assertIn("db-sync started", result.detail)

    def test_redownload_times_out_and_leaves_the_daemon_running(self):
        rt = self.sup.runtimes.setdefault(self.project.pk, Runtime())
        rt.child = StubChild()
        self.op.params = {"started": True, "download_started_at": "2026-01-01T00:00:00+00:00"}
        with self.assertRaisesRegex(operations.StepFailed, "has not finished within 30 min"):
            self.io.redownload(self.op)
        self.assertIsNone(self.op.params["download_started_at"])
        self.assertTrue(rt.child.alive)

    def test_redownload_on_a_paused_project_does_not_start_anything(self):
        with mock.patch.object(type(self.io), "_wants_running", return_value=False), \
                mock.patch.object(Supervisor, "_start") as started:
            detail = self.io.redownload(self.op)
        self.assertIn("paused", detail)
        started.assert_not_called()
