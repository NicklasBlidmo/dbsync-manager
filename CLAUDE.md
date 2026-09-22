# dbsync-manager — notes for Claude

Read first: `docs/dbsync-manager-design.md` (§ numbers in code comments refer
to it) and the newest file in `docs/review-notes/`. Review notes record
changes made outside a coding session and, more importantly, *why*; read them
before touching the files they list, and do not revert those changes without
raising it with Nicklas.

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
  cookie names, loopback always in `ALLOWED_HOSTS` for the healthcheck.
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
- The first production target already runs a live, hand-configured db-sync
  against the same database. Two daemons must never sync the same schemas.
  Order decided 2026-09-22 (design §15): the Bönsta clone `p3922_test` is the
  first project through the manager; Bönsta itself is adopted or moved only
  after that; POC leftovers are cleaned last. Before any cleanup of a
  hand-run sync: `pg_dump -n '<prefix>*'`, tables may hold hand-made
  corrections.
- db-sync's own init adopts existing schemas whose base comment names the
  project (downloads `db_version`, compares base with the GeoPackage). The
  manager's `add` relies on that; it never re-implements init.

Working agreements:

- There is no Docker on the development machine. The image was first built
  and run on the production server on 2026-09-21 (commit 59a8e4f). Changes
  to Dockerfile, compose or s6 scripts after that are unverified until
  Nicklas has built them there; say so rather than assuming they work.
- The production installation is described in the newest review note. Its
  `.env` and any `docker-compose.override.yml` live only on the server.
- Code, comments and docs in this repository are in English.
