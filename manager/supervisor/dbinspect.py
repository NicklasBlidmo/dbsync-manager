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


def postgis_installed(cursor) -> bool:
    """True when the postgis extension exists in the connected database."""
    cursor.execute("SELECT 1 FROM pg_extension WHERE extname = 'postgis'")
    return cursor.fetchone() is not None


def server_facts(database, timeout: int = 5) -> dict:
    """Connect as the row's role and report what a wrong target hides (slice 12).

    Two rounds of the first clean-room install went to a wrong user name and
    a LAN address that pointed at another server; the wizard's new/adopt
    verdict does not make that obvious. Raises on any connection error.
    """
    with connect(database, timeout) as conn, conn.cursor() as cursor:
        cursor.execute("SHOW server_version")
        (server_version,) = cursor.fetchone()
        cursor.execute("SELECT extversion FROM pg_extension WHERE extname = 'postgis'")
        row = cursor.fetchone()
        cursor.execute(
            "SELECT count(*) FROM pg_namespace WHERE nspname NOT LIKE 'pg\\_%' "
            "AND nspname NOT IN ('information_schema', 'public', 'topology', 'tiger', 'tiger_data')"
        )
        (schemas,) = cursor.fetchone()
    return {"server_version": server_version, "postgis_version": row[0] if row else None, "schemas": schemas}


def describe(facts: dict) -> str:
    """'ok, PostgreSQL 16.4, PostGIS 3.4.2, 5 schemas', or the PostGIS complaint."""
    if not facts["postgis_version"]:
        return (f"connected, PostgreSQL {facts['server_version']}, but PostGIS is not installed: "
                "run CREATE EXTENSION postgis there as a superuser")
    n = facts["schemas"]
    return (f"ok, PostgreSQL {facts['server_version']}, PostGIS {facts['postgis_version']}, "
            f"{n} schema{'' if n == 1 else 's'} besides the system ones")


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
