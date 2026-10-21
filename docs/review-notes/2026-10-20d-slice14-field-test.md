# 2026-10-20d: slice 14 field test (graceful stop, exit classification)

*Written by Cowork-Claude from the field test on the production server the
same evening slice 14 (`b2c337b`) was delivered, against the incident note
`2026-10-20b` and the slice note `2026-10-20c`. Four projects in the
manager; the test clone (two-way, sleep 10 s) was the subject, edited from
QGIS through the Mergin Maps plugin during the test. Findings 1 and 2 are
verified in the field; four small things for 14b.*

## What passed

- **Deploy of the slice itself** under the interim rule (pause all four,
  `up -d`, resume): migrations applied, all four `ok` afterwards. The
  image carries patch 0002 (`request_stop` present in the image's
  `dbsync.py`).
- **Graceful stop, idle.** `docker compose restart manager`: four
  "stopping db-sync, waiting for the current cycle (up to 120 s)", four
  "stopped cleanly after 0 s", no "killed". Restart took 12.5 s in total.
- **Graceful stop, under load.** The clone edited and synced from QGIS
  about once a minute (v12 → v27), `docker compose up -d --force-recreate
  manager` five times 45 s apart. Five "stopped cleanly" (0–1 s), no
  "exited unexpectedly", no `checkout_inconsistent`, clone `ok` at the
  server version afterwards. The daemon log shows the mid-cycle case
  explicitly: `stop requested (signal 15); finishing the current
  operation first` → `stop requested; not pulling the remaining
  connections` → `exiting cleanly after pull`. This is the exact
  situation that broke a production project at 13:07 the same day.
- **`checkout_inconsistent`.** Provoked as in the incident: one row
  changed directly in the live `base` schema, the connection's checkout
  directory removed, Pause/Resume. Result: `start download` → `not
  synchronized with source GPKG` → process `crashed`, **not restarted**,
  one `error` event with the operator text ("use Re-init on the project
  page, dump the installation's own schema first"), condition active,
  Re-init button highlighted, **one alert mail** (second run, with
  recipients set). Re-init repaired it: `running`/`ok`, the planted row
  gone.
- `manage.py events --project --since` and the Events filters: used
  throughout; the terminal command is what made this note possible.

## Findings for 14b

1. **Re-init clears conditions without resolving them.** After Re-init
   the condition is gone from the project page, but there is no
   `resolved` event and no recovery mail, although the activation mail
   went out. The operation should go through `conditions.resolve(...)`
   ("resolved: re-initialised") for every active condition it clears, so
   the dedup pairing and the recovery mail work. Same check for Remove.
2. **Recipients can only be set in the admin, and nothing defaults.**
   The clone had an empty "Notify emails" because the add-project wizard
   never asks for recipients; the first `checkout_inconsistent`
   activation mailed nobody and said nothing. Two of the four production
   projects turned out to be in the same state. Fix (operator's
   proposal, 2026-10-21):
   - An installation-wide default recipient list, editable in the admin
     (a singleton row; `InstallationSettings` was removed in slice 10, so
     this is a deliberate, minimal reintroduction: one page, one field).
   - The wizard's last step gets a "Notify emails" field and a checkbox
     "Use the installation's default recipients", checked by default
     when a default list exists.
   - Semantics: an empty project field means "use the default". That
     applies to existing projects too, so setting the default list
     covers every project that was added without recipients. Scheduled
     hooks' `notify_emails` follow the same rule.
   - Only when both the project field and the default list are empty:
     an `info` event "alert not mailed: no recipients configured" on
     activation and a warning line on the project page and in the
     projects table.

3. **Health lags the condition.** With `checkout_inconsistent` active,
   the project page still said health `ok` "since 28 min ago", because
   health is recomputed only by the check every 300 s. Recompute health
   when a condition is activated or resolved, and run a check right after
   a successful start: after the five restarts the list showed a stale
   version and "Last pull" for up to ten minutes, which the operator read
   as a stall.
4. **Pause path wording.** Pause logs only "db-sync stopped (pid N)",
   without the "stopping … waiting" / "stopped cleanly after N s" pair,
   although the daemon log shows it did stop gracefully. Use the same
   two events for every stop path (pause, remove, re-init, restart
   request, shutdown).

## Applied (Claude Code, 2026-10-21, slice 14b)

| Finding | Change |
|---|---|
| 1 | `SupervisorIO._resolve_all`: Re-init (`reset`) and Remove (`mark_removed`) call `conditions.resolve` for every active condition ("resolved: re-initialised" / "resolved: sync removed") before clearing the set; `mergin_unreachable` resolves without a mail there, since its mail is grouped per server |
| 2 | `InstallationSettings` (migration 0013): one row, one field, `notify_emails`; admin opens the row directly, no delete. `alerts.recipients_for(owner)`: the owner's list or the default; used by `notify`, `notify_group` and `notify_hook`. Wizard step 2: the field plus "Use the installation's default recipients when the field is empty", ticked when a default exists; empty field and unticked box is a form error. Preview shows what the alerts go to. `conditions.activate`/`alert` write "alert not mailed: no recipients ..." (info) when neither list exists; the Projects page shows a "no recipients" badge and the project page a notice |
| 3 | `conditions._save` recomputes health from the active set (`failed` while a deterministic condition is active, `unknown` once it clears, until the next check); `_start` resets the check timer so the first tick after a start checks |
| 4 | `_stopping_event` / `_stopped_event` used by pause, the stuck restart, the operation's `stop_child` and `_handle_exit`: "stopping db-sync (pid N), waiting for the current cycle (up to 120 s)" and "stopped cleanly after N s" or "ignored SIGTERM; killed after N s" (error) |

Tests: `ConditionsFollowTests`, `ResetResolvesConditionsTests`, the pause
stop pair in `ReconcilePauseTests`, wizard recipients and the "no
recipients" flag in `ui/tests.py`. 156 pass without a database.

## Not tested tonight

Findings 3 (`LoginError` → `mergin_unreachable`, 401/403 →
`mergin_auth_failed`), 4 (crash-loop reminders) and 6 (Pause all / Resume
all buttons) of `2026-10-20b`: covered by unit tests in the slice, not yet
exercised in the field. Finding 3 can be tested the same way as on
2026-10-20 (account URL to an invalid host) now that the stop is graceful
and the clone has recipients.

## Operator notes

- `docker compose up -d --build` with an unchanged image does **not**
  recreate the container (five "Running" lines, nothing happened). Use
  `--force-recreate` or `restart` to exercise a stop.
- The interim "pause before deploy" rule is withdrawn: stops are graceful
  since `b2c337b`, and `stop_grace_period` is in compose.
- The provocation recipe for `checkout_inconsistent` (for the test docs):
  pause; `UPDATE <base schema>."<layer>" SET <col> = 'x' WHERE fid =
  (SELECT min(fid) …)`; `rm -rf /data/projects/<id>/work/*/<modified
  schema>`; resume. A changed base row alone is not detected (db-sync
  only compares base with the GeoPackage after a fresh download);
  removing the checkout is what triggers the comparison.
- The clone still carries schemas from an earlier configuration (a second
  set with a different prefix pattern, owned by another role); they are
  not used by its current connections and should be dropped by hand.
