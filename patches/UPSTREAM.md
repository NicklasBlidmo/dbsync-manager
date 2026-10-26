# Offering patch 0002 upstream

Patch `0002` is the manager's only local change to db-sync. This file
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
