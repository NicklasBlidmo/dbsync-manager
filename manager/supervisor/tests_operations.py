"""
The operation step machine (design §6 "Operations", §13): restart safety,
failure before anything destructive, retry and cancel. Pure: a fake io
records what would happen.
"""

from unittest import mock

from django.test import SimpleTestCase

from registry.models import Operation

from . import operations
from .operations import WAIT, StepFailed, advance, can_cancel, request_cancel, request_retry


class FakeIO:
    """Every step succeeds unless told otherwise; stop_child waits N times."""

    def __init__(self, wait_stop=0, fail=None):
        self.calls = []
        self.wait_stop = wait_stop
        self.fail = fail or {}

    def _step(self, name, operation):
        self.calls.append(name)
        if name in self.fail:
            raise StepFailed(self.fail[name])
        if name == "stop_child" and self.wait_stop > 0:
            self.wait_stop -= 1
            return WAIT
        return f"{name} done"

    def __getattr__(self, name):
        if name in operations.REMOVE_STEPS or name in operations.REINIT_STEPS:
            return lambda operation: self._step(name, operation)
        raise AttributeError(name)


def make_op(kind="remove", **params):
    op = Operation(pk=11, kind=kind, params=params, status="pending", step="", steps=[])
    return op


class AdvanceTests(SimpleTestCase):
    def setUp(self):
        self.saves = 0

    def save(self):
        self.saves += 1

    def test_remove_runs_every_step_in_order_and_logs_them(self):
        io = FakeIO()
        op = make_op(drop_schemas=True)
        self.assertEqual(advance(op, io, save=self.save), "done")
        self.assertEqual(io.calls, operations.REMOVE_STEPS)
        self.assertEqual(op.status, "done")
        self.assertIsNotNone(op.finished_at)
        self.assertEqual([e["name"] for e in op.steps], operations.REMOVE_STEPS)
        self.assertTrue(all(e["ok"] for e in op.steps))
        self.assertGreater(self.saves, len(operations.REMOVE_STEPS))   # written before and after each step

    def test_stop_child_waits_across_ticks_without_duplicate_entries(self):
        io = FakeIO(wait_stop=2)
        op = make_op()
        self.assertEqual(advance(op, io, save=self.save), "wait")
        self.assertEqual(op.status, "running")
        self.assertEqual(op.step, "stop_child")
        self.assertEqual(advance(op, io, save=self.save), "wait")
        self.assertEqual(advance(op, io, save=self.save), "done")
        self.assertEqual(io.calls.count("block"), 1)            # not re-run after the wait
        self.assertEqual(io.calls.count("stop_child"), 3)
        self.assertEqual([e["name"] for e in op.steps], operations.REMOVE_STEPS)  # one entry per step

    def test_failed_archive_stops_before_anything_destructive(self):
        io = FakeIO(fail={"pre_remove_hook": "hook 'archive' failed (exit code 1)"})
        op = make_op(drop_schemas=True)
        self.assertEqual(advance(op, io, save=self.save), "failed")
        self.assertEqual(op.status, "failed")
        self.assertEqual(op.step, "pre_remove_hook")
        self.assertIn("archive", op.error)
        self.assertNotIn("drop_schemas", io.calls)
        self.assertTrue(can_cancel(op))

    def test_retry_resumes_at_the_failed_step(self):
        io = FakeIO(fail={"pre_remove_hook": "nope"})
        op = make_op(drop_schemas=True)
        advance(op, io, save=self.save)
        io.fail = {}
        with mock.patch.object(Operation, "save"):
            self.assertTrue(request_retry(op))
        self.assertEqual(op.status, "pending")
        self.assertEqual(advance(op, io, save=self.save), "done")
        self.assertEqual(io.calls, ["block", "stop_child", "pre_remove_hook", "pre_remove_hook",
                                    "drop_schemas", "delete_workdir", "mark_removed"])
        attempts = [e["attempt"] for e in op.steps if e["name"] == "pre_remove_hook"]
        self.assertEqual(attempts, [1, 2])

    def test_cancel_is_refused_once_drop_has_started(self):
        io = FakeIO(fail={"drop_schemas": "must be owner of view x"})
        op = make_op(drop_schemas=True)
        advance(op, io, save=self.save)
        self.assertEqual(op.step, "drop_schemas")
        self.assertFalse(can_cancel(op))
        with mock.patch.object(Operation, "save"):
            self.assertFalse(request_cancel(op))
            self.assertEqual(op.status, "failed")
            # Before the destructive step cancel works.
            op2 = make_op()
            op2.status = "pending"
            self.assertTrue(request_cancel(op2))
            self.assertEqual(op2.status, "cancelled")

    def test_reinit_has_its_own_steps_and_snapshot_can_refuse(self):
        io = FakeIO(fail={"snapshot_pending": "pending database changes would be lost"})
        op = make_op(kind="reinit", init_from="gpkg")
        self.assertEqual(advance(op, io, save=self.save), "failed")
        self.assertEqual(io.calls, ["block", "stop_child", "snapshot_pending"])
        io.fail = {}
        op.status = "pending"
        self.assertEqual(advance(op, io, save=self.save), "done")
        self.assertEqual(io.calls[3:], ["snapshot_pending", "pre_remove_hook", "drop_schemas", "delete_workdir", "reset"])

    def test_unexpected_exception_is_recorded_as_failure(self):
        class BrokenIO(FakeIO):
            def delete_workdir(self, operation):
                raise OSError("disk on fire")

        op = make_op()
        self.assertEqual(advance(op, BrokenIO(), save=self.save), "failed")
        self.assertIn("disk on fire", op.error)
        self.assertEqual(op.step, "delete_workdir")
