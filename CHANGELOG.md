# Changelog

## Unreleased

- Add wizard, step 2: the schema-name table is fetched with POST. As a
  GET the whole form rode in the query string, and with 18 GeoPackages
  it passed gunicorn's request-line limit, so the table never refreshed.
- Project page: the `isotime` and `ago` filters used `django.utils.timezone.utc`,
  removed in Django 5.0, so every log line with a naive timestamp raised a
  500. Found by the db-tagged test run on the server; the filters now use
  `datetime.timezone.utc` and have tests without a database.
- Tests: the relative-time test asserted UTC as the local zone and failed
  when the test container had `TZ` set; it now pins `TIME_ZONE`.

## v1.0.0 (2026-09-28)

First release. Ten slices (design §14): registry and credentials,
supervisor, consistency check and health, add wizard, hooks and alerts,
remove and re-init as operations, log history, UI foundation, visual
pass, functional polish; plus the release-readiness pass that made the
repository generic. In production at the author's organisation since
2026-09-25. See `docs/review-notes/` for the field history behind the
invariants in `CLAUDE.md`.
