# 2026-10-26d: slice 17 part A, rebase conflicts made visible

*Written by Claude (coding session) after implementing part A of the
brief `2026-10-26c`. Part B, the two-way test on the clone, is run
together with Nicklas afterwards and gets its own note; part C's README
section is written from the code and the brief's background and is to
be corrected from B's outcome. Nothing here has run against Docker or a
database: 187 tests pass on the development machine, the db-tagged ones
and the field items are for gis-ubuntu.*

## What db-sync and geodiff actually do (read before coding)

- `pull()` (db-sync 2.4.0, `dbsync.py` 860–950): `base2our` is the
  changeset base → modified. If it is not empty the pull "needs rebase":
  after `mc.pull_project()` it runs `geodiff rebase-db --driver postgres
  <conn> <base> <modified> <base2their> <conflicts>` where the conflicts
  path is `<tempfile.gettempdir()>/<project name>-dbsync-pull-conflicts`.
  `tempfile.gettempdir()` honours `TMPDIR`, which the supervisor sets per
  daemon to `/data/projects/<id>/tmp` (review note 2026-09-28b). So the
  file is ours.
- geodiff 2.3.0 (`geodiff.cpp` 500–562): the conflict file is removed
  before the rebase and written only when the conflict list is not
  empty. After a rebase: file present ⇔ there were conflicts.
- The JSON (`changesetutils.cpp conflictsToJSON`):
  `{"geodiff": [{"table", "type": "conflict", "fid": "<pk as text>",
  "changes": [{"column": <position>, "base", "old", "new"}]}]}`. In
  `rebase-db` "theirs" is the server's changeset and "ours" the
  database's, so `old` is the server's value (which stands) and `new`
  the database's (dropped). An update that met the server's delete has
  no `old` (`_handle_delete`: "our UPDATE conflicts with their DELETE:
  record as conflict, delete wins"). Column names are not in the file,
  only positions; neither is a uuid.
- The log says nothing usable: "Applying new version [WITH rebase]" is
  DEBUG and the daemon logs at INFO; geodiff's "No conflicts present"
  is debug too. The file is the signal.
- One file name per project, not per connection: a second rebase in the
  same cycle overwrites the first before the supervisor's next tick.
  Documented in CLAUDE.md; a per-connection name upstream would be a
  one-line change in `pull()` and can ride with PR #194's follow-up.

## What was built

- `supervisor/conflicts.py`: `parse` / `summarize` / `name_columns` /
  `describe` (pure), `save` (move to `conflicts/<YYYYmmddTHHMMSS>-<sync
  file>.json`, suffix `-2` on a name clash, keep the newest 50),
  `list_saved` / `overview` / `safe_name` for pages and API, and
  `collect(project, tmpdir, work_dir)`: every waiting file is read; an
  empty or non-JSON one is dropped; the connection is found from the
  last check's table list (`SyncStatus.tables`), column positions become
  names from the modified schema when the database answers (a nicety,
  never required), then
  - two-way (or unknown direction): one `warning` event from source
    `dbsync`, "rebase: N conflicts in <layer> (<columns>), M deleted on
    the server; fid …: database values dropped (server wins); see
    conflicts/<name>", plus a mail through `alerts.notify` when the
    project's `mail_on_conflict` is on;
  - one-way (`one_way` or `db_edits_cannot_push`): no warning; instead
    `content_divergence:<file>:<table>` at error level with its mail,
    the brief's "same condition as content divergence". The check keeps
    it while base and modified differ and resolves it when they agree.
    With `DBSYNC_CONTENT_CHECK=false` the check cannot see it and
    resolves it at the next check (one alert, one recovery mail); noted
    in the code, acceptable for a case that should never happen.
- `Supervisor._collect_conflicts` runs from `_observe` every tick and in
  `_start` right before `processes.private_tmpdir` empties TMPDIR, so a
  file from the last cycle before a stop survives. `_paths` gained
  `"tmp"`.
- `SyncProject.mail_on_conflict` (migration 0017), in the admin's "Hooks
  and alerts" fieldset. Default off.
- Project page: "Conflicts" section (collapsed; count and latest time in
  the heading) with time, GeoPackage, layer, count, deleted-on-server
  count, fids, and a download link for operators
  (`/projects/<id>/conflicts/<name>`, name validated against the saved
  pattern; viewers get 403).
- API: `conflicts: {last_at, count_30d}` on every project (from file
  names, no parsing), `GET /api/v1/projects/<id>/conflicts/` (newest
  first, per-table counts and fids) and `.../conflicts/<name>/` (the
  file as geodiff wrote it). `docs/api.md` updated.
- README "Two-way sync" (part C, 5): the four rules and the installation's
  recommendation. Rule 3's second half ("field updates, database
  deletes") is left for B to fill, as the brief says we cannot predict
  it from the code.

## Verified here

187 tests without a database, among them: parse/summarize/describe
against a file in geodiff's shape (update, two-column update, update vs
server delete, second table); save keeps 50 of 53 and suffixes a clash;
collect: two-way → warning without mail, with mail when the flag is on,
one-way → the two conditions at error level, unknown connection → the
project name, empty file dropped; the observe step calls collect with
the project's tmp dir; `_start` saves the file before emptying TMPDIR.
`makemigrations --check`, Django check, the project template compiles,
the db-tagged modules import.

## For gis-ubuntu (with part B)

1. `docker compose --profile test run --rm test`: new are
   `ConflictCollectionTests` (supervisor), `ApiTests.test_conflicts_list_and_file`,
   `PagesTests.test_project_page_lists_conflict_files_and_operators_download`.
2. T6b on the clone is the first real file: confirm the name in
   `/data/projects/<id>/tmp/` before the tick moves it (`ls` within 30 s
   of the pull), then the event text, the section, the download, and
   the API's `count_30d`. Compare the event's fid with the row edited.
3. Column names: the event should say `(namn)` rather than
   `(column #3)`; if it does not, the modified schema's column order
   differs from geodiff's and the mapping must be checked before trusting
   it.
4. T6c/T6d: whether a database update of a field-deleted row appears
   as "deleted on the server" (no `old` in the file), and what the other
   order produces; README rule 3 gets the answer.
5. Logtail: watch for `GEODIFF:` error events during the rebase cycles
   (db-sync logs geodiff's stderr at ERROR); if geodiff's debug lines
   land there, add them to `logtail` as noise.
6. `mail_on_conflict` on the clone for one T6b: one mail with subject
   "alert rebase_conflict", then off again.

## Not in this slice

uuid in the event text: the file has only fids; a lookup in `modified`
by key is possible (the check knows the key and uuid column from
`content.plan`) and worth adding if the field test shows fids are hard
to use. Conflict resolution from the UI: later, if asked.
