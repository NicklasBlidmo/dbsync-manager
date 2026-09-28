# First installation on the production server, 2026-09-21 (evening)

Reviewer: Claude (Cowork session with Nicklas). Commit `59a8e4f` was built and
started on `gis-ubuntu` (192.0.2.10), against the existing PostGIS
container `ak-postgis`, database `ak`. It took seven attempts. None of the
failures was in the data model or the supervisor; all were in the first ten
minutes of an installation, which is where a tool meant to be shared with
others is judged. This note records each failure, the change it led to, and
why the change looks the way it does.

The changes are **uncommitted in the working tree**. Python parts are
verified (22 unit tests; preflight run against a real PostgreSQL 16 for each
failure mode; start script run with a stubbed `s6-setuidgid`; gunicorn with
`DJANGO_DEBUG=false`). **Not verified: anything that needs Docker** — the
compose port variables, the s6 scripts inside the image, `TZ` in the
container. Nicklas verifies those on the server; see "Upgrading the server".

## What now runs in production

- `/opt/dbsync-manager`, image built from `59a8e4f`, UI on
  `http://192.0.2.10:8090/` (office LAN only, plain HTTP).
- Registry in database `ak`, schema `dbsync_manager`, owned by role
  `ak_manager`, which has nothing else: the schema was pre-created with
  `CREATE SCHEMA ... AUTHORIZATION`, no `CREATE` on the database. 19 tables
  there, none in `public`. The least-privilege path from the first review
  note is thereby confirmed in the field.
- The hand-configured Bönsta sync (`ak-dbsync`, schemas `p3922_bonsta_*`) was
  not touched and keeps running. Decision: it stays as the reference until
  the slice where the supervisor starts children; the first project added
  through the manager will be the *clone* of Bönsta (prefix `p3922_test`).
  Bönsta itself moves under the manager by re-init at that point, not before.
- The same host also runs `application-manager` (nginx on 80/443/8080/8443),
  its dev instance (8081/8444) and `mergin-qa` (8000). This matters for
  findings 1 and 7.
- Server-local, not in git: `.env`, and `docker-compose.override.yml` (port
  mapping; superseded by finding 1, see "Upgrading the server").

## Findings and changes

### 1. Host port 8080 was hardcoded, and taken

`Bind for 0.0.0.0:8080 failed: port is already allocated`; 8081 likewise. A
server that runs anything else very likely has 8080 in use. The workaround
was an override file with `ports: !override`, which breaks the rule that no
file next to the repository should need writing to deploy.

**Change.** `docker-compose.yml`: `"${BIND_IP:-0.0.0.0}:${HTTP_PORT:-8080}:8080"`,
both documented in `.env.example`. `BIND_IP` also answers the open point from
the first note (UI published on all interfaces). `docker-compose.override.yml`
added to `.gitignore`, since servers will have them anyway.

### 2. `generate_keys` could not run when it was needed

The documented command was `docker compose run --rm --no-deps manager python
manage.py generate_keys`. It fails twice over on a fresh `.env`: the settings
raise `ImproperlyConfigured` because `DJANGO_SECRET_KEY` is required, and
`compose run` goes through the s6 entrypoint, which runs `migrate` (no
database with `--no-deps`) and stops the container. Separately, Django's
`get_random_secret_key()` emits `$` and `#`; Compose interpolates `$` in env
files, so the stored key would silently differ from the printed one.

**Change.** `manager/generate_keys.py`: standard library only, no Django.
Prints the pair, or with `--write .env` fills them in. Values are URL-safe
base64 (the Fernet key in exactly `Fernet.generate_key()`'s format; a test
round-trips it through `registry.crypto`). The management command remains as
a thin wrapper for the case where settings already load.

### 3. Duplicate key lines gave empty keys

The keys were pasted as new lines *above* the template's empty
`DJANGO_SECRET_KEY=` lines. In an env file the last line wins, so the
container saw empty values and the error said the key was "required", which
the operator had, as far as he could tell, provided.

**Change.** `--write` puts the value on the existing line and removes further
lines for the same key. It never replaces a non-empty value without
`--force`: replacing `DBSYNC_MANAGER_SECRET_KEY` after credentials are stored
makes them unreadable (§10). `.env.example` says the same in words.

### 4. A configuration error produced a traceback storm

`S6_BEHAVIOUR_IF_STAGE2_FAILS=2` plus `restart: unless-stopped` is the right
policy (a manager without a registry must not look alive), but each failure
printed ~60 lines, several times a second. The one useful line scrolled away
and the log had to be stopped to be read. Three different causes in a row
looked identical on screen: missing key, `DATABASE_URL=DATABASE_URL=...`
(name pasted into the value), refused login.

**Change.**
- `manager/preflight.py`, run before `migrate`. No Django import. Checks the
  environment (keys present, Fernet key valid, `DATABASE_URL` shape, `TZ`
  known), then connects, waiting up to `DBSYNC_DB_WAIT` seconds for a
  database that is still starting, and checks that the registry schema exists
  or can be created. One line per problem, each saying what to change.
- `docker/scripts/migrate`: on preflight or migrate failure, print a pointer
  and `sleep ${DBSYNC_FAIL_DELAY:-30}` before exiting. Restart policy
  unchanged; the loop is merely slow enough to read.
- The message for a refused login says that PostgreSQL reports a wrong
  password and a **non-existent role** identically. That ambiguity cost an
  iteration here.
- `localhost` in `DATABASE_URL` is *not* rejected up front, because it is
  correct with `network_mode: host`; it is explained only when the connection
  actually fails.

Every message was produced against a real server before being kept: missing
key, doubled variable name, unreachable host, closed port, wrong password,
missing role, schema missing without `CREATE`, and the success case.

### 5. Healthcheck was refused by `ALLOWED_HOSTS`

`.env` had `ALLOWED_HOSTS=192.0.2.10` (my instruction, which dropped the
template's loopback entries). The healthcheck calls
`http://127.0.0.1:8080/healthz`, got HTTP 400, and the container would have
been marked unhealthy while working.

**Change.** `settings.py` always appends `localhost`, `127.0.0.1`, `[::1]`.
The healthcheck is the tool's own and must not depend on the operator
remembering it. Verified: `/healthz` 200 via loopback with only the LAN
address configured; a foreign `Host` header still gets 400.

### 6. `HOME` and time zone

`Control server error: [Errno 13] Permission denied: '/root/.gunicorn'`:
`s6-setuidgid` changes the user, not the environment. Harmless today, but
db-sync and hooks will inherit the same wrong `HOME` later. **Change:**
`export HOME=/home/manager` in `web/run`, `supervise/run`, `scripts/migrate`.

gunicorn logged `+0100`, Django UTC, the host is `+0200`. Django exports
`TIME_ZONE` as `TZ` for its own process only. **Change:** `TIME_ZONE` is read
from `TZ`, which the container gets from `.env`, so every process uses the
same clock; `tzdata` added to requirements in case the base image lacks zone
data. Storage stays UTC (`USE_TZ`).

### 7. "CSRF verification failed" at the first login

`curl` showed the server setting `csrftoken` correctly, without `Secure`.
An incognito window worked, and so did the normal window after clearing site
data for the address. Cookies are scoped to the host name, not the port, and
the same address serves other web applications (see above), which from their
container layout look like Django and would then use the same default cookie
names. A `Secure` `csrftoken` set over HTTPS by a neighbour cannot be
overwritten from plain HTTP and is not sent over it either, which gives
exactly this error. **This explanation is an inference**; the browser's cookie
jar was not inspected. The fix is right regardless, since apps sharing
`sessionid` log each other out.

**Change.** `SESSION_COOKIE_NAME = "dbsync_manager_sessionid"`,
`CSRF_COOKIE_NAME = "dbsync_manager_csrftoken"`. Side effect: everyone is
logged out once by the upgrade.

## Upgrading the server (for Nicklas, after commit and push)

```sh
cd /opt/dbsync-manager && git pull
# move the override into .env, then remove it
cat >> .env <<'EOT'
BIND_IP=192.0.2.10
HTTP_PORT=8090
TZ=Europe/Stockholm
EOT
rm docker-compose.override.yml
docker compose config | grep -A4 'ports:'     # expect host_ip 192.0.2.10, published "8090"
docker compose build && docker compose up -d && docker compose logs -f --tail 30 manager
```

Expected: `dbsync-manager preflight: configuration and registry database ok`,
`No migrations to apply`, no `/root/.gunicorn` error, all timestamps on
Swedish time, `docker compose ps` healthy after a minute. Log in again (new
cookie names). If the build or start fails, paste the log lines; this part
has not been run anywhere yet.

## Still open

- From the first note, unchanged: no database-backed test of the schema
  bootstrap; the design gap on adopting an existing sync (§15, before the
  slice where the supervisor starts children).
- Reverse proxy with TLS (§12). The host already has an nginx on 80/443, but
  it belongs to another application; decide whether it becomes the shared
  proxy. Then `CSRF_TRUSTED_ORIGINS` and `SECURE_COOKIES=true`.
- Backup: the registry now lives in `ak-postgis`'s `pgdata`, the Fernet key in
  `/opt/dbsync-manager/.env`. Neither has a documented backup routine yet
  (open question in the project log since the POC).
- `preflight.py` duplicates the name of the registry schema and the URL
  parsing that `settings.py` gets from `dj-database-url`. Deliberate (it must
  not import settings), but the two must be kept in step.

## Files touched

```
docker-compose.yml
.env.example
.gitignore
README.md
CLAUDE.md
requirements.txt
docker/scripts/migrate
docker/s6-rc.d/web/run
docker/s6-rc.d/supervise/run
manager/dbsync_manager/settings.py
manager/generate_keys.py                                (new)
manager/preflight.py                                    (new)
manager/registry/management/commands/generate_keys.py
manager/registry/tests.py
docs/review-notes/2026-09-21b-first-installation.md     (new, this file)
```
