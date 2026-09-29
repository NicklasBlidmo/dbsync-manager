# Slice 13 brief: scheduled hooks (and backup as their first use)

*Written by Cowork-Claude 2026-09-29. Decision with Nicklas 2026-09-28:
backup should be configurable from the admin without building a bespoke
backup feature. The manager already runs scripts with database
credentials, masked logs and failure e-mails: hooks. What is missing is a
hook that runs on a clock instead of on an event. This slice adds that,
generically, and documents backup as the first scheduled hook. Restore
stays a documented manual procedure (`docs/backup.md`); a restore button
in a web UI is more dangerous than useful.*

## What exists (design §8, slices 5–7)

`Hook` rows in the admin: `kind` (`post_init`, `pre_remove`, `post_pull`),
`command` run with `sh -c`, `PG*` variables and a per-run pgpass for the
project's sync database, optional "run as" database (`Database` row) for
hooks that need more privileges, `DBSYNC_SYNC_ROLE`, `DBSYNC_SCHEMA_PREFIX`
and friends in the environment, output masked and capped in a
`sync_run` row, "Run hook now" from the project page, e-mail on failure
through the project's alert recipients. Known open item from slice 6:
hooks run inside the supervisor tick and block it.

## A. The model

1. `Hook.kind` gets a fourth value, `scheduled`. Scheduled hooks are not
   attached to a project; they belong to the installation.
2. New fields, used only when `kind = scheduled`: `enabled` (bool),
   `run_at` (time of day, in `TZ`), `run_as` (existing FK, here
   mandatory: a scheduled hook has no project to borrow credentials
   from), `timeout_s` (default 3600), `notify_emails` (comma-separated,
   because there is no project to take recipients from),
   `last_started_at`, `last_finished_at`, `last_status`.
3. `SyncRun.project` becomes nullable so that a scheduled run is logged
   with the same machinery (`kind = hook`, `project = NULL`, `hook` set).
   Migration with a check that no existing row is affected.
4. Environment for a scheduled hook: the `PG*` variables and pgpass of the
   run-as database, `DBSYNC_HOOK_NAME`, `DBSYNC_RUN_ID`, `DBSYNC_DATA_DIR`,
   `TMPDIR` under `/data/hooks/<hook id>/tmp` (same rule as daemons,
   2026-09-28b). No project variables.

## B. Scheduling

5. Once per tick the supervisor checks each enabled scheduled hook: if
   local time is past `run_at` and `last_started_at` is before today
   (local date), start it. One run per day, at most; a container that
   was down at `run_at` runs it at the first tick after start. No cron
   syntax in v1: "daily at HH:MM" covers backup, vacuum and reports, and
   anyone who needs more has cron on the host.
6. **Hooks must not block the tick.** Run every hook (scheduled and
   project hooks alike) in a worker thread with the `timeout_s`; the tick
   only starts and reaps them. This closes the slice 6 finding. A hook
   still running when the supervisor stops gets SIGTERM, then SIGKILL
   after 10 s, and its run is marked `interrupted` (also closes the
   "SIGTERM during running hook" item). Only one instance of a given
   scheduled hook runs at a time; a second due run is skipped with a
   logged reason.
7. Failure handling: non-zero exit or timeout → `last_status = failed`,
   one e-mail to `notify_emails` with the masked tail of the output and a
   link to the run; recovery on the next success → one e-mail. Same
   dedup rule as project conditions (§7): one mail per transition, not
   per run.

## C. UI

8. Home page gets a section **Scheduled** under the projects table (a
   `<details>` collapsed by default, open when any hook is `failed`):
   name, run_at, run as, last run (relative time), status badge, "Run
   now" button for operators. Clicking the name opens a page with the
   last 20 runs (same partial as the project page's hooks table).
9. Admin: the `Hook` form shows the scheduled-only fields only when
   `kind = scheduled` (a little JS or two fieldsets). The `kind` field
   stays locked once the hook is referenced (open item from slice 6,
   include it here).

## D. Backup as the first scheduled hook (documentation, no code)

10. `docs/backup.md` gets a section "Scheduled backup from the manager"
    that replaces the cron recipe as the recommended routine:
    - A run-as `Database` row with a role that has `pg_read_all_data`
      (the `backup` role from the existing docs).
    - A bind mount in the server-local `docker-compose.override.yml`:
      `/srv/backup/dbsync:/backup`.
    - The hook command, verbatim:
      ```sh
      set -e
      out=/backup/${PGDATABASE}-$(date +%Y%m%d-%H%M).pgdump
      pg_dump -Fc --no-password -f "$out.partial" && mv "$out.partial" "$out"
      find /backup -name "${PGDATABASE}-*.pgdump" -mtime +30 -delete
      echo "backup: $out ($(du -h "$out" | cut -f1))"
      ```
      `PG*` are set by the manager, so `pg_dump` needs no arguments for
      the connection.
    - `run_at` 02:15, `timeout_s` 3600, `notify_emails` the operator.
    - The reminder that `DBSYNC_MANAGER_SECRET_KEY` is not in the dump.
11. `tools/backup.sh` stays for installations that prefer host cron; the
    doc says which to pick (manager hook: failure e-mail and history in
    the UI; cron: independent of the manager being up).
12. Version check: the image's `pg_dump` must have the same major version
    as the server. Verify what the base image ships (`pg_dump --version`
    in the container) and state it in the README's requirements; if the
    server is newer, the Dockerfile gets the client from
    apt.postgresql.org pinned to `PG_CLIENT_MAJOR` build arg. Not built
    unless needed.

## E. Tests and acceptance

- Unit: due-today logic across midnight and `TZ`; one run per day; a
  hook that was down at `run_at` runs at the next tick; a running hook
  is not started twice.
- `_reconcile` test with a hook that sleeps: the tick returns while the
  hook runs; a timeout marks the run `failed` and kills the process.
- Field test on gis-ubuntu: a scheduled hook `echo hello` at now+2 min →
  runs once, shows in Scheduled with `ok`; a failing hook → e-mail
  received; the backup hook against `ak` → a dump in `/srv/backup/dbsync`
  that `pg_restore -l` can list; run_at reached while the container is
  down → runs after start.
- Review note in the usual form, and `docs/backup.md` updated.

## Not in this slice

Cron expressions, restore from the UI, hooks per workspace, part 2.

## Applied (Claude Code, same day)

| Item | Change |
|---|---|
| A1, A2 | `Hook.Kind.SCHEDULED`; fields `enabled`, `run_at`, `notify_emails`, `last_started_at`, `last_finished_at`, `last_status`; the existing `database` FK is the run-as and `Hook.clean()` requires it and `run_at` for scheduled hooks. `timeout_s` keeps its default of 600; the backup recipe sets 3600 |
| A3 | Migration 0011: `SyncRun.project` nullable, new `SyncRun.hook`; additive only, no existing row changes (docstring says so) |
| A4 | `hooks.scheduled_environment`: PG* and pgpass of the run-as row, `DBSYNC_HOOK_NAME`, `DBSYNC_HOOK_ID`, `DBSYNC_RUN_ID`, `DBSYNC_DATA_DIR`, `TMPDIR` under `/data/hooks/<id>/tmp`; no project variables |
| B5 | `supervisor/scheduling.is_due`: past `run_at` and no start on the local date; a container down at `run_at` runs at the first tick after start. "Run now" does not count as the day's scheduled run |
| B6 | `hooks.start_job`/`HookJob`: every hook in a daemon thread; the tick starts and reaps. post_init result applied in `_finish_hook_job`; manual runs one at a time; post_pull waits when another hook runs; `pre_remove` returns `Wait` until the worker ends. `execute` polls every second and escalates a shutdown SIGTERM to SIGKILL after 10 s; the run is marked `interrupted`; `mark_orphaned_runs` at supervisor start closes runs a previous supervisor left running. One instance per scheduled hook (`scheduled_jobs`) |
| B7 | `last_status` ok/failed/interrupted; `alerts.notify_hook` on `scheduling.transition` (failed, recovered), masked tail and a link to `/scheduled/<id>/` |
| C8 | Home: **Scheduled** `<details>` under the projects table, open when a hook is failed; name, daily at, runs as, last run, badge, Run now; polled every 30 s. `/scheduled/<id>/`: status, Run now, last 20 runs (shared partial `hook_runs_table.html`), the command |
| C9 | Admin: fieldset "Scheduled hooks only" shown while kind = scheduled (`ui/admin-hook.js`), `notify_emails` comma-separated, last_* read-only; kind stays locked once referenced (existing) |
| D10, D11 | `docs/backup.md`: "Scheduled backup from the manager (recommended)" with role, bind mount, the hook verbatim, Run now check, key reminder, and "Manager hook or cron?"; cron section kept |
| D12 | Not verifiable here (no Docker): the base image's `pg_dump` version is unknown, and the bundled PostGIS is 16. The Dockerfile takes `PG_CLIENT_MAJOR` (empty = unchanged build; a value installs that client from apt.postgresql.org), compose passes it from `.env`, README and backup.md say how to check. **Both the arg path and the version check are for the field test** |
| E | Unit: `tests_scheduling.py` (due across midnight and TZ, once per day, down at run_at, running not started twice, mail per transition); `tests_hooks.py`: tick returns while the hook runs, manual runs one at a time, pre_remove step waits, SIGTERM→SIGKILL escalation (POSIX); `ui/tests.py` ScheduledPageTests (db); `registry/tests.py` Hook validation. Field test on gis-ubuntu remains |
