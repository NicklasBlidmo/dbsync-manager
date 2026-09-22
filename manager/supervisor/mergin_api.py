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


def permission_with_source(info: dict, username: str | None = None):
    """(permission, source): permission is reader, writer, owner or None.

    Three sources, in order of precision:
    - "role": `role`, which project_info reports for the calling user;
    - "access": the project's `access` name lists, when `username` is
      given (the paginated project list has no `role`, but may list
      owners, writers, editors and readers by name);
    - "flags": the `permissions` flags, where `upload` means it can push.
      This cannot tell an owner from a writer, nor see a role held through
      the workspace; callers that display it should say "can push".
    `editor` (servers from 2024.4) can push and is treated as writer.
    """
    if not isinstance(info, dict):
        return None, None
    role = info.get("role")
    if isinstance(role, str):
        role = role.lower()
        if role == "editor":
            return "writer", "role"
        if role in ("reader", "writer", "owner"):
            return role, "role"
    access = info.get("access")
    if username and isinstance(access, dict):
        for key, value in (("ownersnames", "owner"), ("writersnames", "writer"),
                           ("editorsnames", "writer"), ("readersnames", "reader")):
            names = access.get(key) or []
            if username in names:
                return value, "access"
    perms = info.get("permissions")
    if isinstance(perms, dict):
        return ("writer" if perms.get("upload") else "reader"), "flags"
    return None, None


def permission(info: dict, username: str | None = None):
    """The account's permission on the project: reader, writer, owner or None."""
    return permission_with_source(info, username)[0]


# --- wizard helpers (design §9) --------------------------------------------


def workspaces(account) -> list:
    """[{'name': ..., 'role': ...}, ...] the account can see."""
    try:
        items = _client(account).workspaces_list()
    except Exception:
        _clients.pop(account.pk, None)
        raise
    result = []
    for item in items or []:
        name = item.get("name") if isinstance(item, dict) else None
        if name:
            result.append({"name": name, "role": item.get("role"), "id": item.get("id")})
    return sorted(result, key=lambda w: w["name"].lower())


def search_projects(account, workspace: str, text: str = "", limit: int = 50) -> list:
    """Projects in `workspace` whose name contains `text`, with the account's permission."""
    try:
        page = _client(account).paginated_projects_list(
            page=1, per_page=limit, name=text or None, only_namespace=workspace, order_params="name_asc",
        )
    except Exception:
        _clients.pop(account.pk, None)
        raise
    result = []
    for item in page.get("projects", []) if isinstance(page, dict) else []:
        perm, source = permission_with_source(item, account.username)
        result.append({
            "path": f"{item.get('namespace')}/{item.get('name')}",
            "name": item.get("name"),
            "version": version_number(item),
            "updated": item.get("updated"),
            "permission": perm,
            # What the list can honestly say: a role, or only whether it can push.
            "permission_label": perm if source in ("role", "access") else (
                {"writer": "can push", "reader": "read-only"}.get(perm, "none")
            ),
        })
    return result


def geopackages(info: dict) -> list:
    """GeoPackage files of a project from project_info: [{'path', 'size'}, ...]."""
    files = info.get("files", []) if isinstance(info, dict) else []
    result = []
    for entry in files:
        path = entry.get("path") if isinstance(entry, dict) else None
        if path and path.lower().endswith(".gpkg"):
            result.append({"path": path, "size": entry.get("size")})
    return sorted(result, key=lambda f: f["path"].lower())
