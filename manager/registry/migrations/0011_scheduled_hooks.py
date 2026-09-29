# Scheduled hooks (slice 13 brief, A1-A3). Only additive: Hook gets the
# fields used when kind = scheduled, SyncRun.project becomes nullable so a
# scheduled run is logged with the same row type, and SyncRun.hook records
# which hook a run belongs to. No existing row changes: every SyncRun keeps
# its project, every Hook gets enabled=True and empty scheduling fields.

import django.contrib.postgres.fields
import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('registry', '0010_host_help_bundled'),
    ]

    operations = [
        migrations.AlterField(
            model_name='hook',
            name='kind',
            field=models.CharField(choices=[('post_init', 'post_init'), ('pre_remove', 'pre_remove'), ('post_pull', 'post_pull'), ('scheduled', 'scheduled')], max_length=20),
        ),
        migrations.AlterField(
            model_name='hook',
            name='database',
            field=models.ForeignKey(blank=True, help_text="Connection the hook's PG* variables and pgpass use. Project hooks: leave empty to use the project's sync connection. Scheduled hooks: required.", null=True, on_delete=django.db.models.deletion.PROTECT, related_name='hooks', to='registry.database', verbose_name='run as (database connection)'),
        ),
        migrations.AddField(
            model_name='hook',
            name='enabled',
            field=models.BooleanField(default=True, help_text='Scheduled hooks only: a disabled hook is never started by the clock.'),
        ),
        migrations.AddField(
            model_name='hook',
            name='run_at',
            field=models.TimeField(blank=True, help_text="Scheduled hooks only: time of day, in the installation's TZ. Once per day, at the first supervisor tick after this time.", null=True),
        ),
        migrations.AddField(
            model_name='hook',
            name='notify_emails',
            field=django.contrib.postgres.fields.ArrayField(base_field=models.EmailField(max_length=254), blank=True, default=list, help_text='Scheduled hooks only: one mail when a run fails, one when the next run succeeds.', size=None),
        ),
        migrations.AddField(
            model_name='hook',
            name='last_started_at',
            field=models.DateTimeField(blank=True, editable=False, null=True),
        ),
        migrations.AddField(
            model_name='hook',
            name='last_finished_at',
            field=models.DateTimeField(blank=True, editable=False, null=True),
        ),
        migrations.AddField(
            model_name='hook',
            name='last_status',
            field=models.CharField(blank=True, default='', editable=False, max_length=12),
        ),
        migrations.AlterField(
            model_name='syncrun',
            name='project',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.CASCADE, related_name='runs', to='registry.syncproject'),
        ),
        migrations.AddField(
            model_name='syncrun',
            name='hook',
            field=models.ForeignKey(blank=True, editable=False, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='runs', to='registry.hook'),
        ),
    ]
