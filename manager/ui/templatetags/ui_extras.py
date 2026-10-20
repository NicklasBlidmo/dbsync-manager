from django import template

register = template.Library()

BADGE_CLASSES = {
    # desired
    "running": "ok", "paused": "muted", "removed": "muted",
    # process
    "stopped": "muted", "initializing": "info", "stopping": "info", "crashed": "bad", "blocked": "bad",
    # health
    "failed": "bad",
    "unknown": "muted", "ok": "ok", "catching_up": "info", "stale": "warn",
    "behind": "warn", "diverged": "bad", "stuck": "bad",
    # direction
    "one_way": "ok", "two_way": "info", "db_edits_cannot_push": "warn",
    # event levels
    "info": "muted", "warning": "warn", "error": "bad",
}


@register.filter
def badge(value):
    """CSS class for a state, health, direction or level value."""
    return BADGE_CLASSES.get(str(value), "muted")


@register.filter
def version(value):
    return f"v{value}" if value is not None else "–"


@register.filter
def isotime(value, fmt="H:i:s"):
    """An ISO timestamp string (as stored in JSON logs) in the page's time zone."""
    from datetime import datetime, timezone as dt_timezone

    from django.utils import dateformat, timezone

    if not value:
        return ""
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError:
        return str(value)
    if timezone.is_naive(parsed):
        parsed = timezone.make_aware(parsed, dt_timezone.utc)
    return dateformat.format(timezone.localtime(parsed), fmt)


@register.filter
def ago(value):
    """Relative time for a recent moment: '59 s ago', '3 min ago', '2 h ago',
    else the local date and time. Empty for None."""
    from datetime import datetime, timezone as dt_timezone

    from django.utils import dateformat, timezone

    if not value:
        return ""
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value)
        except ValueError:
            return value
    if timezone.is_naive(value):
        value = timezone.make_aware(value, dt_timezone.utc)
    seconds = (timezone.now() - value).total_seconds()
    if seconds < 0:
        seconds = 0
    if seconds < 60:
        return f"{int(seconds)} s ago"
    if seconds < 3600:
        return f"{int(seconds // 60)} min ago"
    if seconds < 86400:
        return f"{int(seconds // 3600)} h ago"
    return dateformat.format(timezone.localtime(value), "Y-m-d H:i")


@register.filter
def condition_label(key):
    """'db_behind:1-punkter.gpkg' -> 'db_behind (1-punkter.gpkg)'."""
    name, _, rest = str(key).partition(":")
    return f"{name} ({rest})" if rest else name
