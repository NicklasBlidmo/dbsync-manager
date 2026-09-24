# Slice 6 field test, 2026-09-24

Reviewer: Claude (Cowork session with Nicklas), commit `55905a5` on
`gis-ubuntu` with `p3922_test`.

## Remove re-created the schemas it had just dropped (bug, found in the field)

**Symptom.** Remove with drop: the log showed `pre_remove hook ok`, then
`started db-sync` in the same tick, and init re-created all 14 schemas.

**Cause.** `tick()` loads the project list once, with connections
prefetched, then runs the operations, then reconciles the list. After
`mark_removed` the loaded object was stale: still `desired_state =
running`, `removed_at` None, connections cached. The operation was popped
from `open_operations` because it was done, so `_reconcile` saw a project
that wanted to run and had no open operation, rendered a config from the
cached connections, and started a daemon. The same would have happened
after `reset`, where it is harmless only by luck (a re-init wants a start
anyway, but from a fresh row).

**Fix.**
- `tick()` skips the reconcile of every project an operation touched in
  this tick; the next tick re-reads the registry.
- `_start()` re-reads `desired_state` and `removed_at` from the registry
  right before spawning and refuses if the project is removed or no longer
  wants to run. Independent of the first fix: it also covers a project
  removed or paused in the admin between the tick's read and its start.
- A db-tagged test runs a real `tick()` with a pending remove and asserts
  that nothing is spawned in that tick or the next; another asserts the
  re-read guard on its own.

**Keep in mind.** Anything loaded at the top of a tick is a snapshot. A
step that changes a project's registry row must end that project's
participation in the current tick.
