# Slice 8 brief: UI foundation (stylesheet, tokens, connection table)

*Written by Cowork-Claude 2026-09-28 after the Bönsta takeover. Scope agreed
with Nicklas. This slice prepares the UI for a colour/typography pass that
will be done in Claude Design afterwards; it must not itself change the
colour scheme.*

## Why now

All seven slices are field-tested and Bönsta runs in production. Before the
visual pass we need two things in place: a stylesheet that can be replaced
wholesale (today all CSS is a `<style>` block in `base.html`), and a project
page that shows per-file state in a form that scales to eight or more
GeoPackages. The current card grid repeats project-level facts in every
card and squeezes the one thing that varies per file (versions and row
counts) into 280 px.

## Part A: stylesheet and design tokens

1. Move the `<style>` block from `manager/ui/templates/ui/base.html` to
   `manager/ui/static/ui/app.css`, linked with `{% static %}`. WhiteNoise
   already serves `static/ui/htmx.min.js`, so no settings change is needed
   beyond `collectstatic` running as it does today.
2. Keep the `:root` token block at the very top of the file, and treat it as
   the contract Claude Design will fill in. Extend it so that colour is not
   the only thing it controls:

   ```css
   :root {
     /* colour: surfaces and text */
     --bg, --fg, --muted, --line, --panel
     /* colour: semantic pairs (fg / bg), used by .badge and .notice */
     --ok, --ok-bg, --info, --info-bg, --warn, --warn-bg, --bad, --bad-bg
     /* new */
     --font-body, --font-mono, --text-sm, --text-md, --text-lg
     --radius, --space-1 .. --space-4
     --shadow (may be "none")
   }
   ```

   Same set under `@media (prefers-color-scheme: dark)`. Every component
   rule below the token block uses only tokens; no literal colours, radii or
   font sizes outside `:root`. Current values are kept exactly, so this
   commit has no visible effect.
3. Give the components stable class names that a design tool can target.
   The ones that exist today are fine (`.badge`, `.panel`, `.notice`,
   `.button`, `.logbox`, `.filters`, `.schemas`); add `.page-title`,
   `.section-title` for `h1`/`h2` in `main`, `.status-bar` for the badge row
   in the status panel, and `.table` on every data table so the bare
   `table` selector can go. Inline `style="..."` attributes in templates
   move into classes (`.nowrap`, `.pre-short`, `.mt-2`, and the
   `float:right` action group becomes `.actions`, see part B).
4. Do not touch Django admin styling; it keeps its own theme.

## Part B: project page, connections as a table

### B1. Status panel gets the project-level facts

The panel at the top already shows desired/process/health. Add, once, what
today is repeated per card:

- **Server version** (from any connection's `server_version`; they are the
  same project). Show `v5387` and, if connections disagree on
  `server_version`, a warn badge "server versions differ" (should not
  happen; it would mean one check is stale).
- **Mergin permission** and **direction** (from the first connection that
  has them; if connections disagree on direction, show each distinct value
  with a count, e.g. "one-way ×7, two-way ×1", as a warn badge).
- **Other writers**, collapsed into one line: the union across connections,
  with a per-schema tooltip when they differ. The sentence "superusers are
  not listed, since they can always write" appears here once and nowhere
  else.
- The action buttons (Pause/Resume, Re-init, Remove) become a `.actions`
  flex group aligned right on wide screens and wrapping below the badges on
  narrow ones; no `float` inside a `<p>`.

### B2. Connections table replaces the card grid

One row per connection (`SyncConnection`), rendered by
`partials/status.html` inside the same `#status` HTMX swap as today.

| Column | Content | Emphasis rule |
|---|---|---|
| File | `sync_file` | always normal |
| Schemas | `modified_schema` / `base_schema`, full names, `--text-sm`, muted | wraps at `/` if needed, never ellipsis |
| Checkout | `local_version` | muted when equal to server; normal + warn badge when behind server |
| Database | `db_version` | muted when equal to checkout; warn badge "behind checkout" when `interrupted_pull`; bad badge when behind server for more than one check (reuse the existing streak logic if it exists at connection level, else just warn) |
| Rows | for a single-table gpkg: `gpkg / base / mod` on one line with `.num`; for several tables: "N tables" with a summary badge | muted when all three agree; badge from the per-table logic when not |
| Checked | `checked_at` as `H:i:s` today, `Y-m-d H:i` otherwise; tooltip with full timestamp and `last_pull_at` | muted; bad badge with `check_error` text if the latest check failed |
| State | badges only: none when everything is in phase; otherwise the existing ones (`base behind ×n`, `mismatch once`, `pending database changes`, `interrupted pull, not user edits`) | |

"In phase" means: `local_version == server_version == db_version` and every
table row has `gpkg == base == modified` and `streak == 0` and not
`pending`. A project where all eight rows are in phase must read as a calm
table of muted numbers; any deviation must be the only strong thing on the
page.

Sorting: connections in `sync_file` order, except that rows with any badge
in State come first. This keeps the anomaly at the top without a filter.

### B3. Expandable row for per-table detail

Each connection row can be expanded to show the per-table counts that the
cards show today (Table, GPKG, Base, Mod, badges). Implementation with
`<details>`/`<summary>` inside the first cell, or a second `<tr>` toggled
by a button with `aria-expanded`; no new JavaScript beyond what htmx gives
us. Rules:

- Collapsed by default when the connection is in phase.
- Expanded automatically (server-side, at render) when any table row of
  that connection carries a badge, so the reason is visible without a
  click.
- Expansion state must survive the 10-second HTMX swap. Simplest: the
  server decides open/closed from the data as above, and a manual toggle
  is remembered in `sessionStorage` per connection pk (try/catch around
  storage; degrade to server default).
- A single-table gpkg still gets the expandable row, so the layout is
  uniform, but its summary line already shows the three counts, so
  expanding adds nothing but the table name.

### B4. What is removed

- The `.grid` of `.panel` cards for connections, and the `.schemas`
  ellipsis rule.
- The sentence about superusers at the bottom of the status panel (moved to
  the writers line, B1).
- `server_version`, permission and direction from the per-connection
  rendering (moved to B1). `mergin_permission` stays on the model.

## Part C: small things in the same slice

Cheap fixes in templates already being edited; larger functional items
(pull history grouped per version, collapsible Events/Hooks, local time in
the operation step log, wizard confirmation step) wait for slice 9 so the
Claude Design pass has a stable page to work on.

- `project.html`: the `h1` subtitle (database, prefix) moves to a
  `.page-subtitle` line under the title instead of inline in the `h1`.
- `projects_table.html`: the `!` marker for a failed check becomes a small
  bad badge "check failed" with the error in the tooltip.
- `runs.html`: the `title` on "checkout only" is kept; add the same to
  "applied" ("the check has seen the database at this version").
- `status.html`: the "No divergence detected (counts, versions)…" sentence
  becomes a muted line in the status bar, only when health is ok.

## Acceptance

- `git diff --stat` for part A shows `base.html` shrinking and `app.css`
  appearing; a screenshot before and after part A is pixel-identical apart
  from font hinting.
- On Bönsta (eight single-table GeoPackages, health ok) the project page
  shows one status panel and one eight-row table, no card grid, no
  horizontal scrollbar at 1200 px, and nothing in strong colour.
- Setting `post_init_ok_at = NULL` or pausing the daemon during a field
  change (the `db_behind` test from the slice 3 note) makes exactly the
  affected rows sort to the top with a warn badge and auto-expanded detail.
- `manager/ui/tests.py`: extend the project page test to assert the table
  rows and the in-phase/deviation rendering with a fabricated status that
  has one connection behind; add a test that a connection with two tables
  renders "2 tables" collapsed when in phase and expanded when one table
  has `streak >= 2`.
- Dark mode still works (only tokens changed).

## Hand-off to Claude Design (after this slice)

Cowork-Claude prepares a brief with the token list from part A, current
light/dark values, the component class list, and saved HTML of the
projects list and the Bönsta project page. Claude Design returns new token
values and, possibly, component tweaks; those land as a change to the top
of `app.css` only.

## Not in this slice

Colour choices, typography choices, layout of the wizard, admin, pull
history grouping, Events/Hooks collapsing, operation step-log timestamps,
any supervisor or model change.
