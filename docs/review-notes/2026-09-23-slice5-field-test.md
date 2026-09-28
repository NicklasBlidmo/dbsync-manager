# Slice 5 field test, 2026-09-23

Reviewer: Claude (Cowork session with Nicklas), commit `fe0809d` on
`gis-ubuntu` with `p3922_test`. Everything below passed as designed; the
note exists for the two design questions at the end and for the record of
what was actually exercised.

## Hooks

| Step | Result |
|---|---|
| `post_init` hook `list schemas` (`psql -v ON_ERROR_STOP=1 -c '\dn' -c 'select current_user, version()'`), "Run post_init now" | ok in 0.1 s; psql connected through `PGHOST`/`PGUSER` and the per-run pgpass file, no password in the command or output; `current_user = ak_dbsync`; `post_init_ok_at` set |
| `post_init` hook `fail on purpose` (`echo "simulated failure" >&2; exit 3`), "Run post_init now" on the running project | run recorded as failed (exit 3) with the stderr text; the project kept running. Correct: a manual run is a repair action and never blocks a working project |
| Same hook as the configured `post_init` with `post_init_ok_at` reset to NULL (SQL), project resumed | project went `blocked` on the next tick with "post_init hook has not succeeded; use 'Run hook now' or re-initialise"; no daemon started |
| Hook switched back to `list schemas`, "Run post_init now" | run ok, `post_init_ok_at` set, daemon started on the following tick |

Not exercised: `post_pull`, hook timeout, the automatic `post_init` after a
real init (needs a new project or slice 6 re-init).

## E-mail (Microsoft 365 direct send, `docs/m365-direct-send.md`)

`SMTP_HOST=<mx-prefix>.mail.protection.outlook.com`, port 25,
STARTTLS, no credentials, `SMTP_FROM=dbsync-manager@<your-domain>`,
`DBSYNC_BASE_URL=http://192.0.2.10:8090`. `manage.py sendtestemail`
delivered. Then the `db_behind` provocation from 2026-09-22 (base-table
trigger, one point with `Beskrivning = 'db_behind'`): one mail when the
condition activated after two check intervals, one on `resolved` after
pause/resume, both with the project link. The container sends through the
host's address, so the firewall rule for gis-ubuntu covers it.

## Design questions raised, for §8

1. **Which role runs the hook.** Hooks run as the project's *sync* role
   (`PGUSER` = the database row's user). That is right for the sync and
   wrong for the organisation's actual `post_init`: Arkeologikonsult's
   `run_sql.sh` / `grants.sql` create views, functions and grants in the
   project schema, which `ak_admin` owns, and `pre_remove` will run
   `pg_dump`. Options: (a) a second database row ("ak (admin)") selected
   per hook, (b) a `hook_database` field on the project, (c) give the sync
   role CREATE on the database. (c) breaks the single-writer rule (the sync
   role must own nothing outside the sync schemas). Suggest (b), or (a) as
   a field on the hook itself, so that the hook carries the organisation's
   privileges and the sync keeps the minimum.
2. **§8 wording.** "Non-zero: ... for `post_init` the project is marked
   `error`" describes the automatic run only. State the two paths
   explicitly: automatic `post_init` after init blocks the project on
   failure; a manual "Run hook now" never blocks and only unlocks on
   success. The hook table should show which path ran each hook (after
   init / post_pull / requested by `<user>`).

## Also needed before the first real `post_init`

The organisation's scripts are not in the image. A read-only bind mount in
the server-local `docker-compose.override.yml` (`/opt/ak-db:/opt/ak-db:ro`)
makes `run_sql.sh`, `sql/` and `import_lookups.sh` available without a
rebuild; the hook then reads `SQLDIR=/opt/ak-db/sql` and
`$DBSYNC_SCHEMA_PREFIX`. `psql` is in the image since slice 1;
`import_lookups.sh` needs `ogr2ogr`, which is not, so lookups stay a
manual step or the image gains `gdal-bin`.

Server state after the test: `DBSYNC_CHECK_INTERVAL` back to 300, trigger
and test hooks removed, `post_init` on `p3922_test` = `list schemas`.
