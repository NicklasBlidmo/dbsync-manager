# 2026-10-20: log classification after three weeks in production

*Written by Cowork-Claude from Nicklas's observations on gis-ubuntu with
five projects running (Bönsta, Nyköping, Hässlinge, Minninge2, the test
clone). Two kinds of `error` events dominate the Events tables; neither is
an error in the sense that anyone needs to act. Proposed: reclassify, and
turn the one that can matter into a condition with a delayed alert.*

## 1. "There are pending changes on server - need to pull them first."

Seen in bursts (2–4 per hour) on three projects, two of them Reader /
one-way. db-sync's cycle is pull then push; `push()` first asks the server
whether it has a newer version than the checkout and raises this error if
so, *before* looking for local database changes. With field teams syncing
every minute the window between pull and push is hit often. The daemon
continues; the next cycle pulls the new version. Health stays `ok`.

Classify as **info** ("server moved during the cycle; retried next cycle").
Raise to **warn** only if the same message repeats in N consecutive cycles
(suggest N = 5) *and* the project shows `pending database changes`, which
is the real stuck case (local edits that never get pushed). On a one-way
project it can never be more than info.

## 2. HTTP 503 / "upstream connect error … Connection refused" from app.merginmaps.com

Mergin's edge could not reach its backend; seen twice in a morning, a few
seconds each. The daemon fails that cycle (or exits and is restarted by the
supervisor); the next cycle succeeds.

Do not log each occurrence as an `error` event. Make it a **condition**
`mergin_unreachable` (per project, or installation-wide since it is the
same server for all projects), set on the first failure, cleared on the
next successful API call, with the alert e-mail sent only when the
condition has been active for more than `DBSYNC_MERGIN_DOWN_ALERT_AFTER`
(default 900 s). Same dedup rules as the other conditions (§7). The event
log gets one `info` line when the condition is set and one when cleared,
with the duration.

Also treat the same way: connection reset, timeout, and 502/504 from the
Mergin API. Authentication failures (401/403) and 404 stay errors; they
are configuration problems.

## 3. Housekeeping while there

- The Events table shows "(previous error repeated 3 more times)" which is
  good; make the collapsed count also apply across the info level once 1
  and 2 are in.
- The consistency check logs `catching_up` for a project whose checkout is
  one version behind during active field work; that is correct and should
  stay.

## Applied (Claude Code, same day)

| Item | Change |
|---|---|
| 1 | `logtail.classify`: an ERROR line containing "pending changes on server" is `server_moved`; `apply` records it as info ("server moved during the cycle; retried next cycle"), counts a streak (reset by a push result or another error, not by the pull that follows), and at 5 in a row asks `project_facts` (pending tables from the last check's counts, every connection one-way) and writes one warning naming the tables; one-way or no pending edits stays info |
| 2 | `check.is_transient_api_error` (5xx, 429, refused, reset, timeout, DNS; 401/403/404 and the rest are configuration errors). `gather` puts a transient failure in `Observed.mergin_unreachable` instead of `error`, `evaluate` carries server version and permission forward from the row, and `run_project_check` returns the condition. `core._track_mergin`: `conditions.activate(mail=False)` with an info event on the first failure, `conditions.alert` once after `DBSYNC_MERGIN_DOWN_ALERT_AFTER` (900 s, `.env`), `resolve` with the duration and a mail only if one went out; a supervisor adopting a running outage assumes the mail went out when the condition is older than the threshold. Per project (the condition is keyed on the project row like the others) |
| 3 | The "(previous message repeated N more times)" collapse now applies to info and error alike; `catching_up` untouched |
| Test | `tests_logtail.ServerMovedTests` (info, five with pending → warn once, one-way → info, no pending → info, push resets, collapse across levels); `tests_check.MerginUnreachableTests` (classification, facts carried forward) and `MerginUnreachableFlowTests` through `_reconcile` (set without mail, mail after 15 min once, resolve with duration and mail; short outage without mail; adopted mid-outage) |

## Test

- Unit: a log line "pending changes on server" → `info`; five in a row
  with `pending database changes` → `warn`; five in a row on a one-way
  project → still `info`.
- Unit: 503 from `project_info` → condition set, no mail; cleared after
  the next successful call within 15 min → no mail, one info event with
  duration; still failing after 15 min → one mail; recovery → one mail.
