"""
Read-only look into a sync database with the sync's own credentials (§3).

The registry role has no access to synced schemas, so every fact about
them comes through a separate connection built from the project's
`database` row. Only SELECTs and catalogue queries run on it.
"""

import json

import psycopg


def connect(database, timeout: int = 5):
    return psycopg.connect(database.conn_info(with_password=True), connect_timeout=timeout, autocommit=True)


def base_schema_comments(database, schemas) -> dict:
    """Map each schema name to db-sync's comment as a dict, or None.

    db-sync writes `{"name": "<ws>/<project>", "version": "v123", ...}` on
    the base schema after init and after each applied pull (§6). A missing
    schema and a schema without a parsable comment both give None.
    """
    result = {name: None for name in schemas}
    if not schemas:
        return result
    with connect(database) as conn, conn.cursor() as cursor:
        cursor.execute(
            "SELECT nspname, obj_description(oid, 'pg_namespace') "
            "FROM pg_namespace WHERE nspname = ANY(%s)",
            [list(schemas)],
        )
        for name, comment in cursor.fetchall():
            if not comment:
                continue
            try:
                parsed = json.loads(comment)
            except ValueError:
                continue
            if isinstance(parsed, dict):
                result[name] = parsed
    return result


def parse_comment(comment) -> dict | None:
    """db-sync's JSON schema comment as a dict, or None."""
    if not comment:
        return None
    try:
        parsed = json.loads(comment)
    except ValueError:
        return None
    return parsed if isinstance(parsed, dict) else None


def parse_version(text) -> int | None:
    """'v123' -> 123; None for anything else."""
    if not isinstance(text, str) or not text.startswith("v"):
        return None
    try:
        return int(text[1:])
    except ValueError:
        return None
