"""
A project's state and table list as data (slice 15, C and D): what the
project page shows, in one structure, for the page's own sections and the
read-only API. Nothing is computed here that the check, the log tail and
the operations have not already recorded.
"""

from registry.models import Operation, SyncRun

CONTENT_KINDS = ("only_base", "only_modified", "changed")


def _iso(value):
    return value.isoformat() if value else None


def project_direction(statuses):
    """The project's direction from its connections': the most cautious wins."""
    directions = {s.direction for s in statuses if s.direction}
    if not directions:
        return None
    if "db_edits_cannot_push" in directions:
        return "db_edits_cannot_push"
    if "two_way" in directions:
        return "two_way"
    return "one_way"


# --- C: tables -------------------------------------------------------------------


def layers_for(connection, status) -> list:
    """Every layer of the connection's GeoPackage (the check's list), with the
    schemas it syncs into and whether skip_tables/include_tables leaves it out."""
    tables = list((status.tables if status is not None else None) or [])
    skip, include = set(connection.skip_tables or []), set(connection.include_tables or [])
    layers = []
    for entry in tables:
        name = entry.get("name")
        if include and name not in include:
            skipped_by = "include_tables"
        elif skip and name in skip:
            skipped_by = "skip_tables"
        else:
            skipped_by = None
        layers.append({
            "name": name,
            "modified_schema": connection.modified_schema,
            "base_schema": connection.base_schema,
            "geometry_column": entry.get("geometry_column"),
            "synced": skipped_by is None,
            "skipped_by": skipped_by,
        })
    return layers


def tables_for(project) -> list:
    statuses = {s.connection_id: s for s in project.statuses.all()}
    result = []
    for connection in project.connections.all():
        status = statuses.get(connection.pk)
        result.append({
            "sync_file": connection.sync_file,
            "modified_schema": connection.modified_schema,
            "base_schema": connection.base_schema,
            "checked_at": _iso(status.checked_at) if status is not None else None,
            "layers": layers_for(connection, status),
        })
    return result


# --- D: state ----------------------------------------------------------------------


def _content(status) -> dict:
    """{table: counts and sample} for the tables with a difference."""
    out = {}
    for table, result in (status.content_diff or {}).items():
        if sum(int(result.get(k, 0) or 0) for k in CONTENT_KINDS):
            out[table] = {k: result.get(k, 0) for k in CONTENT_KINDS} | {"sample": result.get("sample", [])}
    return out


def connection_state(connection, status) -> dict:
    data = {
        "sync_file": connection.sync_file,
        "modified_schema": connection.modified_schema,
        "base_schema": connection.base_schema,
        "checked_at": None, "check_error": "", "server_version": None, "local_version": None, "db_version": None,
        "mergin_permission": None, "direction": None, "last_pull_at": None,
        "gpkg_count": {}, "base_count": {}, "modified_count": {}, "content_divergence": {},
    }
    if status is None:
        return data
    data.update({
        "checked_at": _iso(status.checked_at), "check_error": status.check_error,
        "server_version": status.server_version, "local_version": status.local_version, "db_version": status.db_version,
        "mergin_permission": status.mergin_permission, "direction": status.direction,
        "last_pull_at": _iso(status.last_pull_at),
        "gpkg_count": dict(status.gpkg_count or {}), "base_count": dict(status.base_count or {}),
        "modified_count": dict(status.modified_count or {}), "content_divergence": _content(status),
    })
    return data


def operation_for(operation) -> dict:
    return {
        "id": operation.pk,
        "kind": operation.kind,
        "status": operation.status,
        "step": operation.step,
        "steps": list(operation.steps or []),
        "error": operation.error,
        "params": {k: v for k, v in (operation.params or {}).items() if k != "result"},
        "result": (operation.params or {}).get("result"),
        "requested_at": _iso(operation.requested_at),
        "requested_by": operation.requested_by.get_username() if operation.requested_by_id else None,
        "finished_at": _iso(operation.finished_at),
    }


def verification_for(project):
    """The latest verify operation (slice 15, B), or None."""
    operation = project.operations.filter(kind=Operation.Kind.VERIFY).order_by("-requested_at").first()
    if operation is None:
        return None
    result = (operation.params or {}).get("result") or {}
    data = operation_for(operation)
    data["ok"] = result.get("ok") if operation.status == Operation.Status.DONE else None
    return data


def state_for(project) -> dict:
    statuses = {s.connection_id: s for s in project.statuses.all()}
    checked = [s for s in statuses.values() if s.checked_at]
    servers = [s.server_version for s in checked if s.server_version is not None]
    locals_ = [s.local_version for s in checked if s.local_version is not None]
    dbs = [s.db_version for s in checked if s.db_version is not None]
    confirmed = (
        SyncRun.objects.filter(project=project, kind=SyncRun.Kind.PULL, ok=True, finished_at__isnull=False)
        .order_by("-finished_at").values_list("finished_at", flat=True).first()
    )
    attempt = (
        SyncRun.objects.filter(project=project, kind__in=(SyncRun.Kind.PULL, SyncRun.Kind.PUSH))
        .order_by("-started_at").values_list("started_at", flat=True).first()
    )
    active = dict(project.active_conditions or {})
    connections = [connection_state(c, statuses.get(c.pk)) for c in project.connections.all()]
    return {
        "id": project.pk,
        "mergin_project": project.mergin_project,
        "mergin_account": project.mergin_account.name,
        "database": project.database.name,
        "schema_prefix": project.schema_prefix,
        "desired_state": project.desired_state,
        "process_state": project.process_state,
        "process_reason": project.process_reason,
        "health": project.health,
        "health_since": _iso(project.health_since),
        "direction": project_direction(list(statuses.values())),
        "versions": {
            "server": max(servers) if servers else None,
            "checkout": min(locals_) if locals_ else None,
            "database": min(dbs) if dbs else None,
        },
        "last_pull_confirmed_at": _iso(confirmed),
        "last_attempt_at": _iso(attempt),
        "last_check_at": _iso(max((s.checked_at for s in checked), default=None)),
        "active_conditions": active,
        "count_divergence": sorted(k for k in active if k.startswith(("divergence:", "db_behind:", "stranded_db_edits:"))),
        "content_divergence": {c["sync_file"]: c["content_divergence"] for c in connections if c["content_divergence"]},
        "last_verification": verification_for(project),
        "connections": connections,
    }
