"""
What a db-sync exit means, from its last stderr lines (review note
2026-10-20b, findings 2 and 3).

Backoff and `process_crashed` are for crashes that might go away. Two
families do not:

- deterministic: the daemon will fail the same way on every start. A
  checkout that db-sync finds "not synchronized with source GPKG" (a pull
  killed between "GeoPackage updated" and "base schema updated") is
  repaired only by Re-init; a database without the postgis extension by
  the DBA. The supervisor stops restarting, raises the condition with one
  mail, and the check reports health `failed`.
- Mergin connectivity: LoginError, URLError, a timeout or a 5xx from the
  API is `mergin_unreachable` seen from the daemon's side, fed into the
  same delayed, grouped alert as the check's; the restart delay stays at
  60 s so the server's return is noticed. 401/403 is a configuration
  error: `mergin_auth_failed`, one mail, restarts capped at 300 s.
"""

from dataclasses import dataclass

from .check import DETERMINISTIC_CONDITIONS, TRANSIENT_API_MARKERS

CHECKOUT_INCONSISTENT, DATABASE_MISSING_POSTGIS = DETERMINISTIC_CONDITIONS
MERGIN_UNREACHABLE = "mergin_unreachable"
MERGIN_AUTH_FAILED = "mergin_auth_failed"

_DETERMINISTIC = (
    (CHECKOUT_INCONSISTENT,
     ("not synchronized with source GPKG", "--force-init", "Modified and base schemas already exist"),
     "the checkout and the database no longer agree; use Re-init on the project page "
     "(dump the installation's own schema first, docs/backup.md)"),
    (DATABASE_MISSING_POSTGIS,
     ("Cannot find or activate postgis extension", "activate postgis"),
     "the database has no postgis extension; create it as a superuser, then Pause and Resume"),
)
_AUTH_MARKERS = ("401", "403", "unauthorized", "forbidden", "invalid username or password", "invalid credentials")
_UNREACHABLE_MARKERS = tuple(m.lower() for m in TRANSIENT_API_MARKERS) + (
    "loginerror", "urlerror", "urlopen error", "connection error", "max retries exceeded",
)


@dataclass
class ExitKind:
    condition: str
    advice: str
    deterministic: bool = False


def classify_exit(tail: str) -> ExitKind | None:
    """The condition an exit belongs to, or None for an ordinary crash."""
    text = tail or ""
    lower = text.lower()
    for condition, markers, advice in _DETERMINISTIC:
        if any(marker in text for marker in markers):
            return ExitKind(condition, advice, deterministic=True)
    if any(marker in lower for marker in _AUTH_MARKERS):
        return ExitKind(MERGIN_AUTH_FAILED, "the Mergin account's credentials are refused; fix them under Settings")
    if any(marker in lower for marker in _UNREACHABLE_MARKERS):
        return ExitKind(MERGIN_UNREACHABLE, "the Mergin server cannot be reached; retried every minute")
    return None
