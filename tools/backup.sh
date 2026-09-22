#!/bin/sh
# Nightly dump of the database that holds the dbsync-manager registry and
# the synced schemas. See docs/backup.md.
#
#   backup.sh <connection-uri> <target-dir> [keep-days]
#
# Writes <target-dir>/<dbname>-YYYYMMDD-HHMM.pgdump (pg_dump custom format)
# and deletes dumps older than keep-days (default 30). Exit code is
# pg_dump's, so cron mails a failure.
#
# PG_DUMP_CONTAINER=<name>  run pg_dump inside that container (same major
#                           version as the server) instead of on the host.
set -eu

uri="${1:?usage: backup.sh <connection-uri> <target-dir> [keep-days]}"
target="${2:?usage: backup.sh <connection-uri> <target-dir> [keep-days]}"
keep_days="${3:-30}"

dbname=$(printf '%s' "$uri" | sed -e 's#.*/##' -e 's#?.*##')
stamp=$(date +%Y%m%d-%H%M)
out="$target/$dbname-$stamp.pgdump"
tmp="$out.partial"

mkdir -p "$target"
chmod 700 "$target" 2>/dev/null || true

if [ -n "${PG_DUMP_CONTAINER:-}" ]; then
    docker exec -i "$PG_DUMP_CONTAINER" pg_dump -Fc --no-password -d "$uri" > "$tmp"
else
    pg_dump -Fc --no-password -d "$uri" -f "$tmp"
fi
mv "$tmp" "$out"
chmod 600 "$out"

find "$target" -name "$dbname-*.pgdump" -type f -mtime +"$keep_days" -delete

size=$(du -h "$out" | cut -f1)
echo "$(date '+%Y-%m-%d %H:%M:%S') dbsync-manager backup: $out ($size), keeping $keep_days days"
echo "reminder: DBSYNC_MANAGER_SECRET_KEY is not in this dump; it belongs in the password manager"
