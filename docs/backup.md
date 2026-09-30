# Backup and restore

What exists, where it lives, and what losing it means (design §10):

| State | Where | Lost means |
|---|---|---|
| Registry (`dbsync_manager` schema) | PostgreSQL | Projects, credentials and history gone; syncs cannot be restarted |
| Synced schemas (`<prefix>_*` base and modified) | PostgreSQL, same database | The data; base is rebuilt from Mergin, modified is not if it holds unpushed edits |
| Work dirs, generated configs, logs | `/data` volume | Checkouts, rebuilt from Mergin at the version in the base schema comment |
| `DBSYNC_MANAGER_SECRET_KEY` | `.env` on the server | Every stored credential unreadable; re-enter all Mergin accounts and databases |
| `DJANGO_SECRET_KEY` | `.env` on the server | Everyone logged out once; nothing else |

## Routine

1. **Nightly dump of the whole database** that holds the registry and the
   synced schemas: a scheduled hook in the manager (below, recommended) or
   `tools/backup.sh` from cron. One dump gives one consistent snapshot of
   registry and data together, which a schema-only dump does not. `/data`
   is not backed up.
2. **The Fernet key in the organisation's password manager**, entered the
   day `.env` is created and again whenever it changes. It must not sit
   next to the dumps: a dump plus the key is every stored password.
3. **A restore test once**, on a throwaway database, before the routine is
   trusted.

### Scheduled backup from the manager (recommended)

The manager runs scripts with database credentials, masked output, a
history in the UI and a failure e-mail: hooks. A hook of kind
`scheduled` runs daily at a time of day (slice 13, design §8), and the
backup is its first use. Restore stays the manual procedure below.

1. **A run-as connection.** Under Settings → Databases add a row for a
   role that can read everything, for example a `backup` role with
   `pg_read_all_data` (PostgreSQL 14+):

   ```sql
   CREATE ROLE backup LOGIN PASSWORD '...';
   GRANT pg_read_all_data TO backup;
   ```

   With the bundled PostGIS the host is `postgis`; on an existing server
   it is the server's address as seen from the container.
2. **A place for the dumps**, bind-mounted in the server-local
   `docker-compose.override.yml` (ignored by git):

   ```yaml
   services:
     manager:
       volumes:
         - /srv/backup/dbsync:/backup
   ```

   The directory must be owned by the container's `manager` user:

   ```sh
   sudo chown "$(docker compose exec manager id -u manager)" /srv/backup/dbsync
   ```
3. **The hook.** Settings → Hooks → Add: kind `scheduled`, run as the
   `backup` row, `run_at` 02:15, `timeout_s` 3600, `notify_emails` the
   operator, and this command verbatim (`PG*` are set by the manager, so
   `pg_dump` needs no connection arguments):

   ```sh
   set -e
   out=/backup/${PGDATABASE}-$(date +%Y%m%d-%H%M).pgdump
   pg_dump -Fc --no-password -f "$out.partial" && mv "$out.partial" "$out"
   find /backup -name "${PGDATABASE}-*.pgdump" -mtime +30 -delete
   echo "backup: $out ($(du -h "$out" | cut -f1))"
   ```

4. **Run now** once from the Scheduled section on the Projects page and
   check that the run is `ok` and the file exists. Then list the dump
   through the manager container, where `pg_restore` of the right
   version and `/backup` are:

   ```sh
   docker compose exec manager pg_restore -l /backup/<file>.pgdump | grep -c ' SCHEMA - '
   ```

   The count is the number of schemas in the dump (a plain `grep SCHEMA`
   also counts COMMENT and ACL lines; piping the file through
   `docker exec -i` fails with "did not find magic string"). Every night
   after that: `ok` in the section, a mail only when a run fails and
   again when the next one succeeds.

The dump does not contain `DBSYNC_MANAGER_SECRET_KEY`; that key stays in
the password manager (item 2 of the routine).

**Version check.** `pg_dump` must not be older than the server's major
version, or it refuses with "server version mismatch". Compare
`docker compose exec manager pg_dump --version` with
`SELECT version()` on the server. If the image's client is older (the
base image ships the distribution's client; the bundled PostGIS is 16),
set `PG_CLIENT_MAJOR=16` in `.env` and rebuild (`docker compose build`):
the Dockerfile then installs that client from apt.postgresql.org.

**Manager hook or cron?** The hook gives a failure mail and a history in
the UI, and runs inside the container with the same `pg_dump` for every
installation. Cron on the host is independent of the manager being up.
Installations that prefer cron keep `tools/backup.sh`:

### Cron

On the database host, as a user that can run `pg_dump` against the server
(a read-only role is enough) and write to the backup location:

```sh
# /etc/cron.d/dbsync-manager-backup
15 02 * * * backup /opt/dbsync-manager/tools/backup.sh \
  "postgres://backup:***@127.0.0.1:5432/gis" /srv/backup/dbsync-manager 30 \
  >> /var/log/dbsync-manager-backup.log 2>&1
```

The three arguments are the connection URI, the target directory and the
number of daily dumps to keep. Put the password in `~/.pgpass` of the cron
user instead of in the URI where the crontab allows it.

When PostgreSQL runs in a container, run `pg_dump` from a container with the
same major version, for example:

```sh
docker exec <postgres-container> pg_dump -Fc -U <role> -d <db> > dump.pgdump
```

`tools/backup.sh` does this for you when `PG_DUMP_CONTAINER` is set.

## Restore

The dump is in custom format (`-Fc`), so parts can be restored separately.

**Everything, into an empty database:**

```sh
pg_restore -d <db> --no-owner --role=<owner> dump.pgdump
```

**The registry only** (for example after a bad migration):

```sh
pg_restore -d <db> -n dbsync_manager --clean --if-exists dump.pgdump
```

Then start the manager with the **same** `DBSYNC_MANAGER_SECRET_KEY` as when
the dump was taken. With a different key every credential decrypts to an
error; the fix is to re-enter them in the UI, not to hunt for the old key.

**One synced project** (schemas `<prefix>_*`):

```sh
pg_restore -d <db> -n '<prefix>_s_*' -n '<prefix>_b_*' --clean --if-exists dump.pgdump
```

After restoring synced schemas, the project's checkout in `/data` may be at
a newer version than the restored base schema. Use re-init from the
database side if the restored data is the source of truth, or let the
manager re-download the checkout (delete the project's work dir; db-sync
downloads the version in the base schema comment on the next start).
Design §10 "State, backup and recovery" describes choosing the source.

## Installation worksheet

Fill in for your installation and keep it with the organisation's own
documentation, not in this repository.

| Item | Value |
|---|---|
| Database (name, server or container) | |
| Dump target (directory or share) | |
| Retention (days) | |
| Cron user and schedule | |
| Key store for `DBSYNC_MANAGER_SECRET_KEY` | |
| Restore test done (date, by whom) | |
| Schemas that hold hand-made corrections and need a separate dump before any cleanup | |
