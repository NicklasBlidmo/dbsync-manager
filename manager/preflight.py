#!/usr/bin/env python3
"""
Start-up checks, run before `migrate` by the container (docker/scripts/migrate).

A configuration mistake used to surface as a 60-line Django traceback,
repeated several times a second by the restart policy. This script checks the
things that actually go wrong on a first installation and says what to fix in
one line each. It does not import the Django settings, so it can report a
missing key instead of crashing on it. Exit code 0 = go on, 1 = do not start.
"""

import os
import sys
import time
from urllib.parse import unquote, urlsplit

PREFIX = "dbsync-manager preflight:"
REGISTRY_SCHEMA = "dbsync_manager"


def check_environment(env):
    """Return a list of problems with the configuration; no I/O."""
    problems = []

    for name in ("DJANGO_SECRET_KEY", "DBSYNC_MANAGER_SECRET_KEY", "DATABASE_URL"):
        if not env.get(name, "").strip():
            problems.append(
                f"{name} is empty or missing in .env. If the file has two lines for it, "
                "the last one wins; keep one. Keys: python3 manager/generate_keys.py --write .env"
            )

    fernet = env.get("DBSYNC_MANAGER_SECRET_KEY", "").strip()
    if fernet:
        try:
            from cryptography.fernet import Fernet

            Fernet(fernet.encode())
        except Exception:  # noqa: BLE001 - any failure means the key is unusable
            problems.append(
                "DBSYNC_MANAGER_SECRET_KEY is not a valid Fernet key (44 URL-safe base64 characters). "
                "A `$` in the value is interpolated by Compose; generate it with generate_keys.py"
            )

    url = env.get("DATABASE_URL", "").strip()
    if url:
        if "=" in url.split("://", 1)[0]:
            problems.append(
                f"DATABASE_URL starts with {url.split('://', 1)[0]!r}: the variable name was "
                "pasted into the value. The line must read DATABASE_URL=postgres://..."
            )
        else:
            parts = urlsplit(url)
            if parts.scheme not in ("postgres", "postgresql", "pgsql", "postgis"):
                problems.append(f"DATABASE_URL must start with postgres://, got {parts.scheme or 'nothing'}://")
            elif not parts.hostname or not parts.path.strip("/"):
                problems.append("DATABASE_URL needs a host and a database: postgres://role:password@host:5432/dbname")

    tz = env.get("TZ", "").strip()
    if tz:
        try:
            from zoneinfo import ZoneInfo

            ZoneInfo(tz)
        except Exception:  # noqa: BLE001
            problems.append(f"TZ={tz!r} is not a known time zone name (example: Europe/Stockholm)")

    return problems


def explain_connection_error(message, user, host, port, dbname):
    """One line for the PostgreSQL errors seen in practice."""
    text = message.lower()
    if "password authentication failed" in text:
        return (
            f"login as role {user!r} was refused. PostgreSQL gives this same message for a wrong "
            "password and for a role that does not exist; check both (see README, Database)"
        )
    if "does not exist" in text and "database" in text:
        return f"database {dbname!r} does not exist on {host}:{port}"
    if "no pg_hba.conf entry" in text:
        return f"{host} does not accept connections from this container (pg_hba.conf)"
    if any(s in text for s in ("connection refused", "timeout", "timed out", "no route", "unreachable")):
        if host in ("localhost", "127.0.0.1", "::1"):
            # Not rejected up front: it is correct with network_mode: host.
            return (
                f"cannot reach PostgreSQL at {host}:{port}. Inside the container, {host} is the "
                "container itself; use the server's LAN address or a shared Docker network"
            )
        return (
            f"cannot reach PostgreSQL at {host}:{port}. Is it published on that address, and does "
            "a host firewall (ufw) allow the Docker network?"
        )
    if "name or service not known" in text or "could not translate host name" in text:
        return f"host name {host!r} does not resolve from inside the container"
    return message.strip().splitlines()[-1]


def check_database(url, wait_seconds):
    """Connect (retrying while the server is starting) and check the schema."""
    import psycopg

    parts = urlsplit(url)
    host, port = parts.hostname, parts.port or 5432
    user, dbname = unquote(parts.username or ""), parts.path.strip("/")
    params = dict(
        host=host, port=port, dbname=dbname, user=user,
        password=unquote(parts.password or ""), connect_timeout=5,
    )

    deadline = time.monotonic() + wait_seconds
    while True:
        try:
            conn = psycopg.connect(**params)
            break
        except psycopg.OperationalError as exc:
            message = str(exc)
            transient = any(s in message.lower() for s in ("starting up", "connection refused", "timeout", "timed out"))
            if transient and time.monotonic() < deadline:
                time.sleep(2)
                continue
            return [explain_connection_error(message, user, host, port, dbname)]

    with conn, conn.cursor() as cursor:
        cursor.execute(
            "SELECT EXISTS (SELECT 1 FROM pg_namespace WHERE nspname = %s),"
            " has_database_privilege(current_user, current_database(), 'CREATE')",
            [REGISTRY_SCHEMA],
        )
        schema_exists, can_create_schema = cursor.fetchone()
        if schema_exists:
            cursor.execute("SELECT has_schema_privilege(current_user, %s, 'CREATE')", [REGISTRY_SCHEMA])
            if not cursor.fetchone()[0]:
                return [
                    f"role {user!r} may not create tables in schema {REGISTRY_SCHEMA}. "
                    f"Fix: ALTER SCHEMA {REGISTRY_SCHEMA} OWNER TO {user};"
                ]
        elif not can_create_schema:
            return [
                f"schema {REGISTRY_SCHEMA} does not exist and role {user!r} may not create it. Fix, as a "
                f"database admin: CREATE SCHEMA {REGISTRY_SCHEMA} AUTHORIZATION {user};"
            ]
    return []


def main():
    problems = check_environment(os.environ)
    if not problems:
        wait = int(os.environ.get("DBSYNC_DB_WAIT", "30") or 30)
        problems = check_database(os.environ["DATABASE_URL"].strip(), wait)
    for problem in problems:
        print(f"{PREFIX} {problem}", flush=True)
    if problems:
        return 1
    print(f"{PREFIX} configuration and registry database ok", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
