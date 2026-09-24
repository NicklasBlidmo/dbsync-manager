# Slice 6 field test, 2026-09-24

Reviewer: Claude (Cowork session with Nicklas), commit `6544af3` on
`gis-ubuntu` with the clone `NicklasBlidmo/p3922_test`. Four tests: re-init,
interrupted operation, failed `pre_remove`, real remove with drop. The first
three passed; the fourth exposed one serious bug (finding 1), fixed in `6d0f5cf` and
described by Claude Code in the first section below; the field observations
are kept so the report is complete.

## What passed

| Test | Result |
|---|---|
| Re-init | All seven steps ok in one tick after `stop_child` (SIGTERM at 10:37:18, confirmed next tick). New init from GeoPackage, eight fresh checkouts, base comments at v9, `post_init` hook `list schemas` run automatically with path "after init (blocks on failure)" — the first real run of that path — `health: ok` at 10:47 |
| Interrupted operation | A 30 s window between SIGTERM and the next tick was too short to hit by hand (two attempts landed after the operation had finished; `docker compose logs -f` lagged one tick). Made deterministic with a `pre_remove` hook `sleep 120; echo archived` on a re-init: `docker compose restart manager` during the sleep. After restart the supervisor resumed at `pre_remove_hook` (hook re-run from the start, 120.0 s, attempt 2), then drop, workdir, reset, start, automatic `post_init`. Resume works |
| Failed `pre_remove` | `fail on purpose` (exit 3) as `pre_remove`, remove with drop: operation `failed` at step 3 with "nothing has been dropped. Fix the hook and retry, or cancel (run 11)"; 16 schemas still present; retry and cancel offered; cancel returned the project to `running` on its own |
| Real remove, part 1 | `archive pg_dump` (`pg_dump -Fc -n "${DBSYNC_SCHEMA_PREFIX}_*" ...`, run as `AK Admin` = `ak_admin`) ok in 0.3 s; `drop_schemas`, `delete_workdir`, `mark_removed` ok |

## Findings

### 1. Remove re-created the schemas it had just dropped (bug, found in the field)

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

**Field observations (2026-09-24 12:21).** Log order: `pre_remove hook
'archive pg_dump' ok (0.3s)` → `started db-sync ... pid 1250` →
`IntegrityError: registry_syncstatus ... Key (connection_id)=(3) is not
present` → next tick `project 1 vanished from the registry; stopping its
daemon`. In those 30 s init recreated 14 `p3922_test_*` schemas and
`/data/projects/1`. Cleaned up by hand. **Remove is re-verified on the
clone before the Bönsta takeover**; the clone is kept in Mergin for that,
and adding it again is the first field test of the add wizard (slice 4).

### 2. Confirmation on remove with drop

The project name is required only when the last check saw pending database
changes. That snapshot can be five minutes old, and remove-with-drop is the
most destructive action in the tool, yet easier to confirm than re-init.
Require the name whenever drop is checked, always on re-init.

### 3. Step log overwrites the detail on re-run

`stop_child` shows "no daemon running" although SIGTERM was sent (the log
has `operation N: SIGTERM to db-sync pid ...`): the second attempt, next
tick, overwrote the first attempt's detail. This is what made us suspect a
missed stop in the first place. Keep each attempt's detail.

### 4. Step table shows UTC

`08:37` in the step rows, `10:37` in the header and everywhere else. Same
clock (`TZ`) as the rest of the page; see the first review note, item 6.

### 5. SIGTERM during a running hook

With the supervisor inside a 120 s hook, "stopping after the current tick"
could not complete; Docker killed the container after its 10 s grace
(exit 137). The step machine survived it, but graceful shutdown is
impossible during any long hook (a real `pg_dump` of a large project). On
SIGTERM: terminate the running hook's process group (the timeout path
already does), record the attempt as interrupted, exit cleanly.

### 6. Hooks and operation steps block the whole tick

During the 120 s hook nothing else ran: no other project, no check. Fine
with one project; with ten and a ten-minute `pre_remove` everything
stalls. Run hooks and steps in a worker the tick polls. Not blocking for
Bönsta, but belongs in §6 as a known limitation until done.

### 7. Check runs against a project without checkouts

Immediately after `reset` the check ran and every card showed
`checkout: [Errno 2] No such file or directory ... .mergin/mergin.json`
until init had created the checkouts. Skip connections whose checkout does
not exist while the project is `initializing`.

### 8. Access log drowns `docker compose logs`

HTMX polling gives one line per panel every five seconds for every open
project page. Turn gunicorn's access log off by default, or filter
`/operation/`, `/status` and `/log`.

### 9. Hook `kind` can be changed on a hook a project already uses

Changing a hook's kind leaves the project pointing at a hook of the wrong
kind, silently. Lock the field once the hook is referenced, or clear the
references.

### 10. Host field in the database form

Add the help text "as seen from the manager's container; localhost is the
container itself". `.env.example` and preflight already say it for
`DATABASE_URL`; the admin form does not.

## Server state after the test

`p3922_test`: registry row with `removed_at` set, no connections; schemas
dropped by hand; work dir removed; daemon count 0. Bönsta (`ak-dbsync`,
`p3922_bonsta_*`, 17 schemas) untouched throughout. The clone still exists
in Mergin (`NicklasBlidmo/p3922_test`) for the remove re-test. Hooks in the
registry: `list schemas` (post_init), `fail on purpose` (pre_remove),
`slow archive` (pre_remove), `archive pg_dump` (pre_remove, run as AK
Admin). Database rows: `gis-ubuntu` (ak_dbsync), `AK Admin` (ak_admin).

## Addressed (Claude Code, same day)

| Finding | Change |
|---|---|
| 2 | The project name is required for remove whenever drop is ticked, as well as when the last check saw pending changes or failed; always for re-init |
| 3 | One step-log entry per attempt; a wait records what it did ("SIGTERM sent to pid N") instead of being overwritten |
| 4 | Step times rendered through an `isotime` filter in the page's time zone |
| 5 | SIGTERM to the supervisor terminates the running hook's process group; the step is logged as interrupted and the operation stays `running` at that step for the next supervisor; an interrupted automatic `post_init` leaves the project `initializing` instead of blocking it |
| 6 | Recorded in design §6 as a known limitation, worker listed under v1.1 |
| 7 | No consistency check while a project is `initializing`; retried every tick until it is |
| 8 | gunicorn access log off unless `WEB_ACCESS_LOG=1` (unverified s6 change until built) |
| 9 | A hook's `kind` is read-only in the admin once a project references it |
| 10 | Help text on the database form's host field (migration 0005) |
