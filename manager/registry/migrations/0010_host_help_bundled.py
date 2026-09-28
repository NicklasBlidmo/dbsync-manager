# Help text only (slice 12 brief, B7): the bundled PostGIS is reached as 'postgis'.

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('registry', '0009_drop_installation_settings'),
    ]

    operations = [
        migrations.AlterField(
            model_name='database',
            name='host',
            field=models.CharField(help_text="As seen from inside the manager container: 'localhost' is the container itself; use the server's LAN address or Docker service name. With the bundled PostGIS: 'postgis'.", max_length=255),
        ),
    ]
