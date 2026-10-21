# Default alert recipients for the installation (review note 2026-10-20d,
# finding 2). One row; projects and scheduled hooks with an empty
# notify_emails use it.

import django.contrib.postgres.fields
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('registry', '0012_health_failed'),
    ]

    operations = [
        migrations.CreateModel(
            name='InstallationSettings',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('notify_emails', django.contrib.postgres.fields.ArrayField(base_field=models.EmailField(max_length=254), blank=True, default=list, help_text='Comma-separated. Used by every project and scheduled hook whose own list is empty.', size=None, verbose_name='default alert recipients')),
                ('updated_at', models.DateTimeField(auto_now=True)),
            ],
            options={
                'verbose_name': 'installation settings',
                'verbose_name_plural': 'Settings (installation)',
            },
        ),
    ]
