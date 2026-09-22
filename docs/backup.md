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
   synced schemas, with `tools/backup.sh`. One dump gives one consistent
   snapshot of registry and data together, which a schema-only dump does
   not. `/data` is not backed up.
2. **The Fernet key in the organisation's password manager**, entered the
   day `.env` is created and again whenever it changes. It must not sit
   next to the dumps: a dump plus the key is every stored password.
3. **A restore test once**, on a throwaway database, before the routine is
   trusted.

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

## Arkeologikonsult values

Filled in for the first installation; other installations replace them.

| Item | Value |
|---|---|
| Database | `ak` in container `ak-postgis` on gis-ubuntu |
| Dump target | the archive share used by the `pre_remove` hook |
| Key store | the company password manager, entry "dbsync-manager Fernet key" |
| Before cleaning any hand-run sync | `pg_dump -n 'p3922_bonsta*'`, since `kontext_grupp` and `sektion_kontext` may hold hand-made corrections |
