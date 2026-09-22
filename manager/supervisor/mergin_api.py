"""
Mergin Maps API access for the consistency check (design §6 step 5, §7).

Uses mergin-client, which the db-sync base image ships. One client per
Mergin account is cached for the life of the supervisor and re-created
when a call fails, which covers token expiry.
"""

import hashlib
import logging

log = logging.getLogger("supervisor.mergin")

_clients: dict = {}


def _fingerprint(account) -> str:
    return hashlib.sha256(f"{account.url}\0{account.username}\0{account.secret_enc}".encode()).hexdigest()


def _client(account):
    from mergin import MerginClient  # imported here: only present in the image

    key = account.pk
    fingerprint = _fingerprint(account)
    cached = _clients.get(key)
    if cached and cached[0] == fingerprint:
        return cached[1]
    client = MerginClient(account.url, login=account.username, password=account.secret, plugin_version="dbsync-manager")
    _clients[key] = (fingerprint, client)
    return client


def project_info(account, project_path: str) -> dict:
    """GET /v1/project/<workspace>/<name> as a dict. Raises on any failure."""
    try:
        return _client(account).project_info(project_path)
    except Exception:
        _clients.pop(account.pk, None)
        raise


def version_number(info: dict):
    """'v123' -> 123, or None."""
    text = info.get("version") if isinstance(info, dict) else None
    if isinstance(text, str) and text.startswith("v"):
        try:
            return int(text[1:])
        except ValueError:
            return None
    return None


def permission(info: dict):
    """The account's permission on the project: reader, writer, owner or None.

    The API reports `role` for the calling user on newer servers; older ones
    only give the `permissions` flags, where `upload` means it can push.
    """
    if not isinstance(info, dict):
        return None
    role = info.get("role")
    if isinstance(role, str) and role.lower() in ("reader", "writer", "owner"):
        return role.lower()
    perms = info.get("permissions")
    if isinstance(perms, dict):
        if perms.get("upload"):
            return "writer"
        return "reader"
    return None
