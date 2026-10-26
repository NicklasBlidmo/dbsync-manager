"""
Rebase conflicts made visible (slice 17, A; brief 2026-10-26c).

When both sides of a two-way project changed between two cycles, db-sync's
pull() runs `geodiff rebase-db`: the database's changes are rewritten on
top of the server's. The same column of the same row is a conflict: the
server's value stands, the database's value is dropped and written by
geodiff to `<TMPDIR>/<project name>-dbsync-pull-conflicts` (only when
there were conflicts; geodiff removes the file first). db-sync never looks
at that file, and the log says nothing at INFO. The supervisor's per-daemon
TMPDIR is ours, so every tick (and before a start, which empties TMPDIR)
the file is moved to `/data/projects/<id>/conflicts/<time>-<file>.json`,
the newest KEEP are kept, and one event is written.

geodiff's file (changesetutils.cpp conflictsToJSON):

    {"geodiff": [{"table": "punkter", "type": "conflict", "fid": "17",
                  "changes": [{"column": 3, "base": ..., "old": <server>, "new": <database>}]}]}

`column` is the column's position in the table, `old` is the server's
value ("theirs"), `new` the database's ("ours"); an update that met the
server's delete has no `old`. Column names come from the modified schema
when the database answers, else "column #3".

Nothing here raises a condition in a two-way project: a rebase conflict
is what two-way means. `SyncProject.mail_on_conflict` sends a mail per
event for installations where a dropped database edit is news. In a
one-way project nothing may write `modified`, so a rebase there is the
same error-level condition as content divergence (`content_divergence:
<file>:<table>`), with its mail.
"""

import json
import logging
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

from django.utils import timezone

from registry import events
from registry.models import Event, SyncStatus

from . import alerts, conditions

log = logging.getLogger("supervisor.conflicts")

SUBDIR = "conflicts"
KEEP = 50
TMP_SUFFIX = "-dbsync-pull-conflicts"
SAVED_RE = re.compile(r"^(?P<ts>\d{8}T\d{6})-(?P<label>[A-Za-z0-9._-]+)\.json$")
FID_LIST_MAX = 12
MAIL_CONDITION = "rebase_conflict"


@dataclass
class Conflict:
    table: str
    fid: str
    columns: list = field(default_factory=list)   # [{"column": int|str, "base", "server", "database"}]
    deleted_on_server: bool = False               # database update vs server delete: delete wins


def parse(text: str) -> list:
    """geodiff's JSON → [Conflict]. Anything that is not its shape is an empty list."""
    try:
        data = json.loads(text)
    except ValueError:
        return []
    entries = data.get("geodiff") if isinstance(data, dict) else None
    result = []
    for entry in entries or []:
        if not isinstance(entry, dict):
            continue
        changes = entry.get("changes") or []
        columns = []
        for change in changes:
            if not isinstance(change, dict):
                continue
            columns.append({
                "column": change.get("column"),
                "base": change.get("base"),
                "server": change.get("old"),
                "database": change.get("new"),
            })
        deleted = bool(changes) and all("old" not in c for c in changes if isinstance(c, dict))
        result.append(Conflict(table=str(entry.get("table", "?")), fid=str(entry.get("fid", "?")),
                               columns=columns, deleted_on_server=deleted))
    return result


def summarize(conflicts: list) -> dict:
    """{table: {"count", "fids", "deleted_on_server", "columns"}}, tables sorted."""
    summary = {}
    for conflict in conflicts:
        entry = summary.setdefault(conflict.table, {"count": 0, "fids": [], "deleted_on_server": 0, "columns": []})
        entry["count"] += 1
        entry["fids"].append(conflict.fid)
        if conflict.deleted_on_server:
            entry["deleted_on_server"] += 1
        for column in conflict.columns:
            name = column["column"]
            if name not in entry["columns"]:
                entry["columns"].append(name)
    return dict(sorted(summary.items()))


def name_columns(summary: dict, names_by_table: dict) -> dict:
    """Replace column positions by names where `names_by_table` ({table: [names in order]}) knows them."""
    for table, entry in summary.items():
        names = names_by_table.get(table) or []
        entry["columns"] = [
            names[c] if isinstance(c, int) and 0 <= c < len(names) else (c if isinstance(c, str) else f"column #{c}")
            for c in entry["columns"]
        ]
    return summary


def describe(summary: dict, saved_name: str) -> str:
    """The event text: one line per table, the fids when there are few."""
    parts = []
    for table, entry in summary.items():
        text = f"{entry['count']} conflict{'s' if entry['count'] != 1 else ''} in {table}"
        if entry["columns"]:
            text += f" ({', '.join(str(c) for c in entry['columns'])})"
        if entry["deleted_on_server"]:
            text += f", {entry['deleted_on_server']} deleted on the server"
        fids = entry["fids"]
        if len(fids) <= FID_LIST_MAX:
            text += f"; fid {', '.join(fids)}"
        else:
            text += f"; fid {', '.join(fids[:FID_LIST_MAX])} and {len(fids) - FID_LIST_MAX} more"
        parts.append(text)
    return (f"rebase: {'; '.join(parts)}: database values dropped (server wins); see {SUBDIR}/{saved_name}")


# --- files ------------------------------------------------------------------------------


def saved_dir(work_dir) -> Path:
    return Path(work_dir) / SUBDIR


def _stamp(path: Path) -> datetime:
    try:
        return datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.get_default_timezone())
    except OSError:
        return timezone.localtime()


def _slug(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", text).strip("._-") or "project"


def pending_files(tmpdir) -> list:
    """geodiff's conflict files waiting in a daemon's TMPDIR."""
    try:
        return sorted(p for p in Path(tmpdir).iterdir() if p.is_file() and p.name.endswith(TMP_SUFFIX))
    except OSError:
        return []


def save(path: Path, work_dir, label: str, at: datetime | None = None) -> Path:
    """Move a conflict file into the project's conflicts dir; keep the newest KEEP."""
    at = at or _stamp(path)
    target_dir = saved_dir(work_dir)
    target_dir.mkdir(parents=True, exist_ok=True)
    base = f"{at:%Y%m%dT%H%M%S}-{_slug(label)}"
    target = target_dir / f"{base}.json"
    n = 1
    while target.exists():
        n += 1
        target = target_dir / f"{base}-{n}.json"
    os.replace(path, target)
    prune(work_dir)
    return target


def prune(work_dir, keep: int = KEEP) -> int:
    files = sorted(saved_dir(work_dir).glob("*.json")) if saved_dir(work_dir).is_dir() else []
    removed = 0
    for path in files[:-keep] if len(files) > keep else []:
        try:
            path.unlink()
            removed += 1
        except OSError:
            pass
    return removed


def saved_entry(path: Path) -> dict:
    """What the page and the API show for one saved file."""
    match = SAVED_RE.match(path.name)
    at = None
    label = ""
    if match:
        try:
            at = timezone.make_aware(datetime.strptime(match["ts"], "%Y%m%dT%H%M%S"), timezone.get_default_timezone())
        except ValueError:
            at = None
        label = match["label"]
    if at is None:
        at = _stamp(path)
    try:
        summary = summarize(parse(path.read_text(encoding="utf-8")))
    except OSError:
        summary = {}
    return {
        "name": path.name,
        "at": at,
        "connection": label,
        "tables": {t: {"count": e["count"], "fids": e["fids"], "deleted_on_server": e["deleted_on_server"]}
                   for t, e in summary.items()},
        "count": sum(e["count"] for e in summary.values()),
        "size": path.stat().st_size if path.exists() else 0,
    }


def list_saved(work_dir) -> list:
    """Newest first."""
    directory = saved_dir(work_dir)
    if not directory.is_dir():
        return []
    return [saved_entry(p) for p in sorted(directory.glob("*.json"), reverse=True)]


def overview(work_dir, days: int = 30) -> dict:
    """For the API: {last_at, count_30d} from the file names alone."""
    directory = saved_dir(work_dir)
    names = sorted(p.name for p in directory.glob("*.json")) if directory.is_dir() else []
    last_at = None
    recent = 0
    cutoff = timezone.now() - timedelta(days=days)
    for name in names:
        match = SAVED_RE.match(name)
        if not match:
            continue
        try:
            at = timezone.make_aware(datetime.strptime(match["ts"], "%Y%m%dT%H%M%S"), timezone.get_default_timezone())
        except ValueError:
            continue
        last_at = at if last_at is None or at > last_at else last_at
        if at >= cutoff:
            recent += 1
    return {"last_at": last_at, "count_30d": recent}


def safe_name(name: str) -> bool:
    return bool(SAVED_RE.match(name)) or bool(re.match(r"^\d{8}T\d{6}-[A-Za-z0-9._-]+-\d+\.json$", name))


# --- the supervisor's step ------------------------------------------------------------------


def _connection_for(project, tables: list):
    """The connection whose GeoPackage holds these layers, from the last check's table list."""
    wanted = set(tables)
    for status in SyncStatus.objects.filter(project=project).select_related("connection"):
        names = {t.get("name") for t in (status.tables or [])}
        if wanted and wanted <= names:
            return status.connection, status.direction
    return None, None


def _column_names(project, connection, tables: list) -> dict:
    """{table: [column names in order]} from the modified schema; {} when the database does not answer."""
    if connection is None:
        return {}
    try:
        from . import content, dbinspect

        with dbinspect.connect(project.database) as db, db.cursor() as cursor:
            columns = content.schema_columns(cursor, connection.modified_schema)
        return {t: [name for name, _ in columns.get(t, [])] for t in tables}
    except Exception as exc:  # noqa: BLE001 - names are a nicety
        log.info("column names for %s not available: %s", project, exc)
        return {}


def collect(project, tmpdir, work_dir) -> list:
    """Move every waiting conflict file, write its event, mail when asked. Returns the saved entries."""
    saved = []
    for path in pending_files(tmpdir):
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as exc:
            log.warning("cannot read %s: %s", path, exc)
            continue
        conflicts = parse(text)
        if not conflicts:
            try:
                path.unlink()
            except OSError:
                pass
            continue
        summary = summarize(conflicts)
        tables = list(summary)
        connection, direction = _connection_for(project, tables)
        label = connection.sync_file if connection is not None else project.mergin_project.split("/", 1)[-1]
        target = save(path, work_dir, label)
        name_columns(summary, _column_names(project, connection, tables))
        message = describe(summary, target.name)
        one_way = direction in (SyncStatus.Direction.ONE_WAY, SyncStatus.Direction.DB_EDITS_CANNOT_PUSH)
        if one_way and connection is not None:
            # Nothing may write `modified` in a one-way project: the same
            # condition as content divergence, error level, mailed. The
            # check keeps it while base and modified differ and resolves it
            # when they agree again (with DBSYNC_CONTENT_CHECK off it is
            # resolved at the next check instead).
            for table in tables:
                conditions.activate(
                    project, f"content_divergence:{connection.sync_file}:{table}",
                    f"{connection.sync_file}: {message} (one-way project: database edits that cannot have come "
                    "from the field were rebased)", level=Event.Level.ERROR,
                )
        else:
            events.warning(project, message, source=Event.Source.DBSYNC)
            if project.mail_on_conflict:
                alerts.notify(project, MAIL_CONDITION, message)
        saved.append({"path": target, "summary": summary, "connection": connection, "message": message})
        log.info("conflict file for %s saved as %s", project, target.name)
    return saved
