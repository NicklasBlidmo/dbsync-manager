"""
When a scheduled hook is due (slice 13, design §8 "Scheduled hooks").

Pure functions; the supervisor calls them once per tick with the local
time (settings.TIME_ZONE, which is TZ from .env). The rule is "daily at
HH:MM": a hook runs once per local date, at the first tick after run_at.
A container that was down at run_at runs it at the first tick after
start. No cron syntax in v1; anyone who needs more has cron on the host.
"""

from django.utils import timezone


def is_due(run_at, last_started_at, now_local) -> bool:
    """True when the clock is past run_at and today's run has not started.

    `run_at` is a naive time of day in the local zone, `last_started_at`
    an aware datetime (UTC in the registry), `now_local` an aware datetime
    in the local zone.
    """
    if run_at is None:
        return False
    if now_local.time() < run_at:
        return False
    if last_started_at is None:
        return True
    return timezone.localtime(last_started_at, now_local.tzinfo).date() < now_local.date()


def transition(previous_status, ok: bool):
    """The e-mail a run's outcome calls for: 'failed', 'recovered' or None.

    One mail per transition, not per run (same rule as project conditions,
    design §7): a hook that fails every night mails once, and once more
    when it works again.
    """
    if not ok:
        return None if previous_status == "failed" else "failed"
    return "recovered" if previous_status == "failed" else None
