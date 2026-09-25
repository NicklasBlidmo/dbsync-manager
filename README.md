# dbsync-manager

Web UI and supervisor for [Mergin Maps DB Sync](https://github.com/MerginMaps/db-sync):
set up, monitor, pause and remove synchronisations between Mergin Maps
projects and PostGIS, packaged as one Docker container.

The design is in [docs/dbsync-manager-design.md](docs/dbsync-manager-design.md).
Section numbers in code comments (§6, §8, ...) refer to it.

## Status

Slice 7 of 7 (design §14). The supervisor renders db-sync's config from the
registry, starts one daemon per project with `desired_state = running`,
stops it on `paused`, restarts it with backoff after a crash, re-attaches to
daemons left running by a previous supervisor, and every `DBSYNC_CHECK_INTERVAL`
runs the consistency check: server, checkout and database versions, row
counts GeoPackage vs base vs modified, Mergin permission and database
writers, from which it derives `health` and the alert conditions (design §6).
The web UI at `/` shows every project with its three badges (desired,
process, health) and a project page with status per GeoPackage, direction
facts, live log tail, events and pause/resume. "Add sync" is a wizard:
pick account and database, search a Mergin workspace, tick the project's
GeoPackages, get schema names derived from a prefix (editable), and a
preview that checks the target database (new schemas, adoption of an
existing hand-run sync, or a conflict) and the resulting direction before
anything is created. The admin form remains for edge cases.
Hooks (design §8) run inside the container with PG* variables and a per-run
pgpass file: `post_init` gates `running`, `post_pull` runs after the check
when the database version advanced, and any configured hook can be run
from the project page. Alert conditions send one e-mail when they become
active and one when they resolve, to the project's recipients.
Remove and re-init are restart-safe operations (design §6): the daemon is
stopped, the `pre_remove` hook runs before anything destructive, schemas
are dropped only when asked (remove) or always (re-init), and every step
is logged on the project page with retry and, before the drop, cancel.
The daemon's log is followed and classified into pull/push runs and events
(a pull counts as applied only when the check sees the database at that
version; geodiff NOTICE lines are info, repeated errors are counted), and
events and runs older than `DBSYNC_RETENTION_DAYS` are pruned daily.
All seven slices of v1 are in; remaining: field test of the log history,
then the Bönsta takeover (design §15).

## Quick start

```sh
cp .env.example .env && chmod 600 .env
python3 manager/generate_keys.py --write .env    # fills the two key lines in place
# edit .env: DATABASE_URL, ALLOWED_HOSTS, and BIND_IP/HTTP_PORT if 8080 is taken
docker compose build
docker compose up -d                      # existing PostGIS server
# docker compose --profile postgis up -d  # or: bundled PostGIS
docker compose logs -f manager            # "preflight: ... ok", migrations, "tick: no projects"
docker compose exec manager python manage.py createsuperuser
```

Open `http://<ALLOWED_HOSTS entry>:<HTTP_PORT>/` and log in with the account
you just created; there are no default credentials. Add a Mergin account and
a database under Settings (admin) → Registry before adding a project. Users
in the `viewer` group see everything; `operator` can pause and resume;
`admin` manages credentials and users. Staff users get the admin link.

No Python on the host? Generate the keys with the image instead, bypassing
its entrypoint (which would run migrate first), and paste the two values
onto the existing `DJANGO_SECRET_KEY=` and `DBSYNC_MANAGER_SECRET_KEY=` lines:

```sh
docker compose run --rm --no-deps --entrypoint python manager generate_keys.py
```

Copy `DBSYNC_MANAGER_SECRET_KEY` somewhere safe outside the server. Once a
credential has been saved it cannot be replaced (design §10).

### When it does not start

The container runs a preflight before `migrate` and prints one line per
problem, prefixed `dbsync-manager preflight:`: a missing or malformed key, a
malformed `DATABASE_URL`, an unreachable server, a refused login, a missing
schema the role may not create. It then waits `DBSYNC_FAIL_DELAY` seconds
before the restart policy tries again. Fix `.env` and run
`docker compose up -d`; Compose recreates the container when `.env` changed.

"CSRF verification failed" at login, with a correct `.env`, means the browser
holds cookies from another application on the same host name. The manager
uses its own cookie names to avoid this; clear the site data once if the
browser still has cookies from an older build.

### Database

The `postgis` service is behind the Compose profile `postgis` and is not
started by a plain `docker compose up`. Most installations already have a
PostGIS server and point `DATABASE_URL` at it; from inside the container
that is the server's LAN address or a shared Docker network, never
`localhost`.

The role in `DATABASE_URL` owns the `dbsync_manager` schema and nothing
else; it is not the role syncs run as. Either give it `CREATE` on the
database, or pre-create the schema and grant nothing more:

```sql
CREATE ROLE dbsync_manager LOGIN PASSWORD '...';
CREATE SCHEMA dbsync_manager AUTHORIZATION dbsync_manager;
```

All manager tables, Django's own included, live in that schema. Nothing is
created in `public` or in synced schemas.

### Hooks and the organisation's scripts

Hooks run inside the container (design §8). Scripts that live outside the
image are bind-mounted read-only through a server-local
`docker-compose.override.yml` (ignored by git):

```yaml
services:
  manager:
    volumes:
      - /opt/ak-db:/opt/ak-db:ro
```

A hook command then reads them, e.g. `SQLDIR=/opt/ak-db/sql
/opt/ak-db/run_sql.sh "$DBSYNC_SCHEMA_PREFIX" all`. `psql` and `ogr2ogr`
are in the image. A hook that needs more privileges than the sync role
(views, grants, `pg_dump`) gets its own "run as" database connection in
the admin; `DBSYNC_SYNC_ROLE` tells the script which role to grant to.

### E-mail

Alerts go out through whatever `SMTP_*` in `.env` points at. With
`SMTP_USER` and `SMTP_PASSWORD` empty the manager sends without
authentication, which is how Microsoft 365 direct send works; see
[docs/m365-direct-send.md](docs/m365-direct-send.md) for the tenant-side
setup and the checks to run when mail stops. `manage.py sendtestemail`
sends a test message with the configured settings.

## Layout

```
Dockerfile            FROM lutraconsulting/mergin-db-sync; manager on top (§10)
docker-compose.yml    manager + optional postgis
docker/               s6-overlay services: migrate → web + supervise
manager/              Django project
  dbsync_manager/     settings (all from environment), urls
  generate_keys.py    standalone key generator (no Django needed)
  preflight.py        start-up checks with one-line messages
  registry/           data model (§5), crypto (§4), admin, groups (§4)
  supervisor/         `manage.py supervise` (§6): processes, config, check, health
  ui/                 HTMX pages (§9): projects, project page, add wizard, login
patches/              applied to db-sync at image build (#163)
tools/                check_sync.sh, standalone cron check without the manager
docs/                 design document
```

## Development

```sh
python -m venv .venv && . .venv/bin/activate      # or .venv\Scripts\activate
pip install -r requirements.txt
cd manager
export DJANGO_SECRET_KEY=dev DBSYNC_MANAGER_SECRET_KEY=$(python -c "from cryptography.fernet import Fernet;print(Fernet.generate_key().decode())")
export DATABASE_URL=postgres://postgres:postgres@localhost:5432/gis
python manage.py check
python manage.py test --exclude-tag=db   # unit tests, no database needed
python manage.py migrate && python manage.py runserver
```

Database-backed tests (tagged `db`) run against a throwaway PostGIS with a
non-superuser role, on a machine with Docker:

```sh
docker compose --profile test run --rm test
```

## Per-project files under /data

```
/data/projects/<id>/config.yaml        generated from the registry, mode 600
/data/projects/<id>/dbsync.log         db-sync's --log-file (INFO), rotated
/data/projects/<id>/dbsync.stderr.log  everything db-sync writes to stderr, rotated
/data/projects/<id>/dbsync.pid         pid + config path, used to re-attach
/data/projects/<id>/work/<project>/<modified schema>/   the checkout (#163 patch)
```
