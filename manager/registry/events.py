"""Write `event` rows (design §5): the audit trail and the alert conditions."""

from .models import Event


def record(project, level, message, *, source=Event.Source.SUPERVISOR, condition="", user=None):
    return Event.objects.create(
        project=project,
        level=level,
        source=source,
        message=message,
        condition=condition,
        user=user,
    )


def info(project, message, **kwargs):
    return record(project, Event.Level.INFO, message, **kwargs)


def warning(project, message, **kwargs):
    return record(project, Event.Level.WARNING, message, **kwargs)


def error(project, message, **kwargs):
    return record(project, Event.Level.ERROR, message, **kwargs)
