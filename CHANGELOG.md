# Changelog

## v1.1.0 (2026-10-27)

The first public release. Since v1.0.0 the tool has run in production
for a month with four projects, and the slices below came out of that:
a setup script proven in three clean-room installs, scheduled hooks
(nightly backup as the first), graceful stop on every path, exit
classification so a deterministic failure is one mail and not a crash
loop, content checks and Verify, a JSON API with tokens, db-sync 2.4.0
with upstream's version of our one-checkout-per-connection fix, and
two-way sync tested in the field and documented with its rules and its
conflict records. Upstream: MerginMaps/db-sync #187 (merged), #194 and
#196 (open). Upgrading from v1.0.0: see slice 16 below
(`migrate_checkouts` once, after Verify on every project).

- Slice 18 (two-way test 2026-10-26e, last section): the stuck rule's
  clock runs from the later of the checkout's last move and the
  daemon's start, and the rule waits one check interval after a start;
  a project resumed after a long pause is no longer restarted at once.
  Stop events report what is known: "stopped cleanly within N s" (the
  exit is seen on the next tick) or "after N–M s" when the daemon
  outlived a tick, instead of a figure measured to the reaping tick.
- Slice 17 B (two-way test 2026-10-26e): the direction was the other
  way round. In a concurrent edit the database syncs last and wins: the
  field's value is overwritten on both sides, and a database update of
  a field-deleted row is dropped. Event text, page, `docs/api.md` and
  the README's "Two-way sync" rules corrected (the API's
  `deleted_on_server` stays as an alias of `deleted_in_field`). The
  case geodiff records nowhere, a field update of a row the database
  deleted, is now logged by db-sync (patch `0003`, offered upstream) and
  becomes the `warning` event "field update lost: ..." with the field's
  values.
- Slice 17 A (brief 2026-10-26c): rebase conflicts become visible. The
  conflict file geodiff writes to the daemon's TMPDIR during a two-way
  rebase (database and field changed the same value) is moved to
  `/data/projects/<id>/conflicts/<time>-<file>.json` (last 50 kept) and
  becomes one `warning` event naming layer and rows; a
  rebase in a one-way project is the error-level `content_divergence`
  condition. Project setting `mail_on_conflict` (admin, default off)
  mails each one. Project page section "Conflicts" with download; API
  `conflicts: {last_at, count_30d}` on the project and
  `GET /api/v1/projects/<id>/conflicts/[<name>/]`. README section
  "Two-way sync". Migration 0017.
- Slice 16 (brief 2026-10-21d): db-sync 2.4.0. One checkout per connection
  is upstream (#187, our fix for #163), so patch `0001` is removed; the
  checkout now lives at `work/<project>-<modified schema>` and holds only
  the synced GeoPackage (#128). `supervisor/layout.py` is the one place
  that knows the layout. Upgrading an installation requires
  `migrate_checkouts` once (Projects page or `manage.py`): projects in
  the old layout are not started until the `migrate` operation has
  stopped the daemon, removed the old tree and let db-sync download the
  new checkout, one project at a time. Patch `0002` (graceful stop) is
  rebased on 2.4.0's retrying main loop; the generated config sets
  `max_failed_retries: 1` so a failed login or init still reaches the
  supervisor's exit classification. db-sync's `.mergin_auth.json` under
  the work dir is documented as a secret.
- Slice 15 (brief 2026-10-21): the check compares the content of base
  and modified per table (one statement with a FULL OUTER JOIN on the
  GeoPackage key, IS DISTINCT FROM per column, EWKB bytes for
  geometries), stores the three counts and a sample of up to 200 rows on
  the status row, and raises `content_divergence:<file>:<table>` at
  error level in a one-way project; in a two-way project it is shown,
  with an info event when it appears and when it clears.
  `DBSYNC_CONTENT_CHECK=false` switches it off. The project page has a
  Tables section (layers, geometry column, schemas, skipped). A
  `verify` operation (project page button, `manage.py verify`, API)
  compares the checkout GeoPackage with base through geodiff, read-only,
  and raises `checkout_inconsistent` on a difference. A read-only JSON
  API under `/api/v1/` with bearer tokens from the admin (`docs/api.md`).
- Slice 14b (field test 2026-10-20d): Re-init and Remove resolve every
  active condition (resolved event and recovery mail); an installation
  default list of alert recipients under Settings → Installation
  settings, used by every project and scheduled hook whose own list is
  empty, with the add wizard requiring addresses or an explicit tick of
  the default, and a "no recipients" warning where alerts would mail
  nobody; health follows a condition at once (`failed` on activation,
  `unknown` on resolve until the next check) and a check runs at the
  first tick after a start; every stop path (pause, remove, re-init,
  restart, shutdown) logs "stopping ... waiting for the current cycle"
  and "stopped cleanly after N s" or "killed after N s".
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
