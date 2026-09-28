# 2026-09-28b: daemons crash in step after a container restart (geodiff temp files)

*Written by Cowork-Claude from the field, same day. Slice 8 (`e161f3d`) was
deployed at 10:20 with `docker compose up -d --build`; both projects went
`process crashed` and stayed there. Slice 8 is not the cause; the restart
was the first time two projects were started in the same tick.*

## Symptom

`docker compose logs manager`, every tick after start:

```
10:20:22,095 started db-sync for Arkeologikonsult/3922_Bönsta, pid 94
10:20:22,924 started db-sync for NicklasBlidmo/p3922_test, pid 96
10:20:53,193 db-sync for Arkeologikonsult/3922_Bönsta exited (exit code 1); restart in 30s
10:20:53,202 db-sync for NicklasBlidmo/p3922_test exited (exit code 1); restart in 30s
10:21:23,248 started ... (restart 1)   ← both again in the same tick
10:21:53,290 exited ... restart in 60s ← and so on, forever
```

`dbsync.log` of both projects, at the same millisecond in both files:

```
10:20:24,118 -ERROR - GEODIFF: Error: Failed to create a copy of modified source for driver postgres
Failed to insert row to gpkg_geometry_columns table (SQLITE3 error [1032]: attempt to write a readonly database)
10:20:24,130 -ERROR - GEODIFF: Error: Failed to create a copy of modified source for driver postgres
Failure initializing spatial metadata (SQLITE3 error [1]: unknown error: 10)
['geodiff', 'diff', '--driver-1', 'sqlite', '', '--driver-2', 'postgres', '...', '/tmp/riLhmPSJ']
```

`/tmp` afterwards: ~35 empty files with eight-letter names, owned by
`manager`, mode 644.

## What was ruled out (in order)

- Disk and inodes: 19 GB and 2.6 M inodes free.
- `/tmp` permissions: `drwxrwxrwt`, writable as `manager`.
- Compose, Dockerfile, s6 scripts: no diff since `3c0b0c6`.
- Host: kernel unchanged since 2026-08-02, Docker not restarted, base
  image `mergin-db-sync:2.3.0` is the same digest as before.
- apt package drift (`gdal-bin` pulls libsqlite3/libspatialite): geodiff
  links its own SQLite (`pygeodiff.libs/libsqlite3-*.so`), so system
  libraries are irrelevant.
- SQLite on overlayfs: both Python's `sqlite3` and
  `pygeodiff.GeoDiff().make_copy_sqlite()` write fine into `/tmp` and
  `/data` as `manager`.

## Cause

SQLite error 1032 is `SQLITE_READONLY_DBMOVED`: the file at the path is no
longer the inode SQLite opened. Two processes were using the same
`/tmp/<8 letters>` file. geodiff names its temporary copies with a
pseudo-random string; two daemons started in the same second get the same
seed and therefore the same sequence of "random" names. From then on each
pull cycle in the two daemons creates, truncates and unlinks each other's
copies. The supervisor's restart backoff keeps them in step (both restart
in the same tick), so the loop never resolves.

It had not happened before because `p3922_test` and Bönsta were started
days apart, and single restarts (2026-09-24) only touched one daemon.

Confirmed by pausing `p3922_test`: Bönsta started alone at 10:38:28,
pulled eight files, and was still running at the next tick
(`log for Arkeologikonsult/3922_Bönsta: {'runs': 8, ...}`). Resuming the
test project in a later tick gave two healthy daemons.

(Claude Code: please confirm the seeding in geodiff's source when
implementing; the behaviour is established, the exact line is not.)

## Fix for the supervisor

1. **Per-daemon `TMPDIR`.** In `_start`, set `TMPDIR=<project work dir>/tmp`
   (create it, `0700`) in the environment of the db-sync subprocess. geodiff
   and Python's `tempfile` honour `TMPDIR`. Colliding names in different
   directories are harmless, and the directory is removed with the project
   (`delete_workdir`). Also pass it to hooks for the same reason.
2. **Stagger starts.** When a tick starts more than one daemon, sleep at
   least 1.1 s between spawns. Cheap insurance for any other time-seeded
   randomness in db-sync.
3. **Clean the directory on start.** Empty the per-project `tmp/` before
   spawning; a crashed daemon leaves copies behind (35 files in 5 minutes
   here).
4. Test: a `_reconcile` test that starts two projects in one tick and
   asserts distinct `TMPDIR` values in the spawn environment and the delay
   between the two spawns (patch `time.sleep`).
5. Consider the restart backoff: after N failures in step, a small random
   jitter on the restart delay would break the lock-step even without 1
   and 2. Optional.

## Operational notes

- Until the fix is deployed: after any container restart with more than
  one project, pause all but one, wait for it to run through a tick, then
  resume the others one tick apart. The Bönsta page should show eight pulls
  in "Pulls and pushes" at the version it was at.
- Empty leftovers in `/tmp` inside the container can be ignored; they go
  with the next image rebuild.
- The zombie `[python] <defunct>` rows in `ps` are exited daemons not yet
  reaped by the supervisor; they disappear at the next tick.
- Slice 8 itself has been in production since 10:20 with no other finding
  so far; the UI review continues separately.

## Addressed (Claude Code, same day)

Implemented as proposed: `processes.private_tmpdir()` creates and empties
`<work dir>/tmp` (mode 700) before every spawn and the daemon runs with
`TMPDIR`/`TMP`/`TEMP` set to it; hooks get the same directory; two spawns
in one tick are at least 1.1 s apart (`Supervisor._stagger`); the restart
backoff gets 0-5 s of random jitter. `tests_spawn.py` goes through
`_reconcile` for two projects in one tick and asserts distinct `TMPDIR`
values, the emptied directory and the sleep between the spawns.

**Seeding, checked in the sources (db-sync 2.3.0, geodiff 2.3.0).** The
colliding names are not seeded by the second; they are worse than that:

- geodiff: `randomTmpFilename()` = `tmpdir() + "geodiff_" + randomString(6)`
  with `rand()` and **no `srand()` anywhere**, so every geodiff process
  draws the identical sequence from the start. `tmpdir()` reads `TMPDIR`
  with `/tmp/` as default. Two daemons that cycle in step are at the same
  point of the sequence and pick the same names.
- db-sync: `_compare_datasets()` names its changeset `random.choices(ascii_letters, k=8)`
  (Python's `random`, seeded from `os.urandom`, so those eight-letter names
  differ between processes), but `_geodiff_...details/summary` use the
  **fixed** names `dbsync-changeset-details` and `dbsync-changeset-summary`
  in `tempfile.gettempdir()`, which collide between any two concurrent
  daemons regardless of timing.

Both `tempfile.gettempdir()` and geodiff's `tmpdir()` honour `TMPDIR`, so
the per-daemon directory removes every collision; the stagger and the
jitter only make the lock-step less likely to recur on the way there.
