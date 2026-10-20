# db-sync patches

Applied to `/mergin-db-sync` at image build (Dockerfile: `git apply
--include='*.py'`, in file-name order). Each patch is offered upstream;
a patch is deleted here once a db-sync release ships the fix and
`DBSYNC_VERSION` is bumped.

| Patch | What | Upstream |
|---|---|---|
| `0001-Use-a-separate-working-directory-per-connection-fixes-163.patch` | one checkout per connection, so several GeoPackages of one project all sync | MerginMaps/db-sync#163 |
| `0002-Stop-gracefully-on-SIGTERM.patch` | SIGTERM sets a flag; the loops stop at the next connection boundary, a pull or push in progress completes, exit 0 (review note 2026-10-20b, finding 1) | to be offered |

`upstream/` holds the unmodified 2.3.0 sources the patches apply to, and
`*.patched` the result after both patches, as the image has them.
`supervisor/tests_patches.py` applies the patches to `upstream/` the way
the Dockerfile does and compares with `*.patched`, and exercises the
SIGTERM handling with the patched modules loaded under stubs. Regenerate
`*.patched` when a patch changes, or the test fails.
