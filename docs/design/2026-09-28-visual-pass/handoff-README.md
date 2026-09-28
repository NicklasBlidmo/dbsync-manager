# Handoff: dbsync-manager visual pass

## Overview
A new visual design for dbsync-manager (Django + HTMX), so that it reads as a companion tool to Mergin Maps: Mergin's colour family (forest, grass, light green), Inter, calm table density, and no Mergin logo or wordmark. The brand is the plain text "dbsync-manager". Structure, templates and information hierarchy are **unchanged**. This is a pure CSS pass, following the brief `claude-design-brief.md`.

## What to implement
1. Replace the top of `manager/ui/static/ui/app.css` with the token blocks in `app.css` in this folder: the `@font-face` rules, `:root` (light), and `@media (prefers-color-scheme: dark)`.
2. Replace the existing component rules with the ones in the rest of `app.css`. Every rule uses only tokens, apart from a few deliberate `color-mix()` hover tints.
3. Add Inter 400 and 600 as `manager/ui/static/ui/fonts/Inter-Regular.woff2` and `Inter-SemiBold.woff2` (from https://github.com/rsms/inter/releases, OFL). No CDN.
4. Template changes (slice 9, optional):
   - `class="button primary"` on "Add sync". This is the only new class, and it is the page's single primary action.
   - The active nav link needs `aria-current="page"` or `class="active"`.
   - The header expects `.site-header > a.brand`, `nav > a` and `.user`. Adjust the selectors if the markup differs.

## About the design files
`app.css` is production-ready and is the deliverable itself. `mock/` contains an HTML mock (open `mock/dbsync-manager Visual Pass.dc.html` in a browser) showing the project list, the project page and a state-check screen in light and dark. It is **a reference, not code to copy**. `mock-themes.css` re-scopes the same tokens to `.theme-light` / `.theme-dark` only so that both modes can be shown side by side; the app should use `app.css`. The mock markup approximates the Django templates. Where the real markup differs, keep the templates and adjust the selectors.

## Fidelity
High-fidelity: final colours, type, spacing, radii and states.

## Design tokens

### Light
| Token | Value | Use |
|---|---|---|
| --bg | #EFF5F3 | page ground (Mergin light green) |
| --fg | #12181F | text (Night) |
| --muted | #52625F | secondary text, table headers, schema names |
| --line | #D6E0DC | table rules, panel borders |
| --panel | #FFFFFF | panels, header, tables, log |
| --ok / --ok-bg | #004C45 / #DDF3E6 | running, healthy, applied (palest, so "all fine" stays calm) |
| --info / --info-bg | #1F4E79 / #E1ECF7 | two_way, initializing, pending, links |
| --warn / --warn-bg | #5A3A10 / #F7D79C | attention |
| --bad / --bad-bg | #8A1C12 / #F1B9AD | failed, crashed, divergence (darkest background) |
| --brand | #004C45 | outline buttons, brand text, focus ring (addition) |
| --brand-fill | #73D19C | primary button, active nav underline (addition) |
| --brand-on-fill | #004C45 | text on grass (addition) |
| --shadow | 0 1px 2px rgb(18 24 31 / .05) | cards |

### Dark
| Token | Value |
|---|---|
| --bg | #0E1513 |
| --fg | #E4ECE9 |
| --muted | #95A8A3 |
| --line | #26332F |
| --panel | #17211E |
| --ok / --ok-bg | #A6E8C1 / #173D2F |
| --info / --info-bg | #A9CBF2 / #1A2A3E |
| --warn / --warn-bg | #F4CB46 / #4A3A0E |
| --bad / --bad-bg | #FFC2B8 / #6B1D15 |
| --brand / --brand-fill / --brand-on-fill | #8FDDB0 / #73D19C / #004C45 |
| --shadow | none |

The three `--brand*` tokens are additions to the contract. The existing names are unchanged.

### Shared
- --font-body: Inter, system-ui, -apple-system, "Segoe UI", Roboto, sans-serif
- --font-mono: ui-monospace, "Cascadia Mono", "SF Mono", Menlo, Consolas, monospace
- Type: --text-xs .75rem (12px), --text-sm .8125rem (13px), --text-md 14px (body), --text-lg 1rem (16px), --text-xl 1.25rem (20px); --line-height 1.5. Weights 400 and 600 only.
- Radius: --radius 6px, --radius-sm 4px, --radius-pill 999px
- Space: --space-1 .25rem, --space-2 .5rem, --space-3 .75rem, --space-4 1rem, --space-5 1.5rem

### Contrast (WCAG, calculated; verify with a checker before merging)
| Pair | Light | Dark |
|---|---|---|
| fg on bg / panel | 16.18 / 17.85 | 15.39 / 13.73 |
| muted on bg / panel | 5.82 / 6.42 | 7.41 / 6.61 |
| ok on ok-bg | 8.51 | 8.56 |
| info on info-bg | 7.24 | 8.66 |
| warn on warn-bg | 7.40 | 7.08 |
| bad on bad-bg | 5.45 | 7.54 |
| info (link) on bg / panel | 7.85 / 8.66 | 11.02 / 9.83 |
| brand on bg / panel | 8.98 / 9.91 | 11.57 / 10.32 |
| brand-on-fill on brand-fill | 5.34 | 5.34 |

Colour-blind separation (brief rule 2): in light mode the badge backgrounds step down in relative luminance ok 0.85 → info 0.83 → warn 0.71 → bad 0.56. The order is therefore readable without hue, and ok vs info also differ on the blue–yellow axis, which deutan/protan vision keeps. In dark mode ok and info differ in both hue and text colour (pale green vs pale blue), which fixes today's confusion.

## Components (one line on why)
- **.site-header**: 52px white bar with a 1px line. Brand in 600 forest; nav links 600 muted; the active link in forest with a 2px grass underline. It echoes Mergin's tabs without copying their sidebar.
- **.page-title / .page-subtitle / .section-title**: 20/13/16px, 600, no colour. The hint text in `.section-title small` is 400 muted. Calm hierarchy through weight rather than colour.
- **.table**: a white card on the green ground (1px line, 6px radius), 12px 600 muted headers, row rules only, 8×12px cells, no rule on the last row, faint hover. Same density principle as Mergin's tables, but tighter.
- **.num**: right-aligned, `tabular-nums`, nowrap. Numbers line up.
- **.connections**: `.toggle` is a bare chevron (rotates 90° on `aria-expanded="true"`, 120ms). The file name is nowrap. `.schemas` is 12px mono muted. `.detail` gets the ground colour so the expanded row reads as nested.
- **.badge**: a pill, 12px 600, 20px line height, tinted pair. `.muted` is transparent with an inset line ring (--panel is white, so a filled background would disappear).
- **.notice / .notice.bad**: 12×16px, 6px radius, info or bad pair. No left border.
- **.panel / .status-bar / .actions / .facts**: the white card. The action row is separated by a line, and the facts use a `max-content 1fr` grid.
- **pre / .logbox**: 12px mono, line height 1.55, white card, max-height 24rem, pre-wrap.
- **button / .button**: a 32px pill with a 1px forest outline and 600 13px text. Hover tint 8%, active 16%, disabled 45%. `.primary` = grass fill with forest text. Matches Mergin's secondary and primary buttons.
- **.filters**: a flex row with 12/16px gap; muted labels; selects 32px, 4px radius. The button is pushed to the right.
- **.login**: a white card, max 22rem, centred at 10vh.
- **Focus**: `:focus-visible` 2px --brand, offset 2px.
- **< 760px**: tables scroll horizontally, the schemas column is hidden, and main padding shrinks to 16px.

## Behaviour
No new behaviour. HTMX, live log and expandable rows work as before; only the look changes. Brief rule 1: on a healthy page only "Add sync" is saturated; warn and bad are the strongest tints.

## Assets
No images or icons. No Mergin logo or wordmark. Inter (OFL) is the only font.

## Files
- `app.css`: the deliverable (tokens + component rules + @font-face).
- `mock/dbsync-manager Visual Pass.dc.html`: the mock (light and dark: Projects, Project detail, State check). Open it in a browser; `support.js` is required next to it.
- `mock/dbsync/app.css`, `mock/dbsync/mock-themes.css`: the mock's stylesheets.
- `screenshots/`: "after" mocks at 1280 px — 1–2 Projects, 3–4 Project detail, 5–6 State check (light/dark).
- The "before" screenshots are in the original folder (`uploads/GUI/dbsync-manager/`).
