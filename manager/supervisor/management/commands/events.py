"""
Events for the terminal (review note 2026-10-20b, finding 5): the project
page shows a window, and container logs are gone after a recreate.

    manage.py events --project 3 --since 2026-10-20T13:00 [--until ...] [--level error] [--limit 500]
"""

from datetime import datetime

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from registry.models import Event, SyncProject


def parse_when(text: str):
    try:
        value = datetime.fromisoformat(text)
    except ValueError as exc:
        raise CommandError(f"not a date/time: {text!r} (use 2026-10-20 or 2026-10-20T13:07)") from exc
    return value if timezone.is_aware(value) else timezone.make_aware(value)


class Command(BaseCommand):
    help = "Print events, oldest first: --project <id or workspace/name> --since <ISO> [--until] [--level] [--condition]"

    def add_arguments(self, parser):
        parser.add_argument("--project", help="project id or Mergin path (workspace/name); omitted = every project and the installation")
        parser.add_argument("--since", help="ISO date or date/time in TZ, e.g. 2026-10-20T13:00")
        parser.add_argument("--until", help="ISO date or date/time in TZ")
        parser.add_argument("--level", choices=[c[0] for c in Event.Level.choices])
        parser.add_argument("--condition", help="only events carrying this condition key (prefix match)")
        parser.add_argument("--limit", type=int, default=500)

    def handle(self, *args, **options):
        qs = Event.objects.select_related("project", "user").order_by("at")
        if options["project"]:
            text = options["project"]
            project = SyncProject.objects.filter(pk=text).first() if text.isdigit() else None
            if project is None:
                project = SyncProject.objects.filter(mergin_project=text).first()
            if project is None:
                raise CommandError(f"no project {text!r}")
            qs = qs.filter(project=project)
        if options["since"]:
            qs = qs.filter(at__gte=parse_when(options["since"]))
        if options["until"]:
            qs = qs.filter(at__lt=parse_when(options["until"]))
        if options["level"]:
            qs = qs.filter(level=options["level"])
        if options["condition"]:
            qs = qs.filter(condition__startswith=options["condition"])
        count = 0
        for event in qs[:options["limit"]]:
            count += 1
            who = event.project.mergin_project if event.project_id else "-"
            source = event.source + (f"({event.user.get_username()})" if event.user_id else "")
            first, _, rest = event.message.partition("\n")
            line = f"{timezone.localtime(event.at):%Y-%m-%d %H:%M:%S} {event.level:<7} {source:<14} {who:<28} {first}"
            if event.condition:
                line += f"  [{event.condition}]"
            self.stdout.write(line)
            for extra in rest.splitlines():
                self.stdout.write("    " + extra)
        self.stderr.write(f"{count} event(s)")
