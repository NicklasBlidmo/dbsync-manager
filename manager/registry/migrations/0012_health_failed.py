# Health `failed` (review note 2026-10-20b, finding 2): a deterministic daemon
# failure, reported by the check while checkout_inconsistent or
# database_missing_postgis is active. Choices only; no row changes.

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('registry', '0011_scheduled_hooks'),
    ]

    operations = [
        migrations.AlterField(
            model_name='syncproject',
            name='health',
            field=models.CharField(choices=[('failed', 'failed'), ('diverged', 'diverged'), ('behind', 'behind'), ('stuck', 'stuck'), ('stale', 'stale'), ('unknown', 'unknown'), ('catching_up', 'catching up'), ('ok', 'ok')], default='unknown', editable=False, max_length=12),
        ),
    ]
