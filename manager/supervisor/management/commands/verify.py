"""
Start a verify operation from the terminal (slice 15, B7):

    manage.py verify --project <id or workspace/name>

The supervisor runs it on its next tick; the project page and
GET /api/v1/operations/<id>/ show the result.
"""

from django.core.management.base import BaseCommand, CommandError

from registry import events
from registry.models import Event, SyncProject
from supervisor import verify as verify_mod


class Command(BaseCommand):
    help = "Start a verify operation (checkout GeoPackage against the base schema) for a project"

    def add_arguments(self, parser):
        parser.add_argument("--project", required=True, help="project id or Mergin path (workspace/name)")

    def handle(self, *args, **options):
        text = options["project"]
        project = SyncProject.objects.filter(pk=text, removed_at__isnull=True).first() if text.isdigit() else None
        if project is None:
            project = SyncProject.objects.filter(mergin_project=text, removed_at__isnull=True).first()
        if project is None:
            raise CommandError(f"no project {text!r}")
        try:
            operation = verify_mod.request_verify(project, None)
        except verify_mod.VerifyConflict as exc:
            raise CommandError(str(exc)) from exc
        events.record(project, Event.Level.INFO, f"verify requested from the terminal (operation {operation.pk})",
                      source=Event.Source.USER)
        self.stdout.write(f"operation {operation.pk} queued for {project.mergin_project}; "
                          f"the supervisor runs it on its next tick")
