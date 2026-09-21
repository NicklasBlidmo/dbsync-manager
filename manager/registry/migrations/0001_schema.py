"""
Create the registry schema (design §3, §12).

The registry role owns `dbsync_manager` and nothing else; every table of the
manager, including Django's own, lives here because the connection's
search_path puts this schema first (settings.DATABASES).

In normal use the schema already exists when this runs: the pre_migrate
handler in registry/apps.py creates it, because Django's django_migrations
table is created before any migration (see registry/schema.py). This
migration stays as the recorded, ordered step and as a safety net, and uses
the same check-then-create helper so it also works for a role that owns a
pre-created schema without CREATE on the database.
"""

from django.db import migrations

from registry.schema import ensure_registry_schema


def create_schema(apps, schema_editor):
    ensure_registry_schema(schema_editor.connection)


class Migration(migrations.Migration):
    initial = True

    dependencies = []

    # Runs before contenttypes/auth create their tables so those land in the
    # schema too. Django orders it first because everything else in this app
    # depends on it and the auth apps have no reason to run earlier.
    run_before = [
        ("contenttypes", "0001_initial"),
        ("auth", "0001_initial"),
    ]

    operations = [
        migrations.RunPython(create_schema, migrations.RunPython.noop),
    ]
