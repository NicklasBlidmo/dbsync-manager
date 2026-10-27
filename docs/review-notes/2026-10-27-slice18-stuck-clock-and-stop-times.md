# 2026-10-27: slice 18, the stuck clock restarts with the daemon; stop events say what is known

*Written by Claude (coding session) from the last section of the two-way
test note `2026-10-26e`. Nothing here has run against Docker or a
database: 192 tests pass on the development machine.*

## The stuck rule after a resume

What happened: a project paused for most of an afternoon was resumed,
and seconds later the stuck rule restarted its daemon ("checkout still
at v46 since 13:51 while db-sync is running"). `local_version_changed_at`
measures the checkout's last move; a paused checkout does not move, and
the rule did not care when the daemon had started.

Now `check.evaluate` takes `running_since`, the start time of the
current daemon (the supervisor derives it from `Runtime.last_start_at`
in `_maybe_check`; `None` when the daemon is not running, in which case
the rule is not evaluated anyway). Two changes to the rule:

- the clock runs from the later of the checkout's last move and the
  daemon's start, so a resume, a restart and an adoption after a
  supervisor restart all start it afresh (adoption sets `last_start_at`
  too);
- the rule is evaluated only once the daemon has been up for one check
  interval (`DBSYNC_CHECK_INTERVAL`, 300 s).

The message names the time the clock runs from and the daemon's start.
The one-automatic-restart logic in `_maybe_check` is unchanged: after
that restart the daemon's new start resets the clock, which is the same
wait as before (`DBSYNC_STUCK_AFTER`), now for the same reason on both
sides of the restart.

`db_behind` also reads `local_version_changed_at` (database behind the
checkout for two check intervals). It is about data, not about the
daemon, and a database that was behind before the pause is still behind
after it; left as is.

## Stop times

"stopped cleanly after 30 s" when the daemon exited within a second: the
figure was SIGTERM to the tick that reaped the child. The supervisor
only sees an exit on a tick, so the honest statement is a range.
`Runtime.last_alive_at` is the last tick that saw the child alive
(`_reconcile`, and `SupervisorIO.stop_child` for operations); the
stopped event says

- "stopped cleanly within N s (the exit is seen on the next tick)" when
  no tick after SIGTERM saw the daemon alive, N being SIGTERM to that
  tick (30 s with the default tick);
- "stopped cleanly after N–M s" when it outlived a tick: alive at N,
  gone at M;
- "ignored SIGTERM; killed after N s" as before.

The same `_stopped_event` serves pause, restart, operations and the
migrate step. The shutdown path (`Supervisor.shutdown`) already waits
for the children and reports the real wait; unchanged.

Not done: a short blocking wait after SIGTERM would give an exact figure
in the common case (patch 0002 exits within a second between cycles)
at the price of up to 1.5 s of tick per stopping daemon. The range is
enough for the question the events answer, "did it finish its cycle".

## Verified here

192 tests without a database, among them: `evaluate` with a daemon
started two minutes ago is not stuck, at 20 minutes not yet, at 31
minutes stuck with the start in the message, and a later restart resets
it; `_maybe_check` passes the start time derived from `last_start_at`
and `None` when not running; the pause test now expects "within 0 s";
a daemon alive one tick after SIGTERM gets "after 60–90 s".

## For gis-ubuntu

1. Pause the clone for an hour, resume: no stuck restart, no warning;
   the first check after resume says `catching_up` or `ok`.
2. Pause during a sleep: the event reads "stopped cleanly within 30 s
   (the exit is seen on the next tick)". Pause during a long pull (the
   slice 14 recipe): "stopped cleanly after N–M s" with N ≥ 30.
3. The one real stuck case still works: the design's POC scenario
   (Reader account on a project where the server moves and the daemon
   logs 403 every cycle) restarts once after 30 min of uptime plus
   stillness, then alerts.
