# dbsync-manager — notes for Claude

Read first: `docs/dbsync-manager-design.md` (§ numbers in code comments refer
to it) and the newest file in `docs/review-notes/`. Review notes record
changes made outside a coding session and, more importantly, *why*; read them
before touching the files they list, and do not revert those changes without
raising it with Nicklas.

Invariants learned the hard way:

- Every manager table, Django's own included, lives in schema
  `dbsync_manager`. Nothing in `public`, nothing in synced schemas. The
  schema is created on `pre_migrate` (`registry/schema.py`) because Django
  creates `django_migrations` before the first migration runs.
- The registry role is not a superuser. Test database work with a role that
  has only `CREATE` on the database, or only ownership of a pre-created
  `dbsync_manager` schema. A superuser hides permission bugs.
- Never `CREATE SCHEMA IF NOT EXISTS`: PostgreSQL checks `CREATE` on the
  database before `IF NOT EXISTS`. Check `pg_namespace`, then create.
- No tracked file should need editing to deploy. Differences between
  installations go in `.env` (design §10). The bundled PostGIS is opt-in via
  the Compose profile `postgis`; the default is an external server.
- Static files are served by WhiteNoise. The admin must be checked with
  `DJANGO_DEBUG=false`, which is how the image runs.
- The first production target already runs a live, hand-configured db-sync
  against the same database. Two daemons must never sync the same schemas.

Working agreements:

- The Docker build has not been verified yet (no Docker on the development
  machine). Say so rather than assuming it works.
- Code, comments and docs in this repository are in English.
