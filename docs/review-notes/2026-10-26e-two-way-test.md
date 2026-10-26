# 2026-10-26e: the two-way test (slice 17 part B) — what db-sync does when both sides edit

*Written by Cowork-Claude from the test run on 2026-10-26 afternoon on
the test clone (two-way: the Mergin account owns the project, the
database role owns the `modified` schema; daemon sleep 10 s). Database
edits by SQL, field edits through the Mergin Maps QGIS plugin, the
project paused to create the concurrent window. Seven cases; every one
has an outcome. Two of them contradict what slice 17a and the README
assumed, and one has no record at all.*

## Outcomes

| Case | Database side | Field side | Result (both sides after the cycle) | Record |
|---|---|---|---|---|
| T2 attribute | `UPDATE` one column, one row | — | pushed as a new version 18 s later; field sees it after sync; `base` = `modified` again | — |
| T3 new feature | `INSERT` with fid, uuid, geometry | — | pushed; appears in the field with the same uuid | — |
| T3b fid collision | `INSERT` fid 630 | new feature, also fid 630 | rebase: the field's row keeps 630, the database's row is renumbered to 631 and pushed; both exist on both sides | none (not a conflict for geodiff) |
| T6a same row, different columns | column B | column A | both values on both sides | none |
| **T6b same row, same column** | value D | value F | **D on both sides: the database wins**, pushed as a new version; F is gone from the field | conflict file + warning event; `old` = F (the overwritten value), `new` = D |
| T6c field deletes, database updates | `UPDATE` row | delete row | row gone on both sides; the database's update is dropped, nothing pushed | conflict file ("1 deleted on the server") + warning event |
| **T6d field updates, database deletes** | `DELETE` row | `UPDATE` row | row gone on both sides, the delete pushed; **the field's update is lost** | **no conflict file, no event** |

## The rules, as they actually are

1. A delete wins over an update, whichever side deletes.
2. Same row, same column: the side that syncs last wins (Mergin Maps'
   documented rule, "the editor who syncs last wins"; no timestamps).
   In a db-sync setup the database is *always* the last to sync in a
   concurrent edit: the daemon pulls the field's version first (that is
   the rebase) and pushes its own change on top. So **the database wins
   every same-column conflict**, and the field's value is overwritten on
   both sides.
3. Different columns of one row, and new rows (fids renumbered), merge.
4. Records: a same-column conflict and a database update of a
   field-deleted row are written to the conflicts file and surfaced by
   the manager (slice 17a). A field update of a database-deleted row
   leaves no record anywhere; geodiff does not count it as a conflict.

Two side findings: a database writer must match the layer exactly
(the point layer is PointZ; a 2D `ST_MakePoint` is refused by
PostGIS), and nothing the field form would have filled in (defaults,
expressions) is filled in for a database insert.

## Fixes for 17b

1. **Event text and README have the wrong direction.** Slice 17a wrote
   "database values dropped (server wins)". Replace with: same column →
   "field value overwritten by the database value; the overwritten
   value is in the conflict file"; field-deleted row → "database update
   dropped: the row was deleted in the field". The `old`/`new` naming in
   the file is geodiff's; the event should say which side each is.
2. **README "Two-way sync"**: the four rules above, verbatim, and the
   recommendation that follows from rule 2 and 4: during active field
   work keep the project one-way, or make sure no database writer
   touches rows the field may edit. A field crew's correction can be
   silently replaced by an office edit, and an office edit of a row the
   field deletes is silently dropped.
3. **T6d detection.** The manager can detect the undetectable case
   itself: in `pull` with rebase, a row that the database deleted since
   `base` and that the server's changeset updates. Both facts are in
   hand (the `base→modified` changeset db-sync computes before the pull,
   and the `base→their` changeset after). Compare the two for delete
   vs update on the same fid and log a warning event "field update of
   fid N lost: the row was deleted in the database" with the field's
   values from the `their` changeset. Reading the two temp changesets
   is enough; no new geodiff call.
4. Offer upstream: the conflicts file is per project, not per
   connection (a second rebase in the same cycle overwrites the first);
   and update-vs-delete with the delete on the db-sync side should be
   recorded. One issue with both.

## State after

Clone at v46; rows 629 and 631 deleted, 630 (field) present; fid 2 and
3 carry test values. Nothing to clean except the test rows if the clone
is kept. Two conflict files in `conflicts/`, both as expected.

## Applied (slice 17b, 2026-10-26)

| Fix | Done |
|---|---|
| 1 direction | `conflicts.py` names the sides (`field` = geodiff's `old`, overwritten; `database` = `new`, stands); the event reads "N field values in <layer> (<columns>) overwritten by the database values, fid …" and "N database updates of rows deleted in the field in <layer> dropped with the row, fid …"; page, api.md, CHANGELOG and CLAUDE.md corrected; the API keeps `deleted_on_server` as an alias of the new `deleted_in_field` (v1 never removes a field) |
| 2 README | the four rules verbatim, the recommendation, and the two side findings for database writers |
| 3 T6d detection | patch `0003`: db-sync's `pull()` compares `base2our` (delete) with `base2their` (update) by row key before the rebase and logs `WARNING - field update lost: table … row (column 0 = N) was deleted in the database; the update from Mergin Maps (…) is dropped with the row`; logtail makes that one WARNING line a `warning` event (other WARNING lines stay errors). Two `geodiff as-json` calls per rebasing pull, inside db-sync, so it is per connection and needs no new file |
| 4 upstream | `patches/UPSTREAM.md`: one db-sync issue with both points (lost update, per-project file name) and the PR text and test for 0003; the geodiff angle is named in the issue |

Review note: `2026-10-26f-slice17b.md`.

