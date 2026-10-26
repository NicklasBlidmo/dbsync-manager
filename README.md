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

### Operations

**Deploys, restarts and stops are graceful.** `docker compose up -d
--build`, `restart`, `stop` and `down` send SIGTERM to the container;
the supervisor passes it to every daemon, and a daemon finishes its
current pull or push before it exits (patch `0002` in `patches/`). The
stop takes up to 150 s (`stop_grace_period` in `docker-compose.yml`); do
not shorten it. Before that patch a stop mid-pull left a checkout that
db-sync refused to use until Re-init. Every project gets two events per
stop: "stopping db-sync, waiting for the current cycle" and "stopped
cleanly after N s" or "killed after N s".

**Pause all / Resume all** on the Projects page (operators) stop every
running sync between cycles and resume exactly the projects that Pause
all paused; projects that were already paused stay paused. Use it before
work on the database or the Mergin account, or when a deploy must be
done during field hours.

**Health `failed`** means the daemon would fail the same way on every
start and is not restarted: the project page shows the condition, the
mail says what to do. `checkout_inconsistent` (the checkout and the
database no longer agree, typically after a daemon was killed mid-pull)
needs **Re-init** on the project page: dump the installation's own
schema first if views or grants live next to the sync schemas
([docs/backup.md](docs/backup.md), "Repair after an inconsistent
checkout"), choose "GeoPackage in Mergin", confirm that no database
changes are pending, then re-apply the views and grants. Nothing outside
the sync schemas is touched. `database_missing_postgis` needs the DBA,
then Pause and Resume.

**Upgrading an installation to db-sync 2.4.0 (checkout layout).** The
image from slice 16 on runs db-sync 2.4.0, which keeps one checkout per
connection at `work/<project>-<modified schema>` holding only the synced
GeoPackage (upstream #187 and #128; before, patch `0001` used
`work/<project>/<modified schema>` with the whole project). After `pull`,
`build` and `up -d` the Projects page shows "Checkout migration pending"
and the projects with the old layout are not started. Run **Verify** on
every project first (a project that fails needs Re-init instead), then
**Migrate checkouts** on the Projects page or
`docker compose exec manager python manage.py migrate_checkouts`. The
supervisor takes one project at a time: stop between cycles, remove the
old tree, start, and wait until db-sync has downloaded one GeoPackage per
connection at the database's version and compared it with `base`; the
project's events say how long it took. db-sync also writes
`work/.mergin_auth.json` (mode 0600, the Mergin session token, reused
across restarts) and retries a failed login or init once inside the
process before it exits (`max_failed_retries: 1` in the generated
config; the supervisor owns the restart policy).

**Mergin connectivity.** A daemon that exits because the Mergin server is
unreachable is restarted every minute and feeds the same
`mergin_unreachable` condition as the check (one mail per server after
`DBSYNC_MERGIN_DOWN_ALERT_AFTER`); refused credentials are
`mergin_auth_failed`, one mail, restarts every five minutes. A crash loop
that persists reminds after an hour and then daily, with the restart
count and the latest stderr lines.

**Events after the fact.** The project page's Events table has a
date/time and level filter and paging; the terminal has

```sh
docker compose exec manager python manage.py events --project <id or workspace/name> --since 2026-10-20T13:00 [--level error]
```

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

A trigger the installation puts on a synced `_s_` table runs inside
db-sync's session, with db-sync's `search_path`: it must schema-qualify
everything it touches, or its NOTICE ("relation ... does not exist")
turns up as an info event on the project page.

Hooks run in worker threads: a long hook never stalls the supervisor,
and one that is still running when the container stops gets SIGTERM,
then SIGKILL after 10 s, and shows as `interrupted`.

**Scheduled hooks** (kind `scheduled`) belong to the installation rather
than to a project and run once a day at `run_at` (in `TZ`) as their own
"run as" connection, with their own alert recipients: one mail when a
run fails, one when the next run succeeds. They appear in the
**Scheduled** section of the Projects page with a "Run now" button and a
history. A nightly `pg_dump` is the first use; [docs/backup.md](docs/backup.md)
has the hook verbatim. `pg_dump` in the image must not be older than the
server: check `docker compose exec manager pg_dump --version`, and if
the server is newer set `PG_CLIENT_MAJOR=<server major>` in `.env` and
rebuild, which installs that client from apt.postgresql.org.

### Tables, content checks and verify

The project page's **Tables** section lists every layer of each
GeoPackage with its geometry column and the `modified`/`base` schemas it
syncs into, and whether `skip_tables`/`include_tables` leaves it out.

The check compares not only row counts but the content of `base` and
`modified` per table (one statement per table pair, geometries on their
EWKB bytes). In a one-way project any difference is the condition
`content_divergence` (someone wrote into `modified` directly); in a
two-way project it is the normal state between pushes and only shown,
with an event when it appears and when it clears. `DBSYNC_CONTENT_CHECK=false`
in `.env` switches the comparison off.

**Verify** (project page, operators) compares the checkout GeoPackage
with `base` through geodiff, the way db-sync's own init check does,
read-only while the daemon keeps running. A difference raises
`checkout_inconsistent` with the same mail and Re-init path as the
daemon's own detection. Also `manage.py verify --project <id>` and the
API below; a daily run is a scheduled hook that calls the API.

### Two-way sync

A project is two-way when the Mergin account is Writer on it (the
direction badge follows the permission at every check). Then database
edits in `modified` are pushed to Mergin on the next cycle, and the
field's changes are pulled into `base` and `modified`. When both sides
changed between two cycles, db-sync rebases the database's changes on
top of the server's (`geodiff rebase-db`) and pushes the result. The
rules, as the two-way test of 2026-10-26 found them (review note
`2026-10-26e-two-way-test.md`):

1. A delete wins over an update, whichever side deletes.
2. Same row, same column: the side that syncs last wins (Mergin Maps'
   documented rule, "the editor who syncs last wins"; no timestamps).
   In a db-sync setup the database is *always* the last to sync in a
   concurrent edit: the daemon pulls the field's version first (that is
   the rebase) and pushes its own change on top. So **the database wins
   every same-column conflict**, and the field's value is overwritten on
   both sides.
3. Different columns of one row, and new rows (fids renumbered), merge.
4. Records: a same-column conflict and a database update of a
   field-deleted row are written to the conflicts file and surfaced by
   the manager: the project page's **Conflicts** section lists the
   files with layer and rows (fid), operators download them, and one
   `warning` event names the rows and says which value was overwritten.
   A field update of a database-deleted row leaves no record in geodiff;
   the manager's patch `0003` makes db-sync log it, and it appears as the
   `warning` event "field update lost: ..." with the field's values.
   Nothing is mailed unless the project's `mail_on_conflict` is on
   (admin), because in a two-way project a conflict is expected
   behaviour. In a one-way project a rebase can only mean someone wrote
   into `modified`: it is the error-level condition `content_divergence`
   with its mail.

What follows from rules 2 and 4: a field crew's correction can be
silently replaced by an office edit, and an office edit of a row the
field deletes is silently dropped. During active field work keep the
project one-way, or make sure no database writer touches rows the field
may edit. Two details for database writers: a geometry must match the
layer exactly (a PointZ layer refuses a 2D point), and nothing the field
form would fill in (defaults, expressions) is filled in for a database
insert.

### JSON API

Tools next to the manager read every project's state and table list,
and start verify, through `/api/v1/` with a bearer token created under
Settings → API tokens. [docs/api.md](docs/api.md) has the endpoints,
the JSON shapes and the stability promise.

### E-mail

Alerts go to the project's recipients, or, when the project has none, to
the installation's default list under Settings → Installation settings
(scheduled hooks the same). Set the default list once and every project
added without recipients is covered; until a project has one or the
other, the Projects page shows "no recipients" and an activation writes
"alert not mailed" instead of mailing. The add wizard asks for addresses
or an explicit tick of the default.

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
/data/projects/<id>/work/<project>-<modified schema>/   the checkout of one connection: the synced
                                       GeoPackage and .mergin/ (db-sync 2.4.0)
/data/projects/<id>/work/.mergin_auth.json   db-sync's Mergin session token, mode 0600 (a secret)
/data/projects/<id>/work/.layout-v2    marker: the 2.3.0 checkout tree is gone (migrate_checkouts)
```

`supervisor/layout.py` is the one place in the code that knows this layout.

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
