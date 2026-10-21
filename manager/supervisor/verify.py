"""
Content verification GeoPackage <-> base, on demand (slice 15, B).

The pair that reveals a failed apply (checkout advanced, database not) is
the checkout GeoPackage against the `base` schema: the comparison db-sync
runs itself after a fresh download, before it says "'base' schema is not
synchronized with source GPKG". db-sync does it with the geodiff CLI
(`_compare_datasets` in the vendored source: `geodiff diff` between the
sqlite and postgres drivers, then `geodiff as-summary`); the same two
commands are run here, read-only, while the daemon keeps running. A
difference raises `checkout_inconsistent`, the condition slice 14 raises
for the daemon's own detection, with the same mail and Re-init path.

Runs as the operation `verify` (operations.VERIFY_STEPS): restart-safe,
logged, one open operation per project, started from the project page,
the API or `manage.py verify`.
"""

import json
import os
import subprocess
import time
from pathlib import Path

from django.conf import settings
from django.db import IntegrityError
from django.utils import timezone

from registry.models import Operation

from . import processes

GEODIFF_EXE = os.environ.get("GEODIFF_EXE", "geodiff")
GEODIFF_TIMEOUT = 600


class VerifyError(Exception):
    """geodiff could not compare; the message is for the operator."""


class VerifyConflict(Exception):
    """Another operation is open on the project."""

    def __init__(self, operation):
        super().__init__(f"operation {operation.pk} ({operation.kind}, {operation.status}) is open")
        self.operation = operation


def filter_args(skip_tables, include_tables) -> list:
    """geodiff's table filter, as db-sync builds it (skip and include are exclusive)."""
    if include_tables:
        return ["--include-tables", ";".join(include_tables)]
    if skip_tables:
        return ["--skip-tables", ";".join(skip_tables)]
    return []


def _run(cmd, env):
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, env=env, timeout=GEODIFF_TIMEOUT)
    except FileNotFoundError as exc:
        raise VerifyError(f"geodiff is not available: {exc}") from exc
    except subprocess.TimeoutExpired as exc:
        raise VerifyError(f"geodiff did not finish within {GEODIFF_TIMEOUT}s") from exc
    if result.returncode != 0:
        # Never the command: its connection string carries the password.
        tail = "\n".join((result.stderr or "").strip().splitlines()[-5:])
        raise VerifyError(f"geodiff {cmd[1]} failed (exit {result.returncode}): {tail}")
    return result


def compare_checkout_with_base(connection, *, tmpdir) -> list:
    """[{table, insert, update, delete}] for tables where the checkout and base differ; [] = in sync."""
    gpkg = Path(connection.checkout_dir) / connection.sync_file
    if not gpkg.exists():
        raise VerifyError(f"no checkout for {connection.sync_file} ({gpkg}); the daemon has not downloaded it yet")
    tmpdir = Path(tmpdir)
    changeset = tmpdir / f"verify-{connection.pk}.diff"
    summary_path = tmpdir / f"verify-{connection.pk}.json"
    for path in (changeset, summary_path):
        if path.exists():
            path.unlink()
    env = dict(os.environ, TMPDIR=str(tmpdir), TMP=str(tmpdir), TEMP=str(tmpdir))
    conn_info = connection.project.database.conn_info(with_password=True)
    try:
        _run([GEODIFF_EXE, "diff", "--driver-1", "sqlite", "", "--driver-2", "postgres", conn_info,
              *filter_args(connection.skip_tables, connection.include_tables),
              str(gpkg), connection.base_schema, str(changeset)], env)
        _run([GEODIFF_EXE, "as-summary", str(changeset), str(summary_path)], env)
        with open(summary_path, encoding="utf-8") as handle:
            data = json.load(handle)
    finally:
        for path in (changeset, summary_path):
            try:
                path.unlink()
            except FileNotFoundError:
                pass
    return differences(data)


def differences(summary_json: dict) -> list:
    """The tables with changes from geodiff's as-summary JSON."""
    tables = []
    for item in (summary_json or {}).get("geodiff_summary", []) or []:
        counts = {k: int(item.get(k, 0) or 0) for k in ("insert", "update", "delete")}
        if any(counts.values()):
            tables.append({"table": item.get("table", "?"), **counts})
    return tables


def verify_project(project, connections=None) -> dict:
    """Compare every connection; the result that the operation stores."""
    connections = list(connections if connections is not None else project.connections.all())
    work_dir = settings.DBSYNC_DATA_DIR / "projects" / str(project.pk)
    tmpdir = processes.private_tmpdir(work_dir / "verify")
    started = time.monotonic()
    result = {"checked_at": timezone.now().isoformat(timespec="seconds"), "ok": True, "connections": {}}
    for connection in connections:
        entry = {"ok": True, "tables": [], "error": ""}
        try:
            entry["tables"] = compare_checkout_with_base(connection, tmpdir=tmpdir)
            entry["ok"] = not entry["tables"]
        except VerifyError as exc:
            entry["ok"] = False
            entry["error"] = str(exc)
        result["connections"][connection.sync_file] = entry
        result["ok"] = result["ok"] and entry["ok"]
    result["seconds"] = round(time.monotonic() - started, 1)
    return result


def describe(result: dict) -> str:
    parts = []
    for sync_file, entry in (result.get("connections") or {}).items():
        if entry.get("error"):
            parts.append(f"{sync_file}: {entry['error']}")
        elif entry.get("tables"):
            tables = ", ".join(f"{t['table']} ({t['insert']}+ {t['update']}~ {t['delete']}-)" for t in entry["tables"])
            parts.append(f"{sync_file}: checkout and base differ in {tables}")
    return "; ".join(parts) if parts else "checkout and base agree for every connection"


def request_verify(project, user=None) -> Operation:
    """Create the operation the supervisor runs on its next tick."""
    try:
        return Operation.objects.create(project=project, kind=Operation.Kind.VERIFY, params={}, requested_by=user)
    except IntegrityError as exc:
        open_operation = project.operations.filter(status__in=Operation.OPEN_STATUSES).order_by("-requested_at").first()
        raise VerifyConflict(open_operation) from exc
