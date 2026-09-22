# Slice 2 field test, 2026-09-22

Reviewer: Claude (Cowork session with Nicklas), running commit `571842e` on
`gis-ubuntu` with the first project added through the manager.

## Pause did not work (bug, found in the field)

**Symptom.** Setting `desired_state = paused` in the admin had no effect:
no SIGTERM, `process_state` stayed `running`, the daemon kept syncing.
Start had worked.

**Cause.** `Supervisor` had two things called `_stop`: the attribute
`self._stop = threading.Event()` from slice 1 (the signal that ends the
loop) and the method `def _stop(self, project, rt, now)` from slice 2 (stop
a daemon). The instance attribute shadowed the method, so `_reconcile`
called a `threading.Event` object. `tick()` caught the resulting exception
per project and logged it, so the loop survived, which was the intended
behaviour of that guard and also why the failure was quiet.

**Why the tests missed it.** The process tests called `child.terminate()`
directly, never `Supervisor._stop`. No test went through `_reconcile` with a
paused project, so the pause path had never been executed anywhere before
the field test.

**Fix (commit after this note).**
- Attribute renamed to `_stop_requested`; the three uses updated.
- Tests through `_reconcile`: a stub child that must receive SIGTERM and
  `process_state = stopping`, then SIGKILL after the grace period, then
  `stopped` with an info event and no crash bookkeeping; a real spawned
  child (POSIX) terminated and reaped through the same path; and a guard
  that `Supervisor._stop` is callable and distinct from the Event.

**Keep in mind.** A per-project `except Exception` in the loop is right,
but it turns a programming error into a log line. Every new loop step gets
a test that goes through `_reconcile`, not only through the helper it uses.
