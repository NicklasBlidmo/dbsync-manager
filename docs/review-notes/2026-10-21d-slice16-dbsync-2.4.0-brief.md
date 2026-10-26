# Slice 16 brief: db-sync 2.4.0, drop patch 0001, hyphen checkout layout

*Written by Cowork-Claude 2026-10-21. Upstream merged our PR #187 (fix for
#163) on 2026-10-07 in its revised form, and released it in db-sync
2.4.0 on 2026-10-19. The manager still runs 2.3.0 with patch 0001, which
is the PR's first form. This slice moves to the release, removes the
patch, and switches the checkout layout to what upstream chose. It also
has to absorb two other 2.4.0 changes that touch the manager.*

## What changed upstream between 2.3.0 and 2.4.0 (relevant to us)

Read `git log 2.3.0..2.4.0` and the diff of `dbsync.py`, `config.py`,
`dbsync_daemon.py`, `requirements.txt` before writing a line. Found so far:

1. **#187 (ours), revised.** Checkout per connection at
   `<working_dir>/<project name>-<modified schema>` (hyphen), helper
   `name_project_checkout_dir()`; the unused `project_name` argument of
   `init()` is gone. Patch 0001 assumed `<working_dir>/<project name>/<modified
   schema>`; that is what `SyncConnection.checkout_dir`,
   `config_render.WORKING_SUBDIR`, `check.local_version_of`, `verify.py`
   and the tests assume today.
2. **#128 "Download only the sync file".** `_download_project` passes
   `include=[sync_file]` to `mergin-client` (now 0.15.0): a checkout
   holds one GeoPackage plus `.mergin/`, not the whole project. A
   re-download of a connection is now one file instead of the whole
   project (48 files on one production project the other day). Existing
   full checkouts keep working but stay full until re-downloaded.
3. **#190 "Log in again when the server rejects the auth token"** plus an
   `AuthTokenStore` that writes `<working_dir>/.mergin_auth.json`
   (owner-readable, 0600) so the token survives restarts and
   `--force-init`. For the manager: one token file per project under
   `/data/projects/<id>/work/`; it is a secret (masking, never in logs or
   hook output, excluded from anything that lists or copies the work
   dir; `pre_remove` and Remove delete it with the work dir). Fewer
   logins per restart is good news for the daemon churn we have.
4. **geodiff pinned** to a specific release in `requirements.txt`; the
   image tag is now produced from the release ("Docker build: tag from
   release, skip prereleases"). Confirm `lutraconsulting/mergin-db-sync:2.4.0`
   exists on Docker Hub before anything else; if the tag is named
   differently, note it in `Dockerfile`.
5. `clean()` (force-init) now keeps the auth token; `init()` paths around
   `_redownload_project` changed. Patch 0002 (SIGTERM) must be rebased on
   the 2.4.0 main loop; `patches/upstream/` and `*.patched` regenerated;
   `tests_patches.py` must pass against the new sources.

## A. Image and patches

6. `Dockerfile`: `ARG DBSYNC_VERSION=2.4.0`. Delete patch 0001 and its
   row in `patches/README.md` (keep a line saying it shipped in 2.4.0).
   Rebase 0002; the README's "offered upstream" column gets the issue or
   PR link once it is filed (see F).
7. `requirements.txt` comment: still true? mergin-client 0.15.0 and the
   pinned geodiff come from the base image; nothing to add.

## B. Checkout layout in the manager

8. `SyncConnection.checkout_dir` → `work_dir / "work" / f"{project_name}-{modified_schema}"`.
   One function, used everywhere that touches a checkout path
   (`check.py`, `verify.py`, `config_render`, housekeeping, logtail if it
   looks at paths). Grep for `"work"` and `modified_schema` joined to a
   path; there must be exactly one place that knows the layout.
9. `config_render` keeps rendering `working_dir` per project; db-sync
   adds the hyphenated subdirectory itself now, so make sure the manager
   does not also add it.

## C. Migrating existing checkouts

Two ways; the brief chooses the second, the review note states why if
the first turns out necessary.

- *Rename*: `mv <work>/<project>/<modified>` →
  `<work>/<project>-<modified>` for each connection, no network. Keeps
  full (non-sparse) checkouts and `.mergin` metadata written by
  mergin-client 0.13.
- **Re-download (chosen)**: remove the old `<work>/<project>/` tree
  and let db-sync's existing path do the rest: with schemas present and
  no checkout, `init()` re-downloads the connection at the database's
  version (sparse: one file) and compares base with the GeoPackage.
  Clean sparse checkouts, nothing inherited from 0.13. The comparison is
  the same one that bit us on 2026-10-20, so the gate is: **every
  project must pass Verify (slice 15) before the upgrade**, and a
  project that fails gets Re-init instead.

10. Implement as a one-off operation `migrate_checkouts` (or a step in
    the supervisor's first tick after upgrade, guarded by a marker file
    `work/.layout-v2`): for each project, stop the daemon gracefully,
    remove the old tree, start; log one event per project with the
    time of the re-download. Projects are done one at a time so the
    Mergin server sees at most one re-download at a time. The operator
    runs it from the home page or `manage.py migrate_checkouts`; until
    it has run, the supervisor refuses to start daemons and the home
    page says why.
11. `.mergin_auth.json` is created on first login after upgrade; nothing
    to migrate.

## D. Docs

12. `README.md` requirements: db-sync 2.4.0, mergin-client 0.15.
    `docs/backup.md`: the work dir now contains a token file; the dump
    does not include it and a restore needs no action (re-login).
    `CHANGELOG.md`: "db-sync 2.4.0; one checkout per connection is
    upstream; checkouts hold only the synced GeoPackage; patch 0001
    removed; upgrade requires `migrate_checkouts` once".
13. `patches/README.md`: 0001 row removed, 0002 rebased, upstream link.

## E. Tests and field test

- `tests_patches.py` against the 2.4.0 sources.
- Unit: `checkout_dir` for a project with a `/` in its Mergin name and a
  schema with underscores; `local_version_of` on a sparse checkout
  (only `.mergin/mergin.json` and one file).
- DB-tagged: `migrate_checkouts` on a fixture with two projects; the
  marker; refusal to start before it has run.
- Field, on the test instance first (`dbsync-fresh`, port 8091, with
  the clone), then production: Verify on every project → all in sync;
  pull, build, `up -d`; `migrate_checkouts`; each project back to `ok`
  with a sparse checkout (`ls` shows one `.gpkg` and `.mergin/`); five
  `--force-recreate` restarts during edits (slice 14 recipe) still
  "stopped cleanly"; one Verify afterwards; `.mergin_auth.json` present
  with mode 0600 and never in any log or hook output.

## F. Upstream, in the same breath

Patch 0002 is now the only local patch. File it upstream as an issue
("SIGTERM kills db-sync mid-pull; proposal: finish the current connection
and exit between cycles") with the patch as a PR against 2.4.0, the way
#187 went. The maintainers merged #187 in a week; this one is smaller and
has a test.

## Not in this slice

Anything in db-sync 2.4.0 the manager does not touch (Windows build,
project-id-less projects). The `close` API (point 8 of the installation's
assignment) stays a later slice.

## Applied (slice 16, 2026-10-26)

| Item | Done |
|---|---|
| Upstream diff 2.3.0..2.4.0, image tag | read before coding; `lutraconsulting/mergin-db-sync:2.4.0` exists on Docker Hub (2026-10-19), `latest` points at it |
| A6 Dockerfile, patch 0001, 0002 rebased | `DBSYNC_VERSION=2.4.0`; 0001 deleted; 0002 rebased on `run_daemon` with `sleep_unless_stopped()`; `patches/upstream/` = 2.4.0, `*.patched` regenerated; `tests_patches` extended |
| A7 requirements comment | updated (mergin-client 0.15.0, pinned geodiff from the base image) |
| B8, B9 one place for the layout | `supervisor/layout.py`; `SyncConnection.checkout_dir`, check, verify and the config renderer go through it; `working_dir` unchanged (`<work_dir>/work`) |
| C10 migrate_checkouts | `migrate` operation (`wait_turn → block → stop_child → remove_old_checkout → redownload`), one project at a time; home-page notice and button, `manage.py migrate_checkouts [--list]`; projects in the old layout are not started |
| C11 token file | nothing to migrate; documented as a secret (README, backup.md, CLAUDE.md) |
| D12, D13 docs | README, backup.md, CHANGELOG, patches/README.md |
| E tests | `tests_patches` (apply, 2.4.0 sources, four behaviours), `tests_layout`, `tests_migrate` (refusal through `_reconcile`, the steps), `tests_operations`, db-tagged `MigrateCheckoutsDbTests` and the UI test; field items in the review note |
| F upstream | `patches/UPSTREAM.md` has the issue, the PR text and the test change; filing needs Nicklas's GitHub (no `gh` here) |
| Also found | 2.4.0 retries login/init in-process for about an hour by default; the config sets `max_failed_retries: 1` so exits still reach `exits.classify_exit` (review note) |

Review note: `2026-10-26-slice16-dbsync-2.4.0.md`.

