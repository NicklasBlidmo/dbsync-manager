# 2026-10-26b: slice 16 field test (db-sync 2.4.0 in production)

*Written by Cowork-Claude from the upgrade of the production server to
slice 16 (`3082a0a`, db-sync 2.4.0), 2026-10-26 morning, four projects.
Everything passed; one gap in the upgrade notice to fix in docs.*

## Sequence and results

1. **Gate**: Verify on all four projects before pulling: all in sync.
2. `git pull && docker compose up -d --build`: the four projects came up
   `blocked` with the warning "not started: the checkout has the db-sync
   2.3.0 layout; run 'Migrate checkouts' …", and the home page showed the
   migration notice with the four names and the button. Health column
   showed `check failed` meanwhile (the check looks for the new layout).
3. **Migrate checkouts** from the notice: one project at a time, 30 s
   each (one tick), four "migrate operation N completed", all `running`
   and `ok` after the first check. 09:35:58 → 09:38:13 for four projects.
4. Layout after migration: `work/<project>-<modified schema>/` per
   connection containing only the synced `.gpkg` and `.mergin/`
   (sparse checkout, #128); `work/.mergin_auth.json` present, 0600,
   owned by `manager`.
5. **Graceful stop under 2.4.0** (rebased patch 0002): the clone edited
   from QGIS, five `--force-recreate` 45 s apart: five "stopped cleanly"
   (0–1 s), no "killed", no unexpected exit, clone `ok` afterwards.
6. **Deterministic exit reaches the manager** (note's point 7, the
   `max_failed_retries: 1` decision): planted base row + removed
   checkout, resume → `crashed` with `checkout_inconsistent`, health
   `failed`, alert mail, within two minutes. Without the setting 2.4.0's
   in-process retries would have hidden it for about an hour.
7. Re-init → `running`/`ok`, condition resolved, recovery mail.

## For the docs (no code)

- The gate must be taken **before** `up -d`: after the upgrade both the
  check and Verify look for the new layout, so Verify on an unmigrated
  project cannot pass. The notice says "Run Verify on each first", which
  reads as "now". Change it to "Verify was the pre-upgrade gate; a
  project that fails after migration needs Re-init", and say the same
  in `CHANGELOG.md`/README upgrade notes. A blocked, unmigrated project
  could also show health `awaiting migration` instead of `check failed`.
- Upstream: patch 0002 is field-verified on 2.4.0; `patches/UPSTREAM.md`
  is ready to file.

## State after

Production on db-sync 2.4.0, patch 0001 gone, 0002 the only local patch.
Four projects `ok`. Checkouts sparse. Old `work/<project>/` trees removed
by the migration.
