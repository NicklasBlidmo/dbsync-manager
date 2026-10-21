# Slice 15, E11: API tokens. The secret is stored as a SHA-256 hash and shown
# once at creation; a token acts as its user.

import django.db.models.deletion
import django.utils.timezone
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('registry', '0014_content_diff_tables_verify'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name='ApiToken',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('name', models.CharField(help_text="The tool that holds the token, e.g. 'akdb' or 'MerginQA'; shown in the event log.", max_length=100, unique=True)),
                ('token_hash', models.CharField(editable=False, max_length=64, unique=True)),
                ('prefix', models.CharField(editable=False, help_text='The first characters of the token, to tell tokens apart.', max_length=8)),
                ('created_at', models.DateTimeField(default=django.utils.timezone.now, editable=False)),
                ('last_used_at', models.DateTimeField(blank=True, editable=False, null=True)),
                ('revoked', models.BooleanField(default=False, help_text='A revoked token is refused; create a new one to rotate.')),
                ('user', models.ForeignKey(help_text='The token acts as this user: its groups decide what it may read and do.', on_delete=django.db.models.deletion.CASCADE, related_name='api_tokens', to=settings.AUTH_USER_MODEL)),
            ],
            options={
                'verbose_name': 'API token',
                'ordering': ['name'],
            },
        ),
    ]
