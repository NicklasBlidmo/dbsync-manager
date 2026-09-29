# 2026-09-29: second clean-room walk (slice 12 acceptance) — findings for slice 12b

*Written by Cowork-Claude from Nicklas's second clean-room install on
gis-ubuntu: fresh clone after slice 12, `sh tools/setup.sh`, bundled
PostGIS, port 8091. From the moment the container was healthy, everything
downstream (Database row from the printed table, wizard `new` path with
18 files, `running` → `ok`, pause, resume, remove with drop) worked with
the README alone. The setup script itself needed help three times, one of
them serious. These are the fixes for slice 12b; nothing else in slice 12
is reopened.*

## 1. `setup.sh` replaced the production installation (serious)

The script's default `COMPOSE_PROJECT_NAME` was `dbsync-manager`, which is
also the production project's name (from `/opt/dbsync-manager`). Running
`docker compose up -d` in `/tmp/dbsync-fresh` therefore recreated
`dbsync-manager-manager-1` from the test `.env` (port 8091, bundled
PostGIS, empty registry) and production on 8090 went down until it was
restored by `docker compose -p dbsync-manager down` in the test directory
and `docker compose up -d` in `/opt/dbsync-manager`. No data was lost
(registry in the external database, `/data` volume untouched), but a less
careful operator would not have noticed for hours.

Fix, all three:
- Default the project name to the directory name (Compose's own default),
  never to a fixed string.
- Before writing `.env`, run `docker compose ls --format json` and refuse
  a project name that already exists, with the message "a Compose project
  named X already runs from <path>; choose another name".
- Print the project name and the port in the final summary, and in the
  summary say "manage this installation from <directory>; `docker
  compose` commands elsewhere do not affect it".

## 2. Generated passwords broke `DATABASE_URL`

The generated `MANAGER_DB_PASSWORD` began with `!?`. In a URL `?` starts
the query string, so preflight saw no host and no database. Fix: generate
passwords from `[A-Za-z0-9]` only (24 characters is plenty), for all three
passwords; the same rule as CLAUDE.md gives for `.env` values. Preflight:
when the URL parses but has no host, check the password part for
`? @ / : # %` and say "the password in DATABASE_URL contains characters
that are not allowed there; use letters and digits or percent-encode it".

## 3. `setup.sh` was not resumable

It aborted on the preflight failure and, once the operator had fixed
`.env` and started the container by hand, nobody ran `createsuperuser`.
Fix: make every step check its own precondition and skip when done
(`.env` complete → skip questions; image built → skip build; container
healthy → skip wait; a superuser exists → skip). On abort, print the
remaining steps as commands. The README says "run the script again to
continue".

## 4. `postgres://` needs one sentence

The operator replaced `postgres://` with the server's address, reading it
as a host. `.env.example` and the README get, next to `DATABASE_URL`: "the
`postgres://` part is the protocol and never changes; the host goes after
the `@`".

## What passed without help

Port check before build, the `.env` questions, the printed Database row
(host `postgis`, role `dbsync_sync`), PostGIS extension present after
init, Test connection, the wizard on 18 files, health `ok`, pause and
resume, remove with drop (36 schemas gone), two instances logged in at
once without logging each other out.

## Applied (Claude Code, same day)

| Item | Change |
|---|---|
| 1 | `setup.sh`: project name defaults to the directory name (lower-cased, Compose's own rule); `docker compose ls -a --format json` is consulted and a name that already runs from another directory is refused with "a Compose project named X already runs from <path>; choose another name" (re-asked with a terminal, fatal without); the summary names project, port and directory and says that `docker compose` elsewhere does not affect it. Also checked on a re-run with a complete `.env` |
| 2 | `gen_secret` gives 24 letters and digits; every password prompt (also `SETUP_*` presets) accepts letters, digits, `-` and `_` only. Preflight: a URL without host whose password holds `? / : # @` says so; the generic message adds that `postgres://` is the protocol. Test in `registry/tests.py` |
| 3 | Every step checks its precondition: complete `.env` → no questions; image `dbsync-manager:local` present → no build; healthy → no wait; superuser → skipped. A failed exit prints the remaining commands; README says "run it again" |
| 4 | `.env.example` and the README, next to `DATABASE_URL`: `postgres://` is the protocol, the host goes after the `@`, the password is letters and digits |

Unverified here (no Docker or Linux): the whole script; its `.env` helpers,
`ask` with the password rule and the project-name refusal are exercised in
a harness on Windows.

## Acceptance for 12b

Third clean-room walk, scripted with the non-interactive variables, run
by Nicklas without reading this note: zero interventions. Then the
production instance is left as it is (no "fresh start" needed: the same
code path has now been proven from empty twice).
