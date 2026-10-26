"""
Request the checkout layout migration for db-sync 2.4.0 (slice 16, C10):

    manage.py migrate_checkouts [--list]

Every project whose checkout still has the 2.3.0 layout
(<work>/<project>/<modified schema>) gets a `migrate` operation; the
supervisor runs them one at a time: stop the daemon between cycles,
remove the old tree, start, and wait until db-sync has downloaded one
GeoPackage per connection. Run Verify on every project first; a project
that fails Verify needs Re-init instead. The same button is on the
Projects page.
"""

from django.core.management.base import BaseCommand

from supervisor import layout


class Command(BaseCommand):
    help = "Migrate the checkouts of every project to the db-sync 2.4.0 layout (one directory per connection)"

    def add_arguments(self, parser):
        parser.add_argument("--list", action="store_true", help="only list the projects that need it")

    def handle(self, *args, **options):
        projects = layout.projects_needing_migration()
        if not projects:
            self.stdout.write("no project has a checkout in the old layout; nothing to do")
            return
        for project in projects:
            self.stdout.write(f"{project.pk}\t{project.mergin_project}\t{layout.legacy_checkout_tree(project.work_dir, project.mergin_project)}")
        if options["list"]:
            return
        created, skipped = layout.request_migrations(projects, None)
        for operation in created:
            self.stdout.write(f"operation {operation.pk} queued for {operation.project.mergin_project}")
        for project, why in skipped:
            self.stdout.write(f"skipped {project.mergin_project}: {why}")
        if created:
            self.stdout.write("the supervisor runs them one project at a time; the project pages show the steps")
