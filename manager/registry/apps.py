from django.apps import AppConfig
from django.db import connections
from django.db.models.signals import post_migrate, pre_migrate


def _ensure_schema(sender, using, **kwargs):
    from .schema import ensure_registry_schema

    ensure_registry_schema(connections[using])


class RegistryConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "registry"
    verbose_name = "Registry"

    def ready(self):
        from .permissions import ensure_groups

        # The registry schema must exist before Django creates its
        # django_migrations table, which happens before the first migration
        # runs; see registry/schema.py for why a migration is not enough.
        pre_migrate.connect(_ensure_schema, sender=self, dispatch_uid="registry.ensure_schema")

        # Groups (design §4) are created after every migrate, once the
        # permissions for this app's models exist.
        post_migrate.connect(ensure_groups, sender=self, dispatch_uid="registry.ensure_groups")
