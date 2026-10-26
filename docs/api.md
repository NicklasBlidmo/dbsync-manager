# JSON API v1

Read-only facts about every project, and one write action (verify), for
tools that live next to the manager: a CLI that orchestrates projects, a
QA tool that must not report "0 errors" against a database that has
fallen behind the field. Slice 15 (review note `2026-10-21`).

**Stability promise.** Within v1, fields are added, never renamed or
removed. A new major version gets a new path (`/api/v2/`). Values of
enumerated fields (`desired_state`, `process_state`, `health`, condition
keys) are the same strings the UI shows.

## Authentication

Every request carries a token:

```
Authorization: Bearer <token>
```

Tokens are created under Settings → API tokens (admin). The secret is
shown once, at creation; the row keeps a hash, the first characters for
telling tokens apart, the time of last use, and a revoked flag. A token
acts as its user: the user's group decides what it may do (`viewer`
reads, `operator` also verifies). There is no anonymous access, and a
browser session does not count. Without a valid token: `401` with
`WWW-Authenticate: Bearer`. Without the permission: `403`.

Errors are `{"error": "<message>"}` with the HTTP status (401, 403, 404,
405, 409).

Use the address the UI is served on (`BIND_IP`/`HTTP_PORT` in `.env`;
the port is usually bound to the server's LAN address, so `localhost`
answers nothing). Pasting a token into `read -s` over SSH is
error-prone; put it in a file once instead:

```sh
cat > ~/.apitoken      # paste the token, Enter, Ctrl-D
chmod 600 ~/.apitoken
TOKEN=$(tr -d '\r\n' < ~/.apitoken)
curl -sS -w '\n%{http_code}\n' -H "Authorization: Bearer $TOKEN" http://192.0.2.10:8090/api/v1/projects/
```

The last line printed is the status code; pipe the body through
`python3 -m json.tool` once it is `200`.

## `GET /api/v1/projects/`

Every live project with its state (D). Example (one project, shortened):

```json
{
  "projects": [
    {
      "id": 3,
      "mergin_project": "Arkeologikonsult/3922_Bönsta",
      "mergin_account": "service",
      "database": "ak",
      "schema_prefix": "p3922",
      "desired_state": "running",
      "process_state": "running",
      "process_reason": "",
      "health": "ok",
      "health_since": "2026-10-20T17:42:11+02:00",
      "direction": "two_way",
      "versions": {"server": 7359, "checkout": 7359, "database": 7359},
      "last_pull_confirmed_at": "2026-10-21T08:05:12+02:00",
      "last_attempt_at": "2026-10-21T08:04:50+02:00",
      "last_check_at": "2026-10-21T08:05:12+02:00",
      "active_conditions": {},
      "count_divergence": [],
      "content_divergence": {},
      "last_verification": null,
      "conflicts": {"last_at": null, "count_30d": 0},
      "connections": [
        {
          "sync_file": "1-polygoner.gpkg",
          "modified_schema": "p3922_s_polygoner",
          "base_schema": "p3922_b_polygoner",
          "checked_at": "2026-10-21T08:05:12+02:00",
          "check_error": "",
          "server_version": 7359,
          "local_version": 7359,
          "db_version": 7359,
          "mergin_permission": "writer",
          "direction": "two_way",
          "last_pull_at": "2026-10-21T08:05:12+02:00",
          "gpkg_count": {"polygoner": 1412},
          "base_count": {"polygoner": 1412},
          "modified_count": {"polygoner": 1412},
          "content_divergence": {}
        }
      ]
    }
  ]
}
```

Field notes:

- `versions.checkout` and `versions.database` are the lowest over the
  connections, `versions.server` the highest the last check saw.
- `last_pull_confirmed_at`: the last pull that the check saw applied in
  the database (`db_version` reached the checkout's version); never from
  a log line. `last_attempt_at`: the last pull or push db-sync logged.
- `active_conditions`: condition key → ISO time it became active. Keys:
  `divergence:<file>:<table>`, `db_behind:<file>`,
  `stranded_db_edits:<file>`, `content_divergence:<file>:<table>`,
  `stuck`, `check_failed`, `process_crashed`, `checkout_inconsistent`,
  `database_missing_postgis`, `mergin_unreachable`, `mergin_auth_failed`.
- `count_divergence`: the active keys of the count rules.
- `content_divergence`: per sync file, per table with a difference between
  `base` and `modified`: `only_base`, `only_modified`, `changed`, and a
  `sample` of at most 200 rows with `key` (the GeoPackage primary key as
  text), `uuid` (the value of a `uuid` column, or null), `kind`, and
  `columns` (the differing columns; a geometry column is compared on its
  EWKB bytes). In a two-way project this is normal between pushes; in a
  one-way project it is also the condition `content_divergence:...`.
- `last_verification`: the latest verify operation (below) with `ok`
  true/false once done, or null.
- `conflicts`: rebase conflicts (slice 17): `last_at` of the newest
  saved conflict file (ISO or null) and `count_30d`, the number of files
  from the last 30 days. A conflict file is written when both the field
  and the database changed the same value of the same row between two
  cycles of a two-way project; the server's value stands and the
  database's is in the file. `GET /api/v1/projects/<id>/conflicts/`
  lists them.

## `GET /api/v1/projects/<id>/`

The same object under `"project"`, plus `"tables"` (C): per connection,
the GeoPackage path and every layer with the schemas it syncs into.

```json
{
  "project": {"id": 3, "...": "..."},
  "tables": [
    {
      "sync_file": "1-polygoner.gpkg",
      "modified_schema": "p3922_s_polygoner",
      "base_schema": "p3922_b_polygoner",
      "checked_at": "2026-10-21T08:05:12+02:00",
      "layers": [
        {"name": "polygoner", "modified_schema": "p3922_s_polygoner", "base_schema": "p3922_b_polygoner",
         "geometry_column": "geom", "synced": true, "skipped_by": null},
        {"name": "lookup_period", "modified_schema": "p3922_s_polygoner", "base_schema": "p3922_b_polygoner",
         "geometry_column": null, "synced": false, "skipped_by": "skip_tables"}
      ]
    }
  ]
}
```

`layers` comes from the GeoPackage's `gpkg_contents` as the last check
read it (`checked_at`); `skipped_by` is `skip_tables`, `include_tables`
or null. A table that is synced lives as `<modified_schema>.<name>` and
`<base_schema>.<name>`.

## `GET /api/v1/projects/<id>/conflicts/`

The saved rebase conflict files of a project, newest first (the last 50
are kept):

```json
{
  "conflicts": [
    {"name": "20261027T101502-1-punkter.gpkg.json", "at": "2026-10-27T10:15:02+01:00",
     "connection": "1-punkter.gpkg", "count": 2,
     "tables": {"punkter": {"count": 2, "fids": ["17", "41"], "deleted_on_server": 0}}}
  ]
}
```

`GET /api/v1/projects/<id>/conflicts/<name>/` returns one file as
geodiff wrote it: `{"name": ..., "geodiff": [{"table", "type":
"conflict", "fid", "changes": [{"column": <position>, "base", "old",
"new"}]}]}` where `old` is the server's value (which stands) and `new`
the database's (dropped); a change without `old` is a database update of
a row the server deleted.

## `POST /api/v1/projects/<id>/verify/`

Operator. Starts the `verify` operation (B): for every connection the
checkout GeoPackage is compared with the `base` schema by geodiff, the
way db-sync's own init check does, read-only while the daemon keeps
running. Returns `202` with the operation; `409` with the open operation
when another one is running on the project. The supervisor runs it on
its next tick.

```json
{"operation": {"id": 41, "kind": "verify", "status": "pending", "step": "", "steps": [], "error": "",
               "params": {}, "result": null, "requested_at": "2026-10-21T09:00:00+02:00",
               "requested_by": "akdb", "finished_at": null}}
```

A difference raises `checkout_inconsistent` exactly as the daemon's own
detection does (one mail, health `failed`, Re-init on the project page).

A daily verification needs no new scheduling: a scheduled hook with
```sh
curl -fsS -X POST -H "Authorization: Bearer $DBSYNC_API_TOKEN" http://127.0.0.1:8080/api/v1/projects/3/verify/
```
(the token in the hook's command or environment), or
`manage.py verify --project 3` from cron.

## `GET /api/v1/operations/<id>/`

```json
{"operation": {"id": 41, "kind": "verify", "status": "done", "step": "verify",
               "steps": [{"name": "verify", "started_at": "...", "finished_at": "...", "ok": true,
                          "detail": "checkout and base agree for every connection", "attempt": 1}],
               "error": "", "params": {},
               "result": {"checked_at": "2026-10-21T09:00:31+02:00", "ok": true, "seconds": 2.4,
                          "connections": {"1-polygoner.gpkg": {"ok": true, "tables": [], "error": ""}}},
               "requested_at": "...", "requested_by": "akdb", "finished_at": "..."}}
```

`kind` is `remove`, `reinit`, `verify` or `migrate` (the one-off checkout
layout migration for db-sync 2.4.0; its `result` is null).
`result.connections.<file>.tables` lists `{table, insert, update, delete}`
for tables where the checkout and `base` differ (geodiff's summary);
`error` says why a connection could not be compared (no checkout yet,
geodiff failure). `status` is `pending`, `running`, `done`, `failed` or
`cancelled`; `result.ok` is the verdict once `done`.
