# Slice 10 brief: functional polish of the UI

*Written by Cowork-Claude 2026-09-28 after slice 9 (visual pass) was
reviewed in production. Scope agreed with Nicklas. Slice 9 had exactly one
field finding (item 1); the rest is the functional list postponed from
slices 8 and 9. The page structure from slice 8 and the tokens from slice
9 are fixed; this slice changes what the pages show, not how they look.*

## 1. Slice 9 field finding: action buttons too close to the badge row

`status.html`: the Pause/Re-init/Remove group sits a couple of pixels
under the badges; the mock has about 12 px and then the rule under the
buttons. Cause: `.actions` is rendered inside `.status-bar` (a wrapping
flex row), so it lands as the last flex item with only `gap` as distance.
Fix in `app.css`:

```css
.status-bar .actions { flex-basis: 100%; margin-top: var(--space-3); }
```

so the buttons always take a row of their own with the mock's spacing,
whether or not the badge row wraps. Do not move `.actions` out of the
status bar in the template; the rule under the buttons already comes
from `.actions`.

## 2. Status badges: labels instead of prefixes

Today: `desired: running`, `process: running`, `health: ok`. Replace with
a small `.facts`-style group or three labelled badges where the label is
muted text and the badge holds only the value:

```
Desired  [running]   Process  [running]   Health  [ok]   since 2026-09-28 13:35
```

Same in `projects_table.html` headers (already "Desired/Process/Health"),
so the list and the page use the same words. The "since" timestamp and
the "no divergence detected…" sentence stay on the same row, muted.

## 3. Pull history grouped per version

`runs.html` shows one row per connection per pull: eight identical
"applied v11" rows one second apart. Group by `(kind, checkout_version)`
within a window (rows whose `started_at` lie within `sleep_time` of each
other), show one row per group:

| Started (first) | Kind | Version | Result | Files |
|---|---|---|---|---|
| 2026-09-28 11:16:01 | pull | v11 | applied | 8 of 8 |

Result is the *worst* state in the group (`failed` > `checkout only` >
`applied`); "Files" is `n of total connections`, and the row expands (same
`<details>`/toggle pattern as the connections table) to the per-file rows
when the group is not uniform or when the user opens it. Keep the last 20
*groups*, not the last 20 rows. The detail column's "--- pull finished --
at version v11" adds nothing when uniform; show it only in the expanded
rows.

## 4. Events and Hooks collapsed by default

`project.html`: the Events table (50 rows) and the Hooks table (10 runs)
push the log and the pull history off the first screen. Render each
section as `<details>` with the section title as `<summary>`, showing the
count and the newest entry's timestamp in the summary line
("Events · 50 · latest 13:35:02"), collapsed by default, open
automatically when the newest event is `error`/`warning` or the newest
hook run failed. HTMX polling of the hooks partial must not reset the
open/closed state: poll the table body, not the `<details>`.

## 5. Local time everywhere

The operation page's step log shows UTC. Every timestamp rendered in a
template goes through Django's `localtime` (the `TIME_ZONE = env("TZ")`
setting is already in place); grep `views.py` and the operation templates
for `isoformat()`/`strftime` that bypass it. Add a `since`/relative-time
filter (`3 min ago`, `2 h ago`, else the date) for the projects list
"Last pull" and the status bar "since", with the absolute time in
`title`.

## 6. Projects list: one version column, deviation marked

`projects_table.html` has Server / Local / DB, nearly always equal. Show
one "Version" column with the server version; when checkout or database
differ, append a warn badge `checkout v5464` / `db v5463` in the same
cell. Add a "Reset" link to the filter row that clears the three selects.
Confirm the slice 8 part C item "check failed" badge with the error in
`title` is in place.

## 7. Wizard: confirmation of the database, Host help text

- `add/project.html` (step 2): after Choose, show the selected database's
  name and host in the form header so the slice 7 bug (database not
  carried over) is visible before Preview. Preview (step 3) repeats it.
- Host field help text: "As seen from inside the manager container:
  `localhost` is the container itself; use the server's LAN address or
  Docker service name."

## 8. Operations

- `remove` with drop schemas must always require typing the project name,
  not only when the project is running (field finding 2026-09-24).
- Step log: keep every attempt's detail; today a retry overwrites the
  previous attempt's text (field finding 2026-09-24). Append, with the
  attempt number.

## 9. Small things

- `runs.html`: `title` on "applied" ("the check has seen the database at
  this version"), if not already done in slice 8 part C.
- `status.html`: the `.detail` row of a connection with several tables
  shows the table name column first; check the header row aligns with the
  outer table (same column count or `colspan`).
- Header: "Settings (admin)" → "Settings"; the mock already says so.

## Tests and acceptance

- A `runs` grouping test with fabricated `SyncRun` rows: 8 uniform rows →
  one group `8 of 8 applied`; 7 applied + 1 failed → one group `failed`,
  expanded by default.
- Relative-time filter unit test at the boundaries (59 s, 61 min, 25 h).
- Existing tests unchanged; template tests for the labels in item 2.
- After deploy on gis-ubuntu: screenshots at 1280 px light/dark of the
  list and the Bönsta page into `docs/design/2026-09-28-visual-pass/`
  as `after-slice10-*.png`. These are the Lutra presentation set.
- Deploy: pause one project before `docker compose up -d --build` unless
  the `TMPDIR` fix (2026-09-28b) has shipped; resume one tick later.

## Not in this slice

Anything in the supervisor beyond the `TMPDIR` fix (separate), the admin,
the design tokens, part 2 of the project (forms/metadata sync).

## Applied (Claude Code, same day)

| Item | Change |
|---|---|
| 1 | `.status-bar .actions { flex-basis: 100%; margin-top: var(--space-3) }`; the rule under the buttons stays from `.actions` |
| 2 | Status bar: muted `.label` + value-only badges (Desired, Process, Health); "since" relative with the absolute time in `title` |
| 3 | `ui/runs.py` groups runs by kind and checkout version within `max(sleep_time, 60)` s; one row per group with the worst result, "n of total" files, expandable per-file rows (same toggle pattern, opened when not uniform); last 20 groups from up to 400 rows |
| 4 | Events and Hooks are `<details class="section">` with the title as summary, count and latest time; open when the newest event is warning/error or the newest hook run failed; the hooks poll targets the inner div, so the open state survives |
| 5 | `ago` filter (59 s → "59 s ago", 61 min → "1 h ago", 25 h → date) on Last pull and "since"; active-condition timestamps (ISO strings in `active_conditions`) now go through `isotime`; the operation step log already used it since 6b1968f |
| 6 | Projects list: one Version column (server) with `checkout vN` / `db vN` warn badges where they differ; "Reset filters" link when a filter is active; the "check failed" badge with the error in `title` was in place since slice 8 |
| 7 | Step 2 header and the preview show the chosen database as name (user@host:port/db) with a "change" link; host help text reworded as specified (migration 0007) |
| 8 | Both already in place since 6b1968f: the name is required whenever drop is ticked, and every attempt keeps its own step-log entry |
| 9 | `title` on "applied" since slice 8; the detail rows use `colspan` over the outer table's columns; header link reads "Settings" |

Tests: `tests_runs.py` (8 uniform → one applied group; 7 + 1 failed → failed, expanded; kinds, versions and the window split groups; the limit counts groups) and the `ago` boundaries; page tests for the labels, the Version column badges and the database line in step 2. 97 tests pass without a database. The TMPDIR fix has shipped, so no project needs pausing before the rebuild.
