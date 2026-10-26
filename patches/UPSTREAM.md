# Offering the patches upstream

Patch `0002` first (filed), then patch `0003` and the conflicts-file issue (below, to file).

*Filed 2026-10-26: issue MerginMaps/db-sync#193, pull request #194 from
`NicklasBlidmo/db-sync:graceful-stop-on-sigterm` (three commits on 2.4.0:
the patch, the test, a black pass).*

Patch `0002` was the manager's only local change to db-sync until slice 17b added `0003`. This file
holds the issue text, the pull request text and the test change that go
with it, so filing it is copy and paste (there is no GitHub CLI on the
development machine; #187 was filed from the fork
`NicklasBlidmo/db-sync` the same way). Once filed, put the links in
`README.md`'s table.

## Steps

```sh
git clone git@github.com:NicklasBlidmo/db-sync.git && cd db-sync
git remote add upstream https://github.com/MerginMaps/db-sync.git && git fetch upstream --tags
git checkout -b graceful-stop-on-sigterm 2.4.0
git am ../dbsync-manager/patches/0002-Stop-gracefully-on-SIGTERM.patch
# apply the test change below, run pytest test/test_daemon.py, commit, push, open the PR
```

## Issue

**Title:** SIGTERM kills db-sync mid-pull; proposal: finish the current connection and exit between cycles

**Body:**

`dbsync_daemon.py` installs no SIGTERM handler, so a `docker stop`,
`docker compose up -d` after a rebuild, or a service manager restart ends
the process wherever it happens to be. If that is inside `pull()` between
"GeoPackage updated" (the checkout is at the new version) and "base
schema updated" (the changeset has been applied to the database), the
checkout and the `base` schema are at the same version with different
content, and the next start refuses with

```
The db schemas already exist but 'base' schema is not synchronized with source GPKG. Running `dbsync_deamon.py` with `--force-init` should fix the issue.
```

`--force-init` then drops and rebuilds the schemas, which loses any
not-yet-pushed database edits and any views or grants on the schemas.
We hit this in production with four projects and a routine image
rebuild (one daemon was pulling 48 files at that moment).

Proposal, implemented in the PR that follows: a SIGTERM handler that only
sets a flag; `dbsync_pull()`/`dbsync_push()` check it before each
connection; `run_daemon()` checks it at the top of the cycle and between
pull and push; the sleep polls it once a second. A pull or push that has
started always completes; the process exits 0 at the next boundary.
SIGINT keeps its default so ctrl+c still kills immediately. A container
needs `stop_grace_period` long enough for one connection's pull
(we use 150 s).

## Pull request

**Title:** Stop gracefully on SIGTERM: finish the current pull or push first

**Body:**

Fixes #<issue>.

- `dbsync.request_stop()` / `dbsync.stop_requested()`: a module flag,
  set by the SIGTERM handler that `dbsync_daemon.main()` installs.
- `dbsync_pull()` and `dbsync_push()` return before the next connection
  once a stop was requested, after the current connection has completed.
- `run_daemon()` exits 0 at the top of the cycle and between pull and
  push when the flag is set. The exit is a `SystemExit`, which passes
  through the cycle's `except Exception`.
- `sleep_unless_stopped(seconds)` replaces the single `time.sleep()` in
  the loop with one-second steps, so a stop during the (up to one hour)
  retry wait takes effect within a second.
- No change to `--single-run`, to SIGINT, or to Windows (no SIGTERM there).

The patch has run in production (dbsync-manager, four projects) since
2026-10-20: five `docker compose up -d --force-recreate` during active
field edits, every daemon logged "stop requested ... exiting cleanly",
no `--force-init` needed.

Test: `test/test_daemon.py` gains one test, and its `run_daemon` fixture
mocks `dbsync_daemon.sleep_unless_stopped` instead of
`dbsync_daemon.time.sleep` (same fake, same assertions).

## Test change for `test/test_daemon.py`

In the `run_daemon` fixture:

```python
        mocker.patch("dbsync_daemon.sleep_unless_stopped", side_effect=fake_sleep)
```

and the new test:

```python
import signal


@pytest.fixture(autouse=True)
def no_stop_request():
    dbsync._stop_requested = False
    yield
    dbsync._stop_requested = False


def test_stop_request_during_pull_finishes_it_and_exits_before_push(run_daemon, dbsync_mocks):
    """SIGTERM only sets a flag (dbsync.request_stop); the daemon completes the step it is in,
    does not start the next one and exits with code 0 instead of dying mid-pull"""
    pulled = []

    def pull(mc):
        dbsync.request_stop(signal.SIGTERM, None)   # the signal arrives while pulling
        pulled.append("done")                       # ...and the pull still completes

    dbsync_mocks.pull.side_effect = pull

    sleeps, exited = run_daemon(iterations=3)

    assert exited
    assert pulled == ["done"]
    assert sleeps == []                             # no retry wait: a stop is not a failure
    dbsync_mocks.push.assert_not_called()


def test_sleep_unless_stopped_returns_early(mocker):
    sleep = mocker.patch("dbsync_daemon.time.sleep")
    dbsync_daemon.sleep_unless_stopped(3)
    assert sleep.call_count == 3
    dbsync.request_stop(signal.SIGTERM, None)
    dbsync_daemon.sleep_unless_stopped(600)
    assert sleep.call_count == 3
```

`exited` is `True` for any `SystemExit`; the exit code 0 is asserted in
dbsync-manager's own test (`supervisor/tests_patches.py`), which loads the
patched modules under stubs.

---

# Patch 0003 and the conflicts file (slice 17b, to file)

*From the two-way test of 2026-10-26 (`docs/review-notes/2026-10-26e-two-way-test.md`).
Two findings, one db-sync issue: the per-project conflicts file name, and
the one concurrent case that is recorded nowhere. The second finding has
its root in geodiff (its rebase records our-update-vs-their-delete but not
our-delete-vs-their-update); the db-sync issue says so and proposes the
db-sync-side report that patch `0003` is, because db-sync has both
changesets in hand before the rebase and geodiff's change would be a
format change of the conflicts file. A geodiff issue can follow if the
maintainers prefer the fix there.*

## Steps

```sh
cd db-sync   # the fork, with upstream and 2.4.0 as for PR #194
git checkout -b report-lost-field-updates 2.4.0
git am ../dbsync-manager/patches/0003-Report-field-updates-lost-to-a-database-delete.patch
# add the test below to test/test_basic.py or a new test/test_rebase_report.py, black, commit, push, open the PR
```

## Issue

**Title:** Rebase: a field update of a row deleted in the database is dropped without any record; and the conflicts file is per project, not per connection

**Body:**

We ran the concurrent-edit cases of a two-way setup (db-sync 2.4.0,
geodiff 2.3.0, PostgreSQL driver, one GeoPackage, edits in the database
by SQL and in the field through the QGIS plugin, the daemon paused to
create the window). Outcomes:

| database | field | result | record |
|---|---|---|---|
| update column B | update column A, same row | both values on both sides | — |
| update value D | update same column, value F | D on both sides (the database syncs last and wins) | conflicts file, `old` = F, `new` = D |
| update row | delete row | row gone on both sides, the update dropped | conflicts file (entry without `old`) |
| **delete row** | **update row** | **row gone on both sides, the field's update lost** | **nothing** |

Two things we would like to change:

1. **The last case leaves no record.** `geodiff rebase-db` keeps our
   delete over their update (consistent with "a delete wins", fine) but
   writes nothing to the conflicts file for it, so the field crew's edit
   disappears without a trace: not in the database, not in the file, not
   in the log. The information is available in `pull()` before the
   rebase: `base2our` (computed before `mc.pull_project`) holds our
   delete, `base2their` (computed after) holds their update. Matching the
   two by row key and logging one WARNING per lost update is a small
   change in db-sync; PR follows. Recording it in the conflicts file
   instead would be a geodiff change (the rebase's `_handle_delete`
   records only the opposite direction), which we can propose there if
   you prefer.

2. **The conflicts file is named per project**:
   `<tmp>/<project name>-dbsync-pull-conflicts`. With several connections
   (several GeoPackages of one project, which 2.4.0 supports with one
   working directory per connection), a second rebase in the same cycle
   overwrites the first connection's file before anyone can read it.
   Naming it `<project name>-<modified schema>-dbsync-pull-conflicts`,
   like the working directory, keeps every connection's record. We read
   these files (dbsync-manager keeps them and shows them per project), so
   the name matters to us; the same goes for `-dbsync-pull-base2our` /
   `-base2their`.

## Pull request

**Title:** Report field updates that a database delete drops during rebase

**Body:**

Fixes the first point of #<issue>.

- `_entry_row_key(entry)`: the values that identify a changeset entry's
  row (an update carries only an old value for its key columns; a delete
  carries every old value).
- `_lost_field_updates(ours, theirs)`: delete entries of ours matched
  against update entries of theirs by table and key.
- `_report_lost_field_updates(base2our, base2their)`: one WARNING per
  lost update with the row's key and the values the server had; a
  geodiff failure is logged and never breaks the pull.
- `pull()` calls it right before `_geodiff_rebase`. Two `geodiff as-json`
  calls per rebasing pull, nothing else changes; no change for pulls
  without rebase.

Verified on the setup above: the case now logs
`field update lost: table punkter row (column 0 = 631) was deleted in the
database; the update from Mergin Maps (column 3: 'ny text') is dropped
with the row`.

## Test

```python
import logging

import dbsync


def test_lost_field_updates_by_row_key(caplog, mocker):
    """A row deleted in the database (ours) that the server's changeset (theirs) updates is reported
    before the rebase drops it; other combinations are silent"""
    ours = [
        {"table": "t", "type": "delete", "changes": [{"column": 0, "old": 631}, {"column": 1, "old": "x"}]},
        {"table": "t", "type": "update", "changes": [{"column": 0, "old": 5}, {"column": 1, "old": "a", "new": "b"}]},
    ]
    theirs = [
        {"table": "t", "type": "update", "changes": [{"column": 0, "old": 631}, {"column": 1, "old": "x", "new": "field"}]},
        {"table": "t", "type": "update", "changes": [{"column": 0, "old": 7}, {"column": 1, "old": "q", "new": "r"}]},
        {"table": "u", "type": "update", "changes": [{"column": 0, "old": 631}, {"column": 1, "old": 1, "new": 2}]},
    ]
    assert dbsync._lost_field_updates(ours, theirs) == [("t", {0: 631}, [(1, "field")])]
    assert dbsync._lost_field_updates([], theirs) == []

    mocker.patch("dbsync._geodiff_list_changes_details", side_effect=[ours, theirs])
    with caplog.at_level(logging.WARNING):
        dbsync._report_lost_field_updates("base2our", "base2their")
    assert [r.message for r in caplog.records] == [
        "field update lost: table t row (column 0 = 631) was deleted in the database; "
        "the update from Mergin Maps (column 1: 'field') is dropped with the row"
    ]
```

