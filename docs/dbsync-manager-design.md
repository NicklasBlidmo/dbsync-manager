# dbsync-manager — design document

Web UI and supervisor for Mergin Maps DB Sync: set up, monitor, pause and remove
synchronisations between Mergin Maps projects and PostGIS, packaged as one
Docker container.

Status: v0.1 draft, 2026-09-21. Author: Nicklas Blidmo (Arkeologikonsult), with Claude.
Written in English so it can be shared with Lutra Consulting as input to the
planned db-sync rework (Q4 2026). The Arkeologikonsult-specific parts are kept
out of the tool and described in §11.

---

## 1. Why

DB Sync works, but running it is manual: one YAML file per daemon, log files,
no view of what is actually in sync, and no safe way to add or stop a project
without editing config and restarting containers. We found three failure modes
in our proof of concept that the tool does not surface on its own:

- an error inside the transaction that applies a changeset makes the changeset
  disappear silently; the database is then permanently behind the GeoPackage
  and the daemon keeps reporting "No changes";
- several connections to the same project share one checkout, so only the
  first connection's layer gets updates (db-sync issue #163; patched, PR sent);
- a NOTICE raised by a trigger is logged by geodiff as ERROR, and a real error
  looks exactly the same in the log.

The manager's job is to make "is the database in sync with the field data,
and if not, why?" a question you can answer from a browser in ten seconds, and
to make add/pause/remove operations safe, repeatable actions instead of a
runbook.

## 2. Goals and non-goals

Goals:

- Add a sync for a Mergin Maps project from the browser: pick the project
  (searchable via the Mergin API), pick which GeoPackages to sync, choose
  schema names, run init in the background, show progress.
- Continuous status per project and per GeoPackage: server version vs local
  version vs database version, last successful pull, last error, feature
  count in GeoPackage vs in the `modified` schema (the check that catches
  silent divergence).
- Pause, resume, remove (with or without dropping the database schemas),
  re-initialise.
- Log view per project, with db-sync's own NOTICE-as-ERROR lines classified
  correctly.
- Alerts by e-mail when a project stops syncing or diverges.
- Post-init and pre-remove hooks so that organisation-specific work (creating
  views, grants, extra tables) stays outside the tool.
- One container, no Docker socket, no privileges. `docker compose up`.
- Small enough that Lutra can read it in an afternoon.

Non-goals (for v1):

- Replacing db-sync. The manager runs the unmodified db-sync code (with the
  #163 patch until it is released upstream) as a subprocess. Sync semantics are
  db-sync's.
- Editing data. The manager never writes to synced schemas.
- Multi-tenant SaaS. One installation per organisation.
- Two-way sync policy. Direction is whatever db-sync does; the manager makes
  the effective direction *visible* (see §7) but does not enforce it.

## 3. Architecture

```
┌────────────────────────────────────────────────────────────────┐
│  dbsync-manager container                                      │
│                                                                │
│  ┌──────────────┐   ┌────────────────┐   ┌──────────────────┐  │
│  │ web (Django) │──▶│ supervisor     │──▶│ db-sync daemon   │  │
│  │ HTMX pages   │   │ (one process,  │   │ per project      │  │
│  │ REST/JSON    │   │ asyncio)       │   │ (subprocess)     │  │
│  └──────┬───────┘   └───────┬────────┘   └────────┬─────────┘  │
│         │                   │                     │            │
│         ▼                   ▼                     ▼            │
│    schema dbsync_manager  (registry, runs, events)  work dirs  │
└──────────────────────────────┬─────────────────────────────────┘
                               │
                    ┌──────────▼──────────┐        ┌───────────────┐
                    │ PostGIS             │        │ Mergin Maps   │
                    │ synced schemas +    │        │ cloud / CE    │
                    │ dbsync_manager      │        └───────────────┘
                    └─────────────────────┘
```

Decisions:

- **Subprocess per project, not a container per project.** db-sync keeps its
  configuration in a module-global object, so running several projects inside
  one Python process is fragile. A subprocess per project gives isolation
  without Docker-in-Docker. The supervisor starts `dbsync_daemon.py` with a
  generated config file, restarts it with backoff, and stops it on pause.
- **db-sync is vendored, not forked.** The image is built `FROM
  lutraconsulting/mergin-db-sync:<version>`; the manager is added on top. The
  #163 patch is applied at build time and removed when upstream ships the fix.
  Nothing in db-sync's code is called as a library.
- **Registry in PostgreSQL**, schema `dbsync_manager`, in the same database the
  synced schemas live in. No extra service. The manager's own database role
  needs CREATE on the database (it creates the registry schema) and nothing on
  synced schemas.
- **Django + HTMX.** Server-rendered pages with partial updates; no JavaScript
  build step. Django gives users, sessions, permissions, admin and migrations
  for free. Status polling is HTMX `hx-trigger="every 10s"` on the status
  partial.
- **Status is computed, not parsed.** Version and count data come from sources
  of truth — the Mergin API, the `.mergin` metadata in the checkout, the
  comment db-sync writes on the base schema, `SELECT count(*)`, and sqlite on
  the GeoPackage — not from regex over the log. The log is shown, and scanned
  for classification, but never used as state.

## 4. Identities and users

There are three kinds of identity, and they must not be confused:

| Identity | Who | Where it lives | Used for |
|---|---|---|---|
| **Manager user** | A person who opens the web UI | Django `auth_user` (registry) | Logging in, permissions in the UI, audit trail ("who paused project X") |
| **Mergin account** | A service account in Mergin Maps | `dbsync_manager.mergin_account` (username + encrypted password or API token) | db-sync's connection to Mergin. One per installation is normal; several are allowed (e.g. one Reader for one-way projects, one Writer for two-way) |
| **Database role** | A PostgreSQL role | `dbsync_manager.database` (DSN with encrypted password) | db-sync's connection to PostGIS. The role owns the synced schemas |

A manager user is neither a Mergin user nor a database role. When a person
adds a project, they choose *which* Mergin account and *which* database
connection the sync should use; the manager acts with those credentials. This
is the same model as a CI server: people log in, jobs run with service
credentials.

Manager user roles (Django groups):

- **viewer** — read everything, no actions.
- **operator** — add, pause, resume, re-init, remove syncs; cannot manage
  credentials or users.
- **admin** — everything, including Mergin accounts, database connections,
  hooks, users.

Authentication in v1 is Django's username/password with an admin-created
user list (no self-registration). Single sign-on is a v1.1 item: OpenID Connect
via `mozilla-django-oidc`, tested against Entra ID; groups map from OIDC
claims. The data model does not change when SSO is added — only the login
view does.

What the Mergin account can do in Mergin decides what the sync can do. This
is deliberately *not* modelled in the manager: the manager shows the account's
role on each project (from the API) so the operator sees "Reader → one-way
into the database" or "Writer → two-way", but the permission itself is set in
Mergin. Same for the database role: grants are set in PostgreSQL, the manager
only checks and reports (§7).

Secrets: Mergin passwords/tokens and database passwords are stored encrypted
at rest with a key from the environment (`DBSYNC_MANAGER_SECRET_KEY`, Fernet).
They are written unencrypted only into the generated per-project config file,
which lives in the container's private volume with mode 600, and they never
appear in the UI or in logs.

## 5. Data model (schema `dbsync_manager`)

```
mergin_account      id, name, url, username, secret_enc, kind (password|token),
                    created_by, created_at

database            id, name, dsn_enc (host, port, dbname, user, password),
                    created_by, created_at

sync_project        id, mergin_project (workspace/name), mergin_project_id (uuid),
                    mergin_account_id, database_id,
                    schema_prefix, init_from (gpkg|db), sleep_time,
                    state (draft|initializing|running|paused|error|removing|removed),
                    desired_state (running|paused),
                    post_init_hook_id, pre_remove_hook_id,
                    notify_emails[], created_by, created_at, updated_at

sync_connection     id, sync_project_id, sync_file (1-punkter.gpkg),
                    modified_schema, base_schema, skip_tables[], include_tables[]
                    -- one row per GeoPackage; mirrors one db-sync "connection"

sync_run            id, sync_project_id, kind (init|pull|push|hook|check),
                    started_at, finished_at, ok, summary (jsonb)
                    -- one row per cycle, written by the supervisor from the
                    -- child's structured output (see §6)

sync_status         sync_project_id, sync_connection_id,
                    server_version, local_version, db_version,
                    gpkg_count (jsonb per table), db_count (jsonb per table),
                    last_pull_at, last_error_at, last_error,
                    checked_at
                    -- one row per connection, overwritten each check

event               id, sync_project_id, at, level (info|warning|error),
                    source (supervisor|dbsync|hook|user), message,
                    user_id (when a person did it)

hook                id, name, kind (post_init|pre_remove|post_pull), command,
                    timeout_s, created_by
```

Everything a person does (add, pause, remove, change credentials) is an
`event` with `user_id`. Everything the machine does that matters is an
`event` without.

## 6. Supervisor

One asyncio process started with the web server (`manage.py supervise`, or a
second process in the same container via `s6`/`supervisord`; the former is
simpler and enough).

Loop, every `tick` (default 30 s):

1. Read `sync_project` rows. For each project whose `desired_state` is
   `running` and that has no live child: **render** db-sync's `config.yaml`
   from the registry rows (`sync_project` + its `sync_connection`s + the
   referenced `mergin_account` and `database`) into `/data/projects/<id>/`,
   then start `python dbsync_daemon.py config.yaml
   --log-file ... --log-verbosity INFO`, record `state = initializing` if the
   base schema does not exist yet, else `running`.
2. For each project whose `desired_state` is `paused` and has a live child:
   SIGTERM, wait, `state = paused`.
3. For each live child: if it exited, record the exit in `event`, set `state
   = error`, schedule restart with exponential backoff (30 s → 10 min).
4. Every `check_interval` (default 5 min) run the **consistency check** per
   connection and write `sync_status`:
   - `server_version`: Mergin API `GET /v1/project/<ws>/<name>` → `version`.
   - `local_version`: `.mergin/mergin.json` in the checkout.
   - `db_version`: the comment on the base schema (db-sync writes
     `{"name": ..., "version": "v123"}` there after each applied pull).
   - `gpkg_count`: `SELECT count(*)` per table via sqlite3 on the checkout's
     GeoPackage (read-only, `?mode=ro`).
   - `db_count`: `SELECT count(*)` per table in `modified`.
   - Divergence rule: `db_version < local_version`, or any table where
     `gpkg_count != db_count` for two consecutive checks (one check may hit
     the window between file pull and database apply) → `event(warning)` and,
     if configured, e-mail.
5. Run `post_pull` hooks when `db_version` advanced since last check.

**The registry is the source of truth; the config file is a derived
artefact.** It is regenerated on every start and never edited by hand. Nothing
in the manager reads `config.yaml` back; if the file and the registry disagree,
the registry wins on the next start. This is what makes add/pause/remove
plain database operations plus a process signal.

Structured output from the child: db-sync logs are text. The supervisor
tails the log file and classifies lines into `sync_run`/`event` with a small
set of rules (`--- pull finished -- at version vN` → run ok; `DbSyncError`,
`Traceback` → error; `GEODIFF: NOTICE:` → **info**, not error; `pending
changes on server` → info). This is the only place the log is interpreted, and
it is isolated in one module so it can be replaced by a structured channel if
db-sync gains one (a suggestion for the rework: emit JSON lines on a file
descriptor).

Init as a background job: `dbsync_daemon.py` runs init itself on start, so
"initializing" is simply the state until the base schema comment appears.
Progress is shown as which connections have their base schema yet.

## 7. Directionality and safety checks

db-sync is two-way by design. Many installations want one-way (field →
database). The manager does not add a one-way mode (that belongs in db-sync,
and is on our wish list for the rework), but it makes the effective direction
explicit on the project page:

- Mergin role of the sync account on this project (Reader/Writer/Owner), from
  the API.
- Whether any database role other than the sync role has INSERT/UPDATE/DELETE
  on the `modified` schemas (`information_schema.role_table_grants`).
- Result shown as a badge: **one-way (field → DB)** when the account is Reader
  *and* no other role can write; **two-way** otherwise, with the reason.

The manager also warns before add if any of the chosen GeoPackages has a
`*(edit conflict*` sibling in the project, since those files will not sync.

## 8. Hooks

A hook is a command run by the supervisor with a defined environment, used to
do whatever an organisation needs around a sync without putting it in the
tool. Contract:

- Runs inside the manager container (so it is a script mounted into the
  image, or a `psql`/`curl` call). Working directory: the project's data dir.
- Environment: `DBSYNC_PROJECT` (workspace/name), `DBSYNC_PROJECT_ID`,
  `DBSYNC_SCHEMA_PREFIX`, `DBSYNC_MODIFIED_SCHEMAS` (comma-separated),
  `DBSYNC_BASE_SCHEMAS`, `DBSYNC_DSN` (the database connection, with
  password), `DBSYNC_WORK_DIR`, `DBSYNC_EVENT` (`post_init` | `pre_remove` |
  `post_pull`), `DBSYNC_DB_VERSION`.
- Exit code 0 = ok. Non-zero: `event(error)`, and for `post_init` the project
  is marked `error` (the sync still runs; the hook's failure is visible).
- stdout/stderr captured into `sync_run.summary`.
- Timeout per hook (default 600 s).

`post_init` is where views, grants, indexes and lookup imports belong.
`pre_remove` is where you dump or archive. `post_pull` is for lightweight
things (refresh a materialized view, ping a QA system) — it runs at most once
per check interval, not per cycle.

## 9. Pages

- **Projects** (home): table of all syncs with state badge, direction badge,
  server/local/DB version, last pull, divergence indicator. Filters by state.
- **Add sync**: search box → Mergin API project search (name, workspace) →
  project detail: list of GeoPackages with table names and feature counts
  (from the API's file list; counts require a download, so shown after a
  "preview" step) → choose GeoPackages, schema prefix (default derived from
  the project name, editable), Mergin account, database, init direction,
  hooks, notification e-mails → confirm → project page in `initializing`.
- **Project page**: status per connection (the `sync_status` row), the
  direction check, the last 50 events, log tail (live via HTMX polling),
  actions: pause / resume / re-init (with the explicit warning that re-init
  drops and recreates the synced schemas) / remove (checkbox: also drop
  schemas) / run hook now.
- **Settings**: Mergin accounts, databases, hooks, users (admin only), e-mail
  (SMTP).
- **JSON API** (same views, `Accept: application/json`): `GET /api/projects`,
  `GET /api/projects/<id>/status`, `POST /api/projects/<id>/pause` etc., token
  auth, so a CLI or a CI job can drive it. The CLI itself is out of scope for
  v1; the API is what makes it possible.

## 10. Packaging and configuration

```
Dockerfile           FROM lutraconsulting/mergin-db-sync:2.3.0
                     + apply patches/*.patch to /mergin-db-sync
                     + pip install django htmx psycopg[binary] cryptography
                       mergin-client (already present) ...
                     + COPY manager/ /app
                     ENTRYPOINT: migrate, collectstatic, start web + supervisor

docker-compose.yml   services: manager (ports 8080, volume /data), postgis
                     (optional; most installations already have one)

environment          DATABASE_URL           registry + default sync database
                     DBSYNC_MANAGER_SECRET_KEY   Fernet key for stored secrets
                     DJANGO_SECRET_KEY
                     ALLOWED_HOSTS
                     SMTP_*                  optional
                     DBSYNC_TICK, DBSYNC_CHECK_INTERVAL   optional
```

`/data` holds per-project work dirs (db-sync checkouts), generated configs
and logs. It is the only state outside PostgreSQL; losing it means re-init,
not data loss.

Upgrading db-sync = bumping the `FROM` line; the patch directory is empty once
#163 is merged upstream.

## 11. What stays outside the tool (Arkeologikonsult specifics)

The manager must not know anything about archaeology. Our own pieces attach
through hooks and live in our `ak-db` repository:

- `post_init` hook: `run_sql.sh <prefix> all` (core model: indexes, helper
  functions, warning triggers, views), `grants.sql` (read-only roles,
  `ak_dbsync` USAGE on the project schema), `import_lookups.sh`.
- `pre_remove` hook: `pg_dump -n <prefix>*` to the archive share.
- Project close (sync → post-processing mode: drop base schemas, move tables
  into the project schema, tighten constraints, lock the Mergin project) is
  *not* a manager function. It is `akdb project close` in our own CLI, which
  calls the manager's API to remove the sync first and then does the rest.

This split is what makes the manager shareable and lets us swap it for
Lutra's rework later without touching our data model.

## 11b. Relation to the proof-of-concept artefacts (`ak-db/poc/`)

What carries over from the POC, and what does not:

- `poc/patch/0001-*.patch`, `poc/patch/dbsync.py` — **reuse as is.** Applied at
  image build time until upstream ships the fix.
- `poc/check_sync.sh` — **reuse the logic, not the script.** Its three parts map
  directly onto §6 step 4: feature count per table in the GeoPackage vs the
  `modified` schema; the two-consecutive-checks debounce; and the log
  classification (`AK-VALIDERING`/`GEODIFF: NOTICE` are not errors, `pending
  changes on server` is not an error). Port to Python in the supervisor; the
  shell script stays in `ak-db` as the standalone cron check for installations
  without the manager.
- `poc/reserv/gen_dbsync.py` — **do not reuse.** It splits one config into
  one container per GeoPackage, which is the workaround for the unpatched
  db-sync and is wrong with the patch. The only idea worth keeping is that a
  db-sync config is *generated from a register*; in the manager the register
  is the `dbsync_manager` schema, not a hand-written YAML.
- `poc/run_sql.sh`, `sql/grants.sql`, `poc/import_lookups.sh` — **not part of
  the manager.** They are Arkeologikonsult's `post_init` hook (§11).
- `poc/docker-compose.yml` — the PostGIS service definition (ICU locale, role
  init script, healthcheck) is a reasonable template for the optional
  `postgis` service in the manager's compose file; the `dbsync` service is
  replaced by the manager.

## 12. Security notes

- No Docker socket. The container runs as a non-root user; `/data` is owned by
  it.
- Secrets encrypted at rest; per-project config files mode 600; never in the
  UI, API responses or events.
- The database role used by the manager for the registry is separate from the
  roles used by syncs (which are stored as data). Least privilege: the registry
  role has CREATE on the database and owns `dbsync_manager` only.
- CSRF and session security are Django defaults. The API uses per-user tokens.
- The UI should sit behind the organisation's reverse proxy with TLS; the
  container serves plain HTTP on 8080.

## 13. Test plan

- Unit: config generation from `sync_project` + `sync_connection`; log
  classification rules (NOTICE ≠ error; `pull finished` parses version);
  divergence rule with the two-consecutive-checks debounce; hook environment.
- Integration (docker compose with a throwaway PostGIS and a test Mergin
  project): add → init completes → status shows equal counts; edit in Mergin →
  counts diverge for one check then converge; pause → server version advances,
  local does not; resume → catches up; remove with drop → schemas gone;
  post_init hook receives the documented environment.
- Regression for #163: a project with three GeoPackages, edits in all three
  within one sync cycle, all three applied.
- Chaos: kill the child mid-pull; restart the container mid-init; make the
  database unreachable for two ticks. Expected: state `error` with a readable
  event, automatic recovery, no duplicate rows.

## 14. Roadmap

v1 (this document): everything above except SSO and the CLI.
v1.1: OIDC login; per-project notification channels beyond e-mail (webhook).
v2: driven by Lutra's rework — if db-sync gains a structured status channel or
a one-way mode, the manager consumes them and drops its own heuristics.

## 15. Open questions

- Should `add` verify that the chosen Mergin account has at least Reader on
  the project *before* creating anything? (Yes, probably — one API call.)
- Feature counts per table in the add wizard require downloading the
  GeoPackages. Acceptable as an explicit "preview" step, or skip in v1?
- Where should the two-way warning sit: on add (blocking, with an "I know"
  checkbox) or only on the project page?
- Name. `dbsync-manager` is descriptive; Lutra may prefer something that does
  not imply it is theirs.
