# 2026-10-20c: slice 14, graceful stop and exit classification (incident 2026-10-20b)

*Written by Claude Code from the incident note `2026-10-20b`. Findings 1–6
implemented as one slice, finding 7 as documentation. Unverified here:
no Docker, so the image build with patch 0002 and the field test are for
gis-ubuntu.*

## What changed

| Finding | Change |
|---|---|
| 1 | `patches/0002-Stop-gracefully-on-SIGTERM.patch`: `dbsync.request_stop`/`stop_requested` (a flag), the daemon installs the handler for SIGTERM (SIGINT keeps its default), `dbsync_pull`/`dbsync_push` check the flag before each connection, the main loop checks it before pull and before push, the sleep polls it once a second; exit 0 at the next boundary. `patches/upstream/` (2.3.0 sources), `*.patched` (after both patches), `patches/README.md`. `STOP_GRACE` and `SHUTDOWN_GRACE` 120 s, `stop_grace_period: 150s` in compose, `setup.sh` and the README say why a stop takes up to 150 s. `Supervisor.shutdown` writes "stopping db-sync, waiting for the current cycle" per project and "stopped cleanly after N s" or "killed after N s" |
| 2 | `supervisor/exits.py` classifies the last stderr lines. Deterministic exits (`not synchronized with source GPKG`, `--force-init`, `Modified and base schemas already exist`; `Cannot find or activate postgis extension`) are not restarted (`next_start_at = inf`), raise `checkout_inconsistent` / `database_missing_postgis` at error level with one mail, and `check.health_with_conditions` reports health `failed` (migration 0012, most severe) while such a condition is active; the check stays the writer of health. The Re-init button is primary on the project page while `checkout_inconsistent` is active. Re-init clears it (reset empties the conditions and the runtime); a project paused and resumed after a repair by hand starts at once (`next_start_at` reset while paused), and the condition resolves once the daemon has stayed up for a check interval |
| 3 | Mergin connectivity exits (LoginError, URLError, urlopen error, timeouts, 5xx) feed `_track_mergin(down=True)` with the daemon's last line as the message, restart after 60 s plus jitter, and do not count towards `process_crashed`. 401/403: `mergin_auth_failed` at error level, one mail (dedup), backoff capped at 300 s; resolved once the daemon has stayed up |
| 4 | `process_crashed` reminders: `conditions.alert` after 3600 s, then every 86400 s, with the restart count and the latest tail; reset on resolve |
| 5 | Project page Events: `since`/`until` (datetime-local) and `level` filter, 50 per page with newer/older links; the section opens when a filter or page is active. `manage.py events --project <id or path> --since --until --level --condition --limit` |
| 6 | Pause all / Resume all (operators) with a confirmation page listing the projects. Pause all pauses every running project without an open operation and records an event per project with condition `pause_all:<timestamp>`; Resume all resumes the still-paused projects of the latest batch and nothing else. No new storage |
| 7 | README hooks section: a trigger on a synced table runs in db-sync's session and must schema-qualify what it touches |

## Tests

- `tests_patches.py`: both patches apply to `patches/upstream/` as the
  Dockerfile applies them and give `*.patched`; with the patched modules
  loaded under stubs, a stop request during a connection pull lets that
  pull finish and skips the rest, and a stop request during the sleep
  ends `main()` with exit code 0 within a second.
- `tests.py`: through `_reconcile`, a dead child whose stderr tail says
  `not synchronized with source GPKG` gives `crashed`, the condition at
  error level, no restart on the next tick; a LoginError tail feeds
  `_track_mergin` and nothing else, restart within 60 s; a 401 tail gives
  `mergin_auth_failed` with the backoff capped; the third ordinary crash
  raises `process_crashed`, the fourth an hour later sends the reminder;
  `exits.classify_exit` cases.
- `tests_check.py`: health `failed` while a deterministic condition is
  active.
- `ui/tests.py` (db): Pause all pauses the running projects and skips the
  paused one, Resume all resumes exactly those; the events filter and
  paging; the `events` command output.

## Field test (gis-ubuntu)

1. Build and deploy; `docker compose exec manager grep -c request_stop
   /mergin-db-sync/dbsync.py` is 3 or more.
2. With a clone being edited every minute, `docker compose up -d --build`
   five times in a row: every project stays `ok`, each stop takes under
   150 s, Events show "stopped cleanly after N s" for every project.
3. Pause all before a deploy during field hours, Resume all after: the
   confirmation page lists the right projects both ways.
4. Provoke `checkout_inconsistent` on the clone (`kill -9` the daemon
   mid-pull with the old image, or edit the checkout's GeoPackage): the
   project goes `failed`, one mail, no restarts in the log, Re-init
   repairs it and health returns to `ok`.
5. `manage.py events --project <clone> --since <today>` lists the above.
