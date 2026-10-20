# 2026-09-30: slice 13 field test (scheduled hooks) — findings for 13b

*Written by Cowork-Claude from Nicklas's field test on gis-ubuntu, 29–30
September, against the slice 13 brief (`2026-09-29b`). Everything the
slice set out to do works; four small fixes and one re-test remain.*

## What passed

- A scheduled hook runs once at `run_at`, in the first tick after the
  time, with output stored (`hello from Hello` + date) and not run again
  in later ticks.
- Failure → one e-mail; next success → one e-mail; no mail in between.
  Verified twice (exit 3, then timeout).
- **Hooks no longer block the tick.** A `sleep 120` hook started with Run
  now: four `tick:` lines during the run, `ok (120.0s)` after. A second
  Run now during the run is refused in the web UI with a message.
- Timeout 30 s on `sleep 120`: `failed (timed out)` after 30 s, process
  gone, failure mail; recovery mail after the next success.
- **Backup as a scheduled hook** against `ak` with run-as `AK Backup`
  (role `backup`, `pg_read_all_data`): `ok (0.8s)`, a 3.4 MB custom-format
  dump in `/srv/backup/dbsync`, header `PGDMP`, `pg_restore -l` lists the
  schemas. Now scheduled 02:15 with the operator as recipient.
- A second production project (`Arkeologikonsult/3749_nyköping`) was
  added through the wizard's `new` path meanwhile; health ok.

## Findings

1. **Due logic, same day.** The due test compares `last_started_at` with
   today's *date*. Two consequences seen: moving `run_at` later the same
   day never triggers (10:38 run, run_at moved to 09:59 → nothing), and
   the downtime catch-up test could not be verified (Hello had run at
   10:09; run_at set to 10:59, container stopped 10:56–11:00 and again
   11:03–11:06; no run after either start). Fix as proposed on the day:
   due when `now >= today_at(run_at)` and `last_started_at is None or
   last_started_at < today_at(run_at)`. Unchanged schedule → still one
   run per day; downtime → catch-up at first tick; later run_at the same
   day → one more run. Unit test for all three; then **re-run step 6**
   (stop over run_at, start, expect `started (due at …)`).
2. **CRLF in commands.** The admin textarea submits `\r\n`; a multi-line
   command fails on line one with `sh: set: Illegal option -`. Normalise
   line endings in `Hook.clean()`/`save()` and strip `\r` before running.
   The one-line form of the backup command works and is what is in
   production now; `docs/backup.md` may keep the multi-line form once
   this is fixed.
3. **Admin delete button unreadable**: dark red background with the
   forest `--button-fg`. `admin-theme.css`: `.deletelink,
   .deletelink:link, .deletelink:visited, .submit-row a.deletelink:hover
   { color: #fff; }`.
4. **Time format in admin**: `en-us` renders `2:15 a.m.` and `Sept. 30,
   2026`. Set `LANGUAGE_CODE = "en-gb"` in `settings.py` for 24-hour
   times and `30 September 2026`, no other visible change. Our own
   templates already use explicit formats.
5. **Docs**: checking a dump goes through the manager container, where
   `pg_restore` 16 and `/backup` are: `docker compose exec manager
   pg_restore -l /backup/<file> | grep -c ' SCHEMA - '` (plain
   `grep SCHEMA` also counts COMMENT and ACL lines; a pipe through
   `docker exec -i` fails with "did not find magic string"). Add to
   `docs/backup.md`, and note that the backup directory must be owned by
   the container's `manager` uid (`docker compose exec manager id -u
   manager`).

## 13b verified (2026-09-30 13:51)

With the due-logic fix deployed, step 6 passed: `Hello` with `run_at` set
while the container was stopped ran at the first tick after start
(`started (due at 13:50)` at 13:51:23), although it had already run
earlier that day. Findings 1 is closed; 2–5 as delivered in 13b.

## Operational state after the test

- Hook `Hello` can be deleted or kept disabled as a smoke test.
- Hook `Nightly backup`: run_at 02:15, timeout 3600, run-as `AK Backup`,
  notify the operator, one-line command. First unattended run expected
  2026-10-01 02:15; check for `ak-20261001-0215.pgdump` and no mail.
- `docker-compose.override.yml` now exists on the server with the
  `/backup` bind mount and the `/opt/ak-db` read-only mount (the latter
  was documented earlier but had never been created).
- Role `backup` created 2026-09-30 (had only existed in the docs).
- `tools/backup.sh` + cron is no longer the plan for this server.
