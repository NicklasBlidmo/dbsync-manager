"""
Alert conditions with de-duplication (design §6).

A condition (e.g. "db_behind:1-punkter.gpkg", "stuck", "process_crashed")
is raised once, as an event carrying the condition key, when it becomes
active, and once more as "resolved" when it clears. In between nothing is
written. `sync_project.active_conditions` holds the active set with the
time each became active; the project page shows how long they have been.

E-mail on raise/resolve is added in the alerts slice; it hooks into
`activate` and `resolve` here.
"""

from django.utils import timezone

from registry import events
from registry.models import Event, SyncProject

# Prefixes the consistency check owns; apply() touches no other condition.
CHECK_CONDITION_PREFIXES = ("divergence:", "db_behind", "stranded_db_edits", "stuck", "check_failed")


def _save(project, active: dict):
    project.active_conditions = active
    SyncProject.objects.filter(pk=project.pk).update(active_conditions=active)


def activate(project, condition: str, message: str, level=Event.Level.WARNING) -> bool:
    """Raise `condition` unless it is already active. Returns True if raised."""
    active = dict(project.active_conditions or {})
    if condition in active:
        return False
    events.record(project, level, message, condition=condition)
    active[condition] = timezone.now().isoformat(timespec="seconds")
    _save(project, active)
    return True


def resolve(project, condition: str, message: str = "") -> bool:
    active = dict(project.active_conditions or {})
    if condition not in active:
        return False
    events.info(project, message or f"resolved: {condition}", condition=condition)
    del active[condition]
    _save(project, active)
    return True


def apply(project, wanted: set, messages: dict, managed=CHECK_CONDITION_PREFIXES):
    """Make the managed subset of the project's active conditions equal `wanted`."""
    active = dict(project.active_conditions or {})
    current = {key for key in active if key.startswith(managed)}
    for key in sorted(wanted - current):
        activate(project, key, messages.get(key, key))
    for key in sorted(current - wanted):
        resolve(project, key)
