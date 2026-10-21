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
CHECK_CONDITION_PREFIXES = ("divergence:", "db_behind", "stranded_db_edits", "stuck", "check_failed", "content_divergence:")
# Conditions the check raises at error level rather than warning.
ERROR_PREFIXES = ("content_divergence:",)


NO_RECIPIENTS = ("alert not mailed: no recipients on the project and no default list under "
                 "Settings -> Installation settings")


def _save(project, active: dict):
    """Write the active set, and let health follow it at once (review note
    2026-10-20d, finding 3): a deterministic failure is `failed` now, not at
    the next check; once it clears, `unknown` until the check runs."""
    from .check import health_with_conditions

    project.active_conditions = active
    fields = {"active_conditions": active}
    base = SyncProject.Health.UNKNOWN if project.health == SyncProject.Health.FAILED else project.health
    health = health_with_conditions(base, active)
    if health != project.health:
        now = timezone.now()
        project.health, project.health_since = health, now
        fields.update(health=health, health_since=now)
    SyncProject.objects.filter(pk=project.pk).update(**fields)


def _mail(project, condition, message, *, resolved=False) -> bool:
    """Send, or say once per activation that nobody would get it."""
    if not alerts.recipients_for(project):
        if not resolved:
            events.info(project, f"{NO_RECIPIENTS} ({condition})")
        return False
    return alerts.notify(project, condition, message, resolved=resolved)


def activate(project, condition: str, message: str, level=Event.Level.WARNING, *, mail: bool = True) -> bool:
    """Raise `condition` unless it is already active. Returns True if raised."""
    active = dict(project.active_conditions or {})
    if condition in active:
        return False
    events.record(project, level, message, condition=condition)
    active[condition] = timezone.now().isoformat(timespec="seconds")
    _save(project, active)
    if mail:
        _mail(project, condition, message)
    return True


def alert(project, condition: str, message: str, *, mail: bool = True) -> bool:
    """Mail about a condition that is already active (delayed alert). With
    mail=False only the event is written; the caller sends (or groups) the mail."""
    events.warning(project, message, condition=condition)
    if not mail:
        return False
    return _mail(project, condition, message)


def resolve(project, condition: str, message: str = "", *, mail: bool = True) -> bool:
    active = dict(project.active_conditions or {})
    if condition not in active:
        return False
    text = message or f"resolved: {condition}"
    events.info(project, text, condition=condition)
    del active[condition]
    _save(project, active)
    if mail:
        _mail(project, condition, text, resolved=True)
    return True


def apply(project, wanted: set, messages: dict, managed=CHECK_CONDITION_PREFIXES):
    """Make the managed subset of the project's active conditions equal `wanted`."""
    active = dict(project.active_conditions or {})
    current = {key for key in active if key.startswith(managed)}
    for key in sorted(wanted - current):
        level = Event.Level.ERROR if key.startswith(ERROR_PREFIXES) else Event.Level.WARNING
        activate(project, key, messages.get(key, key), level=level)
    for key in sorted(current - wanted):
        resolve(project, key)
