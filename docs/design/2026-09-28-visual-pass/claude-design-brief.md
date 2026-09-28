# Claude Design brief: dbsync-manager visual pass

*Paste the "Prompt" section into Claude Design together with the six
screenshots in this folder (`dbsync-manager/` = what we have,
`Mergin-web/` = what it should harmonise with). The "Hand-off" section is
for Nicklas and Claude Code, not for the design tool.*

---

## Prompt

I need a visual design pass for **dbsync-manager**, a small operations web
UI (Django + HTMX, server-rendered, no JS framework) that supervises
Mergin Maps DB Sync daemons: it shows which Mergin projects are being
synchronised into PostGIS, whether they are healthy, and lets an operator
pause, re-initialise or remove a sync. Users are a handful of GIS
staff at an archaeology company; the tool will also be shown to the
Mergin Maps team, so it should feel like a **companion tool built for
Mergin Maps**: same colour family, same typographic calm, same table
density, without copying their layout or using their logo or wordmark.
It keeps its own name ("dbsync-manager") and a plain text brand.

### What exists

Attached: `Projektöversikt.png` (project list) and `Projektdetaljer.png`
(project page: status panel, connections table with expandable rows, live
log, pull history, hooks, events). These are the real pages with real data.
The layout and information hierarchy are settled; **do not redesign the
structure**. What I want from you is colour, typography, spacing and
component styling, delivered as values for the token contract below.

Also attached for reference: `Dashboard.png`, `Projektfiler.png`,
`Projekthistorik.png`, `Collaborators.png` from the Mergin Maps web app.

### The token contract (this is the deliverable)

The whole UI is styled from one stylesheet whose `:root` block defines
these custom properties. Give me new values for **light** and **dark**
mode. Names must stay exactly as they are.

```css
:root {
  /* colour: surfaces and text */
  --bg        /* page background            now #fff    */
  --fg        /* body text                  now #1f2328 */
  --muted     /* secondary text, table headers, schema names   now #6b7280 */
  --line      /* table rules, panel borders now #e5e7eb */
  --panel     /* panels, header, code/log background, muted badge   now #f6f8fa */
  /* colour: semantic pairs (fg on bg), used by .badge and .notice */
  --ok,   --ok-bg      /* running, healthy, applied   now #1a7f37 on #dafbe1 */
  --info, --info-bg    /* neutral state (two_way, initializing, pending), links  now #0969da on #ddf4ff */
  --warn, --warn-bg    /* attention, not yet broken   now #9a6700 on #fff8c5 */
  --bad,  --bad-bg     /* failed, crashed, divergence now #cf222e on #ffebe9 */
  /* typography */
  --font-body   now system-ui stack
  --font-mono   now ui-monospace stack
  --text-xs .8rem  --text-sm .85rem  --text-md 14px  --text-lg 1.05rem  --text-xl 1.3rem
  --line-height 1.45
  /* shape and rhythm */
  --radius .4rem  --radius-sm .3rem  --radius-pill 1rem
  --space-1 .2rem  --space-2 .4rem  --space-3 .6rem  --space-4 1rem  --space-5 1.5rem
  --shadow none
}
```

Components that consume the tokens (you may propose rule changes for
these, but keep the class names): `.site-header` (brand, nav, user),
`.page-title`, `.page-subtitle`, `.section-title`, `.table` (with `.num`
right-aligned tabular numbers), `.connections` (the expandable table:
`.toggle`, `.schemas`, `.rows`, `.detail`), `.badge` with variants
`.ok .info .warn .bad .muted`, `.notice` and `.notice.bad`, `.panel`,
`.status-bar` + `.actions`, `.facts` (dt/dd), `pre` and `.logbox`,
`button`/`.button`, `.filters`, `.login`.

### Mergin Maps reference values (from their open-source theme)

Grass `#73D19C` (primary), Forest `#004C45` (primary dark, headings,
text on grass), Night `#12181F` (text), secondary text `#AAACAD`, Light
green `#EFF5F3` (page ground), Medium green `#B7CDC4`, Dark green
`#6E9991`, surface border `#DEE2E6`, semantic backgrounds Positive
`#C0EBCF`, Warning `#F7DDAF`, Negative `#F0C4BC`, Negative light
`#FFF0ED`, Informative `#BEDAF0`; accents Sun `#F4CB46`, Sunset
`#FFB673`, Sand `#FFF4E2`, Earth `#4D2A24`, Grape `#5A2740`, Deep ocean
`#1C324A`. Font Inter 400/600, base 0.75rem in their UI, radius 6px,
white cards on the light-green ground.

A likely mapping, to accept or improve: `--bg` light green, `--panel`
white, `--fg` night, `--muted` a darker secondary than #AAACAD (theirs
fails contrast on white for small text), `--line` #DEE2E6, `--info`
forest on informative, `--ok` forest on positive, `--warn` earth on
warning, `--bad` a dark red on negative. Buttons: outline forest like
their secondary buttons; the one primary action per page ("Add sync")
grass with forest text. They publish no dark theme; derive one that
keeps forest/grass recognisable.

### Rules

1. **Calm by default.** On a healthy project page nothing is in strong
   colour except the one primary button. Eight rows of muted numbers must
   read as "all fine". A warn or bad badge must be the only saturated
   element on the page when it appears.
2. The four semantic pairs must be distinguishable for deuteranopia and
   protanopia by lightness and hue, not only hue; ok and info must not be
   confusable (they are today in dark mode). Text on badge backgrounds
   ≥ 4.5:1, muted text on both `--bg` and `--panel` ≥ 4.5:1.
3. The tool runs on an office LAN in Chrome/Edge at 1200–1600 px, and on
   a phone occasionally; nothing narrower than 700 px needs polish.
4. Inter is fine (self-hosted, 400 and 600 only); no other webfonts.
   Monospace stays a system stack.
5. No Mergin Maps logo, icon or wordmark anywhere. The brand is the text
   "dbsync-manager".
6. Do not add a left sidebar; the top header with two links is the
   navigation and stays.

### Deliverable

1. The `:root` block with all tokens for light mode, and the same block
   under `@media (prefers-color-scheme: dark)`.
2. Optional: revised rules for the components listed above, each as a
   complete CSS rule, with one line on why.
3. A mock of the two attached pages in the new tokens, light and dark, so
   we can compare before it is built.

---

## Hand-off (for Nicklas and Claude Code)

- The result lands as a change to the top of
  `manager/ui/static/ui/app.css` (token blocks) plus, if accepted,
  component rules further down. Templates are not touched by the visual
  pass; if the design needs a new class, that is a slice 9 item.
- Inter as a self-hosted webfont: two `.woff2` files under
  `manager/ui/static/ui/fonts/`, `@font-face` at the top of `app.css`,
  `--font-body: Inter, system-ui, …`. WhiteNoise serves them; no CDN.
  Licence OFL, attribution not required.
- Colours are not copyrightable and Mergin Maps' web app is open source
  (AGPL), but the logo and the name are trademarks: the brief says no
  logo, and the header says "dbsync-manager", not "Mergin".
- Verify the delivered contrast pairs with a checker before merging
  (rule 2 above); Claude Design's numbers are a claim, not a test.
- Screenshots in this folder are the "before" set; keep them and add an
  "after" set at the same viewports when slice 9 ships, for the Lutra
  presentation.
