"""
Pages of design §9, server-rendered with HTMX partials.

Slice 3: Projects (home) with the three badges and filters, and the
project page with status per connection, direction facts, events, log
tail and pause/resume. Remove, re-init and "run hook now" arrive with the
operations and hooks slices; the add wizard with slice 4.
"""

from django.conf import settings
from django.contrib.auth.decorators import login_required, permission_required
from django.http import HttpResponseBadRequest, HttpResponseNotAllowed
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from registry import events
from registry.models import Event, SyncProject
from supervisor.logs import tail
from supervisor.processes import LOG_NAME, STDERR_NAME

EVENTS_ON_PAGE = 50
LOG_LINES = 100


def _projects_queryset(request):
    qs = (
        SyncProject.objects.filter(removed_at__isnull=True)
        .select_related("database", "mergin_account")
        .prefetch_related("statuses", "connections")
    )
    filters = {}
    for name in ("desired_state", "process_state", "health"):
        value = request.GET.get(name)
        if value:
            filters[name] = value
            qs = qs.filter(**{name: value})
    return qs, filters


def _project_rows(qs):
    rows = []
    for project in qs:
        statuses = list(project.statuses.all())
        rows.append({
            "project": project,
            "statuses": statuses,
            "server_version": max((s.server_version or 0 for s in statuses), default=None) or None,
            "local_version": min((s.local_version or 0 for s in statuses), default=None) or None,
            "db_version": min((s.db_version or 0 for s in statuses), default=None) or None,
            "last_pull_at": max((s.last_pull_at for s in statuses if s.last_pull_at), default=None),
            "direction": _project_direction(statuses),
            "check_error": any(s.check_error for s in statuses),
        })
    return rows


def _project_direction(statuses):
    directions = {s.direction for s in statuses if s.direction}
    if not directions:
        return None
    if "db_edits_cannot_push" in directions:
        return "db_edits_cannot_push"
    if "two_way" in directions:
        return "two_way"
    return "one_way"


@login_required
def home(request):
    qs, filters = _projects_queryset(request)
    context = {
        "rows": _project_rows(qs),
        "filters": filters,
        "desired_choices": SyncProject.DesiredState.choices,
        "process_choices": SyncProject.ProcessState.choices,
        "health_choices": SyncProject.Health.choices,
    }
    return render(request, "ui/home.html", context)


@login_required
def projects_table(request):
    qs, filters = _projects_queryset(request)
    return render(request, "ui/partials/projects_table.html", {"rows": _project_rows(qs), "filters": filters})


def _project_context(project):
    statuses = list(project.statuses.select_related("connection").order_by("connection__sync_file"))
    for status in statuses:
        tables = sorted(set(status.gpkg_count) | set(status.base_count) | set(status.modified_count))
        status.table_rows = [
            {
                "table": t,
                "gpkg": status.gpkg_count.get(t),
                "base": status.base_count.get(t),
                "modified": status.modified_count.get(t),
                "streak": (status.mismatch_streak or {}).get(t, 0),
                "pending": (status.modified_count.get(t) is not None and status.base_count.get(t) is not None
                            and status.modified_count.get(t) != status.base_count.get(t)),
            }
            for t in tables
        ]
        status.interrupted_pull = (
            status.db_version is not None and status.local_version is not None
            and status.db_version < status.local_version
        )
    return {
        "project": project,
        "statuses": statuses,
        "connections": list(project.connections.all()),
        "active_conditions": sorted((project.active_conditions or {}).items()),
        "can_operate": False,
    }


@login_required
def project_detail(request, pk):
    project = get_object_or_404(SyncProject, pk=pk)
    context = _project_context(project)
    context["events"] = project.events.select_related("user").order_by("-at")[:EVENTS_ON_PAGE]
    context["can_operate"] = request.user.has_perm("registry.change_syncproject")
    context.update(_log_context(project))
    return render(request, "ui/project.html", context)


@login_required
def project_status(request, pk):
    project = get_object_or_404(SyncProject, pk=pk)
    context = _project_context(project)
    context["can_operate"] = request.user.has_perm("registry.change_syncproject")
    return render(request, "ui/partials/status.html", context)


def _log_context(project):
    work_dir = settings.DBSYNC_DATA_DIR / "projects" / str(project.pk)
    return {
        "log_tail": tail(work_dir / LOG_NAME, lines=LOG_LINES),
        "stderr_tail": tail(work_dir / STDERR_NAME, lines=20),
    }


@login_required
def project_log(request, pk):
    project = get_object_or_404(SyncProject, pk=pk)
    return render(request, "ui/partials/log.html", _log_context(project))


ACTIONS = {
    "pause": (SyncProject.DesiredState.PAUSED, "paused the sync"),
    "resume": (SyncProject.DesiredState.RUNNING, "resumed the sync"),
}


@login_required
@permission_required("registry.change_syncproject", raise_exception=True)
@require_POST
def project_action(request, pk, action):
    project = get_object_or_404(SyncProject, pk=pk, removed_at__isnull=True)
    if action not in ACTIONS:
        return HttpResponseBadRequest("unknown action")
    desired, text = ACTIONS[action]
    if project.desired_state != desired:
        SyncProject.objects.filter(pk=project.pk).update(desired_state=desired)
        events.record(project, Event.Level.INFO, f"{request.user.get_username()} {text}",
                      source=Event.Source.USER, user=request.user)
    return redirect("project", pk=project.pk)
