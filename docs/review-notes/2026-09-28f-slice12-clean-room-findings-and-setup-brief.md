# Slice 12 brief: clean-room findings, bundled PostGIS fixes, `setup.sh`

*Written by Cowork-Claude 2026-09-28 after Nicklas ran the clean-room
install from the slice 11 brief (§D) on gis-ubuntu: fresh clone of
v1.0.0, second Compose project `dbsync-fresh`, bundled PostGIS, port 8091,
project `Arkeologikonsult/3448_brasan9` (18 GeoPackages) through the
wizard's `new` path to `running` / `ok`, then pause, resume and remove with
drop. The install works end to end, but only with help from someone who
built the system. Every point below is a step where that help was needed.
Goal of the slice: the same walk-through succeeds with the README alone.*

## A. Bugs found (fix first)

1. **Names table did not refresh for projects with many files.** `add_names`
   was a GET with the whole form in the query string; 18 files exceeded
   gunicorn's 4094-byte request line, the server answered 400 and htmx
   left the table untouched. Already fixed by Claude Code (POST) and
   deployed; listed for the record. Add the test if it is not there yet:
   40 files with long names → 40 rows.
2. **Bundled PostGIS never got the `postgis` extension.** The manager's
   init script is mounted as a directory on `/docker-entrypoint-initdb.d`,
   which masks the image's own `10_postgis.sh`. Evidence: `\dx` in `gis`
   showed only `plpgsql`, no `template_postgis` database, db-sync died
   with "Cannot find or activate `postgis` extension". Fix: mount one
   file, `./docker/postgis-init/20-manager.sql:/docker-entrypoint-initdb.d/20-manager.sql:ro`,
   numbered after the image's script, and let the script also run
   `CREATE EXTENSION IF NOT EXISTS postgis;` as a belt and braces. Test:
   the `test` profile can assert the extension exists after init.
3. **Two instances on one host log each other out.** Cookies are per host
   name, not per port; production (8090) and the test instance (8091)
   shared `dbsync_manager_sessionid` and `…_csrftoken`, and every login
   in one invalidated the other. Fix: suffix both cookie names with
   `HTTP_PORT` (`dbsync_manager_8091_sessionid`); document in
   `.env.example` that instances on the same host need distinct ports and
   project names. Everyone is logged out once at upgrade; say so in the
   CHANGELOG.
4. **Silent htmx failures.** Bug 1 was invisible for an hour because a 4xx
   on an htmx request changes nothing on the page. Add a small listener in
   `base.html` for `htmx:responseError` and `htmx:sendError` that shows a
   `.notice.bad` at the top of `main` with status and URL, dismissable.

## B. Bundled PostGIS: make the first project possible from `.env` alone

5. The init script creates the registry role only. Add a **sync role**:
   `SYNC_DB_ROLE` (default `dbsync_sync`) and `SYNC_DB_PASSWORD` in
   `.env`, `CREATE ROLE … LOGIN PASSWORD …` and `GRANT CREATE ON DATABASE
   … TO …` in the same script. The README's "Add database" step for the
   bundled case is then: Host `postgis`, Port `5432`, Dbname `$POSTGRES_DB`,
   User `$SYNC_DB_ROLE`, Password `$SYNC_DB_PASSWORD`, written out as a
   table. For an external server the README gets the two SQL statements
   the operator runs as superuser (role + grant), next to the existing
   ones for the registry role.
6. README note: init scripts run only when the data volume is empty. An
   installation whose volume already exists gets the extension and roles
   by hand (the three statements, listed).
7. `Database.host` help text: add "with the bundled PostGIS: `postgis`".
8. Wizard preview: it already connects to the target database to inspect
   schemas; also check `SELECT 1 FROM pg_extension WHERE extname='postgis'`
   and refuse with "PostGIS is not installed in database X" before
   anything is created, instead of letting the daemon crash afterwards.
9. Admin, Database row: a **Test connection** action (admin action or a
   button on the change form) that runs the same connect + PostGIS check
   and reports "ok, PostgreSQL 16.x, PostGIS 3.4, n schemas with prefix
   …" or the error. Nicklas lost two rounds to a wrong user name and a
   LAN address that silently pointed at a different server; the
   preview's `new`/`adopt` verdict does not make a wrong target obvious.

## C. `.env.example` and README

10. Instructions written as `#` comments inside a code block are read as
    comments and skipped (this happened with the "edit .env" line). The
    edit step becomes a numbered prose step listing the exact lines.
11. `.env.example` starts with two short blocks: "Bundled PostGIS: set
    exactly these lines" (`COMPOSE_PROFILES=postgis`, `POSTGRES_PASSWORD`,
    `MANAGER_DB_PASSWORD` (twice: its own line and inside `DATABASE_URL`),
    `SYNC_DB_PASSWORD`, `HTTP_PORT`, `ALLOWED_HOSTS`, `DBSYNC_BASE_URL`)
    and "Existing PostGIS server: set these". `COMPOSE_PROJECT_NAME` as a
    commented line with one sentence: a second installation on the same
    host needs its own project name, port and, because cookies are per
    host, its own port-suffixed cookies (bug 3).
12. Port collision surfaces only after a multi-minute build. README: check
    the port before building (`ss -ltn | grep -w <port>`); `setup.sh`
    does it automatically.

## D. `tools/setup.sh`

One command for a Linux host with Docker: `sh tools/setup.sh`. Plain
POSIX sh, no Python on the host required (keys via the image, as the
README already shows), idempotent.

1. Checks: Linux, `docker` and `docker compose` ≥ 2.20, the chosen port
   free, `.env` absent or already complete (never overwrite filled-in
   values; re-running only asks for what is empty).
2. Asks, with defaults: bundled or external PostGIS; for bundled the three
   passwords (offer generated ones); for external the `DATABASE_URL`;
   `BIND_IP`, `HTTP_PORT` (default 8080, suggests the next free port if
   taken), `ALLOWED_HOSTS` (default: the host's primary LAN address),
   `TZ` (default from `/etc/timezone`), `COMPOSE_PROJECT_NAME` (default
   `dbsync-manager`).
3. Writes `.env` (both places for `MANAGER_DB_PASSWORD`), `chmod 600`,
   generates the two keys, prints `DBSYNC_MANAGER_SECRET_KEY` once with
   the instruction to store it in the password manager and asks for a
   keypress before continuing.
4. `docker compose build`, `up -d`, waits for `http://127.0.0.1:<port>/healthz`
   (up to `DBSYNC_DB_WAIT` + 60 s) while tailing preflight lines, then
   `createsuperuser`.
5. Prints the URL and, for bundled PostGIS, the exact Database row to add.
6. Non-interactive mode for repeatable installs: every answer as an
   environment variable (`SETUP_PROFILE=postgis SETUP_HTTP_PORT=8091 …`).
   That is how the clean-room test is scripted from now on.

## E. Acceptance

- `docker compose -p dbsync-fresh down -v && rm -rf /tmp/dbsync-fresh`, then
  the whole clean-room walk again from a fresh clone, this time with
  `sh tools/setup.sh` and the README only, no chat. Target: zero steps
  needing outside help. Record any remaining step in this slice's review
  note.
- Bundled PostGIS after init: `\dx` shows postgis; roles `dbsync_manager`
  and `dbsync_sync` exist with the intended grants; the Database row from
  the README table passes Test connection.
- Two instances on one host: log in to both in the same browser, neither
  logs the other out.
- Preview against a database without PostGIS refuses with the message.
- An htmx request that returns 4xx shows the notice.

## Not in this slice

Scheduled hooks / backup from the UI (slice 13, separate brief), any
change to the supervisor, part 2 of the project.

## Applied (Claude Code, same day)

| Item | Change |
|---|---|
| A1 | Done before this brief (f2a801f): `add_names` is a POST; test posts 40 long names and expects 40 rows |
| A2 | Done before this brief (9c53b44) as `20-manager.sh`, a shell script rather than `.sql` because the postgres entrypoint runs `.sql` without variable substitution and the passwords come from `.env`. Guard test: `registry/tests_deploy.py` fails if any service mounts a directory over `/docker-entrypoint-initdb.d` |
| A3 | `SESSION_COOKIE_NAME`/`CSRF_COOKIE_NAME` carry `HTTP_PORT`; `.env.example` and the CHANGELOG say so (everyone logged out once) |
| A4 | `base.html` listens for `htmx:responseError` and `htmx:sendError` and shows a dismissable `.notice.bad` at the top of `main` with status and path |
| B5 | `SYNC_DB_ROLE` (default `dbsync_sync`) and `SYNC_DB_PASSWORD` in `.env`, created by the init script with `GRANT CREATE ON DATABASE`; README has the Database row as a table and the two SQL statements for an external server |
| B6 | README "Database": init scripts run only on an empty volume; the three statements listed |
| B7 | `Database.host` help text, migration 0010 |
| B8 | `inspect_targets` refuses with "PostGIS is not installed in database X on host" before any pg_namespace query; pure test with a fake cursor |
| B9 | Admin action **Test connection** on Settings → Databases: "ok, PostgreSQL 16.4, PostGIS 3.4.2, n schemas besides the system ones" or the error; db-tagged test with the facts mocked |
| C10 | Quick start is numbered prose; the edit step lists the exact lines per case |
| C11 | `.env.example` opens with the two blocks and a commented `COMPOSE_PROJECT_NAME` |
| C12 | Port check before the build in the README and in `setup.sh` (suggests the next free port) |
| D | `tools/setup.sh`: POSIX sh, checks (Linux, docker, compose ≥ 2.20, port free), asks with defaults or takes `SETUP_*`, writes `.env` (password twice, chmod 600, never overwrites a filled value), keys via python3 or the image with a pause to store the Fernet key, build, up, health wait with preflight lines, createsuperuser (prompt, `DJANGO_SUPERUSER_*`, or skipped without a terminal), summary with the Database row. **Unverified: no Docker or Linux on the development machine; `sh -n` only** |
| E | For gis-ubuntu: the clean-room walk with `setup.sh` and the README only |

## Clean-room walk, updated for this slice (replaces slice 11 brief §D)

On gis-ubuntu, next to the production instance, from a fresh clone. The
project name goes into `.env` as `COMPOSE_PROJECT_NAME`, so no `-p` on
later commands run from that directory. Answer nothing the README does
not say; every step that needs more is a README bug for the review note.

```sh
git clone <repo> /tmp/dbsync-fresh && cd /tmp/dbsync-fresh
SETUP_PROFILE=postgis SETUP_PROJECT_NAME=dbsync-fresh SETUP_HTTP_PORT=8091   SETUP_ALLOWED_HOSTS=192.0.2.10 sh tools/setup.sh
#   -> asks nothing else, prints DBSYNC_MANAGER_SECRET_KEY and pauses, builds,
#      starts, waits for /healthz, runs createsuperuser, prints the Database row
docker compose logs manager | grep -E 'preflight|Applying|groups ensured|tick'
docker compose logs postgis | grep -E 'created (role|sync role)|CREATE EXTENSION|ERROR'
docker compose exec postgis psql -U postgres -d gis -c '\dx' -c '\du'
#   -> postgis listed; roles dbsync_manager and dbsync_sync
```

In the browser on port 8091, README only:

1. Settings → Databases → add the row from the README table (host
   `postgis`, user `dbsync_sync`, password `SYNC_DB_PASSWORD` from `.env`),
   select it, action **Test connection**: "ok, PostgreSQL 16.x, PostGIS
   3.4.x, 0 schemas besides the system ones".
2. Settings → Mergin accounts → the service account.
3. Add sync → `Arkeologikonsult/3448_brasan9` (18 files): tick and untick
   a file and change the prefix, the names table follows each time
   (bug 1). Preview → Create → `running` and `ok` within a few minutes.
4. Pause, resume, then Remove with drop; the operation panel shows every
   step.
5. Two instances: log in to production (8090) and this instance (8091) in
   the same browser; neither logs the other out (bug 3).
6. Failed request notice (bug 4): in the wizard's step 2 open the browser's
   developer tools, set the network to offline, tick a file: the red
   notice appears at the top of the page; go online, Dismiss.

Tear down:

```sh
docker compose down -v && cd / && rm -rf /tmp/dbsync-fresh
```

If the first start fails and is retried, `docker compose down -v` first:
the init script runs only on an empty volume.
