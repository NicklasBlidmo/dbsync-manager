#!/bin/sh
# Runs once, when the bundled PostGIS creates its data volume (the image's
# docker-entrypoint-initdb.d). Mounted as a single file, numbered after the
# image's own 10_postgis.sh: mounting the whole directory masked that script,
# so the postgis extension was never created and db-sync failed with "Cannot
# find or activate postgis extension" (clean-room install, 2026-09-28).
# An existing data volume is never touched; see README "Database".
#
# Creates:
#   - the postgis extension in POSTGRES_DB (belt and braces, IF NOT EXISTS);
#   - the registry role dbsync_manager owning the schema dbsync_manager, so
#     the manager never runs as the postgres superuser (design §12);
#   - the sync role dbsync with CREATE on the database, when
#     SYNC_DB_PASSWORD is set: the values for the first Add database row.
# Passwords come from .env and must not contain a single quote.
set -e
if [ -z "${MANAGER_DB_PASSWORD}" ]; then
    echo "MANAGER_DB_PASSWORD is empty; not creating the dbsync_manager role" >&2
    exit 1
fi
psql -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB" <<EOSQL
CREATE EXTENSION IF NOT EXISTS postgis;
CREATE ROLE dbsync_manager LOGIN PASSWORD '${MANAGER_DB_PASSWORD}';
CREATE SCHEMA dbsync_manager AUTHORIZATION dbsync_manager;
EOSQL
echo "created role dbsync_manager and schema dbsync_manager in ${POSTGRES_DB}"

if [ -z "${SYNC_DB_PASSWORD}" ]; then
    echo "SYNC_DB_PASSWORD is empty; not creating the sync role dbsync (create one by hand, README 'Database')"
    exit 0
fi
psql -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB" <<EOSQL
CREATE ROLE dbsync LOGIN PASSWORD '${SYNC_DB_PASSWORD}';
GRANT CREATE ON DATABASE "${POSTGRES_DB}" TO dbsync;
EOSQL
echo "created sync role dbsync with CREATE on ${POSTGRES_DB}"
