"""
The consistency check (design §6 step 5) and health (§6 State model).

`gather()` collects facts with I/O: the Mergin API, the checkout, the
GeoPackage and the sync database (with the sync's own credentials, §3).
`evaluate()` is pure: given the previous status row and the observation it
sets the new row, the per-connection health and the active conditions.
`run_project_check()` ties them together for one project.

What the sources mean and why the rules look like this is spelled out in
§6; the short version, verified against db-sync 2.3.0 (unchanged in 2.4.0,
which downloads only the sync file into the checkout): the checkout is
pulled first, base then modified are applied, and the base schema comment
is written last, so `db_version < local_version` that persists is an
interrupted pull, GeoPackage vs base catches base falling behind, and
modified vs base is either user edits (fine in two-way) or the remains of
a half-applied pull (when db_version is behind).
"""

import json
import logging
import sqlite3
import time
from contextlib import closing
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path

from django.conf import settings
from django.utils import timezone

from registry import events
from registry.models import SyncProject, SyncStatus

from . import content, dbinspect, mergin_api

log = logging.getLogger("supervisor.check")

Health = SyncProject.Health
Direction = SyncStatus.Direction

HEALTH_ORDER = [Health.FAILED, Health.DIVERGED, Health.BEHIND, Health.STUCK, Health.STALE, Health.UNKNOWN,
                Health.CATCHING_UP, Health.OK]

# Conditions the supervisor raises when the daemon would fail the same way on
# every start (supervisor/exits.py); the check reports them as health `failed`.
DETERMINISTIC_CONDITIONS = ("checkout_inconsistent", "database_missing_postgis")


def health_with_conditions(health, active_conditions):
    """A deterministic daemon failure is health `failed`, whatever the counts say
    (review note 2026-10-20b, finding 2). The check stays the only writer of health."""
    if any(key in (active_conditions or {}) for key in DETERMINISTIC_CONDITIONS):
        return Health.FAILED
    return health

# Key in mismatch_streak that counts consecutive checks with modified != base
# while versions are equal (the stranded-edits rule); table names cannot
# start with "_" in a GeoPackage created by QGIS, and it saves a column.
PENDING_KEY = "_modified_vs_base"


# Text of the Mergin errors that are the server's or the network's problem,
# not this installation's (review note 2026-10-20): condition
# mergin_unreachable with a delayed mail, instead of an error per cycle.
TRANSIENT_API_MARKERS = (
    "502", "503", "504", "429", "upstream connect error", "connection refused", "connection reset",
    "timed out", "timeout", "temporarily unavailable", "service unavailable", "bad gateway",
    "gateway time", "name or service not known", "temporary failure in name resolution",
    "network is unreachable", "remote end closed", "connection aborted",
)
CONFIG_API_MARKERS = ("401", "403", "404", "unauthorized", "forbidden", "not found")


def is_transient_api_error(exc) -> bool:
    """True for outages (5xx, refused, reset, timeout); False for 401/403/404 and anything else."""
    code = getattr(exc, "http_error", None)
    if code in (401, 403, 404):
        return False
    if code in (429, 502, 503, 504):
        return True
    text = str(exc).lower()
    if any(marker in text for marker in CONFIG_API_MARKERS):
        return False
    return any(marker in text for marker in TRANSIENT_API_MARKERS)


@dataclass
class Observed:
    server_version: int | None = None
    local_version: int | None = None
    db_version: int | None = None
    gpkg_count: dict = field(default_factory=dict)
    base_count: dict = field(default_factory=dict)
    modified_count: dict = field(default_factory=dict)
    mergin_permission: str | None = None
    db_writers: list | None = field(default_factory=list)   # None = could not be determined
    error: str = ""      # the check failed; previous values are kept
    warning: str = ""    # a best-effort part failed; the check still counts
    mergin_unreachable: str = ""   # transient API failure: server facts carried forward, condition raised by core
    # Slice 15: content comparison base vs modified ({table: content.shape}) and the
    # GeoPackage's layer list ([{name, geometry_column}]); None = not read.
    content_checked: bool = False
    content_diff: dict = field(default_factory=dict)
    tables: list | None = None


# --- gathering (I/O) ---------------------------------------------------------


def local_version_of(checkout_dir) -> int | None:
    """Version from .mergin/mergin.json in a connection's checkout."""
    path = Path(checkout_dir) / ".mergin" / "mergin.json"
    with open(path, encoding="utf-8") as handle:
        data = json.load(handle)
    return dbinspect.parse_version(data.get("version"))


def _sqlite_ro(gpkg_path):
    # closing(): sqlite3's own context manager commits, it does not close.
    return closing(sqlite3.connect(f"file:{Path(gpkg_path).as_posix()}?mode=ro", uri=True))


def gpkg_tables(gpkg_path) -> list:
    """Feature and attribute tables registered in the GeoPackage."""
    with _sqlite_ro(gpkg_path) as conn:
        rows = conn.execute(
            "SELECT table_name FROM gpkg_contents WHERE data_type IN ('features', 'attributes') ORDER BY 1"
        ).fetchall()
    return [row[0] for row in rows]


def gpkg_counts(gpkg_path, tables) -> dict:
    counts = {}
    with _sqlite_ro(gpkg_path) as conn:
        for table in tables:
            quoted = '"' + table.replace('"', '""') + '"'
            counts[table] = conn.execute(f"SELECT count(*) FROM {quoted}").fetchone()[0]
    return counts


def select_tables(tables, skip, include) -> list:
    if include:
        return [t for t in tables if t in include]
    if skip:
        return [t for t in tables if t not in skip]
    return list(tables)


def db_counts(cursor, schema, tables) -> dict:
    """count(*) per table that exists in `schema`; missing tables are absent from the result."""
    cursor.execute(
        "SELECT table_name FROM information_schema.tables WHERE table_schema = %s AND table_type = 'BASE TABLE'",
        [schema],
    )
    existing = {row[0] for row in cursor.fetchall()}
    counts = {}
    for table in tables:
        if table not in existing:
            continue
        quoted_schema = '"' + schema.replace('"', '""') + '"'
        quoted = '"' + table.replace('"', '""') + '"'
        cursor.execute(f"SELECT count(*) FROM {quoted_schema}.{quoted}")
        counts[table] = cursor.fetchone()[0]
    return counts


WRITERS_SQL = """
WITH tables AS (
    SELECT c.oid, c.relname, c.relowner, c.relacl
    FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
    WHERE n.nspname = %(schema)s AND c.relkind IN ('r', 'p')
),
roles AS (
    SELECT rolname FROM pg_roles
    WHERE rolname <> %(sync_role)s AND rolname NOT LIKE 'pg\\_%%' AND NOT rolsuper
)
SELECT DISTINCT r.rolname
FROM roles r CROSS JOIN tables t
WHERE has_table_privilege(r.rolname, t.oid, 'INSERT')
   OR has_table_privilege(r.rolname, t.oid, 'UPDATE')
   OR has_table_privilege(r.rolname, t.oid, 'DELETE')
UNION
-- A table without explicit grants has relacl NULL, which means the default
-- ACL (owner everything, PUBLIC nothing). acldefault() spells that out;
-- '{}'::aclitem[] would not do, aclexplode() rejects a zero-dimensional array.
SELECT 'PUBLIC'
FROM tables t, aclexplode(COALESCE(t.relacl, acldefault('r', t.relowner))) a
WHERE a.grantee = 0 AND a.privilege_type IN ('INSERT', 'UPDATE', 'DELETE')
ORDER BY 1
"""


def db_writers(cursor, schema, sync_role) -> list:
    """Roles other than the sync role that can write to any table in `schema`.

    has_table_privilege() sees ownership and inherited grants. Superusers are
    left out: they can always write, and listing them would make "one-way"
    unreachable on every installation (§7 note). PUBLIC is included through
    the ACL, since has_table_privilege() cannot name it.
    """
    cursor.execute(WRITERS_SQL, {"schema": schema, "sync_role": sync_role})
    return [row[0] for row in cursor.fetchall()]


def gather(project, connections) -> dict:
    """Observed per connection id. One API call and one DB connection per project."""
    observed = {conn.pk: Observed() for conn in connections}
    if not connections:
        return observed

    # Mergin: server version and permission (project-wide).
    server_version = permission = None
    api_error = ""
    unreachable = ""
    try:
        info = mergin_api.project_info(project.mergin_account, project.mergin_project)
        server_version = mergin_api.version_number(info)
        permission = mergin_api.permission(info)
        if server_version is None:
            api_error = "Mergin API returned no version"
    except Exception as exc:  # noqa: BLE001 - reported in check_error, or as mergin_unreachable
        if is_transient_api_error(exc):
            unreachable = f"Mergin API: {exc}"
        else:
            api_error = f"Mergin API: {exc}"

    # Database: comments, counts, writers (one connection).
    db_error = ""
    writers_error = ""
    content_error = ""
    content_by_conn = {}
    comments = {}
    db_counts_by_conn = {}
    writers_by_conn = {}
    tables_by_conn = {}
    keys_by_conn = {}
    gpkg_error_by_conn = {}
    for conn in connections:
        checkout = conn.checkout_dir
        obs = observed[conn.pk]
        try:
            obs.local_version = local_version_of(checkout)
            gpkg_path = checkout / conn.sync_file
            all_tables = gpkg_tables(gpkg_path)
            tables = select_tables(all_tables, conn.skip_tables, conn.include_tables)
            tables_by_conn[conn.pk] = tables
            obs.gpkg_count = gpkg_counts(gpkg_path, tables)
            geometry = content.gpkg_geometry_columns(gpkg_path)
            obs.tables = [{"name": t, "geometry_column": geometry.get(t)} for t in all_tables]
            keys_by_conn[conn.pk] = content.gpkg_primary_keys(gpkg_path, tables)
        except Exception as exc:  # noqa: BLE001
            gpkg_error_by_conn[conn.pk] = f"checkout: {exc}"

    try:
        with dbinspect.connect(project.database) as db, db.cursor() as cursor:
            comments = dbinspect.base_schema_comments(project.database, [c.base_schema for c in connections])
            for conn in connections:
                tables = tables_by_conn.get(conn.pk, [])
                db_counts_by_conn[conn.pk] = (
                    db_counts(cursor, conn.base_schema, tables),
                    db_counts(cursor, conn.modified_schema, tables),
                )
            # Writers are best-effort: a catalogue quirk here must not take the
            # versions and counts, which decide health, down with it.
            for conn in connections:
                try:
                    writers_by_conn[conn.pk] = db_writers(cursor, conn.modified_schema, project.database.user)
                except Exception as exc:  # noqa: BLE001
                    writers_error = f"writers: {exc}"
                    writers_by_conn[conn.pk] = None
                    db.rollback()
            # Content base vs modified for tables whose counts agree (slice 15,
            # A); a count difference is covered by the count rules already.
            if settings.DBSYNC_CONTENT_CHECK:
                started = time.monotonic()
                for conn in connections:
                    base, modified = db_counts_by_conn.get(conn.pk, ({}, {}))
                    candidates = content.tables_to_compare(tables_by_conn.get(conn.pk, []), base, modified)
                    diffs = {}
                    try:
                        if candidates:
                            base_cols = content.schema_columns(cursor, conn.base_schema)
                            modified_cols = content.schema_columns(cursor, conn.modified_schema)
                            keys = keys_by_conn.get(conn.pk, {})
                            for table in candidates:
                                spec = content.plan(table, keys.get(table, content.DEFAULT_KEY),
                                                    base_cols.get(table, []), modified_cols.get(table, []))
                                if spec is None:
                                    continue
                                diffs[table] = content.compare_table(cursor, conn.base_schema, conn.modified_schema, spec)
                        content_by_conn[conn.pk] = diffs
                    except Exception as exc:  # noqa: BLE001 - best effort, like the writers
                        content_error = f"content: {exc}"
                        db.rollback()
                content_ms = int((time.monotonic() - started) * 1000)
                log.info("content check for %s: %d ms over %d connection(s)", project, content_ms, len(connections))
    except Exception as exc:  # noqa: BLE001
        db_error = f"database: {exc}"

    for conn in connections:
        obs = observed[conn.pk]
        errors = [e for e in (api_error, db_error, gpkg_error_by_conn.get(conn.pk, "")) if e]
        obs.server_version = server_version
        obs.mergin_permission = permission
        obs.mergin_unreachable = unreachable
        comment = comments.get(conn.base_schema) or {}
        obs.db_version = dbinspect.parse_version(comment.get("version"))
        base, modified = db_counts_by_conn.get(conn.pk, ({}, {}))
        obs.base_count, obs.modified_count = base, modified
        obs.db_writers = writers_by_conn.get(conn.pk, [])
        if conn.pk in content_by_conn:
            obs.content_checked = True
            obs.content_diff = content_by_conn[conn.pk]
        if not errors and obs.db_version is None:
            errors.append(f"base schema {conn.base_schema} has no db-sync comment yet")
        obs.error = "; ".join(errors)
        obs.warning = "; ".join(w for w in (writers_error, content_error) if w)
    return observed


# --- evaluation (pure) -------------------------------------------------------


def direction_of(permission, writers):
    """writers None means unknown: a Reader account then gets no verdict."""
    if permission in ("writer", "owner"):
        return Direction.TWO_WAY
    if permission == "reader":
        if writers is None:
            return None
        return Direction.DB_EDITS_CANNOT_PUSH if writers else Direction.ONE_WAY
    return None


def evaluate(status, obs, *, now, check_interval, stuck_after, process_running, label):
    """Update `status` in place from `obs`; return (health, conditions, messages).

    `label` names the connection in condition keys (its sync_file).
    `process_running` is the supervisor's view; the stuck rule needs it.
    Nothing here touches the database; the caller saves `status`.
    """
    conditions = set()
    messages = {}
    status.checked_at = now

    if obs.error:
        status.check_error = obs.error
        last_ok = status.last_ok_check_at
        if last_ok is None:
            return Health.UNKNOWN, conditions, messages
        if now - last_ok > timedelta(seconds=3 * check_interval):
            conditions.add("check_failed")
            messages["check_failed"] = f"consistency check has been failing since {last_ok:%Y-%m-%d %H:%M}: {obs.error}"
            return Health.STALE, conditions, messages
        # A recent good check exists: keep what it said.
        return None, conditions, messages

    # A best-effort part may have failed; shown on the page, but this check counts.
    status.check_error = obs.warning
    status.last_ok_check_at = now

    if obs.mergin_unreachable:
        # The server is temporarily unreachable, not misconfigured: keep what
        # the last successful call said; core raises mergin_unreachable.
        obs.server_version = status.server_version
        obs.mergin_permission = status.mergin_permission

    previous_local = status.local_version
    if obs.local_version != previous_local or status.local_version_changed_at is None:
        status.local_version_changed_at = now
    local_moved_now = status.local_version_changed_at == now and previous_local is not None

    if status.db_version is not None and obs.db_version is not None and obs.db_version > status.db_version:
        status.last_pull_at = now

    status.server_version = obs.server_version
    status.local_version = obs.local_version
    status.db_version = obs.db_version
    status.gpkg_count = dict(obs.gpkg_count)
    status.base_count = dict(obs.base_count)
    status.modified_count = dict(obs.modified_count)
    status.mergin_permission = obs.mergin_permission
    status.db_writers = None if obs.db_writers is None else list(obs.db_writers)
    status.direction = direction_of(obs.mergin_permission, obs.db_writers)

    versions_equal = obs.local_version is not None and obs.local_version == obs.db_version
    streaks = dict(status.mismatch_streak or {})

    # GeoPackage vs base, per table, only when versions agree (§6).
    for table, gpkg_n in obs.gpkg_count.items():
        base_n = obs.base_count.get(table)
        if versions_equal and base_n is not None and gpkg_n != base_n:
            streaks[table] = streaks.get(table, 0) + 1
        else:
            streaks[table] = 0
        if streaks[table] >= 2:
            key = f"divergence:{label}:{table}"
            conditions.add(key)
            messages[key] = (
                f"{label}: table {table} has {gpkg_n} rows in the GeoPackage but {base_n} in the base "
                f"schema for {streaks[table]} checks at version v{obs.local_version}"
            )
    for table in list(streaks):
        if table != PENDING_KEY and table not in obs.gpkg_count:
            del streaks[table]

    # Modified vs base while versions agree: pending database edits.
    pending_tables = [t for t in obs.modified_count if obs.base_count.get(t) is not None and obs.modified_count[t] != obs.base_count[t]]
    if versions_equal and pending_tables:
        streaks[PENDING_KEY] = streaks.get(PENDING_KEY, 0) + 1
    else:
        streaks[PENDING_KEY] = 0
    status.mismatch_streak = streaks

    # Interrupted pull: db_version behind local_version for too long.
    behind = obs.db_version is not None and obs.local_version is not None and obs.db_version < obs.local_version
    if behind and now - status.local_version_changed_at > timedelta(seconds=2 * check_interval):
        key = f"db_behind:{label}"
        conditions.add(key)
        detail = ""
        differing = [t for t in obs.modified_count if obs.base_count.get(t) is not None and obs.modified_count[t] != obs.base_count[t]]
        if differing:
            detail = "; interrupted pull: database differences in " + ", ".join(differing) + " are not user edits and must not be pushed"
        messages[key] = (
            f"{label}: database is at v{obs.db_version} while the checkout is at v{obs.local_version} "
            f"since {status.local_version_changed_at:%Y-%m-%d %H:%M}{detail}"
        )

    # Stranded edits: Reader account, other writers, real pending edits.
    if status.direction == Direction.DB_EDITS_CANNOT_PUSH and streaks[PENDING_KEY] >= 2:
        key = f"stranded_db_edits:{label}"
        conditions.add(key)
        messages[key] = (
            f"{label}: database edits in {', '.join(pending_tables)} can never be pushed "
            f"(account is Reader; writers: {', '.join(obs.db_writers or [])})"
        )

    # Content base vs modified (slice 15, A): a one-way project has no
    # legitimate writer into modified, so any deviation is a condition; in a
    # two-way project it is the normal state between pushes and only shown.
    if obs.tables is not None:
        status.tables = list(obs.tables)
    if obs.content_checked:
        status.content_diff = dict(obs.content_diff)
        status.content_checked_at = now
        if status.direction in (Direction.ONE_WAY, Direction.DB_EDITS_CANNOT_PUSH):
            for table, result in sorted(status.content_diff.items()):
                if not content.total(result):
                    continue
                key = f"content_divergence:{label}:{table}"
                conditions.add(key)
                messages[key] = (
                    f"{label}: table {table}: {content.summarize(result)} between base and modified "
                    "(one-way project: database edits that cannot have come from the field)"
                )

    # Stuck: server ahead, checkout not moving, daemon alive.
    server_ahead = obs.server_version is not None and obs.local_version is not None and obs.server_version > obs.local_version
    stuck = (
        server_ahead
        and process_running
        and now - status.local_version_changed_at > timedelta(seconds=stuck_after)
    )
    if stuck:
        conditions.add("stuck")
        messages["stuck"] = (
            f"{label}: server is at v{obs.server_version}, checkout still at v{obs.local_version} "
            f"since {status.local_version_changed_at:%Y-%m-%d %H:%M} while db-sync is running"
        )

    if any(k.startswith(("divergence:", "stranded_db_edits:", "content_divergence:")) for k in conditions):
        health = Health.DIVERGED
    elif any(k.startswith("db_behind:") for k in conditions):
        health = Health.BEHIND
    elif stuck:
        health = Health.STUCK
    elif server_ahead and local_moved_now:
        health = Health.CATCHING_UP
    elif server_ahead:
        # Ahead but not yet stuck for long enough; still catching up in effect.
        health = Health.CATCHING_UP
    elif behind:
        # Behind but within the pull window: not alarming yet.
        health = Health.CATCHING_UP
    else:
        health = Health.OK
    return health, conditions, messages


def worst(healths):
    healths = [h for h in healths if h]
    if not healths:
        return Health.UNKNOWN
    return min(healths, key=HEALTH_ORDER.index)


# --- per project -------------------------------------------------------------


def run_project_check(project, *, process_running: bool, now=None):
    """Gather, evaluate and save for every connection. Returns (health, conditions, messages)."""
    now = now or timezone.now()
    connections = list(project.connections.all())
    observed = gather(project, connections)

    healths = []
    conditions = set()
    messages = {}
    for conn in connections:
        status, _ = SyncStatus.objects.get_or_create(connection=conn, defaults={"project": project})
        previous_health = getattr(status, "_health", None)
        content_before = {t for t, r in (status.content_diff or {}).items() if content.total(r)}
        health, conds, msgs = evaluate(
            status,
            observed[conn.pk],
            now=now,
            check_interval=settings.DBSYNC_CHECK_INTERVAL,
            stuck_after=settings.DBSYNC_STUCK_AFTER,
            process_running=process_running,
            label=conn.sync_file,
        )
        status.save()
        if observed[conn.pk].content_checked and not any(k.startswith("content_divergence:") for k in conds):
            # Two-way: no condition, but the history shows when it started and ended.
            content_after = {t: r for t, r in (status.content_diff or {}).items() if content.total(r)}
            if content_after and not content_before:
                events.info(project, f"{conn.sync_file}: content differs between base and modified: "
                                     + "; ".join(f"{t}: {content.summarize(r)}" for t, r in sorted(content_after.items())))
            elif content_before and not content_after:
                events.info(project, f"{conn.sync_file}: base and modified agree again")
        if health is None:
            health = project.health if project.health != Health.UNKNOWN else previous_health
        healths.append(health)
        conditions |= conds
        messages.update(msgs)

    unreachable = next((observed[c.pk].mergin_unreachable for c in connections if observed[c.pk].mergin_unreachable), "")
    if unreachable:
        conditions.add("mergin_unreachable")
        messages["mergin_unreachable"] = unreachable

    health = health_with_conditions(worst(healths), project.active_conditions)
    if health != project.health:
        SyncProject.objects.filter(pk=project.pk).update(health=health, health_since=now)
        project.health = health
        project.health_since = now
    return health, conditions, messages
