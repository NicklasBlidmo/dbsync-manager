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
from registry.models import Database, Event, MerginAccount, SyncConnection, SyncProject

from . import processes
from .config_render import ConfigRenderError, WORKING_SUBDIR, render_config, write_config
from .core import BACKOFF_JITTER, BACKOFF_MAX, backoff_delay
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
    def test_renders_dbsync_2_4_layout(self):
        project, conns, account, database = fixtures()
        doc = yaml.safe_load(render_config(project, conns, account, database, "/data/projects/42"))
        self.assertEqual(doc["mergin"], {"url": "https://app.merginmaps.com", "username": "svc", "password": "pw"})
        self.assertEqual(doc["init_from"], "gpkg")
        self.assertEqual(doc["working_dir"], str(Path("/data/projects/42") / WORKING_SUBDIR))
        # max_failed_retries 1: the supervisor, not db-sync, owns retries (slice 16).
        self.assertEqual(doc["daemon"], {"sleep_time": 15, "max_failed_retries": 1})
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


class DeadChild:
    """A child that has already exited."""

    def __init__(self, exit_code=1, pid=777):
        self.alive = False
        self.pid = pid
        self.exit_code = exit_code

    def terminate(self):
        pass

    def kill(self):
        pass


def running_project(pk=9):
    return SyncProject(
        pk=pk, mergin_project="ak/y", schema_prefix="y",
        desired_state=SyncProject.DesiredState.RUNNING, process_state=PS.RUNNING, active_conditions={},
    )


class ExitClassificationTests(SimpleTestCase):
    def test_classify_exit(self):
        from . import exits

        kind = exits.classify_exit("DbSyncError: The db schemas already exist but 'base' schema is not synchronized "
                                   "with source GPKG. Running `dbsync_deamon.py` with `--force-init` should fix the issue.")
        self.assertEqual((kind.condition, kind.deterministic), ("checkout_inconsistent", True))
        kind = exits.classify_exit("Cannot find or activate postgis extension")
        self.assertEqual((kind.condition, kind.deterministic), ("database_missing_postgis", True))
        kind = exits.classify_exit("mergin.common.LoginError: <urlopen error [Errno -2] Name or service not known>")
        self.assertEqual((kind.condition, kind.deterministic), ("mergin_unreachable", False))
        kind = exits.classify_exit("urllib.error.HTTPError: HTTP Error 503: Service Unavailable")
        self.assertEqual(kind.condition, "mergin_unreachable")
        kind = exits.classify_exit("mergin.common.LoginError: HTTP Error 401: Unauthorized")
        self.assertEqual(kind.condition, "mergin_auth_failed")
        self.assertIsNone(exits.classify_exit("KeyError: 'version'"))
        self.assertIsNone(exits.classify_exit(""))


@override_settings(DBSYNC_CHECK_INTERVAL=300)
class ExitHandlingTests(SimpleTestCase):
    """What _handle_exit does with a classified exit, through _reconcile
    (review note 2026-10-20b, findings 2-4)."""

    def setUp(self):
        for target in ("_maybe_check", "_set_state", "_start", "_track_mergin"):
            setattr(self, target, mock.patch.object(Supervisor, target).start())
        self.conditions = mock.patch("supervisor.core.conditions").start()
        self.events = mock.patch("supervisor.core.events").start()
        self.tail = mock.patch("supervisor.core.tail", return_value="").start()
        mock.patch("supervisor.core.hooks.pending_requests", return_value=[]).start()
        mock.patch("supervisor.core.processes.remove_pidfile").start()
        self.addCleanup(mock.patch.stopall)
        self.supervisor = Supervisor(tick_seconds=1)
        self.project = running_project()
        self.rt = Runtime(child=DeadChild(), last_start_at=time.monotonic())
        self.supervisor.runtimes[self.project.pk] = self.rt

    def test_deterministic_exit_is_not_restarted_and_raises_the_condition(self):
        self.tail.return_value = "DbSyncError: 'base' schema is not synchronized with source GPKG. --force-init"
        self.supervisor._reconcile(self.project)
        self.assertEqual(self._set_state.call_args.args[1], PS.CRASHED)
        self.assertIn("not restarted", self._set_state.call_args.args[2])
        self.assertIn("Re-init", self._set_state.call_args.args[2])
        self.conditions.activate.assert_called_once()
        self.assertEqual(self.conditions.activate.call_args.args[1], "checkout_inconsistent")
        self.assertEqual(self.conditions.activate.call_args.kwargs["level"], Event.Level.ERROR)
        self.assertEqual(self.rt.next_start_at, float("inf"))
        self.assertEqual(self.rt.crashes, 0)
        self._start.assert_not_called()
        self.supervisor._reconcile(self.project)      # next tick: still no restart
        self._start.assert_not_called()
        # Paused, then resumed after a repair by hand: starts at once.
        self.project.desired_state = SyncProject.DesiredState.PAUSED
        self.supervisor._reconcile(self.project)
        self.assertEqual(self.rt.next_start_at, 0.0)
        self.project.desired_state = SyncProject.DesiredState.RUNNING
        self.supervisor._reconcile(self.project)
        self._start.assert_called_once()

    def test_mergin_unreachable_exit_feeds_the_condition_and_restarts_within_a_minute(self):
        self.tail.return_value = ("Traceback ...\nmergin.common.LoginError: <urlopen error [Errno 111] "
                                  "Connection refused>")
        now = time.monotonic()
        self.supervisor._reconcile(self.project)
        self._track_mergin.assert_called_once()
        self.assertIs(self._track_mergin.call_args.args[2], True)
        self.assertIn("Connection refused", self._track_mergin.call_args.args[3])
        self.conditions.activate.assert_not_called()
        self.events.error.assert_not_called()
        self.assertEqual(self.rt.crashes, 0)
        self.assertLessEqual(self.rt.next_start_at - now, 60 + BACKOFF_JITTER + 1)
        self.assertIn("Mergin unreachable", self._set_state.call_args.args[2])

    def test_auth_failure_is_its_own_condition_with_capped_backoff(self):
        self.tail.return_value = "mergin.common.LoginError: HTTP Error 401: Unauthorized"
        self.conditions.activate.return_value = True
        now = time.monotonic()
        for _ in range(6):
            self.rt.child = DeadChild()
            self.supervisor._reconcile(self.project)
        self.assertEqual(self.rt.crashes, 6)
        self.assertLessEqual(self.rt.next_start_at - now, 300 + BACKOFF_JITTER + 1)
        activated = [c.args[1] for c in self.conditions.activate.call_args_list]
        self.assertEqual(set(activated), {"mergin_auth_failed"})
        self._track_mergin.assert_not_called()

    def test_process_crashed_alerts_at_three_and_reminds_after_an_hour(self):
        self.tail.return_value = "KeyError: 'version'"
        for _ in range(3):
            self.rt.child = DeadChild()
            self.supervisor._reconcile(self.project)
        self.conditions.activate.assert_called_once()
        self.assertEqual(self.conditions.activate.call_args.args[1], "process_crashed")
        self.assertIsNotNone(self.rt.crash_alerted_at)
        self.rt.child = DeadChild()
        self.supervisor._reconcile(self.project)          # fourth crash, minutes later: an event, no mail
        self.conditions.alert.assert_not_called()
        self.rt.crash_alerted_at -= 3601
        self.rt.child = DeadChild()
        self.supervisor._reconcile(self.project)          # an hour later: the reminder
        self.conditions.alert.assert_called_once()
        self.assertIn("5 consecutive restarts", self.conditions.alert.call_args.args[2])
        self.assertEqual(self.rt.crash_reminders, 1)
        self.rt.child = DeadChild()
        self.supervisor._reconcile(self.project)          # not again until a day has passed
        self.conditions.alert.assert_called_once()


class ConditionsFollowTests(SimpleTestCase):
    """Health follows the conditions at once, and an activation without
    recipients says so (2026-10-20d, findings 2 and 3)."""

    def setUp(self):
        self.objects = mock.patch("supervisor.conditions.SyncProject.objects").start()
        self.events = mock.patch("supervisor.conditions.events").start()
        self.alerts = mock.patch("supervisor.conditions.alerts").start()
        self.addCleanup(mock.patch.stopall)
        self.project = running_project(pk=12)
        self.project.health = SyncProject.Health.OK

    def updates(self):
        return [c.kwargs for c in self.objects.filter.return_value.update.call_args_list]

    def test_deterministic_condition_sets_failed_now_and_unknown_when_resolved(self):
        from . import conditions

        self.alerts.recipients_for.return_value = ["ops@example.org"]
        conditions.activate(self.project, "checkout_inconsistent", "needs Re-init", level=Event.Level.ERROR)
        self.assertEqual(self.project.health, SyncProject.Health.FAILED)
        self.assertEqual(self.updates()[-1]["health"], SyncProject.Health.FAILED)
        self.alerts.notify.assert_called_once()
        conditions.resolve(self.project, "checkout_inconsistent", "resolved: re-initialised")
        self.assertEqual(self.project.health, SyncProject.Health.UNKNOWN)
        self.assertEqual(self.updates()[-1]["health"], SyncProject.Health.UNKNOWN)
        self.assertEqual(self.alerts.notify.call_count, 2)
        self.assertEqual(self.alerts.notify.call_args.kwargs["resolved"], True)

    def test_other_conditions_leave_health_to_the_check(self):
        from . import conditions

        self.alerts.recipients_for.return_value = ["ops@example.org"]
        conditions.activate(self.project, "process_crashed", "boom", level=Event.Level.ERROR)
        self.assertEqual(self.project.health, SyncProject.Health.OK)
        self.assertNotIn("health", self.updates()[-1])

    def test_activation_without_recipients_writes_an_info_event_instead_of_mailing(self):
        from . import conditions

        self.alerts.recipients_for.return_value = []
        conditions.activate(self.project, "process_crashed", "boom", level=Event.Level.ERROR)
        self.alerts.notify.assert_not_called()
        self.assertIn("alert not mailed", self.events.info.call_args.args[1])
        self.assertIn("process_crashed", self.events.info.call_args.args[1])
        self.events.info.reset_mock()
        conditions.resolve(self.project, "process_crashed", "fine")
        self.alerts.notify.assert_not_called()
        self.assertNotIn("alert not mailed", " ".join(c.args[1] for c in self.events.info.call_args_list))


class ResetResolvesConditionsTests(SimpleTestCase):
    def test_reinit_and_remove_resolve_every_active_condition(self):
        # 2026-10-20d, finding 1: Re-init cleared the set without the resolved event or mail.
        with mock.patch("supervisor.core.conditions") as conditions, \
                mock.patch("supervisor.core.events"), \
                mock.patch("supervisor.core.SyncStatus.objects"), \
                mock.patch("supervisor.core.SyncConnection.objects"), \
                mock.patch("supervisor.core.SyncProject.objects"):
            supervisor = Supervisor(tick_seconds=1)
            project = running_project(pk=13)
            project.active_conditions = {"checkout_inconsistent": "2026-10-20T13:07:53+00:00",
                                         "mergin_unreachable": "2026-10-20T13:12:00+00:00"}
            operation = mock.Mock(project=project, project_id=project.pk, params={}, kind="reinit", pk=5)
            supervisor.io.reset(operation)
            calls = {c.args[1]: c for c in conditions.resolve.call_args_list}
            self.assertEqual(set(calls), {"checkout_inconsistent", "mergin_unreachable"})
            self.assertIn("re-initialised", calls["checkout_inconsistent"].args[2])
            self.assertEqual(calls["checkout_inconsistent"].kwargs["mail"], True)
            self.assertEqual(calls["mergin_unreachable"].kwargs["mail"], False)   # grouped per server, not here
            conditions.resolve.reset_mock()
            project.active_conditions = {"process_crashed": "x"}
            supervisor.io.mark_removed(mock.Mock(project=project, project_id=project.pk, params={"drop_schemas": False}, kind="remove", pk=6))
            self.assertEqual(conditions.resolve.call_args.args[1], "process_crashed")
            self.assertIn("sync removed", conditions.resolve.call_args.args[2])


class ReconcilePauseTests(SimpleTestCase):

    def setUp(self):
        # The check (slice 3) and the hook queue (slice 5) query the
        # registry; these tests cover the process path only.
        for target in (mock.patch.object(Supervisor, "_maybe_check"),
                       mock.patch("supervisor.core.hooks.pending_requests", return_value=[])):
            target.start()
            self.addCleanup(target.stop)
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

        # The child is gone: stopped, the "killed" event (it ignored SIGTERM),
        # no crash bookkeeping.
        child.alive = False
        child.exit_code = -15
        supervisor._reconcile(project)
        self.assertIsNone(rt.child)
        self.assertIsNone(rt.stopping_since)
        self.assertEqual(rt.crashes, 0)
        set_state.assert_called_with(project, PS.STOPPED, "")
        self.assertIn("waiting for the current cycle", events_mod.info.call_args_list[0].args[1])
        events_mod.error.assert_called_once()
        self.assertIn("killed after", events_mod.error.call_args.args[1])

    @mock.patch("supervisor.core.events")
    @mock.patch.object(Supervisor, "_set_state")
    def test_pause_logs_the_same_stop_pair_as_shutdown(self, set_state, events_mod):
        # 2026-10-20d, finding 4: every stop path says "stopping ... waiting"
        # and "stopped cleanly after N s".
        supervisor = Supervisor(tick_seconds=1)
        project = paused_project()
        child = StubChild()
        rt = Runtime(child=child)
        supervisor.runtimes[project.pk] = rt
        supervisor._reconcile(project)
        self.assertIn("stopping db-sync (pid 4242), waiting for the current cycle", events_mod.info.call_args.args[1])
        child.alive = False
        child.exit_code = 0
        supervisor._reconcile(project)
        # The exit is seen on a tick, not when it happened: an upper bound (slice 18).
        self.assertIn("stopped cleanly within 0 s (the exit is seen on the next tick)", events_mod.info.call_args.args[1])
        events_mod.error.assert_not_called()

    @mock.patch("supervisor.core.events")
    @mock.patch.object(Supervisor, "_set_state")
    def test_stop_event_gives_both_bounds_when_the_daemon_outlived_a_tick(self, set_state, events_mod):
        supervisor = Supervisor(tick_seconds=1)
        project = paused_project()
        child = StubChild()
        rt = Runtime(child=child)
        supervisor.runtimes[project.pk] = rt
        supervisor._reconcile(project)                 # SIGTERM
        supervisor._reconcile(project)                 # still alive one tick later
        rt.stopping_since -= 90                        # as if the ticks were 60 and 90 s after SIGTERM
        rt.last_alive_at = rt.stopping_since + 60
        child.alive = False
        child.exit_code = 0
        supervisor._reconcile(project)
        self.assertIn("stopped cleanly after 60\u201390 s", events_mod.info.call_args.args[1])


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
        # The check (slice 3) and the hook queue (slice 5) query the
        # registry; these tests cover the process path only.
        for target in (mock.patch.object(Supervisor, "_maybe_check"),
                       mock.patch("supervisor.core.hooks.pending_requests", return_value=[])):
            target.start()
            self.addCleanup(target.stop)
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


class CheckStartTimeTests(SimpleTestCase):
    """Slice 18: the check learns when the daemon started (ReconcilePauseTests mocks _maybe_check away)."""

    @mock.patch("supervisor.core.logtail.close_confirmed_pulls", return_value=0)
    @mock.patch.object(Supervisor, "_project_db_version", return_value=None)
    @mock.patch.object(Supervisor, "_maybe_post_pull")
    @mock.patch.object(Supervisor, "_track_mergin")
    @mock.patch("supervisor.core.conditions")
    @mock.patch("supervisor.core.check.run_project_check", return_value=("ok", set(), {}))
    def test_check_gets_the_daemon_start_time_for_the_stuck_clock(self, run_check, *_):
        supervisor = Supervisor(tick_seconds=1)
        project = running_project()
        rt = Runtime(child=StubChild())
        supervisor.runtimes[project.pk] = rt
        now = time.monotonic()
        rt.last_start_at = now - 600
        with mock.patch.object(SyncProject, "connections", new=mock.PropertyMock(return_value=mock.Mock(all=lambda: [1]))):
            supervisor._maybe_check(project, rt, now)
        running_since = run_check.call_args.kwargs["running_since"]
        from django.utils import timezone

        self.assertLess(abs((timezone.now() - running_since).total_seconds() - 600), 5)
        # Not running: no start time.
        rt.child = None
        rt.last_check_at = 0.0
        with mock.patch.object(SyncProject, "connections", new=mock.PropertyMock(return_value=mock.Mock(all=lambda: [1]))):
            supervisor._maybe_check(project, rt, now)
        self.assertIsNone(run_check.call_args.kwargs["running_since"])
