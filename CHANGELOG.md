# Changelog

## Unreleased

- Graceful stop (review note 2026-10-20b, finding 1): patch `0002` gives
  db-sync a SIGTERM handler; a daemon finishes its current pull or push
  and exits 0 at the next boundary. `STOP_GRACE` and the shutdown wait
  are 120 s, `docker-compose.yml` sets `stop_grace_period: 150s`, and
  every project gets "stopping" and "stopped cleanly / killed" events.
  `patches/upstream/` and `patches/*.patched` with a test that applies
  the patches as the Dockerfile does.
- A deterministic daemon exit (checkout not synchronized with the
  GeoPackage, missing postgis extension) is not restarted: condition
  `checkout_inconsistent` or `database_missing_postgis` with one mail,
  health `failed` (migration 0012), Re-init highlighted on the project
  page (finding 2).
- A daemon exit caused by Mergin being unreachable feeds
  `mergin_unreachable` (delayed, grouped mail) and restarts every minute
  instead of counting towards `process_crashed`; refused credentials
  are `mergin_auth_failed`, one mail, restarts capped at 5 min (finding 3).
- A crash loop that persists reminds after an hour and then daily, with
  the restart count and the latest stderr lines (finding 4).
- Events: date/time and level filter and paging on the project page;
  `manage.py events --project --since [--until] [--level]` (finding 5).
- Pause all / Resume all on the Projects page; Resume all touches only
  the projects the last Pause all paused (finding 6).
- Log classification after three weeks in production (review note
  2026-10-20): "There are pending changes on server" is an info event
  (the server moved between pull and push; the next cycle pulls), and a
  warning only after five consecutive cycles while the last check saw
  pending database edits on a project that is not one-way. Repeated
  identical messages are collapsed at every level.
- A Mergin outage (5xx, connection refused or reset, timeout) is the
  condition `mergin_unreachable` from the first failed check, with an
  info event; the alert mail goes out only after
  `DBSYNC_MERGIN_DOWN_ALERT_AFTER` (default 900 s), and the recovery
  event carries the duration. Server version and permission are kept
  from the last successful call meanwhile. 401/403/404 stay errors.
  Mail is per Mergin server, not per project: the first project past
  the threshold mails one list of every project on that server with the
  condition, and the recovery mail goes when the last of them clears.
- Scheduled hooks are due once the clock is past today's `run_at` and no
  run started since then, instead of "no run today": a `run_at` moved
  later the same day now runs once more, and the catch-up after downtime
  works after a run earlier that day (field test 2026-09-30).
- Admin: 24-hour times and unabbreviated dates (`LANGUAGE_CODE` en-gb);
  the Delete button is readable.
- Hook commands are saved with Unix line endings: a browser submits the
  textarea with CRLF and `sh` failed on the first line with "set: Illegal
  option -". `execute` also strips CR from rows saved earlier.
- Scheduled hooks (slice 13): a hook of kind `scheduled` runs once a day
  at `run_at` (in `TZ`) as its own run-as connection, with its own alert
  recipients (one mail per transition: failed, recovered). "Run now" and
  a history on the Projects page's new **Scheduled** section and on the
  hook's own page. A nightly `pg_dump` is the first use (docs/backup.md).
  Migration 0011: `Hook` gets the scheduling fields, `SyncRun.project`
  becomes nullable and `SyncRun.hook` records the hook of a run.
- Every hook runs in a worker thread: the supervisor tick only starts
  and reaps hooks (closes the slice 6 item). A hook still running at
  shutdown gets SIGTERM, SIGKILL after 10 s, and its run is marked
  `interrupted`; a `pre_remove` hook makes its operation wait instead of
  blocking the loop.
- `PG_CLIENT_MAJOR` in `.env`: rebuild with a newer PostgreSQL client
  from apt.postgresql.org when the server is newer than the image's
  `pg_dump`.
- Cookie names now carry `HTTP_PORT` (`dbsync_manager_8080_sessionid`):
  two installations on one host shared a cookie and logged each other out.
  **Everyone is logged out once at this upgrade**, and again if `HTTP_PORT`
  changes.
- A failed HTMX request (4xx, 5xx or no response) shows a dismissable
  notice at the top of the page instead of changing nothing.
- Settings → Databases: action **Test connection** reports PostgreSQL and
  PostGIS versions and the schema count, or the error.
- Add wizard: the preview refuses a database without the `postgis`
  extension before anything is created.
- Bundled PostGIS: the init script also creates the sync role
  `SYNC_DB_ROLE` (default `dbsync_sync`) from `SYNC_DB_PASSWORD`; the
  README lists the first Database row as a table.
- `tools/setup.sh`: one-command install on a Linux host with Docker,
  interactive or driven by `SETUP_*` variables. Second walk (2026-09-29):
  the project name defaults to the directory name and a name another
  directory already uses is refused (a fixed default had replaced the
  production containers); passwords are letters, digits, `-` and `_`;
  every step is skipped when already done, so a stopped run is resumed by
  running the script again; the summary names the project, port and
  directory.
- Preflight: a `DATABASE_URL` whose password contains `? / : # @` is
  named as such instead of "needs a host and a database"; `.env.example`
  and the README say that `postgres://` is the protocol.
- `.env.example` opens with the exact lines for each case; the README's
  edit step is prose, not a comment in a code block; the port is checked
  before the build.
- Add wizard, step 2: the schema-name table is fetched with POST. As a
  GET the whole form rode in the query string, and with 18 GeoPackages
  it passed gunicorn's request-line limit, so the table never refreshed.
- Project page: the `isotime` and `ago` filters used `django.utils.timezone.utc`,
  removed in Django 5.0, so every log line with a naive timestamp raised a
  500. Found by the db-tagged test run on the server; the filters now use
  `datetime.timezone.utc` and have tests without a database.
- Tests: the relative-time test asserted UTC as the local zone and failed
  when the test container had `TZ` set; it now pins `TIME_ZONE`.
- Bundled PostGIS: the init script was mounted as a directory over
  `/docker-entrypoint-initdb.d`, which masked the image's own
  `10_postgis.sh`, so the `postgis` extension was never created and
  db-sync failed with "Cannot find or activate postgis extension"
  (clean-room install). The script is now `20-manager.sh`, mounted as a
  single file, creates the extension itself, and also creates the sync
  role `dbsync` from `SYNC_DB_PASSWORD` for the first database row.

## v1.0.0 (2026-09-28)

First release. Ten slices (design §14): registry and credentials,
supervisor, consistency check and health, add wizard, hooks and alerts,
remove and re-init as operations, log history, UI foundation, visual
pass, functional polish; plus the release-readiness pass that made the
repository generic. In production at the author's organisation since
2026-09-25. See `docs/review-notes/` for the field history behind the
invariants in `CLAUDE.md`.
