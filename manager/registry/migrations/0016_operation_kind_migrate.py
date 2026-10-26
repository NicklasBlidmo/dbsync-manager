# Slice 16: the one-off `migrate` operation (checkout layout of db-sync 2.4.0)
# needs a longer kind column.

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('registry', '0015_api_token'),
    ]

    operations = [
        migrations.AlterField(
            model_name='operation',
            name='kind',
            field=models.CharField(choices=[('remove', 'remove'), ('reinit', 're-initialise'), ('verify', 'verify'), ('migrate', 'migrate checkouts')], max_length=10),
        ),
    ]
