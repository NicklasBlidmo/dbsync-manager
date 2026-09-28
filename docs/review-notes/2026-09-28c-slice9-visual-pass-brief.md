# Slice 9 brief: apply the Claude Design visual pass

*Written by Cowork-Claude 2026-09-28. Scope agreed with Nicklas: this
slice is the visual pass only, plus the three template lines it needs.
Functional polish (pull history grouped per version, collapsible
Events/Hooks, local time in the operation step log, wizard confirmation
step, badge prefixes) is slice 10 so that before/after screenshots of this
slice compare like with like.*

## Input

Claude Design delivered a hand-off after the brief in
`docs/design/2026-09-28-visual-pass/claude-design-brief.md`. The parts of
it that matter are copied into the repo:

- `docs/design/2026-09-28-visual-pass/handoff-app.css`: the deliverable,
  tokens + `@font-face` + component rules, written against a **mock** of
  our templates, not the templates themselves.
- `docs/design/2026-09-28-visual-pass/handoff-README.md`: token tables,
  contrast figures, one line per component on why.
- `docs/design/2026-09-28-visual-pass/*.png`: the mock rendered at 1280 px
  (project page light, state check light, project page dark). The "before"
  screenshots of the real pages are in the same folder.

The full hand-off, including the HTML mock, is outside the repo in
the private documentation folder (`GUI/dbsync-manager visual pass/`).
Open `mock/dbsync-manager Visual Pass.dc.html` in a browser to see light
and dark side by side. The mock is a reference, not code to copy.

Cowork-Claude recomputed every contrast pair in the README with the WCAG
formula; all figures are correct (lowest: bad on bad-bg 5.45:1 light). The
badge backgrounds step down in luminance ok 0.85 → info 0.83 → warn 0.71 →
bad 0.56, so brief rule 2 (readable without hue) holds.

## The one rule: merge, not replace

The hand-off README says "replace the component rules". Do not. The
delivered CSS targets the mock's markup and drops rules our templates
rely on. Work through `handoff-app.css` rule by rule against the current
`manager/ui/static/ui/app.css`:

1. **Tokens and `@font-face`**: take the two token blocks verbatim (light
   `:root` including `color-scheme: light dark`, and the
   `prefers-color-scheme: dark` block). The three `--brand*` tokens are
   additions; the rest of the contract keeps its names.
2. **Component rules**: adopt each delivered rule, then check what the
   old rule for the same selector did that the new one does not, and keep
   that part if a template needs it. Before deleting any selector,
   `grep -rn "<class>" manager/ui/templates` (and `views.py`, which
   builds some class strings).
3. **Keep, because the delivered CSS lacks them and templates use them**:
   `.pre-short`, `.pre-medium`, `.mt-2/.mt-3/.mt-4`, `.nowrap`,
   `.scroll-x` (+ its nowrap cells), `.grid`, `.block`, `.full-width`,
   `form.inline`, the bare `dl/dt/dd` grid (wizard preview and operation
   pages), `.filters select` sizing, `.page-title .muted` and
   `.section-title .muted` (see 5), `.status-bar .actions` right
   alignment if the status bar keeps the buttons on the badge row — look
   at the current markup and decide; the mock puts the buttons on their
   own row under a rule, which also matches the before screenshot.
4. **The log box**: our markup is `#log.logbox` as the scroll container
   with a `<pre>` inside, and `project.html`'s script measures
   `scrollHeight/scrollTop` on the container. The delivered rule styles
   `pre, .logbox` alike and removes our `.logbox pre { border: 0;
   max-height: none; overflow: visible }`. Keep the container/inner split:
   `.logbox` gets the card look, fixed height and scroll; `.logbox pre`
   stays borderless, unbounded, non-scrolling. Otherwise: frame in frame,
   two scrollbars, and the follow-the-end logic breaks.
5. **Section hints**: the delivered `.section-title small, .section-title
   .hint` rule; our templates use `<span class="muted">` inside `h2`.
   Either add `.hint` to those spans or extend the selector with
   `.section-title .muted`. One or the other, not both.
6. **Drop from the delivered CSS**: `html { font-size: 16px }` (overrides
   the user's browser font setting; `rem` scales from it as it should)
   and `.table { display: block; overflow-x: auto }` in the 760 px query
   (breaks table semantics; the `.scroll-x` wrapper already does this).
   Keep `.connections .schemas { display: none }` on narrow screens if
   the column header is hidden too, else drop it.
7. `color-mix()` is used for hover tints. Fine for Chrome/Edge on the
   office LAN (supported since 111); no fallback needed.
8. `button` now styles every button, including "Log out" in the header
   and form buttons in the wizard. Check the wizard's three pages and the
   operation confirmation page; a full-width or block button there may
   need `display: flex` instead of `inline-flex`.

## Template changes (the only ones in this slice)

- `home.html`: `class="button primary"` on "Add sync". The single primary
  action per page; nothing else gets `.primary`.
- `base.html`: `aria-current="page"` on the active nav link (Projects on
  `/` and project pages, Settings on `/admin/`). Resolve from
  `request.resolver_match` or the URL prefix; keep it simple.
- Section hints per item 5 above, if the `.hint` route is chosen.

## Fonts

Inter 400 and 600, self-hosted: `manager/ui/static/ui/fonts/Inter-Regular.woff2`
and `Inter-SemiBold.woff2` from the Inter release archive
(github.com/rsms/inter/releases, `web/` folder; OFL, no attribution
needed). `@font-face` at the top of `app.css` with `url("fonts/…")`
relative to the stylesheet; WhiteNoise serves them, `collectstatic` in
the Dockerfile picks them up. Add `fonts/*.woff2` to nothing else; no CDN.

## Tests and acceptance

- Existing `ui/tests.py` pass unchanged (no view logic changes).
- One template test: the home page contains `button primary` exactly
  once; the nav has exactly one `aria-current="page"`.
- Screenshots after deploy at 1280 px, light and dark, of the project
  list and the Bönsta page, saved next to the "before" set in
  `docs/design/2026-09-28-visual-pass/` as `after-*.png`. Compare against
  `mock-3-project-detail-light.png`: same structure, calm table, only
  "Add sync" saturated on the list page.
- Log panel: scroll up in the live log, wait 10 s, position is kept;
  scroll to the end, wait 10 s, follows. Same as slice 3's fix.
- Wizard pages 1–3 and an operation confirmation page look right with
  the new buttons and inputs.
- Dark mode via the OS setting: badges distinguishable, log readable.
- Deploy note for Nicklas: a rebuild restarts both daemons; until the
  geodiff `TMPDIR` fix (review note 2026-09-28b) is in, pause one project
  before `docker compose up -d --build` and resume it one tick later.

## Not in this slice

Everything functional (slice 10), the admin's styling, any supervisor
change, the `TMPDIR` fix (separate, higher priority, can ship before or
after this).

## Applied (Claude Code, same day)

Merged as instructed: tokens and `@font-face` verbatim, component rules
from the hand-off with what the templates rely on kept (`.pre-short`,
`.pre-medium`, `.mt-*`, `.nowrap`, `.scroll-x`, `.grid`, `.block`,
`.full-width`, `form.inline`, bare `dl/dt/dd`, `.muted`, `code`,
`.page-title .muted`), the log box kept as container + borderless `pre`,
`html { font-size }` and the block-display table dropped, the schemas
column hidden together with its header below 760 px. Inter 4.1 Regular
and SemiBold from the release archive's `web/` folder, with the OFL text
as `fonts/LICENSE-Inter.txt`. Template lines: `button primary` on Add
sync, `aria-current="page"` from `request.path` (Projects on `/`,
`/projects/…`, `/add/…`; Settings on `/admin/…`; none on `/login/`).

Decisions where the brief left room, for Cowork-Claude to veto:

- Section hints: the selector was extended with `.section-title .muted`
  (no template edits) rather than adding `.hint` to the spans.
- Status-bar actions stay on the badge row, right-aligned, without the
  rule the hand-off's `.actions` draws; hooks' action row keeps the
  hand-off style.
- One template word beyond the brief: the active-condition notices use
  `notice warn` for warning conditions (`db_behind`, `check_failed`),
  since the hand-off's default `.notice` is the info pair and a warning
  in blue would read calmer than its badge. `.notice.warn` added in CSS.
- Inputs and selects everywhere in `main` (wizard, confirmations) take
  the hand-off's `.filters select` look, so the wizard pages match.
- `.connections .toggle::before` supplies the chevron glyph the mock had
  in its markup; the hand-off's rotation on `aria-expanded` is kept.

The TMPDIR fix (2026-09-28b) shipped before this slice, so the deploy
note about pausing a project no longer applies. Screenshots and the
in-browser checks (log follow, dark mode, wizard pages) remain for the
server.
