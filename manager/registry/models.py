"""
The registry: schema `dbsync_manager` (design §5).

The registry is the source of truth; db-sync's config files are derived
from these rows and never read back (§6). Field-level comments point at
the design section that defines the semantics.
"""

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
    host = models.CharField(max_length=255)
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
    class Kind(models.TextChoices):
        POST_INIT = "post_init", "post_init"
        PRE_REMOVE = "pre_remove", "pre_remove"
        POST_PULL = "post_pull", "post_pull"

    name = models.CharField(max_length=100, unique=True)
    kind = models.CharField(max_length=20, choices=Kind.choices)
    command = models.TextField(help_text="Run with sh -c inside the container; must be idempotent (design §8).")
    timeout_s = models.PositiveIntegerField(default=600)

    class Meta:
        ordering = ["kind", "name"]

    def __str__(self):
        return f"{self.name} [{self.kind}]"


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
    sleep_time = models.PositiveIntegerField(default=10, help_text="db-sync daemon sleep between cycles, seconds")

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
        """With the #163 patch each connection has its own checkout under
        db-sync's working_dir: <work_dir>/work/<project name>/<modified schema>
        (supervisor.config_render.WORKING_SUBDIR)."""
        project_name = self.project.mergin_project.split("/", 1)[-1]
        return self.project.work_dir / "work" / project_name / self.modified_schema


# --- observations (§5, §6) --------------------------------------------------


class SyncRun(models.Model):
    class Kind(models.TextChoices):
        INIT = "init", "init"
        PULL = "pull", "pull"
        PUSH = "push", "push"
        HOOK = "hook", "hook"
        CHECK = "check", "check"

    project = models.ForeignKey(SyncProject, on_delete=models.CASCADE, related_name="runs")
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
        return f"{self.project} {self.kind} @ {self.started_at:%Y-%m-%d %H:%M}"


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


# --- operations (§6 Operations) ----------------------------------------------


class Operation(models.Model):
    """A restart-safe multi-step action on a project, executed by the supervisor only."""

    class Kind(models.TextChoices):
        REMOVE = "remove", "remove"
        REINIT = "reinit", "re-initialise"

    class Status(models.TextChoices):
        PENDING = "pending", "pending"
        RUNNING = "running", "running"
        FAILED = "failed", "failed"
        DONE = "done", "done"
        CANCELLED = "cancelled", "cancelled"

    OPEN_STATUSES = (Status.PENDING, Status.RUNNING, Status.FAILED)

    project = models.ForeignKey(SyncProject, on_delete=models.CASCADE, related_name="operations")
    kind = models.CharField(max_length=6, choices=Kind.choices)
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
