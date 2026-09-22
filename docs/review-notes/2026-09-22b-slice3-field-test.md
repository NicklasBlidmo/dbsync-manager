# Slice 3 field test, 2026-09-22 (evening)

Reviewer: Claude (Cowork session with Nicklas), running commit `ff729c4` on
`gis-ubuntu` with `p3922_test`.

## 1. Every check failed with "ACL arrays must be one-dimensional"

**Cause.** In the PUBLIC part of `WRITERS_SQL`, a table without explicit
grants has `relacl` NULL and the fallback was `'{}'::aclitem[]`. An empty
array literal has zero dimensions and `aclexplode()` requires exactly one.
Every synced table has `relacl` NULL right after init, so every connection
failed.

**Fix.** `COALESCE(c.relacl, acldefault('r', c.relowner))`, with `relacl`
and `relowner` taken in the tables CTE. `acldefault()` is what NULL means:
owner everything, PUBLIC nothing.

**Follow-ups, both done.**
- The writers query is best-effort now: its failure sets a note on the
  status row and leaves `db_writers` unknown (no direction verdict for a
  Reader), but versions and counts are still gathered and health computed.
  A catalogue quirk must not hide the answer to "is the database in sync".
- `db_writers` is tested against a table without grants, with a PUBLIC
  grant, and with the owner not being the sync role (compose profile `test`).

**Why the Mergin permission showed "unknown".** Not a second bug: when a
check fails, `evaluate()` keeps the previous row untouched by design, and
there was no previous successful row. The API had returned `owner`. With
the ACL fix the first successful check stores it.

## 2. The log panel jumped to the top every 10 s

`innerHTML` swap resets the scroll position. `#log` is now the scroll
container (fixed height) and a few lines of script follow the end only if
the reader was at the end before the swap, so reading backwards is not
interrupted.

## Observation

`dbsync.log` shows "Going to log in user" every twelve minutes: db-sync's
own token refresh, harmless, and evidence that the daemon ran through the
evening without a restart.

## Outcome after `f1eee74` (20:50)

Project page for `p3922_test`: `health: ok` since 20:48; all eight
connections at server v6 = checkout v6 = database v6; GeoPackage, base and
modified counts equal per table (1-punkter 603, 1-polygoner 368, 2-grupper
47, 3-terrangobjekt-punkter 107, ...); direction **two-way** with reason
`Mergin permission: owner`, `other writers: none found`. The check has thus
been confirmed against the real sources of truth (§6 step 4) and slice 4 can
build on it. Earlier the same evening: pause/resume verified after `f0c909a`
(daemon stopped and restarted without re-init), and a field edit reached
the database within one cycle (T1).

Not yet exercised: `db_behind` on real data (only in unit tests), `stuck`,
and a Reader account (the test account is owner). The first two are worth
provoking on the clone before the Bönsta takeover.

## Cosmetic, for a later pass

- The per-connection count table overflows its card: the "Modified" column
  is cut off at 1-polygoner, 1-punkter, 3-terrangobjekt-linjer and
  3-terrangobjekt-polygoner. Either give the table its own horizontal
  scroll, shorten the headers (GPKG / base / mod), or move the counts out of
  the cards into one table under them.
- Long schema names wrap mid-card; the pair `modified / base` could be a
  single line in a smaller font, or shown on hover.
- "(superusers not listed)" repeated on every card is noise once read;
  once in the page header is enough.
