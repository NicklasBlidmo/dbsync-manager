# Slice 11 brief: release readiness (someone else clones this repo)

*Written by Cowork-Claude 2026-09-28 after an audit of the tracked files
for organisation-specific content and of the from-scratch setup path.
Goal set by Nicklas: another organisation should be able to `git clone`,
follow the README and have a working installation, with or without their
own PostGIS; the repo should carry no Arkeologikonsult-specific text
beyond worked examples (Bönsta as an example is fine).*

## Already done by Cowork-Claude (2026-09-28, uncommitted in the working tree)

- A1 and A2: the two "Arkeologikonsult values" sections are replaced by an
  empty "Installation worksheet" table in `docs/backup.md` and
  `docs/m365-direct-send.md`.
- B, the scrub: all review notes were searched for e-mail addresses, LAN
  addresses, secrets and local paths; six notes were edited (addresses →
  placeholders, `192.0.2.10` for the LAN address). Host and container
  names stay. No secrets were found. What remains of B is the README
  paragraph.

Start with `git status` to see those files, then do A3–A8, the README
paragraph in B, and C.

## Audit result in one paragraph

The code and configuration are already generic: nothing in `manager/`,
`Dockerfile`, `docker-compose.yml`, `.env.example` or `docker/` names the
organisation, and the bundled-PostGIS profile, preflight and key
generator make the fresh path workable. What is not generic is three
"Arkeologikonsult values" sections in docs, one POC script in `tools/`,
a stale README status, two path examples, and the absence of a LICENSE.
The review notes are organisation-specific by nature and are addressed
separately below.

## A. Move out of the repo (done outside: `installationsvarden-gis-ubuntu.md` in the private docs folder)

1. `docs/backup.md`: delete the section "Arkeologikonsult values". The
   document ends after "Restore".
2. `docs/m365-direct-send.md`: delete the section "Arkeologikonsult
   values" (MX host, connector date, pfSense rule, LAN address, From
   addresses). Keep the generic client table with `<your-domain>`.
3. `tools/check_sync.sh`: remove from the repo (`git rm`). It is the POC's
   Swedish-language cron check with organisation defaults and a private
   e-mail address in a comment; the manager's consistency check replaced
   it. Update the README "Layout" line for `tools/` to `backup.sh` only.
4. `code_reuse/`: a directory holding only `patch/`; if nothing references
   it, remove it. `patches/` is the real location.
5. `README.md`, hooks section: the bind-mount example `/opt/ak-db` becomes
   a neutral path (`/opt/sync-scripts`), and the hook example becomes
   `SQLDIR=/opt/sync-scripts/sql /opt/sync-scripts/run_sql.sh
   "$DBSYNC_SCHEMA_PREFIX" all` with a sentence that the scripts are the
   organisation's own.
6. `README.md`, "Status": rewrite. It still says "Slice 7 of 7 … remaining:
   field test of the log history, then the Bönsta takeover". Replace with
   a short "Version" paragraph: v1 complete (design §14, ten slices),
   in production at the author's organisation since 2026-09-25 with a
   worked example of adopting a hand-run sync in design §15; what v1 does
   in five sentences; what is out of scope (forms/metadata sync).
7. `CLAUDE.md`: keep as the maintainer's notes; it names Nicklas and the
   first production target, which is fine for a file addressed to the
   coding assistant of this repo. Replace "raising it with Nicklas" by
   "raising it with the maintainer" if you prefer; nothing else.
8. `docs/dbsync-manager-design.md` §15: reads as a worked example
   (Bönsta, prefixes); keep. Grep it once for e-mail addresses and LAN
   addresses; remove any.

## B. Review notes: keep, and say so

`docs/review-notes/` is the engineering log of the first installation and
the reason many invariants in `CLAUDE.md` exist. It names the server, LAN
addresses, project names and people. Options were: move them out
(CLAUDE.md loses its references and a future maintainer loses the "why"),
scrub them (large edit, small gain), or keep and declare. Decision
proposed: keep, plus one paragraph in the README under a new heading
"Project history":

> `docs/review-notes/` is the field log of the first installation
> (Arkeologikonsult, Sweden, September 2026): what broke, why, and what
> was changed. It refers to that organisation's servers and projects. It
> is kept because `CLAUDE.md` and several design decisions point at it;
> nothing in it is required to run the software.

Before that: grep the notes for `@` (e-mail addresses) and passwords;
LAN addresses and host names may stay. Nicklas decides whether this
holds; if not, the alternative is a private branch or a separate
`dbsync-manager-notes` repo with CLAUDE.md pointing at it by URL.

## C. Setup path

1. **LICENSE** is missing. Without it nobody may legally use the code.
   The base image and db-sync are MIT (Lutra Consulting); the applied
   patch is from their tracker. MIT is the natural choice for a companion
   tool; Nicklas decides the licence and the copyright holder
   (Arkeologikonsult Sverige AB). Add `LICENSE` and a one-line "License"
   section in the README. Inter's OFL is satisfied by shipping the font
   files unchanged; add `manager/ui/static/ui/fonts/OFL.txt` from the
   Inter release.
2. `.env.example` header says the keys are generated with
   `docker compose run --rm --no-deps manager python manage.py generate_keys`,
   which does not exist in that form; the README says
   `python3 manager/generate_keys.py --write .env` or the `--entrypoint
   python … generate_keys.py` form. Make the header match the README.
3. Quick start: add the two things a first-time reader asks after step
   one: where the groups `viewer/operator/admin` come from (created at
   start, log line "groups ensured") and that the first project needs a
   Mergin account with at least Reader on the project and a database row
   whose role owns or may create the sync schemas. One sentence each.
4. Bundled PostGIS: `DATABASE_URL` uses the `postgres` superuser, which
   contradicts the CLAUDE.md rule that the registry role is not a
   superuser. Acceptable for the bundled case only if the README says
   so; better: `docker/postgis-init/10-manager.sql` on the `postgis`
   service that creates `dbsync_manager` role + schema, and
   `.env.example` pointing `DATABASE_URL` at that role. Same mechanism as
   `docker/test/init.sql` already uses for the test profile.
5. `README.md` "Development" section: fine. Add that tests tagged `db`
   need Docker and that the Windows note in CLAUDE.md applies.
6. Version tag: after this slice, `git tag v1.0.0`, and a `CHANGELOG.md`
   with one entry (v1.0.0, date, "first release; ten slices, see
   docs/review-notes for the field history"). Later slices append.

## D. Acceptance: a clean-room install

On gis-ubuntu, without touching the production instance, using a second
Compose project name and the bundled PostGIS:

```sh
git clone <repo> /tmp/dbsync-fresh && cd /tmp/dbsync-fresh
cp .env.example .env && chmod 600 .env
python3 manager/generate_keys.py --write .env
# edit .env: COMPOSE_PROFILES=postgis, POSTGRES_PASSWORD, HTTP_PORT=8091, ALLOWED_HOSTS=192.0.2.10
docker compose -p dbsync-fresh up -d --build
docker compose -p dbsync-fresh logs -f manager     # preflight ok, migrations 0001–0009, groups ensured, tick: no projects
docker compose -p dbsync-fresh exec manager python manage.py createsuperuser
```

Then, in the browser on port 8091, follow only the README: add the Mergin
account, add a database row pointing at the bundled PostGIS (host
`postgis`, a sync role created via psql in that container), run the add
wizard for a small project whose schemas do not exist (the `new` path,
untested on the current code base), see it reach `running` and `ok`, pause
and remove it with drop. Finally `docker compose -p dbsync-fresh down -v`
and `rm -rf /tmp/dbsync-fresh`. Every step that needed knowledge not in
the README is a README bug; list them in the review note of this slice.

## Not in this slice

Part 2 of the project, any UI or supervisor change, publishing the repo
(where it will live, GitHub organisation, issue tracker) — that is a
decision for Nicklas after the clean-room install passes.

## Applied (Claude Code, same day, commit tagged v1.0.0)

| Item | Change |
|---|---|
| A1, A2, B scrub | Cowork-Claude's working-tree edits committed as found |
| A3 | `tools/check_sync.sh` removed; README layout line lists `backup.sh` only |
| A4 | `code_reuse/` no longer existed in the working tree; nothing referenced it |
| A5 | Hooks example uses `/opt/sync-scripts` and says the scripts are the organisation's own |
| A6 | README "Status" replaced by "Version": v1.0.0, production since 2026-09-25, what v1 does, out of scope |
| A7 | CLAUDE.md: "raising it with the maintainer" |
| A8 | Design §11/§11b/§15 keep `ak-db` and Bönsta as the worked example; no addresses found in it |
| B | README "Project history" paragraph as proposed; the notes grep clean for addresses and secrets |
| C1 | `LICENSE` (MIT, Arkeologikonsult Sverige AB, 2026) and a README "License" section; the Inter licence file is `fonts/OFL.txt`. **Licence and holder are the brief's suggestion, for Nicklas to confirm** |
| C2 | `.env.example` header names the two real ways to generate the keys |
| C3 | Quick start explains the groups and what the first project needs |
| C4 | `docker/postgis-init/10-manager.sh` creates `dbsync_manager` role and schema from `MANAGER_DB_PASSWORD` on the bundled PostGIS's first start; compose mounts the directory and passes the variable; `.env.example` points `DATABASE_URL` at that role. **Unverified until the clean-room install** |
| C5 | Development section: db tests need Docker, CLAUDE.md invariants incl. the Windows note |
| C6 | `CHANGELOG.md` with the v1.0.0 entry; tag `v1.0.0` on this commit |
| D | The clean-room install remains for gis-ubuntu; README gaps it finds go into the next review note |
