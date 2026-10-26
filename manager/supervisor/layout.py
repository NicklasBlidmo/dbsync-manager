"""
Where a project's files live under /data and how db-sync 2.4.0 lays out
its checkouts (slice 16, brief 2026-10-21d). This is the one place that
knows the layout; models, check, verify and the migrate operation call it.

    /data/projects/<id>/                      project.work_dir: config.yaml, logs, pidfile, tmp/
    /data/projects/<id>/work/                 db-sync's working_dir (config.yaml)
    /data/projects/<id>/work/<name>-<modified schema>/   the checkout of one connection
                                              (upstream #187; one GeoPackage plus .mergin/, #128)
    /data/projects/<id>/work/.mergin_auth.json   db-sync's stored Mergin auth token (mode 0600,
                                              a secret; upstream #190)
    /data/projects/<id>/work/.layout-v2       marker: the layout of db-sync 2.3.0 with patch 0001
                                              (<work>/<name>/<modified schema>) is gone

`<name>` is the part of `workspace/name` after the slash, as db-sync's
`_get_work_dir()` takes it.

Migrating an installation from 2.3.0 (section C of the brief): a project
whose old tree still exists is not started; the `migrate` operation stops
its daemon, removes the tree, writes the marker and lets db-sync's own
init download one GeoPackage per connection at the database's version.
"""

from pathlib import Path

from django.utils import timezone

WORKING_SUBDIR = "work"
MARKER_NAME = ".layout-v2"
AUTH_TOKEN_NAME = ".mergin_auth.json"


def project_name(mergin_project: str) -> str:
    """The project part of `workspace/name`."""
    return mergin_project.split("/", 1)[-1]


def working_dir(work_dir) -> Path:
    """db-sync's `working_dir` for a project."""
    return Path(work_dir) / WORKING_SUBDIR


def checkout_dir(work_dir, mergin_project: str, modified_schema: str) -> Path:
    """The checkout of one connection: <work>/<name>-<modified schema>."""
    return working_dir(work_dir) / f"{project_name(mergin_project)}-{modified_schema}"


def legacy_checkout_tree(work_dir, mergin_project: str) -> Path:
    """Where db-sync 2.3.0 with patch 0001 kept the checkouts: <work>/<name>/<modified schema>."""
    return working_dir(work_dir) / project_name(mergin_project)


def marker_path(work_dir) -> Path:
    return working_dir(work_dir) / MARKER_NAME


def auth_token_path(work_dir) -> Path:
    return working_dir(work_dir) / AUTH_TOKEN_NAME


def needs_migration(work_dir, mergin_project: str) -> bool:
    """True while the old tree exists and no marker says it was handled.

    A project added after the upgrade never has the old tree; a project
    that never started has no tree at all. Neither needs the operation."""
    if marker_path(work_dir).exists():
        return False
    return legacy_checkout_tree(work_dir, mergin_project).is_dir()


def mark_migrated(work_dir, note: str = "") -> Path:
    path = marker_path(work_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"{timezone.now().isoformat(timespec='seconds')} {note}\n".rstrip() + "\n", encoding="utf-8")
    return path


def is_downloaded(checkout) -> bool:
    """mergin-client writes .mergin/mergin.json when a download has finished."""
    return (Path(checkout) / ".mergin" / "mergin.json").exists()


# --- the registry side of the migration (C10) ----------------------------------


def projects_needing_migration():
    """Live projects whose old-layout tree still exists, in page order."""
    from registry.models import SyncProject

    return [p for p in SyncProject.objects.filter(removed_at__isnull=True).order_by("mergin_project")
            if needs_migration(p.work_dir, p.mergin_project)]


def request_migrations(projects, user=None):
    """One `migrate` operation per project; returns (created, skipped) where
    skipped is [(project, why)]. The supervisor runs them one at a time."""
    from django.db import IntegrityError

    from registry import events
    from registry.models import Event, Operation

    created, skipped = [], []
    for project in projects:
        open_op = project.operations.filter(status__in=Operation.OPEN_STATUSES).order_by("-requested_at").first()
        if open_op is not None:
            skipped.append((project, f"operation {open_op.pk} ({open_op.kind}, {open_op.status}) is open"))
            continue
        try:
            operation = Operation.objects.create(project=project, kind=Operation.Kind.MIGRATE, requested_by=user)
        except IntegrityError:
            skipped.append((project, "an operation is open"))
            continue
        who = user.get_username() if user is not None else "the terminal"
        events.record(project, Event.Level.INFO,
                      f"{who} requested the checkout layout migration (operation {operation.pk})",
                      source=Event.Source.USER, user=user)
        created.append(operation)
    return created, skipped
