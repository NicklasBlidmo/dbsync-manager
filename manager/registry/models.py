"""
The registry: schema `dbsync_manager` (design §5).

The registry is the source of truth; db-sync's config files are derived
from these rows and never read back (§6). Field-level comments point at
the design section that defines the semantics.
"""

import hashlib
import secrets

from django.conf import settings
from django.contrib.postgres.fields import ArrayField
from django.db import models
from django.db.models import Q
from django.utils import timezone

from . import crypto


class OwnedModel(models.Model):
    """Rows a person creates carry who and when (audit trail, §5)."""

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
        editable=False,
    )
    created_at = models.DateTimeField(default=timezone.now, editable=False)

    class Meta:
        abstract = True


# --- credentials (§4) -------------------------------------------------------


class MerginAccount(OwnedModel):
    """A Mergin Maps service account db-sync connects with. Not a manager user."""

    class Kind(models.TextChoices):
        PASSWORD = "password", "Password"
        TOKEN = "token", "API token"

    name = models.CharField(max_length=100, unique=True)
    url = models.URLField(default="https://app.merginmaps.com")
    username = models.CharField(max_length=200)
    secret_enc = models.TextField(editable=False)
    kind = models.CharField(max_length=10, choices=Kind.choices, default=Kind.PASSWORD)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return f"{self.name} ({self.username}@{self.url})"

    @property
    def secret(self) -> str:
        return crypto.decrypt(self.secret_enc)

    @secret.setter
    def secret(self, value: str):
        self.secret_enc = crypto.encrypt(value)


class Database(OwnedModel):
    """A PostgreSQL connection db-sync runs as. Its role owns the synced schemas.

    §5 lists this as one encrypted DSN; it is stored as components with only
    the password encrypted, so host, database and role are readable in the UI.
    """

    name = models.CharField(max_length=100, unique=True)
    host = models.CharField(
        max_length=255,
        help_text="As seen from inside the manager container: 'localhost' is the container itself; "
                  "use the server's LAN address or Docker service name. With the bundled PostGIS: 'postgis'.",
    )
    port = models.PositiveIntegerField(default=5432)
    dbname = models.CharField(max_length=100)
    user = models.CharField(max_length=100)
    password_enc = models.TextField(editable=False)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return f"{self.name} ({self.user}@{self.host}:{self.port}/{self.dbname})"

    @property
    def password(self) -> str:
        return crypto.decrypt(self.password_enc)

    @password.setter
    def password(self, value: str):
        self.password_enc = crypto.encrypt(value)

    @staticmethod
    def _quote(value) -> str:
        """libpq keyword/value quoting."""
        text = str(value)
        if text and not any(ch in text for ch in " '\\"):
            return text
        return "'" + text.replace("\\", "\\\\").replace("'", "\\'") + "'"

    def conn_info(self, with_password: bool = True) -> str:
        """libpq connection string as db-sync's `conn_info` expects it.

        Only the generated per-project config (§4) and hook pgpass files (§8)
        may ask for the password.
        """
        parts = [
            f"host={self._quote(self.host)}",
            f"port={self.port}",
            f"dbname={self._quote(self.dbname)}",
            f"user={self._quote(self.user)}",
        ]
        if with_password:
            parts.append(f"password={self._quote(self.password)}")
        return " ".join(parts)


# --- hooks (§8) ---------------------------------------------------------------


class Hook(OwnedModel):
    """A shell command the supervisor runs (§8): around a project's life
    (post_init, pre_remove, post_pull) or, since slice 13, daily at a time of
    day for the installation as a whole (scheduled: backup, vacuum, reports).
    """

    class Kind(models.TextChoices):
        POST_INIT = "post_init", "post_init"
        PRE_REMOVE = "pre_remove", "pre_remove"
        POST_PULL = "post_pull", "post_pull"
        SCHEDULED = "scheduled", "scheduled"

    name = models.CharField(max_length=100, unique=True)
    kind = models.CharField(max_length=20, choices=Kind.choices)
    command = models.TextField(help_text="Run with sh -c inside the container; must be idempotent (design §8).")
    timeout_s = models.PositiveIntegerField(default=600)
    # The hook carries the organisation's privileges (views, grants,
    # pg_dump) while the sync role keeps the minimum (§8). Empty = run
    # with the project's own sync database row.
    database = models.ForeignKey(
        Database, null=True, blank=True, on_delete=models.PROTECT, related_name="hooks",
        verbose_name="run as (database connection)",
        help_text="Connection the hook's PG* variables and pgpass use. Project hooks: leave empty to use the "
                  "project's sync connection. Scheduled hooks: required.",
    )
    # Scheduled hooks only (slice 13). They belong to the installation, not
    # to a project, so recipients and credentials are their own. last_* are
    # written by the supervisor.
    enabled = models.BooleanField(default=True, help_text="Scheduled hooks only: a disabled hook is never started by the clock.")
    run_at = models.TimeField(
        null=True, blank=True,
        help_text="Scheduled hooks only: time of day, in the installation's TZ. Once per day, at the first supervisor tick after this time.",
    )
    notify_emails = ArrayField(
        models.EmailField(), default=list, blank=True,
        help_text="Scheduled hooks only: one mail when a run fails, one when the next run succeeds.",
    )
    last_started_at = models.DateTimeField(null=True, blank=True, editable=False)
    last_finished_at = models.DateTimeField(null=True, blank=True, editable=False)
    last_status = models.CharField(max_length=12, blank=True, default="", editable=False)   # ok, failed, running, interrupted

    class Meta:
        ordering = ["kind", "name"]

    def __str__(self):
        return f"{self.name} [{self.kind}]"

    @property
    def is_scheduled(self) -> bool:
        return self.kind == self.Kind.SCHEDULED

    @staticmethod
    def normalise_command(command: str) -> str:
        """Unix line endings. A browser submits a textarea with CRLF, and
        sh then sees `set -e\r` as "set: Illegal option -" on the first
        line (backup hook, 2026-09-30)."""
        return (command or "").replace("\r\n", "\n").replace("\r", "\n")

    def clean(self):
        from django.core.exceptions import ValidationError

        self.command = self.normalise_command(self.command)
        if not self.is_scheduled:
            return
        errors = {}
        if self.database_id is None:
            errors["database"] = "A scheduled hook has no project to borrow credentials from: choose the connection it runs as."
        if self.run_at is None:
            errors["run_at"] = "Time of day to run, in the installation's TZ."
        if errors:
            raise ValidationError(errors)

    def save(self, *args, **kwargs):
        # Also for rows written without a form (shell, fixtures, the API).
        self.command = self.normalise_command(self.command)
        super().save(*args, **kwargs)

    def is_referenced(self) -> bool:
        """True when any project uses this hook; its kind is then locked in the admin."""
        if self.pk is None:
            return False
        from django.db.models import Q

        return SyncProject.objects.filter(
            Q(post_init_hook=self) | Q(pre_remove_hook=self) | Q(post_pull_hook=self)
        ).exists()


# --- projects (§5, §6) --------------------------------------------------------


class SyncProject(OwnedModel):
    """One Mergin Maps project synced into one database; one db-sync daemon."""

    class InitFrom(models.TextChoices):
        GPKG = "gpkg", "GeoPackage (create database schemas)"
        DB = "db", "Database (create GeoPackage in Mergin)"

    class DesiredState(models.TextChoices):
        """Written by people only (§6 State model)."""

        RUNNING = "running", "running"
        PAUSED = "paused", "paused"
        REMOVED = "removed", "removed"

    class ProcessState(models.TextChoices):
        """Written by the supervisor only (§6 State model)."""

        STOPPED = "stopped", "stopped"
        INITIALIZING = "initializing", "initializing"
        RUNNING = "running", "running"
        STOPPING = "stopping", "stopping"
        CRASHED = "crashed", "crashed"
        BLOCKED = "blocked", "blocked"

    class Health(models.TextChoices):
        """Written by the consistency check only (§6 step 5). Order = severity."""

        FAILED = "failed", "failed"          # the daemon cannot run until someone acts (Re-init, DBA)
        DIVERGED = "diverged", "diverged"
        BEHIND = "behind", "behind"
        STUCK = "stuck", "stuck"
        STALE = "stale", "stale"
        UNKNOWN = "unknown", "unknown"
        CATCHING_UP = "catching_up", "catching up"
        OK = "ok", "ok"

    mergin_project = models.CharField(max_length=200, help_text="workspace/name")
    mergin_project_id = models.UUIDField(null=True, blank=True)
    mergin_account = models.ForeignKey(MerginAccount, on_delete=models.PROTECT, related_name="projects")
    database = models.ForeignKey(Database, on_delete=models.PROTECT, related_name="projects")

    schema_prefix = models.CharField(max_length=50)
    init_from = models.CharField(max_length=4, choices=InitFrom.choices, default=InitFrom.GPKG)
    sleep_time = models.PositiveIntegerField(default=60, help_text="db-sync daemon sleep between cycles, seconds")

    desired_state = models.CharField(max_length=10, choices=DesiredState.choices, default=DesiredState.RUNNING)
    process_state = models.CharField(
        max_length=12, choices=ProcessState.choices, default=ProcessState.STOPPED, editable=False
    )
    process_reason = models.TextField(blank=True, editable=False)
    health = models.CharField(max_length=12, choices=Health.choices, default=Health.UNKNOWN, editable=False)
    health_since = models.DateTimeField(null=True, blank=True, editable=False)
    # Active alert conditions, key -> ISO time it became active (§6 alert
    # de-duplication). Written by supervisor.conditions only.
    active_conditions = models.JSONField(default=dict, blank=True, editable=False)

    post_init_hook = models.ForeignKey(
        Hook, null=True, blank=True, on_delete=models.SET_NULL, related_name="+",
        limit_choices_to={"kind": Hook.Kind.POST_INIT},
    )
    pre_remove_hook = models.ForeignKey(
        Hook, null=True, blank=True, on_delete=models.SET_NULL, related_name="+",
        limit_choices_to={"kind": Hook.Kind.PRE_REMOVE},
    )
    post_pull_hook = models.ForeignKey(
        Hook, null=True, blank=True, on_delete=models.SET_NULL, related_name="+",
        limit_choices_to={"kind": Hook.Kind.POST_PULL},
    )
    post_init_ok_at = models.DateTimeField(null=True, blank=True, editable=False)

    notify_emails = ArrayField(models.EmailField(), default=list, blank=True)
    # Slice 17: a rebase conflict (two-way, both sides changed the same
    # value; the database wins and the field's value is overwritten) is an
    # event; this mails it too.
    mail_on_conflict = models.BooleanField(
        default=False,
        help_text="Mail the alert recipients for every rebase conflict (a database edit dropped because the field "
                  "changed the same value). Off: the conflict is an event on the project page only.",
    )
    updated_at = models.DateTimeField(auto_now=True)
    removed_at = models.DateTimeField(null=True, blank=True, editable=False)

    class Meta:
        ordering = ["mergin_project"]

    def __str__(self):
        return self.mergin_project

    @property
    def work_dir(self):
        """Per-project state under /data (§10): checkouts, generated config, log."""
        return settings.DBSYNC_DATA_DIR / "projects" / str(self.pk)

    @property
    def is_removed(self) -> bool:
        return self.removed_at is not None


class SyncConnection(models.Model):
    """One GeoPackage of a project; mirrors one db-sync `connection` entry."""

    project = models.ForeignKey(SyncProject, on_delete=models.CASCADE, related_name="connections")
    # Copied from the project so the uniqueness of target schemas per database
    # can be a plain constraint (§5).
    database = models.ForeignKey(Database, on_delete=models.PROTECT, related_name="connections", editable=False)
    sync_file = models.CharField(max_length=255, help_text="Path of the GeoPackage inside the Mergin project")
    modified_schema = models.CharField(max_length=63)
    base_schema = models.CharField(max_length=63)
    skip_tables = ArrayField(models.CharField(max_length=63), default=list, blank=True)
    include_tables = ArrayField(models.CharField(max_length=63), default=list, blank=True)

    class Meta:
        ordering = ["project", "sync_file"]
        constraints = [
            models.UniqueConstraint(fields=["database", "modified_schema"], name="uniq_modified_schema_per_db"),
            models.UniqueConstraint(fields=["database", "base_schema"], name="uniq_base_schema_per_db"),
            models.UniqueConstraint(fields=["project", "sync_file"], name="uniq_sync_file_per_project"),
            models.CheckConstraint(
                condition=~Q(modified_schema=models.F("base_schema")),
                name="modified_and_base_differ",
            ),
        ]

    def __str__(self):
        return f"{self.project} / {self.sync_file}"

    def save(self, *args, **kwargs):
        if self.database_id is None:
            self.database_id = self.project.database_id
        super().save(*args, **kwargs)

    @property
    def checkout_dir(self):
        """db-sync 2.4.0 keeps one checkout per connection (upstream #187):
        <work_dir>/work/<project name>-<modified schema>. supervisor.layout
        is the one place that knows the layout (slice 16)."""
        from supervisor import layout   # registry must not import supervisor at module level

        return layout.checkout_dir(self.project.work_dir, self.project.mergin_project, self.modified_schema)


# --- observations (§5, §6) --------------------------------------------------


class SyncRun(models.Model):
    class Kind(models.TextChoices):
        INIT = "init", "init"
        PULL = "pull", "pull"
        PUSH = "push", "push"
        HOOK = "hook", "hook"
        CHECK = "check", "check"

    # Null for a scheduled hook's run, which belongs to the installation (slice 13).
    project = models.ForeignKey(SyncProject, null=True, blank=True, on_delete=models.CASCADE, related_name="runs")
    hook = models.ForeignKey(Hook, null=True, blank=True, editable=False, on_delete=models.SET_NULL, related_name="runs")
    kind = models.CharField(max_length=5, choices=Kind.choices)
    started_at = models.DateTimeField(default=timezone.now)
    finished_at = models.DateTimeField(null=True, blank=True)
    # null = not yet known. Pull rows become true only when the check sees
    # db_version reach checkout_version, never from a log line (§6).
    ok = models.BooleanField(null=True, blank=True)
    checkout_version = models.PositiveIntegerField(null=True, blank=True)
    summary = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ["-started_at"]
        indexes = [models.Index(fields=["project", "-started_at"])]

    def __str__(self):
        owner = self.project if self.project_id else (self.hook.name if self.hook_id else "installation")
        return f"{owner} {self.kind} @ {self.started_at:%Y-%m-%d %H:%M}"


class SyncStatus(models.Model):
    """Result of the latest consistency check, one row per connection (§6 step 5)."""

    class Permission(models.TextChoices):
        READER = "reader", "reader"
        WRITER = "writer", "writer"
        OWNER = "owner", "owner"
        NONE = "none", "none"

    class Direction(models.TextChoices):
        ONE_WAY = "one_way", "one-way (field → DB)"
        TWO_WAY = "two_way", "two-way"
        DB_EDITS_CANNOT_PUSH = "db_edits_cannot_push", "database edits cannot be pushed"

    project = models.ForeignKey(SyncProject, on_delete=models.CASCADE, related_name="statuses")
    connection = models.OneToOneField(SyncConnection, on_delete=models.CASCADE, related_name="status")

    server_version = models.PositiveIntegerField(null=True, blank=True)
    local_version = models.PositiveIntegerField(null=True, blank=True)
    db_version = models.PositiveIntegerField(null=True, blank=True)

    gpkg_count = models.JSONField(default=dict, blank=True)
    base_count = models.JSONField(default=dict, blank=True)
    modified_count = models.JSONField(default=dict, blank=True)
    mismatch_streak = models.JSONField(default=dict, blank=True)

    mergin_permission = models.CharField(max_length=6, choices=Permission.choices, null=True, blank=True)
    db_writers = models.JSONField(default=list, blank=True)
    direction = models.CharField(max_length=20, choices=Direction.choices, null=True, blank=True)

    last_pull_at = models.DateTimeField(null=True, blank=True)
    last_error_at = models.DateTimeField(null=True, blank=True)
    last_error = models.TextField(blank=True)

    local_version_changed_at = models.DateTimeField(null=True, blank=True)
    checked_at = models.DateTimeField(null=True, blank=True)
    check_error = models.TextField(blank=True)
    last_ok_check_at = models.DateTimeField(null=True, blank=True)
    # Slice 15: content comparison base vs modified per table (counts and a
    # capped sample, supervisor/content.py) and the GeoPackage's layer list.
    content_diff = models.JSONField(default=dict, blank=True)
    content_checked_at = models.DateTimeField(null=True, blank=True)
    tables = models.JSONField(default=list, blank=True)

    class Meta:
        verbose_name_plural = "sync statuses"

    def __str__(self):
        return f"status of {self.connection}"


class Event(models.Model):
    class Level(models.TextChoices):
        INFO = "info", "info"
        WARNING = "warning", "warning"
        ERROR = "error", "error"

    class Source(models.TextChoices):
        SUPERVISOR = "supervisor", "supervisor"
        DBSYNC = "dbsync", "dbsync"
        HOOK = "hook", "hook"
        USER = "user", "user"

    project = models.ForeignKey(SyncProject, null=True, blank=True, on_delete=models.CASCADE, related_name="events")
    at = models.DateTimeField(default=timezone.now)
    level = models.CharField(max_length=7, choices=Level.choices)
    source = models.CharField(max_length=10, choices=Source.choices)
    message = models.TextField()
    # Alert de-duplication key, e.g. "divergence:<table>", "db_behind" (§6).
    condition = models.CharField(max_length=120, blank=True)
    user = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")

    class Meta:
        ordering = ["-at"]
        indexes = [models.Index(fields=["project", "-at"])]

    def __str__(self):
        return f"[{self.level}] {self.message[:60]}"


# --- API tokens (slice 15) -----------------------------------------------------


class ApiToken(models.Model):
    """A bearer token for the JSON API. The secret is shown once at creation
    and stored as a SHA-256 hash; the token acts as its user, so the groups
    decide what it may read and do."""

    name = models.CharField(max_length=100, unique=True,
                            help_text="The tool that holds the token, e.g. 'akdb' or 'MerginQA'; shown in the event log.")
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="api_tokens",
                             help_text="The token acts as this user: its groups decide what it may read and do.")
    token_hash = models.CharField(max_length=64, unique=True, editable=False)
    prefix = models.CharField(max_length=8, editable=False, help_text="The first characters of the token, to tell tokens apart.")
    created_at = models.DateTimeField(default=timezone.now, editable=False)
    last_used_at = models.DateTimeField(null=True, blank=True, editable=False)
    revoked = models.BooleanField(default=False, help_text="A revoked token is refused; create a new one to rotate.")

    class Meta:
        ordering = ["name"]
        verbose_name = "API token"

    def __str__(self):
        return f"{self.name} ({self.prefix}..., {self.user})"

    @staticmethod
    def generate() -> str:
        return secrets.token_urlsafe(32)

    @staticmethod
    def hash(raw: str) -> str:
        return hashlib.sha256(raw.encode()).hexdigest()

    @classmethod
    def authenticate(cls, raw: str):
        """The live token for a secret, or None."""
        return cls.objects.select_related("user").filter(token_hash=cls.hash(raw), revoked=False, user__is_active=True).first()


# --- installation (slice 14b) ------------------------------------------------


class InstallationSettings(models.Model):
    """One row for the installation (review note 2026-10-20d, finding 2):
    the default alert recipients. A project or scheduled hook whose own
    notify_emails is empty uses them, so one list covers every project
    that was added without recipients. Deliberately minimal: one page,
    one field (the earlier InstallationSettings went in slice 10)."""

    notify_emails = ArrayField(
        models.EmailField(), default=list, blank=True, verbose_name="default alert recipients",
        help_text="Comma-separated. Used by every project and scheduled hook whose own list is empty.",
    )
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "installation settings"
        verbose_name_plural = "Settings (installation)"

    def __str__(self):
        return "installation settings"

    @classmethod
    def default_recipients(cls) -> list:
        from django.db import DatabaseError

        try:
            row = cls.objects.order_by("pk").values_list("notify_emails", flat=True).first()
        except DatabaseError:
            return []
        return [a for a in (row or []) if a]


# --- operations (§6 Operations) ----------------------------------------------


class Operation(models.Model):
    """A restart-safe multi-step action on a project, executed by the supervisor only."""

    class Kind(models.TextChoices):
        REMOVE = "remove", "remove"
        REINIT = "reinit", "re-initialise"
        VERIFY = "verify", "verify"          # checkout GeoPackage vs base, read-only (slice 15)
        MIGRATE = "migrate", "migrate checkouts"   # one-off: db-sync 2.4.0 checkout layout (slice 16)

    class Status(models.TextChoices):
        PENDING = "pending", "pending"
        RUNNING = "running", "running"
        FAILED = "failed", "failed"
        DONE = "done", "done"
        CANCELLED = "cancelled", "cancelled"

    OPEN_STATUSES = (Status.PENDING, Status.RUNNING, Status.FAILED)

    project = models.ForeignKey(SyncProject, on_delete=models.CASCADE, related_name="operations")
    kind = models.CharField(max_length=10, choices=Kind.choices)
    params = models.JSONField(default=dict, blank=True)
    status = models.CharField(max_length=9, choices=Status.choices, default=Status.PENDING)
    step = models.CharField(max_length=40, blank=True)
    steps = models.JSONField(default=list, blank=True)
    error = models.TextField(blank=True)
    requested_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    requested_at = models.DateTimeField(default=timezone.now)
    finished_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-requested_at"]
        constraints = [
            # One open operation per project (§6): the second request fails.
            models.UniqueConstraint(
                fields=["project"],
                condition=Q(status__in=("pending", "running", "failed")),
                name="one_open_operation_per_project",
            ),
        ]

    def __str__(self):
        return f"{self.kind} {self.project} ({self.status})"

    @property
    def is_open(self) -> bool:
        return self.status in self.OPEN_STATUSES
