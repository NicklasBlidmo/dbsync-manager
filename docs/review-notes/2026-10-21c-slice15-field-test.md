# 2026-10-21c: slice 15 field test (content divergence, verify, API)

*Written by Cowork-Claude from the field test on the production server
the afternoon slice 15 (`f52d16d`) was delivered. Four projects, deploy
without pausing (graceful stop since slice 14). Everything in the brief
works; nothing to fix, two observations.*

## What passed

- **Deploy**: migration `0014_content_diff_tables_verify` applied, all
  four projects `ok` within the first check.
- **A, content check cost**: 44–67 ms per project over 6–8 connections
  (largest production project 67 ms). Running it in every check is
  free; `DBSYNC_CONTENT_CHECK` stays on.
- **B, Verify on a clean clone**: operation `verify` done in 0.7 s, eight
  "in sync", daemon untouched (no stop events).
- **B, Verify with a planted deviation**: one row changed by hand in the
  live `base` schema, nothing else touched. Verify: `1-punkter: 0
  inserted, 1 updated, 0 deleted`, the other seven in sync,
  `checkout_inconsistent` active, health `failed`, one alert mail. The
  daemon kept running and saw nothing itself (it compares only after a
  fresh download), which is exactly why the operation exists.
- **14b regression**: row restored, Verify again → eight in sync,
  condition resolved, recovery mail, health `ok` at once.
- **E, API**: `GET /api/v1/projects/` without a token → 401. With a
  token created in the admin (full value shown once on save, prefix only
  in the list afterwards): `GET /api/v1/projects/3/` → `project` and
  `tables`; each table entry has the GeoPackage, modified and base
  schema, `checked_at`, and per layer name, schemas, geometry column,
  `synced` and `skipped_by`. This is the structure the installation's
  registry will read instead of deriving schema names.

## Observations

1. The web port is bound to the server's LAN address, so `curl` against
   `localhost` returns nothing and `json.tool` reports "Expecting
   value". `docs/api.md` should say "use the address the UI is served
   on" in the first example, and the examples should print the status
   code (`-w "%{http_code}"`).
2. Pasting a token into `read -s` is error-prone over SSH; the docs'
   recipe can be `cat > ~/.apitoken` (paste, Enter, Ctrl-D), then
   `TOKEN=$(tr -d '\r\n' < ~/.apitoken)`.

## Not tested

A daily Verify from a scheduled hook through the API (needs a token in
the hook environment; decide where it lives before using it), and
`manage.py verify --project`. Both are thin wrappers over what was tested.

## Applied (slice 16, 2026-10-26)

| Item | Done |
|---|---|
| Observation 1 | `docs/api.md`: use the address the UI is served on; the examples print the status code |
| Observation 2 | `docs/api.md`: token into `~/.apitoken` (paste, Enter, Ctrl-D), `TOKEN=$(tr -d '\r\n' < ~/.apitoken)` |
| Not tested items | unchanged; a daily Verify through the API and `manage.py verify` stay thin wrappers |

