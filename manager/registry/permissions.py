"""
Manager user roles as Django groups (design §4).

viewer    read everything, no actions
operator  add, pause, resume, re-init, remove syncs; no credentials, no users
admin     everything, including Mergin accounts, databases, hooks and users

Users still need `is_staff` to open the admin site in slice 1.
"""

import logging

from django.contrib.auth.models import Group, Permission

log = logging.getLogger(__name__)

REGISTRY_MODELS = (
    "merginaccount",
    "database",
    "hook",
    "syncproject",
    "syncconnection",
    "syncrun",
    "syncstatus",
    "event",
    "operation",
)

OPERATOR_WRITE = {
    "syncproject": ("add", "change"),
    "syncconnection": ("add", "change"),
    "operation": ("add",),
}


def _perms(app_label, model, actions):
    codenames = [f"{action}_{model}" for action in actions]
    return list(Permission.objects.filter(content_type__app_label=app_label, codename__in=codenames))


def ensure_groups(sender=None, **kwargs):
    viewer, _ = Group.objects.get_or_create(name="viewer")
    operator, _ = Group.objects.get_or_create(name="operator")
    admin, _ = Group.objects.get_or_create(name="admin")

    view_all = []
    for model in REGISTRY_MODELS:
        view_all += _perms("registry", model, ("view",))
    viewer.permissions.set(view_all)

    operator_perms = list(view_all)
    for model, actions in OPERATOR_WRITE.items():
        operator_perms += _perms("registry", model, actions)
    operator.permissions.set(operator_perms)

    admin_perms = list(Permission.objects.filter(content_type__app_label="registry"))
    admin_perms += _perms("auth", "user", ("add", "change", "delete", "view"))
    admin_perms += _perms("auth", "group", ("view",))
    admin.permissions.set(admin_perms)

    log.info(
        "groups ensured: viewer=%d operator=%d admin=%d permissions",
        len(view_all), len(operator_perms), len(admin_perms),
    )
