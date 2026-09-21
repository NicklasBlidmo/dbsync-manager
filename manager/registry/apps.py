from django.apps import AppConfig
from django.db.models.signals import post_migrate


class RegistryConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "registry"
    verbose_name = "Registry"

    def ready(self):
        from .permissions import ensure_groups

        # Groups (design §4) are created after every migrate, once the
        # permissions for this app's models exist.
        post_migrate.connect(ensure_groups, sender=self, dispatch_uid="registry.ensure_groups")
