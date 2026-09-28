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
you just created; there are no default credentials. The groups `viewer`,
`operator` and `admin` are created at start (log line "groups ensured"):
`viewer` sees everything, `operator` can add, pause, resume, re-init and
remove syncs, `admin` manages credentials and users; staff users get the
admin link. Before the first project, add under Settings (admin) →
Registry a Mergin account that has at least Reader on the project, and a
database row whose role owns the sync schemas or may create them
(`GRANT CREATE ON DATABASE <db> TO <role>`); the sync runs as that role,
never as the registry role.

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

With the bundled PostGIS, `docker/postgis-init/10-manager.sh` runs once
when the data volume is created and makes the registry role
`dbsync_manager` (password `MANAGER_DB_PASSWORD` from `.env`) owning the
schema `dbsync_manager`, so the registry never runs as the `postgres`
superuser; `.env.example`'s `DATABASE_URL` already points at that role.
Sync roles for projects are created the same way by hand:

```sh
docker compose exec postgis psql -U postgres -d gis -c \
  "CREATE ROLE dbsync LOGIN PASSWORD '...'; GRANT CREATE ON DATABASE gis TO dbsync;"
```

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
tools/                backup.sh, the nightly dump (docs/backup.md)
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
