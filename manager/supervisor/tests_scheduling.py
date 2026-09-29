"""
Scheduled hooks (slice 13): when a hook is due, one run per local day, a
hook that was down at run_at, a running hook not started twice, and the
mail on a transition. Pure; the registry writes are stubbed.
"""

from datetime import datetime, time as dtime, timezone as tz
from unittest import mock
from zoneinfo import ZoneInfo

from django.test import SimpleTestCase, override_settings

from registry.models import Database, Hook

from . import hooks, scheduling
from .core import Supervisor

STOCKHOLM = ZoneInfo("Europe/Stockholm")


def local(day, hour, minute, month=9):
    return datetime(2026, month, day, hour, minute, tzinfo=STOCKHOLM)


class DueTests(SimpleTestCase):
    def test_due_after_run_at_once_per_local_day(self):
        run_at = dtime(2, 15)
        self.assertFalse(scheduling.is_due(run_at, None, local(29, 2, 0)))
        self.assertTrue(scheduling.is_due(run_at, None, local(29, 2, 16)))
        started = local(29, 2, 16).astimezone(tz.utc)             # stored in UTC
        self.assertFalse(scheduling.is_due(run_at, started, local(29, 2, 17)))
        self.assertFalse(scheduling.is_due(run_at, started, local(29, 23, 59)))
        self.assertFalse(scheduling.is_due(run_at, started, local(30, 2, 0)))   # tomorrow, but before run_at
        self.assertTrue(scheduling.is_due(run_at, started, local(30, 2, 15)))

    def test_local_midnight_decides_the_day_not_utc(self):
        # 00:05 local is 22:05 UTC the day before: the run at 00:30 local on
        # the 29th must count for the 29th, not for the 28th.
        run_at = dtime(0, 5)
        started = local(29, 0, 30).astimezone(tz.utc)
        self.assertEqual(started.date().day, 28)
        self.assertFalse(scheduling.is_due(run_at, started, local(29, 12, 0)))
        self.assertTrue(scheduling.is_due(run_at, started, local(30, 0, 6)))

    def test_container_down_at_run_at_runs_at_the_first_tick_after_start(self):
        run_at = dtime(2, 15)
        yesterday = local(28, 2, 16).astimezone(tz.utc)
        self.assertTrue(scheduling.is_due(run_at, yesterday, local(29, 14, 0)))

    def test_no_time_is_never_due(self):
        self.assertFalse(scheduling.is_due(None, None, local(29, 12, 0)))

    def test_transition_mails_once_per_change(self):
        self.assertEqual(scheduling.transition("", False), "failed")
        self.assertEqual(scheduling.transition("ok", False), "failed")
        self.assertIsNone(scheduling.transition("failed", False))
        self.assertEqual(scheduling.transition("failed", True), "recovered")
        self.assertIsNone(scheduling.transition("ok", True))
        self.assertIsNone(scheduling.transition("", True))


class FakeJob:
    def __init__(self, done=False, result=None, run=None):
        self.done = done
        self.result = result
        self.run = run
        self.previous_status = ""
        self.seconds = 0


@override_settings(TIME_ZONE="Europe/Stockholm")
class TickScheduledHookTests(SimpleTestCase):
    def setUp(self):
        self.objects = mock.patch("supervisor.core.Hook.objects").start()
        self.start_job = mock.patch("supervisor.core.hooks.start_job").start()
        self.pending = mock.patch("supervisor.core.hooks.pending_scheduled_request", return_value=None).start()
        self.notify = mock.patch("supervisor.core.alerts.notify_hook").start()
        self.addCleanup(mock.patch.stopall)
        database = Database(pk=1, name="gis", host="postgis", dbname="gis", user="backup")
        self.hook = Hook(pk=5, name="backup", kind="scheduled", command="pg_dump", enabled=True,
                         run_at=dtime(2, 15), database=database, notify_emails=["ops@example.org"], timeout_s=3600)
        self.supervisor = Supervisor(1)

    def updates(self):
        return [c.kwargs for c in self.objects.filter.return_value.update.call_args_list]

    def test_starts_when_due_and_never_twice(self):
        self.start_job.return_value = FakeJob(done=False)
        self.supervisor._tick_scheduled_hook(self.hook, local(29, 2, 0))
        self.start_job.assert_not_called()                       # before run_at
        self.supervisor._tick_scheduled_hook(self.hook, local(29, 2, 16))
        self.start_job.assert_called_once()
        self.assertEqual(self.start_job.call_args.args[1], hooks.run_scheduled)
        self.assertEqual(self.start_job.call_args.args[2], self.hook)
        self.assertEqual(self.hook.last_status, "running")
        self.assertIsNotNone(self.hook.last_started_at)
        self.assertEqual(self.updates()[-1]["last_status"], "running")
        self.assertIn("last_started_at", self.updates()[-1])
        self.supervisor._tick_scheduled_hook(self.hook, local(29, 2, 17))   # still running: not started again
        self.supervisor._tick_scheduled_hook(self.hook, local(29, 12, 0))
        self.start_job.assert_called_once()

    def test_disabled_hook_is_not_started(self):
        self.hook.enabled = False
        self.supervisor._tick_scheduled_hook(self.hook, local(29, 12, 0))
        self.start_job.assert_not_called()

    def test_finish_records_status_and_mails_on_transition_only(self):
        failed = hooks.HookResult(ok=False, exit_code=1, timed_out=False, stdout="", stderr="pg_dump: error", duration_s=2.0)
        ok = hooks.HookResult(ok=True, exit_code=0, timed_out=False, stdout="backup: done", stderr="", duration_s=3.0)
        self.hook.last_started_at = local(29, 2, 16).astimezone(tz.utc)   # today's run: reaping must not start another
        # First run fails: last_status failed, one mail "failed".
        job = FakeJob(done=True, result=failed, run=mock.Mock(pk=1))
        job.previous_status = ""
        self.supervisor.scheduled_jobs[self.hook.pk] = job
        self.supervisor._tick_scheduled_hook(self.hook, local(29, 12, 0))
        self.assertEqual(self.hook.last_status, "failed")
        self.assertEqual(self.updates()[-1]["last_status"], "failed")
        self.notify.assert_called_once_with(self.hook, "failed", failed, job.run)
        # Fails again: no second mail.
        job = FakeJob(done=True, result=failed, run=mock.Mock(pk=2))
        job.previous_status = "failed"
        self.supervisor.scheduled_jobs[self.hook.pk] = job
        self.supervisor._tick_scheduled_hook(self.hook, local(29, 12, 1))
        self.notify.assert_called_once()
        # Works again: one mail "recovered".
        job = FakeJob(done=True, result=ok, run=mock.Mock(pk=3))
        job.previous_status = "failed"
        self.supervisor.scheduled_jobs[self.hook.pk] = job
        self.supervisor._tick_scheduled_hook(self.hook, local(29, 12, 2))
        self.assertEqual(self.notify.call_args.args[1], "recovered")
        self.assertEqual(self.hook.last_status, "ok")
        self.assertNotIn(self.hook.pk, self.supervisor.scheduled_jobs)

    def test_run_now_starts_regardless_of_the_clock_and_does_not_count_as_the_daily_run(self):
        request = mock.Mock(summary={"status": "requested", "requested_by": "op"})
        self.pending.return_value = request
        self.start_job.return_value = FakeJob(done=False)
        self.supervisor._tick_scheduled_hook(self.hook, local(29, 1, 0))   # before run_at
        self.start_job.assert_called_once()
        self.assertIs(self.start_job.call_args.kwargs["run"], request)
        self.assertEqual(self.start_job.call_args.kwargs["requested_by"], "op")
        self.assertIsNone(self.hook.last_started_at)
        self.assertNotIn("last_started_at", self.updates()[-1])
