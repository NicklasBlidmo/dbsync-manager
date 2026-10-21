"""
Operations: remove and re-init as restart-safe step machines (design §6
"Operations").

`advance()` is the machine: it runs an operation's steps from the recorded
`step` onwards, writes the step name *before* the step runs, appends to the
step log, and stops at the first failure. Every step is idempotent, so a
supervisor that died mid-step simply runs that step again. The steps' side
effects live behind an `io` object (SupervisorIO in core.py), which is what
keeps this module testable without a database or a daemon.

A step returns a detail string when done, `WAIT` when it needs another
tick (stopping a daemon), or raises StepFailed with a reason the operator
can act on.

    remove:  block → stop_child → pre_remove_hook → drop_schemas
             → delete_workdir → mark_removed
    reinit:  block → stop_child → snapshot_pending → pre_remove_hook
             → drop_schemas → delete_workdir → reset
    verify:  verify (read-only; the daemon keeps running, slice 15)

Cancel is offered only before `drop_schemas` has started; after that the
only way forward is retry.
"""

from django.utils import timezone

from registry.models import Operation

REMOVE_STEPS = ["block", "stop_child", "pre_remove_hook", "drop_schemas", "delete_workdir", "mark_removed"]
REINIT_STEPS = ["block", "stop_child", "snapshot_pending", "pre_remove_hook", "drop_schemas", "delete_workdir", "reset"]
VERIFY_STEPS = ["verify"]
FIRST_DESTRUCTIVE_STEP = "drop_schemas"

class Wait:
    """The step needs another tick; `detail` says what it did this time."""

    def __init__(self, detail: str = "waiting"):
        self.detail = detail


WAIT = Wait()


class StepFailed(Exception):
    """The step cannot complete; the message says what the operator must do."""


class Interrupted(Exception):
    """The supervisor is shutting down mid-step; the step is re-run by the next supervisor."""


def steps_for(operation) -> list:
    if operation.kind == Operation.Kind.REINIT:
        return REINIT_STEPS
    if operation.kind == Operation.Kind.VERIFY:
        return VERIFY_STEPS
    return REMOVE_STEPS


def destructive_started(operation) -> bool:
    return any(entry.get("name") == FIRST_DESTRUCTIVE_STEP for entry in (operation.steps or []))


def can_cancel(operation) -> bool:
    return operation.status in (Operation.Status.PENDING, Operation.Status.RUNNING, Operation.Status.FAILED) \
        and not destructive_started(operation)


def _now_iso():
    return timezone.now().isoformat(timespec="seconds")


def advance(operation, io, *, save=None) -> str:
    """Run the operation as far as it goes this tick. Returns done | wait | failed | interrupted."""
    save = save or operation.save
    steps = steps_for(operation)
    start = steps.index(operation.step) if operation.step in steps else 0
    operation.status = Operation.Status.RUNNING
    operation.error = ""
    log = list(operation.steps or [])

    for name in steps[start:]:
        operation.step = name
        # One entry per attempt, so a wait or an interruption keeps its own
        # detail instead of being overwritten by the next tick (field
        # finding 2026-09-24: "no daemon running" hid "SIGTERM sent").
        attempts = sum(1 for e in log if e.get("name") == name)
        entry = {"name": name, "started_at": _now_iso(), "finished_at": None, "ok": None, "detail": "", "attempt": attempts + 1}
        log.append(entry)
        operation.steps = log
        save()

        try:
            result = getattr(io, name)(operation)
        except Interrupted as exc:
            entry.update(finished_at=_now_iso(), ok=None, detail=f"interrupted: {exc}; the next supervisor re-runs this step")
            operation.steps = log
            save()
            return "interrupted"
        except StepFailed as exc:
            entry.update(finished_at=_now_iso(), ok=False, detail=str(exc))
            operation.status = Operation.Status.FAILED
            operation.error = str(exc)
            operation.steps = log
            save()
            return "failed"
        except Exception as exc:  # noqa: BLE001 - recorded, operator retries
            entry.update(finished_at=_now_iso(), ok=False, detail=f"unexpected error: {exc}")
            operation.status = Operation.Status.FAILED
            operation.error = f"{name}: unexpected error: {exc}"
            operation.steps = log
            save()
            return "failed"

        if isinstance(result, Wait):
            entry.update(finished_at=_now_iso(), ok=None, detail=f"waiting: {result.detail}")
            operation.steps = log
            save()
            return "wait"

        entry.update(finished_at=_now_iso(), ok=True, detail=str(result or ""))
        operation.steps = log
        save()

    operation.status = Operation.Status.DONE
    operation.finished_at = timezone.now()
    save()
    return "done"


def request_retry(operation):
    """Resume at the failed step on the supervisor's next tick."""
    if operation.status != Operation.Status.FAILED:
        return False
    operation.status = Operation.Status.PENDING
    operation.error = ""
    operation.save(update_fields=["status", "error"])
    return True


def request_cancel(operation):
    if not can_cancel(operation):
        return False
    operation.status = Operation.Status.CANCELLED
    operation.finished_at = timezone.now()
    operation.save(update_fields=["status", "finished_at"])
    return True
