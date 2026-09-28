#!/bin/sh
# Runs once when the bundled PostGIS creates its data volume (the image's
# docker-entrypoint-initdb.d). Creates the registry role and its schema so
# the manager never runs as the postgres superuser (design §12, CLAUDE.md).
# MANAGER_DB_PASSWORD comes from .env; it must not contain a single quote.
set -e
if [ -z "${MANAGER_DB_PASSWORD}" ]; then
    echo "MANAGER_DB_PASSWORD is empty; not creating the dbsync_manager role" >&2
    exit 1
fi
psql -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB" <<EOSQL
CREATE ROLE dbsync_manager LOGIN PASSWORD '${MANAGER_DB_PASSWORD}';
CREATE SCHEMA dbsync_manager AUTHORIZATION dbsync_manager;
EOSQL
echo "created role dbsync_manager and schema dbsync_manager in ${POSTGRES_DB}"
