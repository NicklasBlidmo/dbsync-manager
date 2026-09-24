"""
Pages of design §9, server-rendered with HTMX partials.

Slice 3: Projects (home) with the three badges and filters, and the
project page with status per connection, direction facts, events, log
tail and pause/resume. Slice 4: the add wizard. Remove, re-init and "run
hook now" arrive with the operations and hooks slices.
"""

import logging

from django.conf import settings
from django.contrib.auth.decorators import login_required, permission_required
from django.db import IntegrityError
from django.http import HttpResponse, HttpResponseBadRequest
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_GET, require_POST

from registry import events
from registry.models import Database, Event, Hook, MerginAccount, Operation, SyncProject
from supervisor import mergin_api, operations as opsm
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
        "can_add": request.user.has_perm("registry.add_syncproject"),
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
    operation = _open_operation(project) or project.operations.order_by("-requested_at").first()
    return {
        "project": project,
        "statuses": statuses,
        "connections": list(project.connections.all()),
        "active_conditions": sorted((project.active_conditions or {}).items()),
        "can_operate": False,
        "operation": operation,
        "operation_open": operation is not None and operation.is_open,
        "cancellable": operation is not None and opsm.can_cancel(operation),
    }


@login_required
def project_detail(request, pk):
    project = get_object_or_404(SyncProject, pk=pk)
    context = _project_context(project)
    context["events"] = project.events.select_related("user").order_by("-at")[:EVENTS_ON_PAGE]
    context["can_operate"] = request.user.has_perm("registry.change_syncproject")
    context.update(_log_context(project))
    context.update(_hooks_context(project))
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


HOOK_KINDS = ("post_init", "pre_remove", "post_pull")


def _hooks_context(project):
    from registry.models import SyncRun

    configured = [(kind, getattr(project, f"{kind}_hook")) for kind in HOOK_KINDS]
    runs = list(project.runs.filter(kind=SyncRun.Kind.HOOK).order_by("-started_at")[:10])
    for run in runs:
        summary = run.summary or {}
        run.label = f"{summary.get('event', summary.get('hook_kind', '?'))} · {summary.get('hook', '?')}"
        trigger = summary.get("trigger", "manual" if summary.get("requested_by") else "auto")
        if trigger == "manual":
            run.path = f"requested by {summary.get('requested_by') or '?'} (never blocks)"
        else:
            run.path = {"post_init": "after init (blocks on failure)", "post_pull": "after pull",
                        "pre_remove": "operation"}.get(summary.get("event"), "automatic")
        run.run_as = summary.get("run_as", "")
        run.status_text = summary.get("status", "")
        if run.finished_at is None:
            run.state = "requested" if summary.get("status") == "requested" else "running"
        elif run.ok:
            run.state = "ok"
        elif summary.get("timed_out"):
            run.state = "timed out"
        else:
            run.state = f"failed (exit {summary.get('exit_code')})"
        run.output = "\n".join(part for part in (summary.get("stdout", ""), summary.get("stderr", "")) if part)
        run.duration = summary.get("duration_s")
        run.requested_by = summary.get("requested_by")
    return {"project": project, "hooks": [(k, h) for k, h in configured if h], "hook_runs": runs}


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
    needs_name = bool(pending) or check_failed or kind == "reinit"
    errors = []
    if request.method == "POST":
        if needs_name and request.POST.get("confirm_name", "").strip() != project.mergin_project:
            errors.append("type the project name exactly to confirm")
        params = {}
        if kind == "remove":
            params["drop_schemas"] = request.POST.get("drop_schemas") == "1"
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
        "prefix": prefix,
        "rows": rows,
        "hooks": {kind: Hook.objects.filter(kind=kind) for kind in ("post_init", "pre_remove", "post_pull")},
    })


@login_required
@can_add
@require_GET
def add_names(request):
    """The editable schema-name table for the ticked files and the current prefix."""
    prefix = naming.sanitize_identifier(request.GET.get("prefix", ""))
    # The ticked checkboxes are "pick"; the hidden "file" inputs of the
    # previous table come along through hx-include and are ignored here.
    files = [f for f in request.GET.getlist("pick") if f] or [f for f in request.GET.getlist("file") if f]
    rows = [
        {"file": f, "modified": m, "base": b}
        for f in files
        for m, b in [naming.derive_schema_names(prefix, f)]
    ]
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
    return render(request, "ui/add/preview.html", {"data": data, "preview": preview, "hidden": data.as_hidden_fields()})


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
