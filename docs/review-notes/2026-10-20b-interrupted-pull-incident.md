# 2026-10-20b: a container restart left a project in a `--force-init` crash loop

*Written by Cowork-Claude from the incident on the production server,
2026-10-20, four projects in sync, field work active on one of them. The
project was repaired the same evening. This note records the timeline,
what the manager did wrong, and the fixes for the next slice. The
`mergin_unreachable` test that was being run at the time is a side story;
it surfaced a second finding.*

## Timeline (local time, from `dbsync.log` and the `Event` table)

- 13:07:12–13:07:13 The project's daemon is in a normal cycle: two of
  sixteen connections have pulled `1-polygoner.gpkg` v7358 → v7359.
  The log stops mid-cycle.
- 13:07:21 `started db-sync (pid 96)`: a **new container**. The operator
  had run `docker compose up -d --build` after a `git pull`. Docker sent
  SIGTERM to the old container and SIGKILL 10 s later; the daemon died
  between two connection pulls.
- 13:07:22 The new daemon finds no usable checkout for the interrupted
  connection and does a full `start download` (48 files, v7358). The
  `base` schema's comment also says v7358, but its content differs from
  the freshly downloaded GeoPackage:
  `The db schemas already exist but 'base' schema is not synchronized
  with source GPKG. Running dbsync_deamon.py with --force-init should
  fix the issue.` Exit 1.
- 13:07:53 onwards: crash loop with the usual backoff, 30 s doubling to
  the 600 s cap. 26 restarts by 17:40.
- 13:11:12 Another new container (`.env` edit + `up -d`). Same loop.
- 13:12 The shared Mergin account's URL is changed to `https://mergin.invalid`
  to test `mergin_unreachable`. All daemons on that account exit with
  `mergin.common.LoginError … urlopen error`.
- 13:15 One `process_crashed` mail for the project, with the LoginError
  traceback. No `mergin_unreachable` condition, no recovery mail later.
  URL restored; the other projects recover, this one keeps looping.
- 17:40 Repair: `pg_dump -n <project schema>` of the installation's own
  schema, **Re-init** from the web UI (16 sync schemas dropped and
  rebuilt from Mergin), the installation's own views and grants
  re-applied, validation views green. Health `ok`.

Nothing was lost: the sync schemas are a copy of Mergin, and the
installation's own tables outside them survive a Re-init.

## Findings

### 1. Stopping the container kills daemons wherever they are

db-sync has no SIGTERM handling: the default disposition ends the process
at once, in the middle of a pull if that is where it is. A pull that
dies between "GeoPackage updated" and "base schema updated" leaves the
checkout inconsistent, and db-sync's only answer is `--force-init`,
which the manager runs as Re-init. The manager's `STOP_GRACE = 60` does
not help here: Docker's `stop_grace_period` defaults to 10 s and SIGKILLs
the whole container, supervisor included.

Every path that stops a daemon has this exposure: container stop or
recreate, Pause, Remove, Re-init, the stuck rule's one-off restart, and
`restart_requested`. Only container stop cannot wait today, but none of
them waits for a cycle boundary.

Fix, in three parts:

- **Cooperative stop in the daemon.** We already patch `dbsync.py`
  (`patches/`). Add a SIGTERM handler that sets a flag; the main loop
  checks it before each connection's pull/push and in the sleep between
  cycles, and exits 0 when set. A pull or push in progress always
  completes. Offer the same change upstream (issue + PR against
  `lutraconsulting/mergin-db-sync`; the maintainers accepted #187, and
  this one is smaller).
- **Grace periods that match.** `STOP_GRACE` becomes "one full cycle plus
  margin": 120 s. `docker-compose.yml` gets `stop_grace_period: 150s` on
  the manager service so Docker waits for the supervisor, which waits for
  the daemons. `setup.sh` and the README say why the stop takes up to
  two minutes.
- **Supervisor shutdown order.** On SIGTERM the supervisor signals all
  daemons at once (it does), logs one `info` event per project
  ("stopping db-sync, waiting for the current cycle"), and after they
  exit logs "stopped cleanly" or "killed after N s" so the next incident
  is visible in Events.

### 2. A crashed checkout should be a condition, not an endless crash loop

The daemon's exit with the `--force-init` message is deterministic: it
will fail the same way every restart. The supervisor should recognise it
in the tail (`not synchronized with source GPKG`), stop restarting, and
raise a condition `checkout_inconsistent` with `error` level, health
`failed`, one mail saying "needs Re-init from the project page", and the
Re-init button highlighted on the project page. The same for the other
deterministic exits db-sync has (`Modified and base schemas already
exist` with a missing checkout, missing extension). Backoff is for
crashes that might go away.

### 3. `process_crashed` fires for Mergin connectivity failures

A daemon that exits with `LoginError`, `URLError`, a timeout, or a 5xx
from the Mergin API is not a crashed process in the sense of finding 2;
it is `mergin_unreachable` seen from the daemon side. Map it: when the
exit tail matches those, feed `_track_mergin` instead of counting
towards `process_crashed`, keep the restart backoff capped at 60 s (the
server coming back should be noticed quickly), and let the existing
grouped mail after `DBSYNC_MERGIN_DOWN_ALERT_AFTER` do the alerting.
401/403 stay a configuration error: condition `mergin_auth_failed`, one
mail, no backoff escalation beyond 300 s.

### 4. `process_crashed` goes silent after the first mail

One mail at the third crash, then nothing for as long as the loop runs
(here: four hours, 26 restarts). Conditions dedup per transition, which
is right for conditions that flap, but a crash loop that persists should
remind: a second mail after one hour, then daily, each with the restart
count and the latest tail. Finding 2 removes most of these loops, but
not all.

### 5. Events are hard to reach after the fact

The project page shows the last 50 events and the container logs are
gone after a recreate; the timeline above needed a Django shell query.
Add a date/time filter and paging to the Events table, and a management
command `events --project <id> --since <ts> [--level]` for the terminal.

### 6. Pause all / Resume all

The interim rule below asks the operator to pause every project before a
deploy and resume after. Two buttons on the home page, operator only,
with a confirmation listing the projects, make that one click each; with
finding 1 in place they become the graceful version of "stop everything".
Resume all skips projects that were already paused before Pause all was
pressed (store the set in the operation).

### 7. Minor: NOTICE lines from the installation's own triggers

`GEODIFF: NOTICE: … validation could not run (relation … does not
exist)` is a PostgreSQL NOTICE raised by a trigger that the installation
put on a synced `_s_` table, which geodiff passes through as a log line
and the manager records as an `info` event. That classification is
right: such triggers exist to be heard. The message itself revealed
that the trigger cannot see its lookup table under db-sync's default
`search_path` (an installation-side fix: `SET search_path` on the
function). Worth one line in `docs/hooks.md` or wherever installation
triggers are discussed: a trigger on a sync table runs in db-sync's
session and must schema-qualify everything it touches. Nothing to
change in the manager.

## Applied

Findings 1–6 are slice 14, review note `2026-10-20c-slice14-graceful-stop.md`
(same day). Finding 7 is a paragraph in the README's hooks section. The
interim rule below is superseded by the graceful stop once that image is
deployed; the repair procedure is in `docs/backup.md`.

## Interim rule for operators (until finding 1 ships)

Before `docker compose up -d`, `restart`, `stop` or `down` on an
installation with active projects: pause every project, wait until each
shows `paused` (the daemon then exits between cycles), deploy, resume.
Or deploy outside field hours. Goes into `README.md` (Operations) and
`docs/backup.md`'s restore section now; replaced by "stops are graceful
since vX" later.

## Repair procedure, for the docs

For an installation that keeps its own tables and views next to the sync
schemas (as `docs/backup.md` describes): `pg_dump -Fc -n <own schema>`
first; Re-init from the project page (choose "GeoPackage in Mergin",
confirm there are no pending database changes); re-apply the
installation's own views and grants; verify. The project's own tables
outside the sync schemas are untouched; views that reference the sync
schemas are dropped by the cascade and must be recreated.

## Test

- Unit: SIGTERM during a connection pull → the pull completes, the loop
  exits 0 before the next connection; SIGTERM during the sleep → exit
  within one second.
- `_reconcile`: an exit tail with `not synchronized with source GPKG` →
  state `crashed`, no further restarts, condition `checkout_inconsistent`,
  one mail; Re-init clears it.
- `_reconcile`: an exit tail with `LoginError … urlopen error` →
  `mergin_unreachable` path, no `process_crashed`, backoff 60 s; with
  `401` → `mergin_auth_failed`.
- Field: with field work active (or a clone being edited every minute),
  `docker compose up -d --build` five times in a row; every project
  stays `ok`, the stop takes under 150 s, Events show "stopped cleanly".
