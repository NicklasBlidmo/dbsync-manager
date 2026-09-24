from django import template

register = template.Library()

BADGE_CLASSES = {
    # desired
    "running": "ok", "paused": "muted", "removed": "muted",
    # process
    "stopped": "muted", "initializing": "info", "stopping": "info", "crashed": "bad", "blocked": "bad",
    # health
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
    from datetime import datetime

    from django.utils import dateformat, timezone

    if not value:
        return ""
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError:
        return str(value)
    if timezone.is_naive(parsed):
        parsed = timezone.make_aware(parsed, timezone.utc)
    return dateformat.format(timezone.localtime(parsed), fmt)


@register.filter
def condition_label(key):
    """'db_behind:1-punkter.gpkg' -> 'db_behind (1-punkter.gpkg)'."""
    name, _, rest = str(key).partition(":")
    return f"{name} ({rest})" if rest else name
