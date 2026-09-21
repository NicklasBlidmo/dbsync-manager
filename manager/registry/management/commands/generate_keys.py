from django.core.management.base import BaseCommand
from django.core.management.utils import get_random_secret_key

from registry.crypto import generate_key


class Command(BaseCommand):
    help = "Print fresh values for DJANGO_SECRET_KEY and DBSYNC_MANAGER_SECRET_KEY for .env"

    def handle(self, *args, **options):
        self.stdout.write(f"DJANGO_SECRET_KEY={get_random_secret_key()}")
        self.stdout.write(f"DBSYNC_MANAGER_SECRET_KEY={generate_key()}")
