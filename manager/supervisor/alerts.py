"""
E-mail alerts (design §6): one mail when a condition becomes active, one
when it resolves, to the project's notify_emails. Sending never raises
into the supervisor; a failed send is a log line and an event.
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
