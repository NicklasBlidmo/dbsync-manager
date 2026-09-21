from django.core.management.base import BaseCommand

from generate_keys import KEYS, new_values


class Command(BaseCommand):
    help = (
        "Print fresh values for DJANGO_SECRET_KEY and DBSYNC_MANAGER_SECRET_KEY. "
        "Only usable once the settings load, i.e. when keys already exist; for a "
        "first installation run the standalone generate_keys.py instead."
    )

    def handle(self, *args, **options):
        values = new_values()
        for key in KEYS:
            self.stdout.write(f"{key}={values[key]}")
