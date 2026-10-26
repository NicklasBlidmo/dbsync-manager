# db-sync patches

Applied to `/mergin-db-sync` at image build (Dockerfile: `git apply
--include='*.py'`, in file-name order). Each patch is offered upstream;
a patch is deleted here once a db-sync release ships the fix and
`DBSYNC_VERSION` is bumped.

| Patch | What | Upstream |
|---|---|---|
| `0002-Stop-gracefully-on-SIGTERM.patch` | SIGTERM sets a flag; the loops stop at the next connection boundary, a pull or push in progress completes, exit 0 (review note 2026-10-20b, finding 1) | MerginMaps/db-sync#193, PR #194 (filed 2026-10-26) |

Shipped upstream and removed here:

| Patch | Released in |
|---|---|
| `0001-Use-a-separate-working-directory-per-connection-fixes-163.patch` (one checkout per connection) | db-sync 2.4.0 as MerginMaps/db-sync#187, revised to `<working_dir>/<project name>-<modified schema>` (slice 16) |

`upstream/` holds the unmodified 2.4.0 sources the patches apply to, and
`*.patched` the result after the patches, as the image has them.
`supervisor/tests_patches.py` applies the patches to `upstream/` the way
the Dockerfile does and compares with `*.patched`, and exercises the
SIGTERM handling with the patched modules loaded under stubs. Regenerate
`*.patched` when a patch changes, or the test fails.

Upgrading db-sync: diff the upstream tags (`git log <old>..<new>` and the
diff of `dbsync.py`, `dbsync_daemon.py`, `config.py`, `requirements.txt`),
confirm the image tag on Docker Hub, copy the new sources into
`upstream/`, regenerate the patch and `*.patched`, run `tests_patches`,
then read the rest of the manager against what changed (slice 16 is the
worked example: `docs/review-notes/2026-10-26-slice16-dbsync-2.4.0.md`).
