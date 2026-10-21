# Slice 15 brief: content divergence, and a first read-only JSON API

*Written by Cowork-Claude 2026-10-21 from the installation's architecture
work (its "assignment to the manager" note of 2026-10-15, revised today).
This is the first slice that comes from downstream consumers rather than
from operations, so a word on who they are: the installation is building
its own tooling around the manager — a CLI (`akdb`) that will later
orchestrate a project's transition from "syncing" to "post-processing"
through the manager, and a QA tool (MerginQA) that validates field data
by reading the synced database and must never report "0 errors" against
a database that has silently fallen behind the field. Both need to read
facts from the manager by HTTP, and both need to know, per project, which
schemas hold which GeoPackage layers. Nothing in this slice is specific
to that installation; a generic manager needs the same two things.*

## A. Content divergence base ↔ modified (cheap, every check)

What exists: `check.py` compares row counts per table between the
GeoPackage, `base` and `modified`, raises `divergence:<label>:<table>`
and friends, and health follows (`evaluate`). Counts miss an UPDATE: same
number of rows, different content.

1. In `gather`, for each connection and each selected table, compare
   `base` and `modified` on `fid` (the GeoPackage primary key; take the
   name from `gpkg_contents`/`PRAGMA table_info` rather than assuming):
   rows only in base, rows only in modified, rows in both with different
   content. Attributes column by column; geometry as byte equality on
   the EWKB (`ST_AsEWKB`), which is robust to SRID and dimension
   differences in representation. One SQL per table pair, `FULL OUTER
   JOIN` on the key, `IS DISTINCT FROM` per column; no row data pulled
   into Python except the sample below.
2. Result per table: three counts, plus a sample of at most 200 rows with
   `fid`, the value of a `uuid` column if the table has one, the
   deviation type, and the names of the differing columns. Stored on the
   project's check result like the counts are today (not as events), so
   the page and the API read the same thing.
3. Interpretation follows `direction_of`: in a one-way project every
   deviation is someone writing into `modified` directly (the condition
   name `stranded_db_edits:` exists already for the count case; extend
   it or add `content_divergence:<label>:<table>`), level `error`, mail
   like the count divergence. In a two-way project deviations are normal
   between push cycles: show them, no condition. One `info` event when
   the first deviation appears and one when it clears, with the counts,
   so the history is visible.
4. Cost control: skip the comparison for a table when counts already
   differ (the count condition covers it), and cap the sample. Add a
   setting `DBSYNC_CONTENT_CHECK=true` to switch it off per installation.
   Measure on the largest production project and state the time in the
   review note.

## B. Content verification GeoPackage ↔ base (on demand)

The pair that reveals a failed apply (checkout advanced, database not) is
the checkout GeoPackage against `base`, and the comparison db-sync does
itself when it says `'base' schema is not synchronized with source GPKG`.
db-sync runs it only after a fresh download; the manager should be able
to run it on request.

5. An operation `verify` (the `Operation` machinery from slices 5–7:
   restart-safe, logged, blocked while another operation runs) that, for
   each connection, exports `base` to a temporary GeoPackage and runs
   geodiff's comparison the way db-sync's init check does (reuse db-sync's
   own functions from the vendored source rather than reimplementing;
   `dbsync.py` has `_compare_datasets`-style code behind the message
   above). The daemon keeps running; the comparison reads only.
6. Result: `ok`, or per table a count of differing rows; on any
   difference raise `checkout_inconsistent` exactly as slice 14 does for
   the daemon's own detection (same condition, same mail, same Re-init
   path).
7. Triggers: a **Verify** button on the project page (operator), and an
   optional daily run from a scheduled hook — no new scheduling code: the
   hook calls the API endpoint in E with its token, or a management
   command `verify --project`. The installation's `close` flow will call
   it through the API before moving tables.

## C. Table list per project

8. The data exists (`SyncConnection.sync_file`, `modified_schema`,
   `base_schema`, `gather` reads the GeoPackage's table list and
   geometry columns). Expose it as one structure per project: for each
   connection, the GeoPackage path, and for each synced layer: layer
   name, modified schema, base schema, geometry column (or null), and
   whether it is skipped by `skip_tables`/`include_tables`. Used by the
   project page (a collapsed "Tables" section) and by the API.

## D. Sync state per project

9. Same structure, read-only: Mergin server version, checkout version,
   database version (the base comment), time of the last successful
   pull confirmed by the database (the `logtail.close_confirmed_pulls`
   fact), time of the last attempt, direction, desired/process state,
   health, active conditions with their since-times, count and content
   divergence from A, last verification from B. Nothing new is computed;
   this is what the project page already shows, as data.

## E. A first JSON API, read-only

The installation's tools will read C and D by HTTP; the `close` flow
(a later slice) will need write actions under the same authentication.
Keep it small and boring:

10. `GET /api/v1/projects/` (list with D per project) and
    `GET /api/v1/projects/<id>/` (D plus C). Plain Django views returning
    JSON, no framework. Versioned path from day one.
11. Authentication: a model `ApiToken` (name, hashed token, user FK,
    created, last_used, revoked), created in the admin, shown once.
    `Authorization: Bearer <token>`. The token acts as its user, so the
    existing operator/viewer roles apply; the event log will say
    "by <tool name>, user X" when write actions come. Read endpoints
    need a valid token; no anonymous access even on the LAN.
12. `POST /api/v1/projects/<id>/verify/` (operator) starts B and returns
    the operation id; `GET /api/v1/operations/<id>/` reports it. The
    only write action in this slice; it establishes the pattern for
    pause/resume/remove later.
13. `docs/api.md`: endpoints, the JSON shapes with one real example per
    endpoint, token handling, and the stability promise ("fields are
    added, never renamed or removed within v1").

## F. Tests and acceptance

- Unit (no DB): the content comparison SQL on a small fixture with each
  deviation type; one-way raises the condition, two-way does not; the
  sample cap; the skip when counts differ.
- DB-tagged: the comparison against a real PostGIS with a geometry
  column, SRID set, 3D geometry; verify against a checkout where `base`
  was altered by hand (the slice 14 field recipe) → `checkout_inconsistent`.
- API: token required; viewer can read, cannot verify; JSON shapes match
  `docs/api.md`.
- Field: on the largest production project, time of one check with A
  enabled stated in the review note; Verify on the test clone, clean
  and after the slice 14 provocation; a `curl` against both GET
  endpoints with a token from the admin.

## Applied

Review note `2026-10-21b-slice15.md` (same day), with the field items
that remain: timing of the content check on the largest project, Verify
on the clone clean and after the slice 14 provocation, `curl` against
the two GET endpoints.

## Not in this slice

Pause/resume/remove through the API, `abort_on_failure` on hooks, a
webhook on pull — these are the `close` slice. Ten connections per
project needs no code if nothing assumes eight; just confirm in the note.
