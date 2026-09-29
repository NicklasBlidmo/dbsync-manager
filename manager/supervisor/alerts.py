"""
E-mail alerts (design §6): one mail when a condition becomes active, one
when it resolves, to the project's notify_emails. Sending never raises
into the supervisor; a failed send is a log line and an event.

Scheduled hooks (slice 13) mail the same way, to the hook's own
recipients: once when a run fails, once when the next run succeeds.
"""

import logging

from django.conf import settings
from django.core.mail import send_mail

from registry import events

log = logging.getLogger("supervisor.alerts")


def subject_for(project, condition, resolved: bool) -> str:
    state = "resolved" if resolved else "alert"
    return f"[dbsync-manager] {project.mergin_project}: {state} {condition}"


def body_for(project, condition, message, resolved: bool) -> str:
    lines = [
        f"Project:   {project.mergin_project}",
        f"Database:  {project.database.name}",
        f"Condition: {condition} ({'resolved' if resolved else 'active'})",
        "",
        message,
    ]
    base = getattr(settings, "DBSYNC_BASE_URL", "")
    if base:
        lines += ["", f"{base.rstrip('/')}/projects/{project.pk}/"]
    lines += ["", "Sent by dbsync-manager. Nothing more is sent while the condition stays active."]
    return "\n".join(lines)


def notify(project, condition, message, *, resolved: bool = False) -> bool:
    """Send the mail if the project has recipients. Returns True if sent."""
    recipients = [a for a in (project.notify_emails or []) if a]
    if not recipients:
        return False
    try:
        send_mail(
            subject_for(project, condition, resolved),
            body_for(project, condition, message, resolved),
            settings.DEFAULT_FROM_EMAIL,
            recipients,
            fail_silently=False,
        )
    except Exception as exc:  # noqa: BLE001 - alerting must not break the loop
        log.warning("alert mail for %s (%s) failed: %s", project, condition, exc)
        events.warning(project, f"alert e-mail could not be sent ({condition}): {exc}")
        return False
    log.info("alert mail sent for %s: %s%s", project, condition, " resolved" if resolved else "")
    return True


# --- scheduled hooks (slice 13) ---------------------------------------------------


def hook_subject(hook, change: str) -> str:
    return f"[dbsync-manager] scheduled hook {hook.name}: {change}"


def hook_body(hook, change: str, result, run=None) -> str:
    from .hooks import failure_reason

    when = f", daily at {hook.run_at:%H:%M}" if hook.run_at else ""
    lines = [
        f"Hook:     {hook.name} (scheduled{when})",
        f"Runs as:  {hook.database.user + ' on ' + hook.database.host if hook.database else '-'}",
        f"Outcome:  {'ok' if result.ok else failure_reason(result)} after {result.duration_s}s ({change})",
    ]
    if run is not None and getattr(run, "pk", None):
        lines.append(f"Run:      {run.pk}")
    tail = "\n".join((result.stderr or result.stdout).strip().splitlines()[-30:])
    if tail:
        lines += ["", "Last output lines (masked):", tail]
    base = getattr(settings, "DBSYNC_BASE_URL", "")
    if base:
        lines += ["", f"{base.rstrip('/')}/scheduled/{hook.pk}/"]
    lines += ["", "Sent by dbsync-manager. One mail per change: the next one comes when the outcome changes."]
    return "\n".join(lines)


def notify_hook(hook, change: str, result, run=None) -> bool:
    """Mail the hook's recipients about a failed or recovered run. Returns True if sent."""
    recipients = [a for a in (hook.notify_emails or []) if a]
    if not recipients:
        return False
    try:
        send_mail(hook_subject(hook, change), hook_body(hook, change, result, run),
                  settings.DEFAULT_FROM_EMAIL, recipients, fail_silently=False)
    except Exception as exc:  # noqa: BLE001 - alerting must not break the loop
        log.warning("alert mail for scheduled hook '%s' (%s) failed: %s", hook.name, change, exc)
        events.warning(None, f"alert e-mail for scheduled hook '{hook.name}' could not be sent ({change}): {exc}")
        return False
    log.info("alert mail sent for scheduled hook '%s': %s", hook.name, change)
    return True
