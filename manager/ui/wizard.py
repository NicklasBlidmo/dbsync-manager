"""
The add wizard's data: parsing, validation and the target checks (design §9,
§15 "Adopting an existing sync"). Views render; this module decides.

`decide()` is the pure rule for one connection's target schemas:

    init_from  modified  base  base comment            → state
    gpkg       no        no    –                       new
    db         yes       no    –                       new (from database)
    any        yes       yes   names this project      adopt
    any        yes       yes   names another project   reject
    any        yes       yes   missing / unparsable    reject (failed init?)
    gpkg       yes       no    –                       reject
    any        no        yes   –                       reject
    db         no        any   –                       reject (needs modified)

Adoption relies on db-sync's own init: with both schemas present and the
comment naming the project it downloads db_version and continues (§15).
What nobody can check here is that the old daemon has been stopped; the
preview says so and requires the acknowledgement.
"""

from dataclasses import dataclass, field

from django.core.exceptions import ValidationError
from django.core.validators import validate_email

from registry.models import Database, Hook, MerginAccount, SyncConnection, SyncProject
from supervisor import dbinspect
from supervisor.check import db_writers, direction_of

from . import naming


@dataclass
class Row:
    sync_file: str
    modified: str
    base: str


@dataclass
class WizardData:
    account: MerginAccount | None = None
    database: Database | None = None
    project: str = ""
    project_id: str = ""
    prefix: str = ""
    init_from: str = "gpkg"
    sleep_time: int = 60
    hooks: dict = field(default_factory=dict)      # kind -> Hook
    notify_emails: list = field(default_factory=list)
    rows: list = field(default_factory=list)
    ack: bool = False

    def as_hidden_fields(self):
        """(name, value) pairs that carry the data through the preview form.

        `pick` is what decides the file set (one source of truth, also on
        Create); `file`/`modified`/`base` carry the names per file."""
        pairs = [
            ("account", self.account.pk if self.account else ""),
            ("database", self.database.pk if self.database else ""),
            ("project", self.project),
            ("project_id", self.project_id),
            ("prefix", self.prefix),
            ("init_from", self.init_from),
            ("sleep_time", self.sleep_time),
            ("notify_emails", ", ".join(self.notify_emails)),
        ]
        for kind in ("post_init", "pre_remove", "post_pull"):
            hook = self.hooks.get(kind)
            pairs.append((f"{kind}_hook", hook.pk if hook else ""))
        for row in self.rows:
            pairs += [("pick", row.sync_file), ("file", row.sync_file), ("modified", row.modified), ("base", row.base)]
        return pairs


def names_by_file(params):
    """{file: (modified, base)} from the parallel file/modified/base lists, or None if they disagree."""
    files = params.getlist("file")
    modified = params.getlist("modified")
    base = params.getlist("base")
    if not (len(files) == len(modified) == len(base)):
        return None
    return {f: (m.strip(), b.strip()) for f, m, b in zip(files, modified, base) if f}


def parse(post) -> tuple[WizardData, list]:
    """Turn the wizard's POST into WizardData plus a list of error strings."""
    data = WizardData()
    errors = []

    def lookup(model, key, label):
        raw = post.get(key, "")
        if not raw:
            errors.append(f"choose a {label}")
            return None
        try:
            return model.objects.get(pk=int(raw))
        except (ValueError, model.DoesNotExist):
            errors.append(f"{label} not found")
            return None

    data.account = lookup(MerginAccount, "account", "Mergin account")
    data.database = lookup(Database, "database", "database")

    data.project = post.get("project", "").strip()
    if "/" not in data.project:
        errors.append("project must be given as workspace/name")
    data.project_id = post.get("project_id", "").strip()

    data.prefix = post.get("prefix", "").strip()
    if not naming.is_valid_identifier(data.prefix):
        errors.append("prefix must be a PostgreSQL identifier: lower-case letters, digits and _, at most 63 characters")

    data.init_from = post.get("init_from", "gpkg")
    if data.init_from not in ("gpkg", "db"):
        errors.append("init direction must be gpkg or db")

    try:
        data.sleep_time = int(post.get("sleep_time", "60"))
        if data.sleep_time < 1:
            raise ValueError
    except ValueError:
        errors.append("sleep time must be a whole number of seconds, at least 1")

    for kind in ("post_init", "pre_remove", "post_pull"):
        raw = post.get(f"{kind}_hook", "")
        if not raw:
            continue
        try:
            hook = Hook.objects.get(pk=int(raw))
        except (ValueError, Hook.DoesNotExist):
            errors.append(f"{kind} hook not found")
            continue
        if hook.kind != kind:
            errors.append(f"hook '{hook.name}' is a {hook.kind} hook, not {kind}")
            continue
        data.hooks[kind] = hook

    raw_emails = post.get("notify_emails", "").replace("\n", ",").replace(";", ",")
    for address in [a.strip() for a in raw_emails.split(",") if a.strip()]:
        try:
            validate_email(address)
        except ValidationError:
            errors.append(f"not an e-mail address: {address}")
            continue
        data.notify_emails.append(address)

    # The ticked checkboxes are the file set; an empty list is an empty list.
    # The names table's rows only supply names, keyed by file; a picked file
    # without a row gets derived names, a row without a pick is dropped.
    picked = []
    for sync_file in post.getlist("pick"):
        if sync_file and sync_file not in picked:
            picked.append(sync_file)
    names = names_by_file(post)
    if names is None:
        errors.append("schema name rows do not match the files (reload the page)")
        names = {}
    if not picked:
        errors.append("Select at least one GeoPackage")
    seen = {}
    for sync_file in picked:
        mod, bas = names.get(sync_file) or naming.derive_schema_names(data.prefix or "project", sync_file)
        for label, name in (("modified", mod), ("base", bas)):
            if not naming.is_valid_identifier(name):
                errors.append(f"{sync_file}: {label} schema '{name}' is not a valid identifier")
            elif name in seen:
                errors.append(f"schema '{name}' is used twice ({seen[name]} and {sync_file})")
            seen[name] = sync_file
        if mod == bas:
            errors.append(f"{sync_file}: modified and base schema must differ")
        if naming.is_conflict_copy(sync_file):
            errors.append(f"{sync_file} is an edit-conflict copy and will not sync")
        data.rows.append(Row(sync_file=sync_file, modified=mod, base=bas))

    data.ack = post.get("ack") == "1"
    return data, errors


# --- target checks -------------------------------------------------------------


@dataclass
class TargetCheck:
    sync_file: str
    modified: str
    base: str
    state: str            # new | adopt | reject
    reason: str = ""
    writers: list | None = None   # roles that can write to an existing modified schema


def decide(init_from, modified_exists, base_exists, base_comment, project_path) -> tuple[str, str]:
    comment_name = (base_comment or {}).get("name") if isinstance(base_comment, dict) else None
    if modified_exists and base_exists:
        if comment_name == project_path:
            version = (base_comment or {}).get("version", "?")
            return "adopt", f"schemas exist with db-sync's comment for this project at {version}; db-sync will take them over"
        if comment_name:
            return "reject", f"schemas exist and belong to Mergin project '{comment_name}'"
        return "reject", "schemas exist but the base schema has no db-sync comment (a failed init?); remove them or choose other names"
    if init_from == "db":
        if modified_exists and not base_exists:
            return "new", "initialise from the existing modified schema; base will be created"
        if not modified_exists:
            return "reject", "init from database needs the modified schema to exist"
        return "reject", "base schema exists without the modified schema"
    if not modified_exists and not base_exists:
        return "new", "schemas will be created from the GeoPackage"
    if modified_exists:
        return "reject", "modified schema exists without a base schema (a failed init?); remove it or choose other names"
    return "reject", "base schema exists without a modified schema (a failed init?); remove it or choose other names"


def registry_conflicts(database, project_path, rows) -> list:
    """Errors from the registry itself, before looking at the database."""
    errors = []
    if SyncProject.objects.filter(mergin_project=project_path, database=database, removed_at__isnull=True).exists():
        errors.append(f"{project_path} is already synced into database '{database.name}'")
    names = {}
    for row in rows:
        names[row.modified] = row.sync_file
        names[row.base] = row.sync_file
    taken = SyncConnection.objects.filter(database=database).select_related("project")
    for conn in taken:
        for name in (conn.modified_schema, conn.base_schema):
            if name in names:
                errors.append(f"schema '{name}' is already the target of {conn.project} ({conn.sync_file})")
    return errors


def inspect_targets(data: WizardData) -> tuple[list, str]:
    """Look at the target database: one TargetCheck per row, plus an error string if it failed."""
    names = []
    for row in data.rows:
        names += [row.modified, row.base]
    checks = []
    try:
        with dbinspect.connect(data.database) as db, db.cursor() as cursor:
            if not dbinspect.postgis_installed(cursor):
                # Refused here, before anything is created; the daemon would
                # otherwise crash with "Cannot find or activate postgis
                # extension" after the project exists (clean-room, 2026-09-28).
                return [], (f"PostGIS is not installed in database '{data.database.dbname}' on "
                            f"{data.database.host}: run CREATE EXTENSION postgis there as a superuser first")
            cursor.execute(
                "SELECT nspname, obj_description(oid, 'pg_namespace') FROM pg_namespace WHERE nspname = ANY(%s)",
                [names],
            )
            existing = {name: comment for name, comment in cursor.fetchall()}
            for row in data.rows:
                modified_exists = row.modified in existing
                base_exists = row.base in existing
                comment = dbinspect.parse_comment(existing.get(row.base)) if base_exists else None
                state, reason = decide(data.init_from, modified_exists, base_exists, comment, data.project)
                writers = None
                if modified_exists:
                    try:
                        writers = db_writers(cursor, row.modified, data.database.user)
                    except Exception:  # noqa: BLE001 - best effort, as in the check
                        db.rollback()
                        writers = None
                checks.append(TargetCheck(row.sync_file, row.modified, row.base, state, reason, writers))
    except Exception as exc:  # noqa: BLE001
        return [], f"cannot inspect database '{data.database.name}': {exc}"
    return checks, ""


@dataclass
class Preview:
    permission: str | None
    version: int | None
    checks: list
    errors: list
    direction: str | None
    needs_ack: bool
    ack_text: str

    @property
    def ok(self):
        return not self.errors


def preview(data: WizardData, permission, version) -> Preview:
    errors = []
    if permission is None:
        errors.append(f"account '{data.account.name}' has no access to {data.project} (needs at least Reader)")
    errors += registry_conflicts(data.database, data.project, data.rows)
    checks, db_error = inspect_targets(data)
    if db_error:
        errors.append(db_error)
    for check in checks:
        if check.state == "reject":
            errors.append(f"{check.sync_file}: {check.reason}")

    writers_known = all(c.writers is not None for c in checks if c.state != "new")
    writers = sorted({w for c in checks if c.writers for w in c.writers})
    direction = direction_of(permission, writers if writers_known else None)

    reasons = []
    if direction == "db_edits_cannot_push":
        reasons.append(
            f"the account is Reader while {', '.join(writers)} can write to the target tables: "
            "anything written to the database can never be pushed to Mergin"
        )
    if any(c.state == "adopt" for c in checks):
        reasons.append("existing schemas will be taken over: the daemon that used to sync them must be stopped first")
    return Preview(
        permission=permission, version=version, checks=checks, errors=errors,
        direction=direction, needs_ack=bool(reasons), ack_text="; ".join(reasons),
    )


def create(data: WizardData, user) -> SyncProject:
    from django.db import transaction

    from registry import events

    with transaction.atomic():
        project = SyncProject.objects.create(
            mergin_project=data.project,
            mergin_project_id=data.project_id or None,
            mergin_account=data.account,
            database=data.database,
            schema_prefix=data.prefix,
            init_from=data.init_from,
            sleep_time=data.sleep_time,
            desired_state=SyncProject.DesiredState.RUNNING,
            post_init_hook=data.hooks.get("post_init"),
            pre_remove_hook=data.hooks.get("pre_remove"),
            post_pull_hook=data.hooks.get("post_pull"),
            notify_emails=data.notify_emails,
            created_by=user,
        )
        for row in data.rows:
            SyncConnection.objects.create(
                project=project, sync_file=row.sync_file, modified_schema=row.modified, base_schema=row.base,
            )
        files = ", ".join(row.sync_file for row in data.rows)
        events.record(
            project, "info", f"{user.get_username()} added the sync ({len(data.rows)} GeoPackages: {files})",
            source="user", user=user,
        )
    return project
