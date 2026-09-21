# dbsync-manager — design document

Web UI and supervisor for Mergin Maps DB Sync: set up, monitor, pause and remove
synchronisations between Mergin Maps projects and PostGIS, packaged as one
Docker container.

Status: v0.2.1 draft, 2026-09-21. Author: Nicklas Blidmo (Arkeologikonsult), with Claude.
v0.2.1: the consistency rules in §6 are now grounded in db-sync 2.3.0's
actual pull order and the 2026-09-16 POC incident (see §15).
v0.2 incorporates internal review: the consistency check is now stated as
"no divergence detected" rather than "verified" (§6), the direction check
shows facts instead of a verdict (§7), hooks have stricter secret, failure
and idempotence rules (§8), and recovery/backup is spelled out (§10). Process
health and restart-safe operations are deferred to v0.3 (§15).
Written in English so it can be shared with Lutra Consulting as input to the
planned db-sync rework (Q4 2026). The Arkeologikonsult-specific parts are kept
out of the tool and described in §11.

---

## 1. Why

DB Sync works, but running it is manual: one YAML file per daemon, log files,
no view of what is actually in sync, and no safe way to add or stop a project
without editing config and restarting containers. We found three failure modes
in our proof of concept that the tool does not surface on its own:

- an error while applying a changeset to the database (in our case a trigger
  on the `modified` schema) aborts the pull after the base schema has been
  updated but before `modified` has; the checkout is already at the new
  version, db-sync reads the base/modified difference as a local edit and
  keeps trying to push it, and nothing in the log says that the database is
  behind;
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
  count in GeoPackage vs in the database. The counts detect the most common
  silent divergence (a dropped changeset); they cannot prove that content is
  equal, and the UI says "no divergence detected", never "verified" (§6).
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
- **State is computed; history is parsed.** Everything that decides a
  project's state or triggers an alert comes from sources of truth — the
  Mergin API, the `.mergin` metadata in the checkout, the comment db-sync
  writes on the base schema, `SELECT count(*)`, and sqlite on the GeoPackage.
  The run history (`sync_run` rows for pull/push cycles) and the classified
  event lines *are* derived from db-sync's text log, because that is the only
  channel db-sync has. They are diagnostic: shown to the operator, never used
  to decide state or to send alerts. If the log parser breaks, the operator
  loses history, not correctness.
- **Consistency checks use the sync's own database credentials.** The
  registry role has no access to synced schemas (§12), so the supervisor
  opens a separate, read-only connection with the `database` row referenced
  by the project for counts, schema comments and grant inspection. It runs
  only `SELECT`s and catalogue queries on that connection.

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
permission on each project (from the API) so the operator sees whether the
sync can write to Mergin at all, but the permission itself is set in Mergin.
Same for the database role: grants are set in PostgreSQL, the manager only
inspects and reports (§7).

Secrets: Mergin passwords/tokens and database passwords are stored encrypted
at rest with a key from the environment (`DBSYNC_MANAGER_SECRET_KEY`, Fernet).
They are written unencrypted only into two places, both in the container's
private volume with mode 600: the generated per-project config file, and a
per-run `pgpass` file for hooks (§8). They never appear in the UI, the API,
events or captured hook output; hook output is masked before it is stored.

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
                    post_init_hook_id, pre_remove_hook_id, post_pull_hook_id,
                    post_init_ok_at,   -- null until the post_init hook has
                                       -- succeeded; see §8
                    notify_emails[], created_by, created_at, updated_at

sync_connection     id, sync_project_id, sync_file (points.gpkg),
                    modified_schema, base_schema, skip_tables[], include_tables[]
                    -- one row per GeoPackage; mirrors one db-sync "connection"

sync_run            id, sync_project_id, kind (init|pull|push|hook|check),
                    started_at, finished_at, ok, summary (jsonb)
                    checkout_version (pull rows: the vN from the log)
                    -- pull/push rows are opened from the child's log (§6)
                    -- and are diagnostic only; a pull row gets ok = true
                    -- only when the check sees db_version reach
                    -- checkout_version, never from a log line. Hook and
                    -- check rows are written by the supervisor itself, the
                    -- row is created before the work starts and finished after

sync_status         sync_project_id, sync_connection_id,
                    server_version, local_version, db_version,
                    gpkg_count, base_count, modified_count  (jsonb per table),
                    mismatch_streak (jsonb per table: consecutive checks
                                     with gpkg_count != base_count),
                    mergin_permission (reader|writer|owner|none),
                    db_writers (jsonb: roles other than the sync role that
                                can write to any synced table, incl. PUBLIC),
                    direction (one_way|two_way|db_edits_cannot_push),
                    last_pull_at, last_error_at, last_error,
                    checked_at, check_error (null when the check itself ran;
                                             otherwise why it could not)
                    -- one row per connection, overwritten each check; the
                    -- streak counters are the only state carried between checks

event               id, sync_project_id, at, level (info|warning|error),
                    source (supervisor|dbsync|hook|user), message,
                    condition (nullable key such as "divergence:<table>",
                               used for alert de-duplication, §6),
                    user_id (when a person did it)

hook                id, name, kind (post_init|pre_remove|post_pull), command,
                    timeout_s, created_by
```

Everything a person does (add, pause, remove, change credentials) is an
`event` with `user_id`. Everything the machine does that matters is an
`event` without.

## 6. Supervisor

Process ownership: the container runs two processes under `s6-overlay`, the
web server (gunicorn) and `manage.py supervise`. If either exits, s6 restarts
it; if s6 itself cannot keep them up, the container exits and Docker's restart
policy applies. The supervisor holds a session-level `pg_advisory_lock` on a
fixed key in the registry database for its whole lifetime. A second supervisor
against the same registry (an accidental second container, or a developer
running one locally) blocks on that lock and logs a warning instead of
starting children, so every project has exactly one owner at any time. The
web server never starts or signals children; it only writes `desired_state`
and events, which the supervisor picks up on the next tick.

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
4. When a project is `initializing` and every connection's base schema
   comment has appeared: run the `post_init` hook if one is configured (§8).
   On success set `post_init_ok_at` and `state = running`. On failure stop
   the child, set `state = error`, and do not restart it until the hook has
   succeeded (operator: "run hook now") or the project is re-initialised.
5. Every `check_interval` (default 5 min) run the **consistency check** per
   connection and write `sync_status`. The check uses the project's own
   `database` credentials, read-only (§3):
   - `server_version`: Mergin API `GET /v1/project/<ws>/<name>` → `version`.
   - `local_version`: `.mergin/mergin.json` in the checkout.
   - `db_version`: the comment on the base schema (db-sync writes
     `{"name": ..., "version": "v123"}` there after each applied pull).
   - `gpkg_count`: `SELECT count(*)` per table via sqlite3 on the checkout's
     GeoPackage (read-only, `?mode=ro`).
   - `base_count`: `SELECT count(*)` per table in the base schema.
   - `modified_count`: `SELECT count(*)` per table in the modified schema.
   - `mergin_permission`, `db_writers`, `direction`: see §7.
   - If any of these cannot be obtained (API down, checkout missing, database
     unreachable) the row is written with `check_error` set and the previous
     counters are kept; no divergence is reported from a failed check.

   What the three sources mean. This follows from how db-sync 2.3.0's
   `pull()` is ordered (verified in `dbsync.py`): the checkout is pulled
   first (`.mergin/mergin.json` advances), then the changeset is applied to
   the **base** schema, then to the **modified** schema, and only after both
   have succeeded is the comment on the base schema written
   (`_set_db_project_comment`, last statement in `pull()`). A `DbSyncError`
   anywhere in between leaves the comment at the old version. Therefore:

   - `local_version > db_version` that persists means a pull was interrupted
     after the file pull. This is the primary detector, and it catches the
     interruption wherever it happened.
   - **GeoPackage vs base** must be equal once `local_version == db_version`.
     A difference means base itself fell behind (the #163 case unpatched, or
     an apply-to-base that failed).
   - **Modified vs base** is normally the set of edits made directly in the
     database that db-sync has not yet pushed ("pending database changes",
     legitimate in two-way mode). But it is *also* what a half-applied pull
     looks like: in our POC incident base was applied, the apply to modified
     failed on a trigger, and the two schemas ended up with the same rows but
     different attribute values. db-sync then read that difference as a local
     edit and tried to push it (with a Reader account: HTTP 403, forever).
     The distinguishing signal is `db_version`: pending changes while
     `db_version == local_version` are user edits; pending changes while
     `db_version < local_version` are a broken pull and **must not be
     pushed**.

   Note that GeoPackage vs base would *not* have caught the POC incident
   (they were equal), and neither would GeoPackage vs modified counts (same
   rows, different values). The version rule is what catches it. Comparing
   the GeoPackage with the modified schema, as the POC script does, is
   therefore replaced, not just moved.

   Divergence rule, evaluated per table:
   - `db_version < local_version` for longer than `2 × check_interval` →
     `event(warning, condition="db_behind")`. If in addition any
     `modified_count != base_count`, or the last log lines show a push
     attempt, the event text says "interrupted pull; database differences
     are not user edits" and the project page shows the pending changes with
     that warning instead of the neutral "pending database changes" label.
   - `local_version == db_version` and `gpkg_count != base_count` →
     increment `mismatch_streak[table]`, else reset it to 0. One check may hit
     the window between file pull and database apply, so a streak of 1 is
     ignored. Streak ≥ 2 → `event(warning, condition="divergence:<table>")`.
   - `direction == db_edits_cannot_push` and `db_version == local_version`
     and any `modified_count != base_count` for two consecutive checks →
     `event(warning, condition="stranded_db_edits")`, because those edits can
     never leave the database (§7).

   Known gap: an apply to modified that fails without changing row counts
   and without db-sync raising (which is not how 2.3.0 behaves, but the
   rework may change it) would only be visible through the content-level
   check (§14). Until then the version rule is the safety net and the count
   rules are the backup.

   What the check can and cannot say. Equal counts prove nothing about
   content: an UPDATE that was dropped, or a DELETE and an INSERT in the same
   cycle, leave the counts equal. The project page therefore shows **"no
   divergence detected (counts, versions)"** and never "verified" or "in
   sync". A content-level comparison (GeoPackage vs base schema, per table)
   is a v1.1 spike (§14); it is not trivial because geometry encoding, float
   formatting and datetime representation differ between SQLite and
   PostgreSQL, so it needs a normalised per-row hash on both sides.

   Alert de-duplication: an e-mail is sent when a `condition` first becomes
   active for a project, and a "resolved" e-mail when a later check no longer
   raises it. While it stays active nothing more is sent; the project page
   shows how long it has been active.

6. If a `post_pull` hook is configured and `db_version` has advanced since
   the last check, run it once (§8).

**The registry is the source of truth; the config file is a derived
artefact.** It is regenerated on every start and never edited by hand. Nothing
in the manager reads `config.yaml` back; if the file and the registry disagree,
the registry wins on the next start. This is what makes add and pause plain
database operations plus a process signal. Remove and re-init are multi-step
(stop, archive, drop, restart) and need a restart-safe operation log; that
design is deferred to v0.3 (§15) and until then the v1 implementation treats
them as best-effort with a clear "operation interrupted" event.

Structured output from the child: db-sync logs are text. The supervisor
tails the log file and classifies lines into `sync_run`/`event` with a small
set of rules:

| Line | Comes from | Means | Classified as |
|---|---|---|---|
| `--- pull finished -- at version vN` | mergin-client, `pull_project_finalize()`, inside `mc.pull_project()` | Checkout and `.mergin/mergin.json` are at vN. Nothing has been applied to the database yet | `sync_run(kind=pull)` opened with `checkout_version = N` |
| `Pulled new version from Mergin Maps: vN` | db-sync, still before the apply | Same as above | ignored (duplicate) |
| `Applying new version [no rebase]` | db-sync, just before `_geodiff_apply_changeset` | Apply is starting | noted on the open run |
| `DbSyncError`, `Traceback` | db-sync | The cycle failed; if it was mid-pull the comment was not advanced | run closed `ok = false`, `event(error)` |
| `GEODIFF: NOTICE:` | geodiff relaying a PostgreSQL NOTICE from a trigger | Informational | `event(info)`, **not** error |
| `pending changes on server` | db-sync | Informational | `event(info)` |

db-sync logs **nothing** after a successful apply. There is no log line
that means "the database is at vN"; the only evidence is the comment on the
base schema, written last in `pull()`. A pull run is therefore closed with
`ok = true` only by the consistency check, when it observes `db_version`
equal to the run's `checkout_version`. It is never closed as ok because a
line was seen or because no error followed. This is exactly the confusion
that hid the POC incident: every cycle looked successful in the log, because
the log only ever said that the checkout had advanced.

Summary of the contract: `local_version` is read from `.mergin/mergin.json`
(the "pull finished" line carries the same number and can be used as a
fallback); `db_version` is read from the base schema comment;
`db_version < local_version` is a fact-state, "pull interrupted after the
checkout was updated", not a heuristic; and the log gives history (which
version, when, which error), never state.

This is the only place the log is interpreted, and it is isolated in one
module so it can be replaced by a structured channel if db-sync gains one (a
suggestion for the rework: emit JSON lines on a file descriptor, and log an
explicit "applied vN to database" line after the comment is written).

Init as a background job: `dbsync_daemon.py` runs init itself on start, so
"initializing" is simply the state until the base schema comment appears.
Progress is shown as which connections have their base schema yet.

## 7. Directionality and safety checks

db-sync is two-way by design. Many installations want one-way (field →
database). The manager does not add a one-way mode (that belongs in db-sync,
and is on our wish list for the rework). What it does is show three separate
facts on the project page, and an assessment derived from them that is
labelled as an assessment:

1. **Mergin permission** of the sync account on this project
   (Reader/Writer/Owner), from the API. Reader means db-sync cannot push;
   Writer or Owner means it can.
2. **Database writers**: roles other than the sync role that can INSERT,
   UPDATE or DELETE on any table in the modified schemas. This is inspected
   with `has_table_privilege(role, table, 'INSERT')` etc. for every role in
   `pg_roles` plus `PUBLIC`, and includes table owners and superusers.
   `information_schema.role_table_grants` is *not* used: it omits privileges
   granted to PUBLIC and shows only grants visible to the current role, so it
   cannot establish that nobody else can write.
3. **Pending database changes**: any table where `modified_count !=
   base_count` (§6), i.e. edits in the database that db-sync has not pushed.

Assessed direction:

- **one-way (field → DB)**: permission is Reader and no other writers found.
- **two-way**: permission is Writer/Owner (regardless of database writers).
- **database edits cannot be pushed**: permission is Reader *and* other
  writers exist. This is not "two-way"; it is a configuration where anything
  written to the database is stranded and will eventually block db-sync's
  push. It is shown as a warning, and pending changes in this state raise the
  `stranded_db_edits` alert (§6).

The Reader case is described as *supported one-way* only after the
integration test in §13 has shown that db-sync with a Reader account pulls
indefinitely without erroring on push. Until then the badge reads "one-way
(untested)".

The manager also warns before add if any of the chosen GeoPackages has a
`*(edit conflict*` sibling in the project, since those files will not sync.

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
  `DBSYNC_BASE_SCHEMAS`, `DBSYNC_WORK_DIR`, `DBSYNC_EVENT` (`post_init` |
  `pre_remove` | `post_pull`), `DBSYNC_DB_VERSION`, and for `post_pull` also
  `DBSYNC_PREV_DB_VERSION` (the version at the previous hook run).
- Database access: `PGHOST`, `PGPORT`, `PGDATABASE`, `PGUSER` and
  `PGPASSFILE` pointing at a per-run `pgpass` file (mode 600, deleted after
  the run). **The password is never in the environment**, so `env`, `set`
  or a crashing script that dumps its environment cannot leak it, and `psql`
  and libpq-based tools pick it up without any handling in the hook.
- **Idempotent by contract.** A hook may be run more than once for the same
  event (operator "run hook now", a retry after timeout, a container restart
  in the middle of a run). Scripts must use `CREATE ... IF NOT EXISTS`,
  `GRANT` (which is idempotent), `pg_dump` to a versioned file name, and so
  on. The manager does not try to detect a partial previous run.
- Every run is a `sync_run` row (`kind = hook`) created *before* the command
  starts and finished *after*, so an interrupted run is visible as a row
  with `finished_at` null.
- stdout/stderr captured into `sync_run.summary`, capped at 64 KB per stream
  (head and tail kept, middle elided), and **masked**: every stored secret
  of the project's Mergin account and database, and the contents of the
  pgpass file, are replaced by `***` before the text is written to the
  registry. Masking is by exact string match; a hook that prints a secret
  in a transformed form (base64, URL-encoded) defeats it, and the hook
  contract says not to print connection strings at all.
- Timeout per hook (default 600 s). On timeout the process group is killed
  and the run is recorded as failed.
- Exit code 0 = ok. Non-zero or timeout = `event(error)` and:
  - `post_init`: the project does **not** go to `running`. The supervisor
    stops the child and sets `state = error`; `post_init_ok_at` stays null.
    A post_init hook typically creates the grants, views and validation the
    organisation depends on, and a sync running without them is worse than a
    sync that waits. The operator fixes the cause and uses "run hook now";
    on success the project starts.
  - `pre_remove`: the remove is **aborted** before anything destructive.
    Schemas are not dropped and the project stays in its previous state with
    an error event. An archive that failed must never be followed by a drop.
  - `post_pull`: logged, nothing else changes.

`post_init` is where views, grants, indexes and lookup imports belong.
`pre_remove` is where you dump or archive. `post_pull` is for lightweight
things (refresh a materialized view, ping a QA system). Its promise is
narrow: it runs at most once per check interval, when the check finds that
`db_version` has advanced since the last run. Several pulls between two
checks collapse into one run, and the run happens up to `check_interval`
after the pull, not at the moment of it. `db_version` is written by db-sync
only after a pull has been applied to the database, so an advanced version
does mean a pull happened; the hook is not told which one.

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
  actions: pause / resume / re-init / remove (checkbox: also drop schemas) /
  run hook now. Re-init and remove-with-drop show a confirmation that states
  what will be dropped and, from the last check, how many pending database
  changes per table would be lost (`modified_count - base_count`); if that
  number is non-zero or the last check failed, the confirmation requires
  typing the project name. Remove runs the `pre_remove` hook first and
  aborts on failure (§8).
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
                     DBSYNC_LOG_MAX_MB (default 20), DBSYNC_LOG_KEEP (default 5)
                     DBSYNC_RETENTION_DAYS (default 90)   events and runs
```

Housekeeping. Each project's db-sync log is rotated by the supervisor at
`DBSYNC_LOG_MAX_MB`, keeping `DBSYNC_LOG_KEEP` files; the child is started
with `--log-file` and the supervisor rotates by renaming and restarting the
tail, since db-sync does not rotate itself. `event` and `sync_run` rows older
than `DBSYNC_RETENTION_DAYS` are deleted nightly, except events with
`user_id` (the audit trail), which are kept indefinitely. `sync_status` is
one row per connection and needs no pruning.

**State, backup and recovery.** There are three kinds of state:

| State | Where | Lost means |
|---|---|---|
| Registry (`dbsync_manager` schema) | PostgreSQL | All projects, credentials and history gone; syncs cannot be restarted |
| Synced schemas (base + modified) | PostgreSQL | The data; base can be rebuilt from Mergin, modified cannot if it holds unpushed edits |
| Work dirs, configs, logs | `/data` | Checkouts, which can be rebuilt from Mergin |
| Encryption key | `DBSYNC_MANAGER_SECRET_KEY` in the environment | Every stored credential unreadable; registry must be re-entered |

A backup must include the registry, the synced schemas, `/data` and both
secret keys (`DBSYNC_MANAGER_SECRET_KEY`, `DJANGO_SECRET_KEY`). Backing up
the registry without the key is useless.

Losing `/data` is recoverable without data loss **only if** there are no
database edits that db-sync has not pushed, which is guaranteed in a
one-way (Reader) project and must be checked in a two-way one. The recovery
routine is:

1. Set every project to `desired_state = paused` before starting the
   container against the new empty `/data`, so nothing is re-initialised
   automatically.
2. For each project, compare `modified` with `base` per table (the pending
   database changes count from the last `sync_status` row, or a fresh
   `SELECT` if the registry survived). If zero for all tables, the
   database is at `db_version` with nothing unpushed.
3. Choose the source explicitly per project. **Mergin is source**: run
   re-init from GeoPackage; the modified schemas are dropped and recreated,
   and any pending edits are discarded. **Database is source**: dump the
   modified schemas first, then re-init with `init_from = db`, which makes
   db-sync push the database contents as a new Mergin version.
4. Resume.

Re-init is a destructive operation, never a "repair". The UI says so (§9).

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
  onto §6 step 5: feature count per table; the two-consecutive-checks
  debounce; and the log classification (`AK-VALIDERING`/`GEODIFF: NOTICE`
  are not errors, `pending changes on server` is not an error). One change:
  the script compares the GeoPackage with the `modified` schema, the manager
  compares it with `base` and reports `modified` vs `base` separately, so
  legitimate database edits in two-way projects do not look like divergence.
  Port to Python in the supervisor; the shell script stays in `ak-db` as the
  standalone cron check for installations without the manager and should get
  the same base/modified split.
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
- Secrets encrypted at rest; per-project config files and per-run pgpass
  files mode 600; never in the UI, API responses, events or hook environment;
  captured hook output is masked and capped before storage (§8).
- The database role used by the manager for the registry is separate from the
  roles used by syncs (which are stored as data). Least privilege: the registry
  role has CREATE on the database and owns `dbsync_manager` only.
- CSRF and session security are Django defaults. The API uses per-user tokens.
- The UI should sit behind the organisation's reverse proxy with TLS; the
  container serves plain HTTP on 8080.

## 13. Test plan

- Unit: config generation from `sync_project` + `sync_connection`; log
  classification rules (NOTICE ≠ error; `pull finished` parses the checkout
  version and does *not* close the run as ok; a replay of the 2026-09-16
  POC log segment yields runs with `ok` null, never true, since no comment
  is observed);
  divergence rule with the two-consecutive-checks debounce, including that a
  failed check neither raises nor resets a streak; alert de-duplication
  (one mail on activation, one on resolution, nothing in between); hook
  environment and pgpass file; output masking and capping; writer detection
  including a PUBLIC grant and a table owned by another role.
- Integration (docker compose with a throwaway PostGIS and a test Mergin
  project): add → init completes → status shows equal counts and "no
  divergence detected"; edit in Mergin → counts diverge for one check then
  converge without an alert; pause → server version advances, local does
  not; resume → catches up; remove with drop → schemas gone; post_init hook
  receives the documented environment and no password in `env`.
- **Missed UPDATE** (the POC incident, reproduced): a trigger on a modified
  table that raises on a specific attribute value; edit that attribute in
  Mergin. Expected: base gets the change, modified does not, the comment
  stays at the old version, `.mergin/mergin.json` advances. The check must
  raise `db_behind` within two intervals with the text "interrupted pull",
  show the modified/base difference under that warning and not as user
  edits, and the counts alone must show *no* divergence, which the test
  asserts explicitly to document the gap. With the trigger removed and the
  child restarted, the next pull applies and the alert resolves.
- **Missed UPDATE without an error**: the same, but the change is dropped
  without db-sync raising (simulated by editing modified directly after the
  pull). Expected in v1: "no divergence detected". This is the acceptance
  test for the content-level check in v1.1.
- **Reader account**: a project synced with a Reader account for 24 h of
  edits in Mergin. Expected: every pull applied, no push errors, badge
  "one-way". Until this passes, the badge reads "one-way (untested)" (§7).
- **Stranded database edits**: Reader account plus an INSERT by another role
  in `modified`. Expected: `db_edits_cannot_push` shown, `stranded_db_edits`
  alert after two checks, and whatever db-sync does next (error loop or
  silent skip) recorded as a finding for the rework.
- **post_init failure**: hook exits 1. Expected: child stopped, state
  `error`, `post_init_ok_at` null, no sync until "run hook now" succeeds.
- **pre_remove failure**: archive hook exits 1 during remove-with-drop.
  Expected: schemas still present, project not removed, error event.
- **Recovery with unpushed edits**: two-way project, INSERT in `modified`
  that has not been pushed, delete `/data`, follow the §10 routine. Expected:
  the confirmation dialog shows the pending count, and "database is source"
  ends with the row in Mergin.
- Regression for #163: a project with three GeoPackages, edits in all three
  within one sync cycle, all three applied.
- Chaos: kill the child mid-pull; restart the container mid-init; make the
  database unreachable for two ticks; start a second supervisor against the
  same registry. Expected: state `error` with a readable event, automatic
  recovery, no duplicate rows, the second supervisor blocks on the lock.

## 14. Roadmap

v0.3 (next revision of this document, before implementation starts):
process health separated from process liveness, and restart-safe
multi-step operations for remove and re-init. See §15.
v1: everything above except SSO and the CLI.
v1.1: OIDC login; per-project notification channels beyond e-mail (webhook);
**content-level consistency check** as a spike: a normalised per-row hash
per table computed in both SQLite and PostgreSQL (geometry as WKB, floats
with fixed precision, datetimes as ISO 8601 UTC) and compared between the
GeoPackage and the base schema. Only if the spike shows that the hashes can
be made to agree on unchanged data does it become a feature; the UI label
changes from "no divergence detected" to "content verified at vN" when it
runs.
v2: driven by Lutra's rework — if db-sync gains a structured status channel or
a one-way mode, the manager consumes them and drops its own heuristics.

## 15. Open questions

Decided since v0.1:

- `add` verifies that the chosen Mergin account has at least Reader on the
  project before creating anything (one API call), and shows the permission
  it found so the operator sees the resulting direction (§7) before confirming.

Deferred to v0.3 (raised in internal review, not yet designed):

- **Process health vs liveness.** Upstream `dbsync_daemon.py` catches
  `DbSyncError` inside its loop and keeps running, so a project can be stuck
  for weeks with a live child. `state` currently conflates process status and
  sync health. Planned: split into `desired_state`, `process_state` and
  `health`, add the rule "server version advanced but local/DB version has
  not for longer than N" and a `stale`/`unknown` health when the check could
  not run. Needs verification against the pinned db-sync version first.
- **Restart-safe remove and re-init.** An operation table with per-step
  status (registered → restart prevented → child stopped → pre_remove ran →
  schemas dropped → done), each step idempotent, a project locked while an
  operation is open so two users cannot issue conflicting actions, and a
  uniqueness check on target schemas across projects.

Verified against db-sync 2.3.0 and the POC log (2026-09-16):

- The base schema comment is written last in `pull()`, after both geodiff
  applies, so an interrupted pull leaves `db_version < local_version`. The
  version rule is the primary detector (§6). In the POC incident base was
  applied and modified was not, which db-sync then mistook for local edits
  and tried to push; that is why "pending database changes" is shown with a
  warning whenever `db_version < local_version`.
- The same incident is concrete evidence for the deferred process-health
  item above: the daemon stayed alive for hours retrying a push that
  returned 403, and nothing in v0.2 would have changed the project's state.

Still open:

- Feature counts per table in the add wizard require downloading the
  GeoPackages. Acceptable as an explicit "preview" step, or skip in v1?
- Should the "database edits cannot be pushed" configuration (§7) block
  add with an "I understand" checkbox, or only warn on the project page?
- Name. `dbsync-manager` is descriptive; Lutra may prefer something that does
  not imply it is theirs.
