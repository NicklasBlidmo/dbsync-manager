"""
Create the registry schema (design §3, §12).

The registry role owns `dbsync_manager` and nothing else; every table of the
manager, including Django's own, lives here because the connection's
search_path puts this schema first (settings.DATABASES).
"""

from django.db import migrations

SCHEMA = "dbsync_manager"


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
        migrations.RunSQL(
            sql=f'CREATE SCHEMA IF NOT EXISTS "{SCHEMA}"',
            reverse_sql=migrations.RunSQL.noop,
        ),
    ]
