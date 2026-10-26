# Slice 17, A2: a per-project switch to mail every rebase conflict (default off:
# in a two-way project a conflict is expected behaviour).

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('registry', '0016_operation_kind_migrate'),
    ]

    operations = [
        migrations.AddField(
            model_name='syncproject',
            name='mail_on_conflict',
            field=models.BooleanField(default=False, help_text='Mail the alert recipients for every rebase conflict (a database edit dropped because the field changed the same value). Off: the conflict is an event on the project page only.'),
        ),
    ]
