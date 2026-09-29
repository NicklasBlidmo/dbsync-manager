# dbsync-manager

Web UI and supervisor for [Mergin Maps DB Sync](https://github.com/MerginMaps/db-sync):
set up, monitor, pause and remove synchronisations between Mergin Maps
projects and PostGIS, packaged as one Docker container.

The design is in [docs/dbsync-manager-design.md](docs/dbsync-manager-design.md).
Section numbers in code comments (§6, §8, ...) refer to it.

## Version

v1.0.0, first release. Design §14's ten slices are complete, and the tool
has run in production at the author's organisation since 2026-09-25,
where it took over a hand-run sync without re-initialisation (design §15
describes that adoption as a worked example).

What v1 does: it runs one db-sync daemon per Mergin Maps project from a
registry in PostgreSQL, restarts it with backoff and re-attaches after its
own restarts. Every few minutes it compares the Mergin server version, the
checkout and the database, and the row counts of GeoPackage, base and
modified schemas, and derives a health state and alert conditions that go
out by e-mail once when they start and once when they end. A web UI shows
every project with desired, process and health badges, a project page
with per-file state, live log, pull history, events and hooks, and an add
wizard that derives schema names, checks the target database and adopts
existing schemas. Hooks run organisation-specific SQL and scripts around
init, pull and removal with a per-run pgpass file and masked output.
Remove and re-init are restart-safe multi-step operations with retry and
cancel.

Out of scope: syncing forms or metadata (part 2 of the project), editing
data, and multi-tenant use; one installation per organisation.

## Quick start

On a Linux host with Docker, one command asks the questions below, writes
`.env`, generates the keys, builds, starts, waits for the health check and
creates the first user:

```sh
sh tools/setup.sh
```

Every answer can be given as an environment variable instead
(`SETUP_PROFILE=postgis SETUP_HTTP_PORT=8091 …`, listed at the top of the
script), which is how a repeatable install is scripted. If it stops, fix
what it reports and run it again: it continues where it stopped, and
prints the remaining commands in case you prefer to finish by hand. The
Compose project is named after the directory, and a name another
directory already uses is refused, so a second installation on the same
host never replaces the first. By hand, the same steps are:

1. `cp .env.example .env && chmod 600 .env`
2. Check that the port is free before building, because a collision
   otherwise shows only after a multi-minute build: `ss -ltn | grep -w 8080`.
   Pick another `HTTP_PORT` if it is taken.
3. Edit `.env`. With the bundled PostGIS, set `COMPOSE_PROFILES=postgis`
   (uncomment the line), `POSTGRES_PASSWORD`, `MANAGER_DB_PASSWORD` twice
   (its own line and inside `DATABASE_URL`), `SYNC_DB_PASSWORD`,
   `HTTP_PORT`, `ALLOWED_HOSTS` and `DBSYNC_BASE_URL`. With an existing
   PostGIS server, set `DATABASE_URL`, `HTTP_PORT`, `ALLOWED_HOSTS` and
   `DBSYNC_BASE_URL`; the `POSTGRES_*` and `SYNC_*` lines are unused. In
   `DATABASE_URL=postgres://role:password@host:5432/db` the `postgres://`
   part is the protocol and never changes; the server's address goes after
   the `@`, and the password is letters and digits.
4. `python3 manager/generate_keys.py --write .env` fills the two key lines
   in place (no Python on the host: see below).
5. `docker compose build && docker compose up -d`
6. `docker compose logs -f manager` until it prints "preflight: ... ok",
   the migrations and "tick: no projects".
7. `docker compose exec manager python manage.py createsuperuser`

Open `http://<ALLOWED_HOSTS entry>:<HTTP_PORT>/` and log in with the account
you just created; there are no default credentials. The groups `viewer`,
`operator` and `admin` are created at start (log line "groups ensured"):
`viewer` sees everything, `operator` can add, pause, resume, re-init and
remove syncs, `admin` manages credentials and users; staff users get the
admin link. Before the first project, add under Settings (admin) →
Registry a Mergin account that has at least Reader on the project, and a
database row whose role owns the sync schemas or may create them
(`GRANT CREATE ON DATABASE <db> TO <role>`); the sync runs as that role,
never as the registry role. With the bundled PostGIS that row is:

| Field    | Value                                  |
|----------|----------------------------------------|
| Name     | anything, e.g. `gis`                   |
| Host     | `postgis`                              |
| Port     | `5432`                                 |
| Dbname   | `POSTGRES_DB` from `.env` (`gis`)      |
| User     | `SYNC_DB_ROLE` from `.env` (`dbsync_sync`) |
| Password | `SYNC_DB_PASSWORD` from `.env`         |

Select the row in Settings → Databases and run the action **Test
connection**: it reports the PostgreSQL and PostGIS versions and the number
of schemas, or the error. A wrong user name or an address that points at
another server shows here, not in the wizard.

No Python on the host? Generate the keys with the image instead, bypassing
its entrypoint (which would run migrate first), and paste the two values
onto the existing `DJANGO_SECRET_KEY=` and `DBSYNC_MANAGER_SECRET_KEY=` lines:

```sh
docker compose run --rm --no-deps --entrypoint python manager generate_keys.py
```

Copy `DBSYNC_MANAGER_SECRET_KEY` somewhere safe outside the server. Once a
credential has been saved it cannot be replaced (design §10).

### First-time setup

Under Settings (admin) → Registry: a Mergin account and a database. The
add wizard starts with every GeoPackage of a project ticked; Select all
and Select none are on the file list. The tool has no opinion about file
naming.

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

With the bundled PostGIS, `docker/postgis-init/20-manager.sh` runs once,
when the data volume is created, and makes the `postgis` extension, the
registry role `dbsync_manager` (password `MANAGER_DB_PASSWORD` from
`.env`) owning the schema `dbsync_manager`, so the registry never runs as
the `postgres` superuser, and the sync role `SYNC_DB_ROLE` (default
`dbsync_sync`, password `SYNC_DB_PASSWORD`) with `CREATE` on the database.
`.env.example`'s `DATABASE_URL` already points at the registry role, and
the table under Quick start is the first Settings → Databases row.

The init scripts run only while the data volume is empty. An existing
volume (a start before the script existed, or a failed first start that
was not followed by `docker compose --profile postgis down -v`) keeps what
it has; add the extension and roles by hand instead, as `postgres`:

```sql
CREATE EXTENSION IF NOT EXISTS postgis;
CREATE ROLE dbsync_sync LOGIN PASSWORD '...';
GRANT CREATE ON DATABASE gis TO dbsync_sync;
```

(`docker compose exec postgis psql -U postgres -d gis` opens that prompt.)
On an existing server the operator runs the same two role statements as a
superuser for every sync role, one per project or team; the wizard's
preview refuses a database without the `postgis` extension before anything
is created.

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
      - /opt/sync-scripts:/opt/sync-scripts:ro
```

A hook command then reads them, e.g. `SQLDIR=/opt/sync-scripts/sql
/opt/sync-scripts/run_sql.sh "$DBSYNC_SCHEMA_PREFIX" all`, where the
scripts are the organisation's own; the manager only provides the
environment (design §8). `psql` and `ogr2ogr` are in the image. A hook that needs more privileges than the sync role
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
  ui/static/ui/app.css  the stylesheet; its :root tokens are the visual contract
patches/              applied to db-sync at image build (#163)
tools/                setup.sh (first install), backup.sh (nightly dump, docs/backup.md)
docs/                 design document, backup and e-mail guides, review notes
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

Database-backed tests (tagged `db`) need Docker: they run against a
throwaway PostGIS with a non-superuser role, so that permission bugs show
(see CLAUDE.md for the invariants they guard, including the Windows note
about `sqlite3` handles):

```sh
docker compose --profile test run --rm test
```

## Per-project files under /data

```
/data/projects/<id>/config.yaml        generated from the registry, mode 600
/data/projects/<id>/dbsync.log         db-sync's --log-file (INFO), rotated
/data/projects/<id>/dbsync.stderr.log  everything db-sync writes to stderr, rotated
/data/projects/<id>/dbsync.pid         pid + config path, used to re-attach
/data/projects/<id>/tmp/               TMPDIR of the daemon and hooks, emptied on start
/data/projects/<id>/work/<project>/<modified schema>/   the checkout (#163 patch)
```

## Project history

`docs/review-notes/` is the field log of the first installation
(Arkeologikonsult, Sweden, September 2026): what broke, why, and what was
changed. It refers to that organisation's servers and projects. It is kept
because `CLAUDE.md` and several design decisions point at it; nothing in
it is required to run the software. `CHANGELOG.md` lists releases.

## License

MIT, see `LICENSE`. The image builds on Lutra Consulting's db-sync (MIT);
the Inter font is shipped unchanged under the SIL Open Font License
(`manager/ui/static/ui/fonts/OFL.txt`).
