"""
When a scheduled hook is due (slice 13, design §8 "Scheduled hooks").

Pure functions; the supervisor calls them once per tick with the local
time (settings.TIME_ZONE, which is TZ from .env). The rule is "daily at
HH:MM": a hook is due once the clock is past today's run_at and no run
has started since that moment. A container that was down at run_at runs
it at the first tick after start; run_at moved later the same day gives
one more run (field test 2026-09-30). No cron syntax in v1; anyone who
needs more has cron on the host.
"""

from django.utils import timezone


def is_due(run_at, last_started_at, now_local) -> bool:
    """True when now is past today's run_at and no run started at or after it.

    `run_at` is a naive time of day in the local zone, `last_started_at`
    an aware datetime (UTC in the registry), `now_local` an aware datetime
    in the local zone. Comparing with today's run_at rather than today's
    date means: unchanged schedule, one run per day; down over run_at,
    catch-up at the first tick; run_at moved later than the last run the
    same day, one more run; moved earlier, nothing until tomorrow.
    """
    if run_at is None:
        return False
    today_at = now_local.replace(hour=run_at.hour, minute=run_at.minute, second=run_at.second, microsecond=0)
    if now_local < today_at:
        return False
    if last_started_at is None:
        return True
    return timezone.localtime(last_started_at, now_local.tzinfo) < today_at


def transition(previous_status, ok: bool):
    """The e-mail a run's outcome calls for: 'failed', 'recovered' or None.

    One mail per transition, not per run (same rule as project conditions,
    design §7): a hook that fails every night mails once, and once more
    when it works again.
    """
    if not ok:
        return None if previous_status == "failed" else "failed"
    return "recovered" if previous_status == "failed" else None
