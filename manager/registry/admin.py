"""
Django admin as the slice-1 UI (design §14).

Secrets are entered through write-only password inputs and never rendered
back (§4). Fields the supervisor or the check owns are read-only here so
the single-writer rule of §6 holds even in the admin.
"""

from django import forms
from django.contrib import admin, messages
from django.shortcuts import redirect
from django.urls import reverse

from supervisor import dbinspect

from .models import (
    ApiToken,
    Database,
    Event,
    Hook,
    InstallationSettings,
    MerginAccount,
    Operation,
    SyncConnection,
    SyncProject,
    SyncRun,
    SyncStatus,
)


class OwnedAdmin(admin.ModelAdmin):
    def save_model(self, request, obj, form, change):
        if obj.created_by_id is None:
            obj.created_by = request.user
        super().save_model(request, obj, form, change)


class _SecretFormMixin:
    """Adds a write-only input that sets the model's encrypted property."""

    secret_field = None  # name of the form field
    secret_property = None  # name of the model property to set

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        field = self.fields[self.secret_field]
        if self.instance.pk:
            field.required = False
            field.help_text = "Leave blank to keep the current value."
        else:
            field.required = True

    def save(self, commit=True):
        obj = super().save(commit=False)
        value = self.cleaned_data.get(self.secret_field)
        if value:
            setattr(obj, self.secret_property, value)
        if commit:
            obj.save()
        return obj


class MerginAccountForm(_SecretFormMixin, forms.ModelForm):
    secret_field = "secret"
    secret_property = "secret"
    secret = forms.CharField(
        label="Password / token",
        widget=forms.PasswordInput(render_value=False),
        strip=False,
    )

    class Meta:
        model = MerginAccount
        fields = ["name", "url", "username", "kind", "secret"]


@admin.register(MerginAccount)
class MerginAccountAdmin(OwnedAdmin):
    form = MerginAccountForm
    list_display = ["name", "username", "url", "kind", "created_by", "created_at"]
    search_fields = ["name", "username", "url"]
    readonly_fields = ["created_by", "created_at"]


class DatabaseForm(_SecretFormMixin, forms.ModelForm):
    secret_field = "password"
    secret_property = "password"
    password = forms.CharField(widget=forms.PasswordInput(render_value=False), strip=False)

    class Meta:
        model = Database
        fields = ["name", "host", "port", "dbname", "user", "password"]


@admin.register(Database)
class DatabaseAdmin(OwnedAdmin):
    form = DatabaseForm
    list_display = ["name", "host", "port", "dbname", "user", "created_by", "created_at"]
    search_fields = ["name", "host", "dbname", "user"]
    readonly_fields = ["created_by", "created_at"]
    actions = ["test_connection"]

    @admin.action(description="Test connection")
    def test_connection(self, request, queryset):
        """Connect as the row's role and report server, PostGIS and schema count,
        or the error, so a wrong user name or address shows before the wizard."""
        for database in queryset:
            try:
                facts = dbinspect.server_facts(database)
            except Exception as exc:  # noqa: BLE001 - the message is the point
                self.message_user(request, f"{database}: {exc}", messages.ERROR)
                continue
            level = messages.SUCCESS if facts["postgis_version"] else messages.WARNING
            self.message_user(request, f"{database}: {dbinspect.describe(facts)}", level)


@admin.register(Hook)
class HookAdmin(OwnedAdmin):
    list_display = ["name", "kind", "database", "timeout_s", "run_at", "enabled", "last_status", "created_by", "created_at"]
    list_filter = ["kind", "database", "enabled"]
    search_fields = ["name", "command"]
    readonly_fields = ["created_by", "created_at", "last_started_at", "last_finished_at", "last_status"]
    fieldsets = [
        (None, {"fields": ["name", "kind", "command", "timeout_s", "database", "created_by", "created_at"]}),
        ("Scheduled hooks only", {
            # Shown while kind = scheduled (ui/admin-hook.js); Hook.clean() requires run as and run_at then.
            "classes": ["scheduled-only"],
            "description": "Runs once a day at run_at (installation TZ) as the connection above, which is then "
                           "required; one mail to notify_emails when a run fails and one when the next run "
                           "succeeds. A nightly backup is the first use: docs/backup.md.",
            "fields": ["enabled", "run_at", "notify_emails", "last_started_at", "last_finished_at", "last_status"],
        }),
    ]

    class Media:
        js = ["ui/admin-hook.js"]

    def get_readonly_fields(self, request, obj=None):
        fields = list(super().get_readonly_fields(request, obj))
        if obj is not None and obj.is_referenced():
            # A project pointing at a hook of the wrong kind would fail silently.
            fields.append("kind")
        return fields


@admin.register(ApiToken)
class ApiTokenAdmin(admin.ModelAdmin):
    """The secret is generated here and shown once, in the success message."""

    list_display = ["name", "user", "prefix", "created_at", "last_used_at", "revoked"]
    list_filter = ["revoked", "user"]
    fields = ["name", "user", "revoked", "prefix", "created_at", "last_used_at"]
    readonly_fields = ["prefix", "created_at", "last_used_at"]

    def save_model(self, request, obj, form, change):
        if not change:
            raw = ApiToken.generate()
            obj.token_hash = ApiToken.hash(raw)
            obj.prefix = raw[:8]
            messages.success(request, f"Token for '{obj.name}', shown once, copy it now: {raw}")
        super().save_model(request, obj, form, change)


@admin.register(InstallationSettings)
class InstallationSettingsAdmin(admin.ModelAdmin):
    """A singleton: the list opens the one row (or the add form), no delete."""

    fields = ["notify_emails", "updated_at"]
    readonly_fields = ["updated_at"]

    def has_add_permission(self, request):
        return super().has_add_permission(request) and not InstallationSettings.objects.exists()

    def has_delete_permission(self, request, obj=None):
        return False

    def changelist_view(self, request, extra_context=None):
        row = InstallationSettings.objects.order_by("pk").first()
        if row is None:
            return redirect(reverse("admin:registry_installationsettings_add"))
        return redirect(reverse("admin:registry_installationsettings_change", args=[row.pk]))


class SyncConnectionInline(admin.TabularInline):
    model = SyncConnection
    extra = 1
    fields = ["sync_file", "modified_schema", "base_schema", "skip_tables", "include_tables"]


@admin.register(SyncProject)
class SyncProjectAdmin(OwnedAdmin):
    list_display = [
        "mergin_project", "database", "desired_state", "process_state", "health",
        "schema_prefix", "updated_at",
    ]
    list_filter = ["desired_state", "process_state", "health", "database", "mergin_account"]
    search_fields = ["mergin_project", "schema_prefix"]
    inlines = [SyncConnectionInline]
    readonly_fields = [
        "process_state", "process_reason", "health", "health_since",
        "post_init_ok_at", "removed_at", "created_by", "created_at", "updated_at",
    ]
    fieldsets = [
        (None, {"fields": ["mergin_project", "mergin_project_id", "mergin_account", "database"]}),
        ("Sync", {"fields": ["schema_prefix", "init_from", "sleep_time", "desired_state"]}),
        ("Hooks and alerts", {"fields": ["post_init_hook", "pre_remove_hook", "post_pull_hook", "notify_emails"]}),
        ("Observed (read-only)", {"fields": [
            "process_state", "process_reason", "health", "health_since", "post_init_ok_at", "removed_at",
        ]}),
        ("Audit", {"fields": ["created_by", "created_at", "updated_at"]}),
    ]

    def get_queryset(self, request):
        return super().get_queryset(request).select_related("database", "mergin_account")


class ReadOnlyAdmin(admin.ModelAdmin):
    """Rows written by the machine: visible, never edited by hand."""

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False


@admin.register(SyncStatus)
class SyncStatusAdmin(ReadOnlyAdmin):
    list_display = [
        "connection", "server_version", "local_version", "db_version",
        "direction", "mergin_permission", "checked_at", "check_error_short",
    ]
    list_filter = ["direction", "mergin_permission", "project"]

    @admin.display(description="check error")
    def check_error_short(self, obj):
        return (obj.check_error or "")[:60]


@admin.register(SyncRun)
class SyncRunAdmin(ReadOnlyAdmin):
    list_display = ["project", "kind", "started_at", "finished_at", "ok", "checkout_version"]
    list_filter = ["kind", "ok", "project"]
    date_hierarchy = "started_at"


@admin.register(Event)
class EventAdmin(ReadOnlyAdmin):
    list_display = ["at", "project", "level", "source", "condition", "message_short", "user"]
    list_filter = ["level", "source", "project"]
    search_fields = ["message", "condition"]
    date_hierarchy = "at"

    @admin.display(description="message")
    def message_short(self, obj):
        return obj.message[:100]


@admin.register(Operation)
class OperationAdmin(admin.ModelAdmin):
    list_display = ["project", "kind", "status", "step", "requested_by", "requested_at", "finished_at"]
    list_filter = ["kind", "status", "project"]
    readonly_fields = ["status", "step", "steps", "error", "requested_by", "requested_at", "finished_at"]
    fields = ["project", "kind", "params", "status", "step", "steps", "error", "requested_by", "requested_at", "finished_at"]

    def save_model(self, request, obj, form, change):
        if obj.requested_by_id is None:
            obj.requested_by = request.user
        super().save_model(request, obj, form, change)

    def has_change_permission(self, request, obj=None):
        # Operations are driven by the supervisor; the admin only creates them.
        return False if obj is not None else super().has_change_permission(request, obj)
