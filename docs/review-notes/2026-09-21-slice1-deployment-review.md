# Slice 1 deployment review, 2026-09-21

Reviewer: Claude (Cowork session with Nicklas), reading commit `fec4366`
with one question in mind: what happens when slice 1 is installed on a
server that already runs PostGIS and a live db-sync (Arkeologikonsult's
`/opt/ak-db` on 192.0.2.10, database `ak`, PostgreSQL 16)?

The changes below were left **uncommitted in the working tree** for review.
Nothing was committed or pushed. The Docker build is still unverified;
everything marked *verified* was run against a real PostgreSQL 16 with
Django 5.2 outside Docker.

## 1. `migrate` failed for any non-superuser role (bug, verified)

**Symptom.** With `DATABASE_URL` pointing at a role that has `CREATE` on the
database but is not a superuser, which is exactly what README and §12
prescribe:

```
MigrationSchemaMissing: Unable to create the django_migrations table
(permission denied for schema public)
```

**Cause.** Django creates `django_migrations` *before* it runs the first
migration. `0001_schema` had not run yet, so `dbsync_manager` did not exist,
PostgreSQL skipped it in `search_path`, and the table went to `public`. On
PostgreSQL 15+ ordinary roles cannot create in `public`. With the bundled
postgis service the role is the `postgres` superuser, so it "worked", but
`django_migrations` ended up in `public`, contradicting the docstring and §12.

**Fix.**
- `registry/schema.py` (new): `ensure_registry_schema(connection)`, checks
  `pg_namespace` and creates the schema only when missing.
- `registry/apps.py`: connected to `pre_migrate`, which fires before
  `django_migrations` is created. Covers Docker, `manage.py migrate` in
  development and test database creation alike, so no entrypoint step is
  needed.
- `registry/migrations/0001_schema.py`: `RunSQL("CREATE SCHEMA IF NOT
  EXISTS")` replaced by `RunPython` using the same helper. Safe to edit in
  place because the migration has never been applied anywhere.

**Why check-then-create instead of `IF NOT EXISTS`.** PostgreSQL checks
`CREATE` on the database *before* it evaluates `IF NOT EXISTS`. A role that
owns a pre-created schema but has no `CREATE` on the database would fail on
that statement even though there is nothing to do. The least-privilege setup
(`CREATE SCHEMA dbsync_manager AUTHORIZATION <role>` by a DBA, nothing else
granted) must work, and now does.

**Verified.** (A) role with `CREATE` on database, no schema: 19 tables in
`dbsync_manager`, 0 in `public`. (B) pre-created schema, no `CREATE` on
database: same result. Re-running `migrate` is a no-op. `manage.py test
registry` passes.

**Keep in mind.** Anything that must exist before the first migration goes in
`pre_migrate`, not in a migration.

## 2. The bundled PostGIS was not optional (design deviation)

**Symptom.** `postgis` had no profile and `manager` had a hard `depends_on`
on it. `docker compose up -d` therefore always started a second, empty
PostGIS, and with the `.env.example` defaults the registry was created in
*that* database instead of the organisation's own. README's answer was
"delete the service from docker-compose.yml", which means every real
installation carries a local diff against a tracked file.

**Fix (`docker-compose.yml`).**
- `postgis` is behind `profiles: ["postgis"]`.
- `depends_on.postgis.required: false` (Compose 2.20+): ignored when the
  profile is inactive, still waits for health when it is active.
- `POSTGRES_PASSWORD` changed from `${...:?}` to `${...:-}`. Compose
  interpolates all services, also those in inactive profiles, so `:?` would
  stop external-database installations that rightly leave it unset. The
  postgres image itself refuses an empty password, so the bundled case still
  fails loudly.

Plain `docker compose up -d` is now the external-database case (§10: "most
installations already have one"); the bundled case is `--profile postgis` or
`COMPOSE_PROFILES=postgis` in `.env`. No tracked file needs editing for
either. **Not verified with Docker** (no Docker in the review environment);
the YAML parses.

## 3. Admin had no CSS outside DEBUG (bug, verified)

**Symptom.** The image runs `collectstatic` and serves with gunicorn, but
nothing served `/static/`. With `DJANGO_DEBUG=false` (the default) the admin,
which is the whole UI in slice 1, rendered unstyled.

**Fix.** `whitenoise` added to `requirements.txt`; `WhiteNoiseMiddleware`
directly after `SecurityMiddleware`; `STORAGES["staticfiles"]` set to
`CompressedStaticFilesStorage`. The manifest variant was avoided on purpose:
it raises at request time when `collectstatic` has not been run, which would
break `runserver` and tests for no gain here.

**Verified.** gunicorn with `DJANGO_DEBUG=false`: `/healthz` 200,
`/static/admin/css/base.css` 200 `text/css`, `/admin/login/` 200.

## 4. Documentation (`README.md`, `.env.example`)

Quick start shows both database cases; new "Database" section with the two
ways to prepare the role; `.env.example` explains that `localhost` inside the
container is the container, and that `ALLOWED_HOSTS` must list every name or
address typed in the browser (otherwise HTTP 400 from the LAN).

## Still open, not changed

- **Docker build unverified.** Two things to watch on the first `docker
  compose build`: Django 5.2 needs Python 3.10+ in the db-sync 2.3.0 base
  image, and `pip install` may stop on PEP 668 ("externally-managed-
  environment") depending on the base distribution.
- **Port 8080 is published on all host addresses.** Fine on an office LAN
  behind a firewall; consider a `BIND_IP` variable like the POC's
  `LAN_BIND_IP` before this is recommended to others.
- **No test covers the schema bootstrap.** `registry/tests.py` is
  deliberately database-free. A database-backed test asserting that no
  manager table lands in `public` would have caught finding 1.
- **Design gap for a later slice: adopting an existing sync.** §6 `add`
  rejects `init_from = gpkg` when the target schemas exist. An installation
  that already runs db-sync by hand (ours: `p3922_bonsta_s_*`/`_b_*`, fed by
  the `ak-dbsync` container) can therefore only move under the manager by
  re-init: stop the old daemon, drop schemas, let the manager init, run the
  `post_init` hook. That is lossless for a Reader (one-way) account, but the
  views disappear meanwhile. Either document this as the takeover procedure
  or add an "adopt" path that trusts the version in the base schema comment.
  Belongs in §15 before the slice where the supervisor starts children. Until
  then two daemons must never sync the same schemas; slice 1 cannot, since
  the supervisor starts nothing.

## Files touched

```
docker-compose.yml
.env.example
README.md
requirements.txt
manager/dbsync_manager/settings.py
manager/registry/apps.py
manager/registry/schema.py                (new)
manager/registry/migrations/0001_schema.py
CLAUDE.md                                 (new)
docs/review-notes/2026-09-21-slice1-deployment-review.md   (new, this file)
```

Housekeeping: the review left an empty `.git/cowork-stale-index.lock`
(a renamed stale `index.lock`; the review sandbox could not delete files).
It is inert and can be deleted.
