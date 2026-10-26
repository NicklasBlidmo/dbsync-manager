"""
Read-only JSON API v1 (slice 15, E), plus the one write action that
establishes the pattern: POST verify. Plain Django views, no framework.
Fields are added, never renamed or removed within v1 (docs/api.md).
"""

import json

from django.http import JsonResponse
from django.shortcuts import get_object_or_404

from registry import events
from registry.models import Event, Operation, SyncProject
from supervisor import conflicts as conflicts_mod
from supervisor import facts
from supervisor import verify as verify_mod

from .auth import api_view, error

PROJECT_FIELDS = ("mergin_account", "database")


def _projects():
    return (
        SyncProject.objects.filter(removed_at__isnull=True)
        .select_related(*PROJECT_FIELDS)
        .prefetch_related("statuses", "connections")
        .order_by("mergin_project")
    )


@api_view(perm="registry.view_syncproject")
def projects(request):
    return JsonResponse({"projects": [facts.state_for(p) for p in _projects()]})


@api_view(perm="registry.view_syncproject")
def project(request, pk):
    row = get_object_or_404(_projects(), pk=pk)
    return JsonResponse({"project": facts.state_for(row), "tables": facts.tables_for(row)})


@api_view(perm="registry.add_operation", methods=("POST",))
def verify(request, pk):
    row = get_object_or_404(SyncProject.objects.filter(removed_at__isnull=True), pk=pk)
    try:
        operation = verify_mod.request_verify(row, request.api_user)
    except verify_mod.VerifyConflict as exc:
        return JsonResponse({"error": str(exc), "operation": facts.operation_for(exc.operation)}, status=409)
    events.record(row, Event.Level.INFO,
                  f"{request.api_user.get_username()} requested verify through the API (token '{request.api_token.name}', "
                  f"operation {operation.pk})", source=Event.Source.USER, user=request.api_user)
    return JsonResponse({"operation": facts.operation_for(operation)}, status=202)


@api_view(perm="registry.view_syncproject")
def conflicts(request, pk):
    """Slice 17, A3: the saved rebase conflict files, newest first."""
    row = get_object_or_404(SyncProject.objects.filter(removed_at__isnull=True), pk=pk)
    return JsonResponse({"conflicts": facts.conflicts_for(row)})


@api_view(perm="registry.view_syncproject")
def conflict_file(request, pk, name):
    """One saved file as geodiff wrote it (table, fid, per column base/old=server/new=database)."""
    row = get_object_or_404(SyncProject.objects.filter(removed_at__isnull=True), pk=pk)
    path = conflicts_mod.saved_dir(row.work_dir) / name
    if not conflicts_mod.safe_name(name) or not path.is_file():
        return error(404, f"no conflict file {name}")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except ValueError:
        return error(404, f"conflict file {name} is not JSON")
    return JsonResponse({"name": name, "geodiff": data.get("geodiff", []) if isinstance(data, dict) else []})


@api_view(perm="registry.view_operation")
def operation(request, pk):
    row = Operation.objects.filter(pk=pk).select_related("requested_by").first()
    if row is None:
        return error(404, f"no operation {pk}")
    return JsonResponse({"operation": facts.operation_for(row)})
