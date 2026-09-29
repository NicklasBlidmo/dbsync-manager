# dbsync-manager — notes for Claude

Read first: `docs/dbsync-manager-design.md` (§ numbers in code comments refer
to it) and the newest file in `docs/review-notes/`. Review notes record
changes made outside a coding session and, more importantly, *why*; read them
before touching the files they list, and do not revert those changes without
raising it with the maintainer.

Invariants learned the hard way:

- Every manager table, Django's own included, lives in schema
  `dbsync_manager`. Nothing in `public`, nothing in synced schemas. The
  schema is created on `pre_migrate` (`registry/schema.py`) because Django
  creates `django_migrations` before the first migration runs.
- The registry role is not a superuser. Test database work with a role that
  has only `CREATE` on the database, or only ownership of a pre-created
  `dbsync_manager` schema. A superuser hides permission bugs.
- Never `CREATE SCHEMA IF NOT EXISTS`: PostgreSQL checks `CREATE` on the
  database before `IF NOT EXISTS`. Check `pg_namespace`, then create.
- No tracked file should need editing to deploy. Differences between
  installations go in `.env` (design §10). The bundled PostGIS is opt-in via
  the Compose profile `postgis`; the default is an external server.
- Static files are served by WhiteNoise. The admin must be checked with
  `DJANGO_DEBUG=false`, which is how the image runs.
- Anything needed before the container can start (key generation, start-up
  checks) must not import the Django settings, which require the keys, and
  must not go through the s6 entrypoint, which runs migrate first. That is
  why `manager/generate_keys.py` and `manager/preflight.py` are standalone.
- Values written to `.env` must survive Compose: no `$`, `#`, quotes or
  spaces. With duplicate lines the last one wins.
- The host is shared. Assume other Compose projects and other Django apps on
  the same address: no hardcoded host port (`BIND_IP`, `HTTP_PORT`), own
  cookie names that also carry `HTTP_PORT` (two instances of the manager on
  one host logged each other out, 2026-09-28), loopback always in
  `ALLOWED_HOSTS` for the healthcheck.
- Init scripts for the bundled PostGIS are mounted one file at a time into
  `/docker-entrypoint-initdb.d`, never as the directory: a directory mount
  masks the image's own `10_postgis.sh` and the `postgis` extension is
  never created (clean-room install, 2026-09-28). `registry/tests_deploy.py`
  guards it.
- A start-up failure must produce one readable line and a pause, never a
  traceback in a tight restart loop. New failure modes found in the field go
  into `preflight.py` with a test.
- s6-setuidgid does not set HOME; every service script exports it.
- The supervisor re-attaches to daemons a previous supervisor left running
  (pidfile + /proc cmdline check, `supervisor/processes.py`). Never start a
  second daemon for a project whose pidfile process is alive.
- db-sync's config parser (dynaconf) rewrites values that start with `@`.
  The config renderer refuses such secrets; do not "fix" that by quoting.
- db-sync 2.3.0 logs in with username and password only; API token accounts
  render as `blocked` with the reason, not as a crash loop.
- Everything the supervisor loads at the top of a tick is a snapshot. A
  step that changes a project's registry row (mark_removed, reset) ends
  that project's participation in the current tick, and _start re-reads
  desired_state and removed_at right before spawning. A remove once
  re-created the schemas it had just dropped because of this (2026-09-24).
- The first production target already runs a live, hand-configured db-sync
  against the same database. Two daemons must never sync the same schemas.
  Order decided 2026-09-22 (design §15): the Bönsta clone `p3922_test` is the
  first project through the manager; Bönsta itself is adopted or moved only
  after that; POC leftovers are cleaned last. Before any cleanup of a
  hand-run sync: `pg_dump -n '<prefix>*'`, tables may hold hand-made
  corrections.
- "Run hook now" is a sync_run row (kind hook, summary.status requested)
  that the supervisor executes on its next tick; the web process never runs
  a hook itself. Hook output stored in the registry is masked and capped.
- Hooks run in worker threads (`supervisor/hooks.py` start_job); the tick
  starts a job and reaps it on a later tick. Nothing in the loop may wait
  for a hook: post_init applies its result in `_finish_hook_job`, a
  pre_remove step returns Wait, scheduled hooks live in
  `Supervisor.scheduled_jobs`. Tests that drive a hook through
  `_reconcile` call it once to start, `hooks.wait_for_jobs`, then once
  to reap (tests_hooks.PostInitFlowTests.reconcile).
- db-sync's own init adopts existing schemas whose base comment names the
  project (downloads `db_version`, compares base with the GeoPackage). The
  manager's `add` relies on that; it never re-implements init.
- Two daemons started within the same second write geodiff temp files with
  the same "random" names in `/tmp` and kill each other (SQLITE_READONLY_DBMOVED,
  seen 2026-09-28 after a container restart with two projects). Every daemon
  gets its own `TMPDIR` under its project directory, and starts in one tick
  are staggered. A restart loop that keeps two daemons in step never
  resolves on its own.
- In the add wizard the submitted `files` list is the only source of truth
  for which GeoPackages a project gets. The names table, Preview and Create
  all derive from it; an empty list is an empty list (form error), never
  "unchanged" or "all". Client-side helpers that toggle checkboxes must
  dispatch `change` so the HTMX partial follows (2026-09-28: Select none
  left every schema name in place and Preview created connections for
  unchecked files).

Working agreements:

- There is no Docker on the development machine. The image was first built
  and run on the production server on 2026-09-21 (commit 59a8e4f). Changes
  to Dockerfile, compose or s6 scripts after that are unverified until
  Nicklas has built them there; say so rather than assuming they work.
- The production installation is described in the newest review note. Its
  `.env` and any `docker-compose.override.yml` live only on the server.
- Code, comments and docs in this repository are in English.
- Tests run on Windows too: `sqlite3.connect()` used as a context manager
  commits but does not close, and an open handle makes TemporaryDirectory
  cleanup fail there. Use `contextlib.closing` (see supervisor/check.py).
- Every new supervisor loop step gets a test that goes through
  `_reconcile`, with the registry writes stubbed, not only through the
  helper it calls (the pause bug of 2026-09-22 hid behind that gap).
