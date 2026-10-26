"""
Render db-sync's config.yaml from registry rows (design §6).

The registry is the source of truth and the file is a derived artefact:
regenerated on every start, never read back, mode 600 because it holds the
Mergin password and the database password in clear (§4).

Format as db-sync 2.4.0's config.py validates it: `mergin.{url,username,
password}`, `init_from`, `working_dir`, `connections[].{driver,conn_info,
modified,base,mergin_project,sync_file,skip_tables?,include_tables?}` and
`daemon.{sleep_time,max_failed_retries}`.
"""

import os
import tempfile
from pathlib import Path

import yaml

from registry.models import MerginAccount

from .layout import WORKING_SUBDIR  # noqa: F401 - re-exported; layout.py owns the checkout paths

# db-sync's config parser (dynaconf) treats a value starting with "@" as a
# directive: "@int 5" becomes 5, "@format {env[HOME]}" becomes the home
# directory. A password beginning with "@" would be silently mangled.
DIRECTIVE_PREFIX = "@"

# db-sync 2.4.0 retries a failed login or init inside the process with its
# own backoff and exits only after `max_failed_retries` consecutive failures
# (upstream default 10: about an hour). The supervisor owns the restart
# policy and classifies exits (exits.py), so the daemon gets one retry and
# then exits; 0 would mean never. Do not raise this (slice 16).
MAX_FAILED_RETRIES = 1


class ConfigRenderError(Exception):
    """The project cannot be turned into a valid db-sync config; the message says why."""


def _reject_directive(name, value):
    if isinstance(value, str) and value.startswith(DIRECTIVE_PREFIX):
        raise ConfigRenderError(
            f"{name} starts with '{DIRECTIVE_PREFIX}', which db-sync's config parser treats as a "
            "directive and would change; use a value that does not start with it"
        )


def render_config(project, connections, account, database, work_dir) -> str:
    """Return config.yaml text. Pure: takes the rows, touches no database.

    Raises ConfigRenderError for anything db-sync could not run with, and
    registry.crypto.SecretError if a stored secret cannot be decrypted.
    """
    connections = list(connections)
    if not connections:
        raise ConfigRenderError("project has no connections (GeoPackages) to sync")
    if account.kind != MerginAccount.Kind.PASSWORD:
        raise ConfigRenderError(
            "db-sync 2.4.0 logs in to Mergin Maps with username and password only; "
            f"account '{account.name}' is an API token account"
        )
    if "/" not in project.mergin_project:
        raise ConfigRenderError("mergin_project must be given as workspace/name")

    mergin_secret = account.secret
    db_password = database.password
    _reject_directive("the Mergin password", mergin_secret)
    _reject_directive("the database password", db_password)

    entries = []
    for conn in connections:
        if conn.skip_tables and conn.include_tables:
            raise ConfigRenderError(
                f"connection {conn.sync_file}: skip_tables and include_tables cannot both be set"
            )
        entry = {
            "driver": "postgres",
            "conn_info": database.conn_info(with_password=True),
            "modified": conn.modified_schema,
            "base": conn.base_schema,
            "mergin_project": project.mergin_project,
            "sync_file": conn.sync_file,
        }
        if conn.skip_tables:
            entry["skip_tables"] = list(conn.skip_tables)
        if conn.include_tables:
            entry["include_tables"] = list(conn.include_tables)
        entries.append(entry)

    document = {
        "mergin": {"url": account.url, "username": account.username, "password": mergin_secret},
        # str(): a TextChoices member on an unsaved instance is not plain YAML.
        "init_from": str(project.init_from),
        "working_dir": str(Path(work_dir) / WORKING_SUBDIR),
        "connections": entries,
        "daemon": {"sleep_time": int(project.sleep_time), "max_failed_retries": MAX_FAILED_RETRIES},
    }
    return yaml.safe_dump(document, default_flow_style=False, allow_unicode=True, sort_keys=False)


def write_config(path, text) -> Path:
    """Write the file atomically with mode 600 in a directory of mode 700."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(path.parent, 0o700)
    except OSError:
        pass
    fd, tmp_name = tempfile.mkstemp(prefix=".config-", suffix=".yaml", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.chmod(tmp_name, 0o600)
        os.replace(tmp_name, path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise
    return path
