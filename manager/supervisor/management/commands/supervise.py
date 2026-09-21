from django.conf import settings
from django.core.management.base import BaseCommand

from supervisor.core import Supervisor


class Command(BaseCommand):
    help = "Run the supervisor loop (design §6). One instance per registry; started by s6 in the container."

    def add_arguments(self, parser):
        parser.add_argument(
            "--tick",
            type=int,
            default=settings.DBSYNC_TICK,
            help=f"seconds between ticks (default DBSYNC_TICK={settings.DBSYNC_TICK})",
        )

    def handle(self, *args, **options):
        Supervisor(tick_seconds=options["tick"]).run()
