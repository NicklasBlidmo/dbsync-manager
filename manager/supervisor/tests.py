"""
Supervisor unit tests (design §13): config generation, backoff, log
rotation, process handling. No registry database needed.
"""

import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

import yaml
from django.test import SimpleTestCase, override_settings

from registry import crypto
from registry.models import Database, MerginAccount, SyncConnection, SyncProject

from . import processes
from .config_render import ConfigRenderError, WORKING_SUBDIR, render_config, write_config
from .core import BACKOFF_MAX, backoff_delay
from .logs import rotate_if_needed, tail

KEY = crypto.generate_key()


def fixtures(*, kind=MerginAccount.Kind.PASSWORD, mergin_secret="pw", db_password="dbpw"):
    account = MerginAccount(name="svc", url="https://app.merginmaps.com", username="svc", kind=kind)
    account.secret = mergin_secret
    database = Database(name="gis", host="db", port=5432, dbname="ak", user="ak_dbsync")
    database.password = db_password
    project = SyncProject(
        pk=42, mergin_project="ak/bonsta", mergin_account=account, database=database,
        schema_prefix="p3922_test", init_from="gpkg", sleep_time=15,
    )
    conns = [
        SyncConnection(project=project, sync_file="1-punkter.gpkg", modified_schema="p3922_test_s_punkter",
                       base_schema="p3922_test_b_punkter"),
        SyncConnection(project=project, sync_file="d-foto.gpkg", modified_schema="p3922_test_s_foto",
                       base_schema="p3922_test_b_foto", skip_tables=["tmp_notes"]),
    ]
    return project, conns, account, database


@override_settings(DBSYNC_MANAGER_SECRET_KEY=KEY)
class RenderConfigTests(SimpleTestCase):
    def test_renders_dbsync_2_3_layout(self):
        project, conns, account, database = fixtures()
        doc = yaml.safe_load(render_config(project, conns, account, database, "/data/projects/42"))
        self.assertEqual(doc["mergin"], {"url": "https://app.merginmaps.com", "username": "svc", "password": "pw"})
        self.assertEqual(doc["init_from"], "gpkg")
        self.assertEqual(doc["working_dir"], str(Path("/data/projects/42") / WORKING_SUBDIR))
        self.assertEqual(doc["daemon"], {"sleep_time": 15})
        self.assertEqual(len(doc["connections"]), 2)
        first = doc["connections"][0]
        self.assertEqual(first["driver"], "postgres")
        self.assertEqual(first["conn_info"], "host=db port=5432 dbname=ak user=ak_dbsync password=dbpw")
        self.assertEqual(first["modified"], "p3922_test_s_punkter")
        self.assertEqual(first["base"], "p3922_test_b_punkter")
        self.assertEqual(first["mergin_project"], "ak/bonsta")
        self.assertEqual(first["sync_file"], "1-punkter.gpkg")
        self.assertNotIn("skip_tables", first)
        self.assertEqual(doc["connections"][1]["skip_tables"], ["tmp_notes"])
        self.assertNotIn("include_tables", doc["connections"][1])

    def test_no_connections_is_an_error(self):
        project, _, account, database = fixtures()
        with self.assertRaisesRegex(ConfigRenderError, "no connections"):
            render_config(project, [], account, database, "/x")

    def test_token_account_is_rejected(self):
        project, conns, account, database = fixtures(kind=MerginAccount.Kind.TOKEN)
        with self.assertRaisesRegex(ConfigRenderError, "token"):
            render_config(project, conns, account, database, "/x")

    def test_secret_starting_with_at_is_rejected(self):
        # dynaconf would turn "@int 5" into 5 and "@format {env[HOME]}" into a path.
        project, conns, account, database = fixtures(mergin_secret="@format {env[HOME]}")
        with self.assertRaisesRegex(ConfigRenderError, "Mergin password"):
            render_config(project, conns, account, database, "/x")
        project, conns, account, database = fixtures(db_password="@int 5")
        with self.assertRaisesRegex(ConfigRenderError, "database password"):
            render_config(project, conns, account, database, "/x")

    def test_skip_and_include_together_is_an_error(self):
        project, conns, account, database = fixtures()
        conns[1].include_tables = ["a"]
        with self.assertRaisesRegex(ConfigRenderError, "cannot both"):
            render_config(project, conns, account, database, "/x")

    def test_write_config_is_private(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = write_config(Path(tmp) / "proj" / "config.yaml", "a: 1\n")
            self.assertEqual(path.read_text(encoding="utf-8"), "a: 1\n")
            if os.name == "posix":
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)
                self.assertEqual(path.parent.stat().st_mode & 0o777, 0o700)


class BackoffTests(SimpleTestCase):
    def test_schedule(self):
        self.assertEqual([backoff_delay(n) for n in range(0, 7)], [0, 30, 60, 120, 240, 480, 600])
        self.assertEqual(backoff_delay(20), BACKOFF_MAX)


class LogTests(SimpleTestCase):
    def test_rotate_copies_and_truncates_in_place(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "dbsync.log"
            # An O_APPEND writer, as db-sync's FileHandler is.
            with open(path, "ab") as writer:
                writer.write(b"x" * 100)
                writer.flush()
                self.assertFalse(rotate_if_needed(path, max_bytes=200, keep=2))
                self.assertTrue(rotate_if_needed(path, max_bytes=50, keep=2))
                self.assertEqual(path.stat().st_size, 0)
                self.assertEqual((Path(tmp) / "dbsync.log.1").stat().st_size, 100)
                writer.write(b"after\n")
                writer.flush()
            self.assertEqual(path.read_bytes(), b"after\n")
            # Second rotation shifts .1 -> .2, third drops the oldest.
            path.write_bytes(b"y" * 100)
            self.assertTrue(rotate_if_needed(path, 50, keep=2))
            self.assertEqual((Path(tmp) / "dbsync.log.2").read_bytes(), b"x" * 100)
            path.write_bytes(b"z" * 100)
            self.assertTrue(rotate_if_needed(path, 50, keep=2))
            self.assertEqual((Path(tmp) / "dbsync.log.1").read_bytes(), b"z" * 100)
            self.assertEqual((Path(tmp) / "dbsync.log.2").read_bytes(), b"y" * 100)
            self.assertFalse((Path(tmp) / "dbsync.log.3").exists())

    def test_tail(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "f.log"
            path.write_text("".join(f"line {i}\n" for i in range(50)), encoding="utf-8")
            self.assertEqual(tail(path, lines=2), "line 48\nline 49")
            self.assertEqual(tail(Path(tmp) / "missing.log"), "")


@unittest.skipUnless(os.name == "posix", "signals and /proc are POSIX-only")
class ProcessTests(SimpleTestCase):
    def test_spawn_adopt_and_terminate(self):
        with tempfile.TemporaryDirectory() as tmp:
            work = Path(tmp)
            config = work / "config.yaml"
            config.write_text("x", encoding="utf-8")
            pidfile = work / processes.PIDFILE_NAME
            command = [sys.executable, "-c", "import sys,time; time.sleep(30)", str(config)]
            child = processes.spawn(command, work, work / "err.log", pidfile, config)
            try:
                self.assertTrue(child.alive)
                self.assertEqual(processes.read_pidfile(pidfile), (child.pid, config))
                adopted = processes.adopt(pidfile)
                self.assertIsNotNone(adopted)
                self.assertTrue(adopted.adopted)
                self.assertEqual(adopted.pid, child.pid)
                self.assertTrue(adopted.alive)
            finally:
                child.terminate()
            self.assertEqual(processes.wait_for_exit([child], timeout=5), [])
            self.assertFalse(child.alive)
            self.assertIsNotNone(child.exit_code)
            # A dead pid is not adopted, and the stale pidfile is removed.
            self.assertIsNone(processes.adopt(pidfile))
            self.assertFalse(pidfile.exists())

    def test_adopt_rejects_a_pid_running_something_else(self):
        with tempfile.TemporaryDirectory() as tmp:
            pidfile = Path(tmp) / processes.PIDFILE_NAME
            # Our own process is alive but does not run "config.yaml".
            processes.write_pidfile(pidfile, os.getpid(), Path(tmp) / "config.yaml")
            self.assertIsNone(processes.adopt(pidfile))


# --- the pause path through _reconcile (field finding 2026-09-22) ------------
#
# Pause never worked in the first field test: `self._stop = Event()` shadowed
# the `_stop()` method, so _reconcile called the Event. The process tests
# above call child.terminate() directly and could not see it. These go
# through _reconcile with the registry writes stubbed out.

from unittest import mock  # noqa: E402

from .core import STOP_GRACE, Runtime, Supervisor  # noqa: E402

PS = SyncProject.ProcessState


class StubChild:
    def __init__(self):
        self.alive = True
        self.pid = 4242
        self.exit_code = None
        self.terminated = False
        self.killed = False

    def terminate(self):
        self.terminated = True

    def kill(self):
        self.killed = True


def paused_project(pk=7, process_state=PS.RUNNING):
    return SyncProject(
        pk=pk, mergin_project="ak/x", schema_prefix="x",
        desired_state=SyncProject.DesiredState.PAUSED, process_state=process_state,
    )


class ReconcilePauseTests(SimpleTestCase):

    def setUp(self):
        # The check (slice 3) queries the registry; these tests cover the
        # process path only.
        patcher = mock.patch.object(Supervisor, "_maybe_check")
        patcher.start()
        self.addCleanup(patcher.stop)
    def test_stop_is_a_method_not_shadowed_by_an_attribute(self):
        supervisor = Supervisor(tick_seconds=1)
        self.assertTrue(callable(supervisor._stop), "_stop must be the daemon-stopping method")
        self.assertIsNot(supervisor._stop, supervisor._stop_requested)

    @mock.patch("supervisor.core.events")
    @mock.patch.object(Supervisor, "_set_state")
    def test_paused_project_gets_sigterm_then_sigkill_then_stopped(self, set_state, events_mod):
        supervisor = Supervisor(tick_seconds=1)
        project = paused_project()
        child = StubChild()
        rt = Runtime(child=child)
        supervisor.runtimes[project.pk] = rt

        # Tick 1: SIGTERM and process_state stopping.
        supervisor._reconcile(project)
        self.assertTrue(child.terminated)
        self.assertFalse(child.killed)
        self.assertIsNotNone(rt.stopping_since)
        set_state.assert_called_with(project, PS.STOPPING, "")

        # Tick 2, still within the grace period: nothing more.
        child.terminated = False
        supervisor._reconcile(project)
        self.assertFalse(child.terminated)
        self.assertFalse(child.killed)

        # Grace period over and the child still alive: SIGKILL.
        rt.stopping_since -= STOP_GRACE + 1
        supervisor._reconcile(project)
        self.assertTrue(child.killed)

        # The child is gone: stopped, an info event, no crash bookkeeping.
        child.alive = False
        child.exit_code = -15
        supervisor._reconcile(project)
        self.assertIsNone(rt.child)
        self.assertIsNone(rt.stopping_since)
        self.assertEqual(rt.crashes, 0)
        set_state.assert_called_with(project, PS.STOPPED, "")
        events_mod.info.assert_called()
        events_mod.error.assert_not_called()

    @mock.patch("supervisor.core.events")
    @mock.patch.object(Supervisor, "_set_state")
    def test_paused_project_without_child_is_marked_stopped(self, set_state, events_mod):
        supervisor = Supervisor(tick_seconds=1)
        project = paused_project(process_state=PS.CRASHED)
        supervisor._reconcile(project)
        set_state.assert_called_once_with(project, PS.STOPPED, "")


@unittest.skipUnless(os.name == "posix", "signals are POSIX-only")
class ReconcilePauseWithRealChildTests(SimpleTestCase):

    def setUp(self):
        # The check (slice 3) queries the registry; these tests cover the
        # process path only.
        patcher = mock.patch.object(Supervisor, "_maybe_check")
        patcher.start()
        self.addCleanup(patcher.stop)
    @mock.patch("supervisor.core.events")
    @mock.patch.object(Supervisor, "_set_state")
    def test_real_child_is_terminated_and_reaped(self, set_state, events_mod):
        with tempfile.TemporaryDirectory() as tmp:
            work = Path(tmp)
            config = work / "config.yaml"
            config.write_text("x", encoding="utf-8")
            command = [sys.executable, "-c", "import time; time.sleep(30)", str(config)]
            child = processes.spawn(command, work, work / "err.log", work / processes.PIDFILE_NAME, config)
            supervisor = Supervisor(tick_seconds=1)
            project = paused_project(pk=8)
            supervisor.runtimes[project.pk] = Runtime(child=child)
            with override_settings(DBSYNC_DATA_DIR=work):
                supervisor._reconcile(project)          # SIGTERM
                self.assertEqual(processes.wait_for_exit([child], timeout=5), [])
                supervisor._reconcile(project)          # reaped -> stopped
            self.assertIsNone(supervisor.runtimes[project.pk].child)
            set_state.assert_called_with(project, PS.STOPPED, "")
