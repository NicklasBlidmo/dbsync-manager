"""
Create the registry schema when it is missing (design §3, §12).

This cannot be left to a migration alone: Django creates its own
`django_migrations` table *before* it runs the first migration, and with
search_path `dbsync_manager,public` a missing first schema makes that table
land in `public`. On PostgreSQL 15+ an ordinary role may not create there,
so `migrate` fails ("permission denied for schema public"); with a superuser
it succeeds and leaves a manager table outside the registry schema. Both are
wrong, so the schema is created on `pre_migrate`, which fires first.

The existence check is deliberate. PostgreSQL checks CREATE on the database
before it looks at IF NOT EXISTS, so `CREATE SCHEMA IF NOT EXISTS` fails for
a role without that privilege even when the schema is already there. A DBA
who pre-creates the schema (`CREATE SCHEMA dbsync_manager AUTHORIZATION
<role>`) must not have to grant CREATE on the database as well.
"""

from django.conf import settings


def ensure_registry_schema(connection):
    """Create settings.REGISTRY_SCHEMA on `connection` unless it exists."""
    if connection.vendor != "postgresql":
        return False
    schema = settings.REGISTRY_SCHEMA
    with connection.cursor() as cursor:
        cursor.execute("SELECT 1 FROM pg_namespace WHERE nspname = %s", [schema])
        if cursor.fetchone():
            return False
        cursor.execute(f"CREATE SCHEMA {connection.ops.quote_name(schema)}")
    return True
