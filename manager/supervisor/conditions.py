"""
Alert conditions with de-duplication (design §6).

A condition (e.g. "db_behind:1-punkter.gpkg", "stuck", "process_crashed")
is raised once, as an event carrying the condition key, when it becomes
active, and once more as "resolved" when it clears. In between nothing is
written. `sync_project.active_conditions` holds the active set with the
time each became active; the project page shows how long they have been.

E-mail goes out from `activate` and `resolve` through supervisor.alerts,
unless the caller delays it (`mail=False`) and sends it later with
`alert`: mergin_unreachable mails only after DBSYNC_MERGIN_DOWN_ALERT_AFTER
(review note 2026-10-20).
"""

from django.utils import timezone

from registry import events
from registry.models import Event, SyncProject

from . import alerts

# Prefixes the consistency check owns; apply() touches no other condition.
CHECK_CONDITION_PREFIXES = ("divergence:", "db_behind", "stranded_db_edits", "stuck", "check_failed")


def _save(project, active: dict):
    project.active_conditions = active
    SyncProject.objects.filter(pk=project.pk).update(active_conditions=active)


def activate(project, condition: str, message: str, level=Event.Level.WARNING, *, mail: bool = True) -> bool:
    """Raise `condition` unless it is already active. Returns True if raised."""
    active = dict(project.active_conditions or {})
    if condition in active:
        return False
    events.record(project, level, message, condition=condition)
    active[condition] = timezone.now().isoformat(timespec="seconds")
    _save(project, active)
    if mail:
        alerts.notify(project, condition, message)
    return True


def alert(project, condition: str, message: str) -> bool:
    """Mail about a condition that is already active (delayed alert)."""
    events.warning(project, message, condition=condition)
    return alerts.notify(project, condition, message)


def resolve(project, condition: str, message: str = "", *, mail: bool = True) -> bool:
    active = dict(project.active_conditions or {})
    if condition not in active:
        return False
    text = message or f"resolved: {condition}"
    events.info(project, text, condition=condition)
    del active[condition]
    _save(project, active)
    if mail:
        alerts.notify(project, condition, text, resolved=True)
    return True


def apply(project, wanted: set, messages: dict, managed=CHECK_CONDITION_PREFIXES):
    """Make the managed subset of the project's active conditions equal `wanted`."""
    active = dict(project.active_conditions or {})
    current = {key for key in active if key.startswith(managed)}
    for key in sorted(wanted - current):
        activate(project, key, messages.get(key, key))
    for key in sorted(current - wanted):
        resolve(project, key)
