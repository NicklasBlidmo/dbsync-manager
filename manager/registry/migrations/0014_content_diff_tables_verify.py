# Slice 15: the check's content comparison (A) and the GeoPackage's layer
# list (C) on the status row; the `verify` operation (B). Additive only.

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('registry', '0013_installation_settings'),
    ]

    operations = [
        migrations.AddField(
            model_name='syncstatus',
            name='content_diff',
            field=models.JSONField(blank=True, default=dict),
        ),
        migrations.AddField(
            model_name='syncstatus',
            name='content_checked_at',
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='syncstatus',
            name='tables',
            field=models.JSONField(blank=True, default=list),
        ),
        migrations.AlterField(
            model_name='operation',
            name='kind',
            field=models.CharField(choices=[('remove', 'remove'), ('reinit', 're-initialise'), ('verify', 'verify')], max_length=6),
        ),
    ]
