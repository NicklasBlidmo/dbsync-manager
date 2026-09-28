# Bönsta takeover, 2026-09-25

Reviewer: Claude (Cowork session with Nicklas), commit `5dbd033` (slice 7)
on `gis-ubuntu`. The first production project: `Arkeologikonsult/3922_Bönsta`,
synced by hand (`ak-dbsync`, POC) since 2026-09-17, adopted by the manager
at 16:35 without re-initialisation. Health `ok` at the first complete check.

## Sequence and observations

| Step | Observation |
|---|---|
| Inventory | Eight base comments `{"name": "Arkeologikonsult/3922_Bönsta", "version": "v5383"}`; `check_sync.sh` all OK; the POC's Mergin account is the same private account the clone used, Reader on Bönsta |
| Backup | `pg_dump -Fc -n 'p3922_bonsta*'` from inside `ak-postgis`, 17 schemas, 1.2 MB, plus `config.yaml`, in `/srv/backup/ak/` |
| Permission | `project_info`: `role: reader`, `permissions: {delete, update, upload: False}`; `permission()` → `reader`. First real Reader in the field |
| Stop | `docker compose --profile sync stop dbsync`; base comments then at v5384; `check_sync.sh` cron commented out (its checkout freezes and would alarm falsely) |
| Wizard | Preview: eight rows `adopt … at v5384`, `one_way`, writers none, ack "the daemon that used to sync them must be stopped first". The `ö` in the comment is decoded before comparison, so the match on `ö` works |
| Adopt | `dbsync.log`: each of the eight connections downloaded the project at v5384 (the base comment), then pulled v5384 → v5387 applying `1-polygoner.gpkg` and `2-grupper.gpkg` (the field's edits during the stop). No init, no `CREATE SCHEMA`. Base comments v5387 in the database within a minute |
| Check | First check ran before the checkouts existed (finding 7 of the slice 6 note, again); `health: ok` at the next one, direction one-way |

§15's adoption rule is thereby confirmed against real data: db-sync's own
init handles existing schemas with a matching base comment, and the manager
needs nothing beyond accepting them.

## Findings

### 1. The wizard's database choice does not follow the search results (bug; fixed in `3c0b0c6` together with finding 2)

`add/start.html`: the `database` select is inside `#pick` and is included in
the search request, but the "Choose" links are rendered with the value at
search time. Changing the database *after* the results are shown leaves the
links pointing at the first database in the list ("AK Admin" sorts before
"gis-ubuntu"). Steps 2 and 3 then inherit it silently; the preview showed
"Database: AK Admin", `db_edits_cannot_push` and "writers found: ak_dbsync"
for a project whose sync role *is* `ak_dbsync`. Caught by reading the
preview. Fix: `hx-get`/`hx-trigger="change"`/`hx-include="#pick"` on the
database select, as on the workspace select, or make "Choose" a form submit
that reads the select's current value. Had it gone through, the daemon
would have run as `ak_admin` (superuser), which §12 forbids.

### 2. Sleep time defaults to 10 s

Fine for a test, wrong for a field project: 8 connections × 6 cycles per
minute against the Mergin API. Default 60.

### 3. Check before checkout (repeat)

Same as slice 6 note finding 7: the first check after add/adopt reports
eight `[Errno 2] No such file or directory` cards until the checkouts
exist. Skip connections without a checkout while `initializing`.

## Server state

- Manager: project 4 `Arkeologikonsult/3922_Bönsta`, database `gis-ubuntu`
  (`ak_dbsync`), account Nicklas-Privat (Reader), sleep 60, no hooks yet,
  alerts to nicklas.blidmo@arkeologikonsult.se. `p3922_test` removed
  (registry row kept); the clone still exists in Mergin, to be deleted.
- POC: `ak-dbsync` stopped, not removed; `/opt/ak-db/dbsync/` and
  `work/3922_Bönsta` kept for one week as the way back (pause in the
  manager, `rm -rf work/3922_Bönsta`, `docker compose --profile sync start
  dbsync`, cron back). `check_sync.sh` cron commented out.
- Follow-ups: a company service account (Reader) instead of the private
  one; `post_init` hook `ak post_init.sh` with run-as `AK Admin` once
  `run_sql.sh` has been confirmed idempotent (`fyll_relationer.sql` is);
  archive share bind-mount for `pre_remove`; after a week, remove the POC
  `dbsync` service, its work dir and `check_sync.sh`.
