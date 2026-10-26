# Slice 17 brief: two-way sync made visible — rebase conflicts, and a documented two-way test

*Written by Cowork-Claude 2026-10-26, as part of getting the manager ready
to publish. The manager has run one-way projects in production for a
month; its only two-way project is the test clone, used for restart and
Verify tests, never for what two-way is for. Before the README promises
two-way to other installations, two things: the manager must show what
db-sync does when both sides change, and we must have run that case and
written down what happened.*

## Background: what db-sync does in a two-way project

- Mergin → database: pull downloads the GeoPackage diff, geodiff turns it
  into a changeset, applied to `base` and `modified`.
- Database → Mergin: push diffs `base` against `modified`, applies the
  changeset to the checkout GeoPackage, pushes a new Mergin version, then
  applies it to `base`.
- Both sides changed between two cycles: `pull()` sees `base ≠ modified`,
  pulls the server version and runs `geodiff rebase-db`: the database's
  changes are rewritten on top of the server's. Different rows, or
  different columns of one row, merge. The same column of the same row
  is a conflict: the server's value stands, the database's value is
  dropped and written to a conflicts file in the daemon's temp dir
  (`<project>-dbsync-pull-conflicts`); update-vs-delete goes the same
  way; inserted rows with colliding fids get new fids. What survives is
  then pushed. Nothing is lost silently *in the data model*, but the
  person who edited the database is never told that their value lost.

## A. Rebase conflicts become visible

1. The conflicts file is written to `TMPDIR` and discarded. The daemon's
   stdout/log does name the rebase ("Applying new version [WITH rebase]")
   and geodiff logs the conflict count. Make the manager keep it: the
   supervisor's per-daemon `TMPDIR` is ours, so after each pull cycle
   (or on the log line) move any `*-dbsync-pull-conflicts` file to
   `/data/projects/<id>/conflicts/<timestamp>-<connection>.json` and
   keep the last 50.
2. One `warning` event per rebase with conflicts: "rebase: N conflict(s)
   in <layer>, database values dropped (server wins); see conflicts/…",
   with the fid/uuid list from the file when it is short. No condition
   and no mail by default — in a two-way project this is expected
   behaviour — but a per-project setting `mail_on_conflict` (default
   off) for installations where a dropped database edit is news.
3. Project page: a "Conflicts" section (collapsed, count in the heading)
   listing the saved files with time, layer, count, and a download link
   (operator). API (`/api/v1/projects/<id>/`): `conflicts: {last_at,
   count_30d}` and a sub-resource for the list.
4. In a one-way project a rebase should never happen (nothing writes
   `modified`). If one does, it is the same condition as content
   divergence in one-way: `error`, mail.

## B. The two-way test, run and documented

Done on the test clone with the Mergin account raised to Writer on it
and the database role allowed to write the connection's `modified`
schema (the installation's docs describe the one-off grant). Record
each step in a review note `…-two-way-test.md`, with the daemon log
lines and the Events rows, in the manager's own terms (no installation
specifics beyond "a point layer").

- **T2, database → Mergin, attribute**: `UPDATE` one attribute of one
  row in `modified`; within a cycle a new Mergin version exists, the
  field client shows the value, `base = modified` again, the manager's
  content check shows the deviation while pending and clear after.
- **T3, database → Mergin, new feature**: `INSERT` a row with geometry
  and a uuid; it appears in Mergin with that uuid and a fid assigned by
  the database; a second insert from the field in the same cycle gets a
  different fid after rebase.
- **T6a, concurrent, different columns**: field changes column A of a
  row, database changes column B of the same row between two cycles
  (pause the project to make the window): after resume, both values
  present on both sides, no conflict file.
- **T6b, concurrent, same column**: same setup, same column: the
  field's value wins on both sides, one conflict file, one `warning`
  event naming the row, the database's value in the file.
- **T6c, update vs delete**: field deletes the row, database updates
  it: row gone on both sides, conflict recorded.
- **T6d, delete vs update**: field updates, database deletes: expect
  the delete to win or the update — record what geodiff does; this is
  the one we cannot predict from the code.
- **Direction flip**: the project's direction badge follows the Mergin
  permission (Reader → one_way, Writer → two_way) within a check; the
  content check's interpretation flips with it.

Acceptance: every case has a one-line outcome in the note, the events
and conflict files for T6b–d are in the UI, and `README.md`'s "Two-way
sync" section states the rules in four sentences (server wins on the
same column; other columns merge; deletes; where to find the conflict
record).

## C. Docs for publishing

5. `README.md`: a "What two-way means" section from B's outcome, and
   the recommendation the installation itself follows: one-way during
   field work unless you need database edits to reach the field, and
   never both a field crew and a database writer on the same rows.
6. `docs/api.md`: the conflicts fields.

## Tests

- Unit: parsing a conflicts file (geodiff's JSON) into the event text;
  the move-and-keep-50 rule; one-way + rebase → condition.
- DB-tagged: a fixture with a prepared conflicts file in the daemon's
  TMPDIR → file moved, event written.
- Field: B, in full, on the clone.

## Not in this slice

Conflict *resolution* from the UI (choosing the database value back): a
later slice if anyone asks; the record is enough to publish.

## Applied (part A, 2026-10-26)

| Item | Done |
|---|---|
| A1 keep the file | `supervisor/conflicts.py`: every tick and before a start the file in the daemon's TMPDIR moves to `/data/projects/<id>/conflicts/<time>-<sync file>.json`, newest 50 kept; geodiff writes it only when there were conflicts |
| A2 event, setting | one `warning` event per file naming layer, columns, rows (fid, up to 12) and the file; `SyncProject.mail_on_conflict` (migration 0017, admin) mails it through the alert recipients |
| A3 page, API | project page section "Conflicts" with download for operators; `conflicts: {last_at, count_30d}` on the project, `GET /api/v1/projects/<id>/conflicts/` and `.../<name>/`; docs/api.md |
| A4 one-way | `content_divergence:<file>:<table>` at error level with mail, kept and resolved by the check |
| C5, C6 docs | README "Two-way sync" from the code and the background (rule 3's second half waits for B), api.md, CHANGELOG, CLAUDE.md |
| Tests | `tests_conflicts` (parse, describe, keep 50, collect outcomes, the supervisor's two call sites), db-tagged `ConflictCollectionTests`, API and page tests |
| Not in the file | uuids and column names: geodiff writes fids and column positions only; names are looked up in the modified schema when the database answers |

Review note: `2026-10-26d-slice17a-rebase-conflicts.md`. Part B is the field test.

