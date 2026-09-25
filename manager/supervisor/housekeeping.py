"""
Retention (design §10): events and runs older than DBSYNC_RETENTION_DAYS
are deleted, except events with a user (the audit trail), which are kept.
Log files are rotated elsewhere (logs.py). Runs once a day from the tick.
"""

import logging
from datetime import timedelta

from django.conf import settings
from django.utils import timezone

from registry.models import Event, SyncRun

log = logging.getLogger("supervisor.housekeeping")


def prune(days: int | None = None) -> dict:
    days = settings.DBSYNC_RETENTION_DAYS if days is None else days
    cutoff = timezone.now() - timedelta(days=days)
    events_deleted, _ = Event.objects.filter(at__lt=cutoff, user__isnull=True).delete()
    runs_deleted, _ = SyncRun.objects.filter(started_at__lt=cutoff).delete()
    if events_deleted or runs_deleted:
        log.info("pruned %d events and %d runs older than %d days", events_deleted, runs_deleted, days)
    return {"events": events_deleted, "runs": runs_deleted}
