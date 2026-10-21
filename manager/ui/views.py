"""
Pages of design §9, server-rendered with HTMX partials.

Slice 3: Projects (home) with the three badges and filters, and the
project page with status per connection, direction facts, events, log
tail and pause/resume. Slice 4: the add wizard. Remove, re-init and "run
hook now" arrive with the operations and hooks slices.
"""

import logging
from datetime import datetime
from urllib.parse import urlencode

from django.conf import settings
from django.core.paginator import Paginator
from django.utils import timezone
from django.contrib.auth.decorators import login_required, permission_required
from django.db import IntegrityError
from django.http import HttpResponse, HttpResponseBadRequest
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_GET, require_POST

from registry import events
from registry.models import Database, Event, Hook, InstallationSettings, MerginAccount, Operation, SyncProject
from supervisor import content, facts, mergin_api, operations as opsm
from supervisor import verify as verify_mod
from supervisor.logs import tail
from supervisor.processes import LOG_NAME, STDERR_NAME

from . import naming, wizard

log = logging.getLogger("ui")

EVENTS_ON_PAGE = 50
LOG_LINES = 100


# --- projects ------------------------------------------------------------------


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
    default_recipients = InstallationSettings.default_recipients()
    for project in qs:
        statuses = list(project.statuses.all())
        server = max((s.server_version or 0 for s in statuses), default=None) or None
        local = min((s.local_version or 0 for s in statuses), default=None) or None
        db = min((s.db_version or 0 for s in statuses), default=None) or None
        rows.append({
            "project": project,
            "statuses": statuses,
            "server_version": server,
            "local_version": local,
            "db_version": db,
            # One Version column: the server's, with a badge only where the
            # checkout or the database differ (slice 10, item 6).
            "version_badges": [
                (label, v) for label, v in (("checkout", local), ("db", db))
                if v is not None and server is not None and v != server
            ],
            "last_pull_at": max((s.last_pull_at for s in statuses if s.last_pull_at), default=None),
            "direction": _project_direction(statuses),
            # Failed = the latest check did not succeed (a best-effort note on a
            # successful check is not a failure).
            "check_error": any(
                s.check_error and (s.last_ok_check_at is None or s.last_ok_check_at != s.checked_at) for s in statuses
            ),
            "check_error_text": "; ".join(s.check_error for s in statuses if s.check_error)[:300],
            # Alerts would mail nobody (2026-10-20d, finding 2).
            "no_recipients": not (project.notify_emails or default_recipients),
        })
    return rows


def _project_direction(statuses):
    return facts.project_direction(statuses)


@login_required
def home(request):
    qs, filters = _projects_queryset(request)
    context = {
        "rows": _project_rows(qs),
        "filters": filters,
        "desired_choices": SyncProject.DesiredState.choices,
        "process_choices": SyncProject.ProcessState.choices,
        "health_choices": SyncProject.Health.choices,
        "can_add": request.user.has_perm("registry.add_syncproject"),
    }
    context.update(_scheduled_context(request))
    return render(request, "ui/home.html", context)


@login_required
def projects_table(request):
    qs, filters = _projects_queryset(request)
    return render(request, "ui/partials/projects_table.html", {"rows": _project_rows(qs), "filters": filters})


def _table_rows(status):
    """Per-table counts with the badge each row earns (design §6)."""
    tables = sorted(set(status.gpkg_count) | set(status.base_count) | set(status.modified_count))
    interrupted = (
        status.db_version is not None and status.local_version is not None
        and status.db_version < status.local_version
    )
    rows = []
    for t in tables:
        gpkg, base, modified = status.gpkg_count.get(t), status.base_count.get(t), status.modified_count.get(t)
        streak = (status.mismatch_streak or {}).get(t, 0)
        pending = modified is not None and base is not None and modified != base
        badges = []
        if streak >= 2:
            badges.append(("bad", f"base behind ×{streak}"))
        elif streak == 1:
            badges.append(("muted", "mismatch once"))
        if pending:
            badges.append(("warn", "interrupted pull, not user edits") if interrupted else ("info", "pending database changes"))
        # Content base vs modified (slice 15, A): a condition in a one-way
        # project, the normal state between pushes in a two-way one.
        diff = (status.content_diff or {}).get(t)
        if diff and content.total(diff):
            one_way = status.direction in ("one_way", "db_edits_cannot_push")
            badges.append(("bad" if one_way else "info", f"content: {content.summarize(diff)}"))
        rows.append({"table": t, "gpkg": gpkg, "base": base, "modified": modified, "streak": streak,
                     "pending": pending, "badges": badges, "content": diff if diff and content.total(diff) else None,
                     "agree": gpkg is not None and gpkg == base == modified})
    return rows, interrupted


def _connection_rows(project):
    """One row per connection for the status table (slice 8 brief, B2/B3).

    "In phase": local == server == db and every table agrees with streak 0
    and nothing pending. Rows that deviate carry badges and sort first.
    """
    statuses = {s.connection_id: s for s in project.statuses.select_related("connection")}
    rows = []
    for conn in project.connections.all():
        status = statuses.get(conn.pk)
        row = {"connection": conn, "status": status, "badges": [], "checkout_badge": None, "db_badge": None,
               "check_badge": None, "tables": [], "expanded": False, "in_phase": False, "summary": ""}
        if status is None or status.checked_at is None:
            row["summary"] = "not checked yet"
            rows.append(row)
            continue
        tables, interrupted = _table_rows(status)
        row["tables"] = tables
        row["interrupted_pull"] = interrupted
        server, local, db = status.server_version, status.local_version, status.db_version
        row["checkout_muted"] = local is not None and local == server
        row["db_muted"] = db is not None and db == local
        if server is not None and local is not None and local < server:
            row["checkout_badge"] = ("warn", "behind server")
        if interrupted:
            row["db_badge"] = ("warn", "behind checkout")
        elif server is not None and db is not None and db < server:
            row["db_badge"] = ("warn", "behind server")
        for table in tables:
            for badge in table["badges"]:
                if badge not in row["badges"]:
                    row["badges"].append(badge)
        failed = bool(status.check_error) and (status.last_ok_check_at is None or status.last_ok_check_at != status.checked_at)
        if failed:
            row["check_badge"] = ("bad", "check failed")
        elif status.check_error:
            row["check_badge"] = ("muted", "note")
        all_agree = bool(tables) and all(t["agree"] and t["streak"] == 0 and not t["pending"] for t in tables)
        row["in_phase"] = (server is not None and local == server and db == server and all_agree and not failed)
        row["rows_muted"] = all_agree
        if len(tables) == 1:
            t = tables[0]
            row["summary"] = f"{_n(t['gpkg'])} / {_n(t['base'])} / {_n(t['modified'])}"
        else:
            row["summary"] = f"{len(tables)} tables"
        row["expanded"] = any(t["badges"] for t in tables)
        row["attention"] = bool(row["badges"] or row["checkout_badge"] or row["db_badge"] or failed)
        rows.append(row)
    rows.sort(key=lambda r: (not r.get("attention"), r["connection"].sync_file.lower()))
    return rows


def _n(value):
    return "–" if value is None else str(value)


def _project_facts(project, rows):
    """Project-level facts shown once in the status panel (slice 8 brief, B1)."""
    from collections import Counter

    statuses = [r["status"] for r in rows if r["status"] is not None and r["status"].checked_at]
    servers = {s.server_version for s in statuses if s.server_version is not None}
    permissions = [s.mergin_permission for s in statuses if s.mergin_permission]
    directions = Counter(s.direction for s in statuses if s.direction)
    writers_by_schema = {s.connection.modified_schema: s.db_writers for s in statuses if s.db_writers}
    unknown_writers = any(s.db_writers is None for s in statuses)
    writers = sorted({w for ws in writers_by_schema.values() for w in ws})
    facts = {
        "server_version": max(servers) if servers else None,
        "servers_differ": len(servers) > 1,
        "permission": permissions[0] if permissions else None,
        "direction": directions.most_common(1)[0][0] if directions else None,
        "directions_differ": len(directions) > 1,
        "direction_counts": [(d, n) for d, n in directions.most_common()],
        "writers": writers,
        "writers_unknown": unknown_writers and not writers,
        "writers_tooltip": "; ".join(f"{schema}: {', '.join(ws)}" for schema, ws in sorted(writers_by_schema.items()))
        if len({tuple(ws) for ws in writers_by_schema.values()}) > 1 else "",
        "checked": bool(statuses),
    }
    return facts


def _project_context(project):
    rows = _connection_rows(project)
    operation = _open_operation(project) or project.operations.order_by("-requested_at").first()
    return {
        "project": project,
        "rows": rows,
        "facts": _project_facts(project, rows),
        "connections": list(project.connections.all()),
        "active_conditions": sorted((project.active_conditions or {}).items()),
        "needs_reinit": "checkout_inconsistent" in (project.active_conditions or {}),
        "no_recipients": not (project.notify_emails or InstallationSettings.default_recipients()),
        "can_operate": False,
        "operation": operation,
        "operation_open": operation is not None and operation.is_open,
        "cancellable": operation is not None and opsm.can_cancel(operation),
    }


def _parse_when(text):
    """An ISO date or date-time from a filter field, aware in TZ; None when empty or malformed."""
    if not text:
        return None
    try:
        value = datetime.fromisoformat(text)
    except ValueError:
        return None
    return value if timezone.is_aware(value) else timezone.make_aware(value)


def _events_context(project, request):
    """The Events table with a date/time and level filter and paging
    (review note 2026-10-20b, finding 5)."""
    filters = {key: request.GET.get(key, "") for key in ("since", "until", "level")}
    qs = project.events.select_related("user").order_by("-at")
    since, until = _parse_when(filters["since"]), _parse_when(filters["until"])
    if since:
        qs = qs.filter(at__gte=since)
    if until:
        qs = qs.filter(at__lt=until)
    if filters["level"] in dict(Event.Level.choices):
        qs = qs.filter(level=filters["level"])
    page = Paginator(qs, EVENTS_ON_PAGE).get_page(request.GET.get("page"))
    events = list(page.object_list)
    filtering = any(filters.values()) or page.number > 1
    query = urlencode({k: v for k, v in filters.items() if v})
    return {
        "events": events,
        "events_page": page,
        "events_filters": filters,
        "events_query": query + "&" if query else "",
        "events_latest": events[0].at if events else None,
        # Collapsed by default; open when the newest entry needs attention
        # (slice 10, item 4) or the operator is looking at a filtered window.
        "events_open": (bool(events) and events[0].level in ("warning", "error")) or filtering,
        "level_choices": Event.Level.choices,
    }


@login_required
def project_detail(request, pk):
    project = get_object_or_404(SyncProject, pk=pk)
    context = _project_context(project)
    context.update(_events_context(project, request))
    context["tables"] = facts.tables_for(project)
    context["verification"] = facts.verification_for(project)
    context["can_operate"] = request.user.has_perm("registry.change_syncproject")
    context["now_date"] = _today()
    context.update(_log_context(project))
    context.update(_hooks_context(project))
    hook_runs = context["hook_runs"]
    context["hooks_latest"] = hook_runs[0].started_at if hook_runs else None
    context["hooks_open"] = bool(hook_runs) and hook_runs[0].ok is False
    context.update(_runs_context(project))
    return render(request, "ui/project.html", context)


@login_required
def project_status(request, pk):
    project = get_object_or_404(SyncProject, pk=pk)
    context = _project_context(project)
    context["can_operate"] = request.user.has_perm("registry.change_syncproject")
    context["now_date"] = _today()
    return render(request, "ui/partials/status.html", context)


def _today():
    from django.utils import timezone

    return timezone.localdate().strftime("%Y-%m-%d")


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


HOOK_KINDS = ("post_init", "pre_remove", "post_pull")


def _decorate_runs(runs):
    """Label, state, path and output for hook-run rows, project and scheduled alike."""
    for run in runs:
        summary = run.summary or {}
        run.label = f"{summary.get('event', summary.get('hook_kind', '?'))} · {summary.get('hook', '?')}"
        trigger = summary.get("trigger", "manual" if summary.get("requested_by") else "auto")
        if trigger == "manual":
            run.path = f"requested by {summary.get('requested_by') or '?'} (never blocks)"
        elif summary.get("event") == "scheduled":
            hook = run.hook
            run.path = f"daily at {hook.run_at:%H:%M}" if hook is not None and hook.run_at else "schedule"
        else:
            run.path = {"post_init": "after init (blocks on failure)", "post_pull": "after pull",
                        "pre_remove": "operation"}.get(summary.get("event"), "automatic")
        run.run_as = summary.get("run_as", "")
        run.status_text = summary.get("status", "")
        if run.finished_at is None:
            run.state = "requested" if summary.get("status") == "requested" else "running"
        elif run.ok:
            run.state = "ok"
        elif summary.get("interrupted"):
            run.state = "interrupted"
        elif summary.get("timed_out"):
            run.state = "timed out"
        else:
            run.state = f"failed (exit {summary.get('exit_code')})"
        run.output = "\n".join(part for part in (summary.get("stdout", ""), summary.get("stderr", "")) if part)
        run.duration = summary.get("duration_s")
        run.requested_by = summary.get("requested_by")
    return runs


def _hooks_context(project):
    from registry.models import SyncRun

    configured = [(kind, getattr(project, f"{kind}_hook")) for kind in HOOK_KINDS]
    runs = _decorate_runs(list(project.runs.filter(kind=SyncRun.Kind.HOOK).select_related("hook").order_by("-started_at")[:10]))
    return {"project": project, "hooks": [(k, h) for k, h in configured if h], "hook_runs": runs}


RUNS_ON_PAGE = 20
RUN_ROWS_FETCHED = 400   # enough raw rows for 20 groups of a many-file project


def _runs_context(project):
    from registry.models import SyncRun

    from .runs import group_runs

    runs = list(project.runs.exclude(kind=SyncRun.Kind.HOOK).order_by("-started_at")[:RUN_ROWS_FETCHED])
    total = project.connections.count()
    return {"run_groups": group_runs(runs, window_seconds=max(project.sleep_time, 60), total_connections=total,
                                     limit=RUNS_ON_PAGE)}


@login_required
def project_runs(request, pk):
    project = get_object_or_404(SyncProject, pk=pk)
    return render(request, "ui/partials/runs.html", _runs_context(project))


@login_required
def project_hooks(request, pk):
    project = get_object_or_404(SyncProject, pk=pk)
    context = _hooks_context(project)
    context["can_operate"] = request.user.has_perm("registry.change_syncproject")
    return render(request, "ui/partials/hooks.html", context)


@login_required
@permission_required("registry.change_syncproject", raise_exception=True)
@require_POST
def project_run_hook(request, pk):
    from supervisor import hooks as hook_runner

    project = get_object_or_404(SyncProject, pk=pk, removed_at__isnull=True)
    kind = request.POST.get("kind", "")
    hook = getattr(project, f"{kind}_hook", None) if kind in HOOK_KINDS else None
    if hook is None:
        return HttpResponseBadRequest("no such hook on this project")
    operation = _open_operation(project)
    if operation is not None:
        return _conflict(project, operation)
    hook_runner.request_run(project, hook, request.user)
    events.record(project, Event.Level.INFO, f"{request.user.get_username()} requested the {kind} hook '{hook.name}'",
                  source=Event.Source.USER, user=request.user)
    return redirect("project", pk=project.pk)


# --- scheduled hooks (slice 13, design §8) -------------------------------------------

SCHEDULED_BADGES = {"ok": "ok", "failed": "bad", "running": "info", "interrupted": "warn"}


def _run_as_text(hook):
    return f"{hook.database.user} ({hook.database.name})" if hook.database else "no run-as database"


def _scheduled_rows():
    rows = []
    for hook in Hook.objects.filter(kind=Hook.Kind.SCHEDULED).select_related("database").order_by("name"):
        rows.append({
            "hook": hook,
            "run_as": _run_as_text(hook),
            "last": hook.last_finished_at or hook.last_started_at,
            "status": hook.last_status or "never run",
            "badge": SCHEDULED_BADGES.get(hook.last_status, "muted"),
        })
    return rows


def _scheduled_context(request):
    rows = _scheduled_rows()
    failed = sum(1 for row in rows if row["hook"].last_status == "failed")
    return {"scheduled": rows, "scheduled_failed": failed, "scheduled_open": failed > 0,
            "can_operate": request.user.has_perm("registry.change_syncproject")}


@login_required
def scheduled_table(request):
    return render(request, "ui/partials/scheduled.html", _scheduled_context(request))


def _scheduled_hook(pk):
    return get_object_or_404(Hook.objects.select_related("database"), pk=pk, kind=Hook.Kind.SCHEDULED)


def _scheduled_runs(hook):
    from registry.models import SyncRun

    return _decorate_runs(list(hook.runs.filter(kind=SyncRun.Kind.HOOK).select_related("hook").order_by("-started_at")[:20]))


@login_required
def scheduled_detail(request, pk):
    hook = _scheduled_hook(pk)
    context = {
        "hook": hook, "runs": _scheduled_runs(hook), "tz": settings.TIME_ZONE, "run_as": _run_as_text(hook),
        "status": hook.last_status or "never run", "badge": SCHEDULED_BADGES.get(hook.last_status, "muted"),
        "can_operate": request.user.has_perm("registry.change_syncproject"),
    }
    return render(request, "ui/scheduled.html", context)


@login_required
def scheduled_runs(request, pk):
    hook = _scheduled_hook(pk)
    return render(request, "ui/partials/hook_runs_table.html", {"runs": _scheduled_runs(hook)})


@login_required
@permission_required("registry.change_syncproject", raise_exception=True)
@require_POST
def scheduled_run_now(request, pk):
    """'Run now': a sync_run row with status requested; the supervisor starts it on its next tick."""
    from supervisor import hooks as hook_runner

    hook = _scheduled_hook(pk)
    if hook.last_status == "running" or hook_runner.pending_scheduled_request(hook) is not None:
        return HttpResponse(f"scheduled hook '{hook.name}' is already running or requested",
                            status=409, content_type="text/plain")
    hook_runner.request_scheduled_run(hook, request.user)
    events.record(None, Event.Level.INFO, f"{request.user.get_username()} requested the scheduled hook '{hook.name}'",
                  source=Event.Source.USER, user=request.user)
    return redirect("scheduled", pk=hook.pk)


@login_required
@permission_required("registry.add_operation", raise_exception=True)
@require_POST
def project_verify(request, pk):
    """Start the verify operation (slice 15, B): checkout GeoPackage vs base, read-only."""
    project = get_object_or_404(SyncProject, pk=pk, removed_at__isnull=True)
    try:
        operation = verify_mod.request_verify(project, request.user)
    except verify_mod.VerifyConflict as exc:
        return _conflict(project, exc.operation)
    events.record(project, Event.Level.INFO, f"{request.user.get_username()} requested verify (operation {operation.pk})",
                  source=Event.Source.USER, user=request.user)
    return redirect("project", pk=project.pk)


# --- Pause all / Resume all (review note 2026-10-20b, finding 6) ---------------------

PAUSE_ALL_PREFIX = "pause_all:"


def _pause_all_candidates():
    """Running projects without an open operation (the operation owns the project)."""
    return list(
        SyncProject.objects.filter(removed_at__isnull=True, desired_state=SyncProject.DesiredState.RUNNING)
        .exclude(operations__status__in=Operation.OPEN_STATUSES).distinct().order_by("mergin_project")
    )


def _latest_pause_all_batch():
    event = Event.objects.filter(condition__startswith=PAUSE_ALL_PREFIX).order_by("-at").first()
    return event.condition if event else None


def _resume_all_candidates():
    """Projects paused by the latest Pause all that are still paused: the
    batch is the set of events carrying its condition key, so projects
    paused before (or by hand) are left alone. No extra storage."""
    batch = _latest_pause_all_batch()
    if batch is None:
        return [], None
    ids = Event.objects.filter(condition=batch).values_list("project_id", flat=True)
    projects = list(
        SyncProject.objects.filter(pk__in=ids, removed_at__isnull=True, desired_state=SyncProject.DesiredState.PAUSED)
        .order_by("mergin_project")
    )
    return projects, batch


@login_required
@permission_required("registry.change_syncproject", raise_exception=True)
def pause_all(request):
    projects = _pause_all_candidates()
    if request.method == "POST":
        batch = f"{PAUSE_ALL_PREFIX}{timezone.now():%Y%m%dT%H%M%S}"
        for project in projects:
            SyncProject.objects.filter(pk=project.pk).update(desired_state=SyncProject.DesiredState.PAUSED)
            events.record(project, Event.Level.INFO, f"{request.user.get_username()} paused the sync (Pause all)",
                          source=Event.Source.USER, condition=batch, user=request.user)
        return redirect("home")
    skipped = list(SyncProject.objects.filter(removed_at__isnull=True, desired_state=SyncProject.DesiredState.PAUSED)
                   .order_by("mergin_project"))
    return render(request, "ui/bulk.html", {
        "title": "Pause all", "subtitle": "Every running sync stops between cycles: each daemon finishes its current pull or push, then exits.",
        "lead": f"{len(projects)} project{'s' if len(projects) != 1 else ''} will be paused:",
        "projects": projects, "skipped": skipped, "skipped_why": "already paused; Resume all leaves them paused",
        "button": "Pause all", "action_url": request.path, "empty": "No running project to pause.",
    })


@login_required
@permission_required("registry.change_syncproject", raise_exception=True)
def resume_all(request):
    projects, batch = _resume_all_candidates()
    if request.method == "POST":
        for project in projects:
            SyncProject.objects.filter(pk=project.pk).update(desired_state=SyncProject.DesiredState.RUNNING)
            events.record(project, Event.Level.INFO, f"{request.user.get_username()} resumed the sync (Resume all)",
                          source=Event.Source.USER, condition=(batch or "").replace(PAUSE_ALL_PREFIX, "resume_all:"),
                          user=request.user)
        return redirect("home")
    in_batch = {p.pk for p in projects}
    skipped = [p for p in SyncProject.objects.filter(removed_at__isnull=True, desired_state=SyncProject.DesiredState.PAUSED)
               .order_by("mergin_project") if p.pk not in in_batch]
    return render(request, "ui/bulk.html", {
        "title": "Resume all", "subtitle": "Resumes the projects that the last Pause all paused and that are still paused.",
        "lead": f"{len(projects)} project{'s' if len(projects) != 1 else ''} will be resumed:",
        "projects": projects, "skipped": skipped, "skipped_why": "paused before Pause all was pressed, or by hand",
        "button": "Resume all", "action_url": request.path,
        "empty": "Nothing to resume: no project from the last Pause all is still paused.",
    })


ACTIONS = {
    "pause": (SyncProject.DesiredState.PAUSED, "paused the sync"),
    "resume": (SyncProject.DesiredState.RUNNING, "resumed the sync"),
}


def _open_operation(project):
    return project.operations.filter(status__in=Operation.OPEN_STATUSES).order_by("-requested_at").first()


def _conflict(project, operation):
    """HTTP 409 while an operation owns the project (§6)."""
    return HttpResponse(
        f"operation {operation.pk} ({operation.kind}, {operation.status}) is open on {project.mergin_project}; "
        "wait for it, retry it or cancel it first",
        status=409, content_type="text/plain",
    )


@login_required
@permission_required("registry.change_syncproject", raise_exception=True)
@require_POST
def project_action(request, pk, action):
    project = get_object_or_404(SyncProject, pk=pk, removed_at__isnull=True)
    if action not in ACTIONS:
        return HttpResponseBadRequest("unknown action")
    operation = _open_operation(project)
    if operation is not None:
        return _conflict(project, operation)
    desired, text = ACTIONS[action]
    if project.desired_state != desired:
        SyncProject.objects.filter(pk=project.pk).update(desired_state=desired)
        events.record(project, Event.Level.INFO, f"{request.user.get_username()} {text}",
                      source=Event.Source.USER, user=request.user)
    return redirect("project", pk=project.pk)


# --- operations: remove and re-init (§6, §9) -------------------------------------


def _pending_changes(project):
    """Pending database changes per 'file:table' from the last check, plus whether it failed."""
    pending = {}
    checked_at = None
    check_failed = False
    for status in project.statuses.select_related("connection"):
        checked_at = max(filter(None, [checked_at, status.checked_at]), default=None)
        if status.check_error and (status.last_ok_check_at is None or status.last_ok_check_at != status.checked_at):
            check_failed = True
        for table, modified in (status.modified_count or {}).items():
            base = (status.base_count or {}).get(table)
            if base is not None and modified != base:
                pending[f"{status.connection.sync_file}:{table}"] = modified - base
    return pending, checked_at, check_failed


@login_required
@permission_required("registry.add_operation", raise_exception=True)
def project_operation(request, pk, kind):
    if kind not in ("remove", "reinit"):
        return HttpResponseBadRequest("unknown operation")
    project = get_object_or_404(SyncProject, pk=pk, removed_at__isnull=True)
    open_op = _open_operation(project)
    if open_op is not None:
        return _conflict(project, open_op)
    pending, checked_at, check_failed = _pending_changes(project)
    # The name is required whenever data can be lost: always for re-init,
    # and for remove when the schemas are dropped or the last check saw
    # pending changes or failed (field finding 2026-09-24, item 2).
    needs_name = bool(pending) or check_failed or kind == "reinit"
    errors = []
    if request.method == "POST":
        drop = request.POST.get("drop_schemas") == "1"
        if (needs_name or drop) and request.POST.get("confirm_name", "").strip() != project.mergin_project:
            errors.append("type the project name exactly to confirm")
        params = {}
        if kind == "remove":
            params["drop_schemas"] = drop
        else:
            init_from = request.POST.get("init_from", project.init_from)
            if init_from not in ("gpkg", "db"):
                errors.append("init direction must be gpkg or db")
            params["init_from"] = init_from
            params["confirm_pending_loss"] = bool(pending) and not errors
        if not errors:
            try:
                operation = Operation.objects.create(project=project, kind=kind, params=params, requested_by=request.user)
            except IntegrityError:
                return _conflict(project, _open_operation(project))
            events.record(project, Event.Level.INFO,
                          f"{request.user.get_username()} requested {kind} (operation {operation.pk}, {params})",
                          source=Event.Source.USER, user=request.user)
            return redirect("project", pk=project.pk)
    return render(request, "ui/operation.html", {
        "project": project, "kind": kind, "connections": list(project.connections.all()),
        "pending": pending, "checked_at": checked_at, "check_failed": check_failed,
        "needs_name": needs_name, "errors": errors,
    })


@login_required
@permission_required("registry.change_syncproject", raise_exception=True)
@require_POST
def operation_retry(request, pk):
    operation = get_object_or_404(Operation, pk=pk)
    if opsm.request_retry(operation):
        events.record(operation.project, Event.Level.INFO,
                      f"{request.user.get_username()} retried operation {operation.pk} at step '{operation.step}'",
                      source=Event.Source.USER, user=request.user)
    return redirect("project", pk=operation.project_id)


@login_required
@permission_required("registry.change_syncproject", raise_exception=True)
@require_POST
def operation_cancel(request, pk):
    operation = get_object_or_404(Operation, pk=pk)
    if not opsm.can_cancel(operation):
        return HttpResponse("this operation can no longer be cancelled; retry it", status=409, content_type="text/plain")
    opsm.request_cancel(operation)
    SyncProject.objects.filter(pk=operation.project_id, process_state="blocked").update(process_state="stopped", process_reason="")
    events.record(operation.project, Event.Level.INFO, f"{request.user.get_username()} cancelled operation {operation.pk}",
                  source=Event.Source.USER, user=request.user)
    return redirect("project", pk=operation.project_id)


@login_required
def project_operation_panel(request, pk):
    project = get_object_or_404(SyncProject, pk=pk)
    operation = _open_operation(project) or project.operations.order_by("-requested_at").first()
    return render(request, "ui/partials/operation.html", {
        "operation": operation, "cancellable": operation is not None and opsm.can_cancel(operation),
        "can_operate": request.user.has_perm("registry.change_syncproject"),
    })


# --- add wizard (§9) -------------------------------------------------------------

can_add = permission_required("registry.add_syncproject", raise_exception=True)


def _account(request):
    return get_object_or_404(MerginAccount, pk=request.GET.get("account") or 0)


@login_required
@can_add
def add_start(request):
    return render(request, "ui/add/start.html", {
        "accounts": MerginAccount.objects.all(),
        "databases": Database.objects.all(),
    })


@login_required
@can_add
@require_GET
def add_workspaces(request):
    """<option>s for the workspace select of one account."""
    account = _account(request)
    try:
        workspaces = mergin_api.workspaces(account)
        error = ""
    except Exception as exc:  # noqa: BLE001 - shown to the operator
        workspaces, error = [], str(exc)
    return render(request, "ui/add/partials/workspaces.html", {"workspaces": workspaces, "error": error})


@login_required
@can_add
@require_GET
def add_search(request):
    account = _account(request)
    workspace = request.GET.get("workspace", "").strip()
    text = request.GET.get("q", "").strip()
    database = request.GET.get("database", "")
    results, error = [], ""
    if workspace:
        try:
            results = mergin_api.search_projects(account, workspace, text)
        except Exception as exc:  # noqa: BLE001
            error = str(exc)
    return render(request, "ui/add/partials/search_results.html", {
        "results": results, "error": error, "account": account, "database": database, "workspace": workspace,
    })


def _project_files(account, project_path):
    info = mergin_api.project_info(account, project_path)
    files = mergin_api.geopackages(info)
    conflict_originals = {naming.conflict_original(f["path"]) for f in files if naming.is_conflict_copy(f["path"])}
    for entry in files:
        entry["conflict_copy"] = naming.is_conflict_copy(entry["path"])
        entry["has_conflict"] = entry["path"] in conflict_originals
    return info, files


@login_required
@can_add
@require_GET
def add_project(request):
    """Step 2: files, prefix, derived names, options."""
    account = _account(request)
    database = get_object_or_404(Database, pk=request.GET.get("database") or 0)
    project_path = request.GET.get("project", "").strip()
    if "/" not in project_path:
        return HttpResponseBadRequest("project must be workspace/name")
    try:
        info, files = _project_files(account, project_path)
    except Exception as exc:  # noqa: BLE001
        return render(request, "ui/add/error.html", {"message": f"cannot read project {project_path}: {exc}"})
    prefix = naming.default_prefix(project_path)
    selectable = [f for f in files if not f["conflict_copy"]]
    for f in selectable:
        f["checked"] = True
    rows = [
        {"file": f["path"], "modified": m, "base": b}
        for f in selectable
        for m, b in [naming.derive_schema_names(prefix, f["path"])]
    ]
    return render(request, "ui/add/project.html", {
        "account": account,
        "database": database,
        "project": project_path,
        "project_id": info.get("id", ""),
        "version": mergin_api.version_number(info),
        "permission": mergin_api.permission(info),
        "files": files,
        "selectable_count": len(selectable),
        "checked_count": len(selectable),
        "prefix": prefix,
        "rows": rows,
        "hooks": {kind: Hook.objects.filter(kind=kind) for kind in ("post_init", "pre_remove", "post_pull")},
        "default_recipients": InstallationSettings.default_recipients(),
    })


@login_required
@can_add
@require_POST
def add_names(request):
    """The editable schema-name table for the ticked files and the current prefix.

    A POST, not a GET: the whole step-2 form travels with the request
    (hx-include="#add"), and with 18 GeoPackages the query string passed
    gunicorn's 4094-byte request-line limit, so the server answered 400
    and the table never refreshed (2026-09-28). The form's CSRF token is
    included along with the rest of it.
    """
    prefix = naming.sanitize_identifier(request.POST.get("prefix", ""))
    # The ticked checkboxes ("pick") are the file set; an empty list is an
    # empty table. Names the operator typed (the previous table's rows,
    # included through hx-include) are kept for files still picked, unless
    # the prefix changed, which re-derives every row.
    picked = []
    for f in request.POST.getlist("pick"):
        if f and f not in picked:
            picked.append(f)
    typed = {}
    if request.POST.get("names_prefix", "") == prefix:
        typed = wizard.names_by_file(request.POST) or {}
    rows = []
    for f in picked:
        m, b = typed.get(f) or naming.derive_schema_names(prefix, f)
        rows.append({"file": f, "modified": m, "base": b})
    return render(request, "ui/add/partials/names_table.html", {"rows": rows, "prefix": prefix})


def _preview(request):
    data, errors = wizard.parse(request.POST)
    if errors:
        return data, wizard.Preview(None, None, [], errors, None, False, ""), None
    try:
        info = mergin_api.project_info(data.account, data.project)
        permission = mergin_api.permission(info)
        version = mergin_api.version_number(info)
        if not data.project_id and info.get("id"):
            data.project_id = str(info["id"])
    except Exception as exc:  # noqa: BLE001
        permission, version = None, None
        errors.append(f"cannot read {data.project} with account '{data.account.name}': {exc}")
    preview = wizard.preview(data, permission, version)
    preview.errors = errors + preview.errors
    return data, preview, info if not errors else None


@login_required
@can_add
@require_POST
def add_preview(request):
    data, preview, _ = _preview(request)
    return render(request, "ui/add/preview.html", {"data": data, "preview": preview, "hidden": data.as_hidden_fields(),
                                                   "default_recipients": InstallationSettings.default_recipients()})


@login_required
@can_add
@require_POST
def add_create(request):
    data, preview, _ = _preview(request)
    if preview.needs_ack and not data.ack:
        preview.errors.append("tick the acknowledgement to continue")
    if preview.errors:
        return render(request, "ui/add/preview.html", {"data": data, "preview": preview, "hidden": data.as_hidden_fields()})
    project = wizard.create(data, request.user)
    return redirect("project", pk=project.pk)
