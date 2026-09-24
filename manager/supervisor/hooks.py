"""
Hook execution (design §8).

A hook is a shell command run by the supervisor with a defined
environment, inside the container, in the project's work dir. The
contract, in short:

- database access through PG* variables and a per-run pgpass file (mode
  600, deleted afterwards); the password is never in the environment;
- every run is a `sync_run` row created before the command starts and
  finished after, so an interrupted run is visible;
- stdout/stderr are capped at 64 KB per stream (head and tail kept) and
  masked: the project's stored secrets are replaced by *** before storage;
- a timeout kills the whole process group;
- hooks must be idempotent; the manager may run one more than once.

Who reacts to the result (post_init blocks `running`, pre_remove stops
the operation, post_pull is only logged) is the caller's business.
"""

import logging
import os
import signal
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from django.conf import settings
from django.utils import timezone

from registry import events
from registry.models import SyncRun

log = logging.getLogger("supervisor.hooks")

OUTPUT_CAP = 64 * 1024
PGPASS_NAME = "hook.pgpass"
MASK = "***"


@dataclass
class HookResult:
    ok: bool
    exit_code: int | None
    timed_out: bool
    stdout: str
    stderr: str
    duration_s: float
    interrupted: bool = False   # killed because the supervisor is shutting down


# Process groups of hooks running right now, so a SIGTERM to the
# supervisor can end them instead of waiting for Docker's SIGKILL.
_running: set = set()


def interrupt_running():
    for pid in list(_running):
        try:
            os.killpg(pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            pass
    _interrupted.update(_running)


_interrupted: set = set()


# --- pure helpers ----------------------------------------------------------------


def cap(text: str, limit: int = OUTPUT_CAP) -> str:
    """Keep the first and last part of an over-long text with a marker between."""
    if len(text) <= limit:
        return text
    head = limit // 2
    tail = limit - head
    omitted = len(text) - head - tail
    return text[:head] + f"\n[... {omitted} characters omitted ...]\n" + text[-tail:]


def mask(text: str, secrets) -> str:
    """Replace every non-empty secret by ***. Exact matches only (§8)."""
    for secret in secrets:
        if secret:
            text = text.replace(secret, MASK)
    return text


def run_as(project, hook):
    """The database row whose credentials the hook runs with (§8)."""
    return hook.database or project.database


def hook_environment(project, connections, event_kind, *, work_dir, pgpass_path,
                     db_version=None, prev_db_version=None, database=None) -> dict:
    """The documented environment (§8), on top of the supervisor's own.

    `database` is the connection the hook runs as; PG* point at it. The
    sync role's name is passed separately so grants can target it.
    """
    database = database or project.database
    env = dict(os.environ)
    env.update({
        "DBSYNC_PROJECT": project.mergin_project,
        "DBSYNC_PROJECT_ID": str(project.pk),
        "DBSYNC_SCHEMA_PREFIX": project.schema_prefix,
        "DBSYNC_MODIFIED_SCHEMAS": ",".join(c.modified_schema for c in connections),
        "DBSYNC_BASE_SCHEMAS": ",".join(c.base_schema for c in connections),
        "DBSYNC_WORK_DIR": str(work_dir),
        "DBSYNC_EVENT": event_kind,
        "DBSYNC_DB_VERSION": "" if db_version is None else str(db_version),
        "DBSYNC_SYNC_ROLE": project.database.user,
        "PGHOST": database.host,
        "PGPORT": str(database.port),
        "PGDATABASE": database.dbname,
        "PGUSER": database.user,
        "PGPASSFILE": str(pgpass_path),
    })
    if event_kind == "post_pull":
        env["DBSYNC_PREV_DB_VERSION"] = "" if prev_db_version is None else str(prev_db_version)
    env.pop("PGPASSWORD", None)
    return env


def pgpass_line(database) -> str:
    def esc(value):
        return str(value).replace("\\", "\\\\").replace(":", "\\:")

    return f"{esc(database.host)}:{database.port}:{esc(database.dbname)}:{esc(database.user)}:{esc(database.password)}\n"


# --- execution ---------------------------------------------------------------------


def execute(command: str, *, cwd, env, timeout_s: int) -> HookResult:
    """Run `command` with sh -c in its own process group; kill the group on timeout."""
    Path(cwd).mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    proc = subprocess.Popen(
        ["/bin/sh", "-c", command],
        cwd=str(cwd),
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    timed_out = False
    _running.add(proc.pid)
    try:
        try:
            out, err = proc.communicate(timeout=timeout_s)
        except subprocess.TimeoutExpired:
            timed_out = True
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                proc.kill()
            out, err = proc.communicate()
    finally:
        _running.discard(proc.pid)
        interrupted = proc.pid in _interrupted
        _interrupted.discard(proc.pid)
    duration = time.monotonic() - started
    code = None if timed_out else proc.returncode
    stderr = err.decode("utf-8", errors="replace")
    if interrupted:
        stderr += "\n[interrupted: supervisor shutting down]"
    return HookResult(
        ok=(not timed_out and not interrupted and code == 0),
        exit_code=code,
        timed_out=timed_out,
        stdout=out.decode("utf-8", errors="replace"),
        stderr=stderr,
        duration_s=round(duration, 1),
        interrupted=interrupted,
    )


def run_hook(project, hook, event_kind, *, connections=None, db_version=None, prev_db_version=None,
             requested_by=None, run=None, trigger="auto") -> tuple:
    """Run `hook` for `project`; return (SyncRun, HookResult). Never raises for a failing hook.

    `trigger` records which path ran it: "auto" (after init, post_pull, an
    operation) or "manual" ("Run hook now"). The caller decides what a
    failure means; a manual run never blocks a project (§8).
    """
    connections = list(connections if connections is not None else project.connections.all())
    work_dir = settings.DBSYNC_DATA_DIR / "projects" / str(project.pk)
    pgpass_path = work_dir / PGPASS_NAME
    database = run_as(project, hook)

    summary = {
        "hook": hook.name,
        "hook_kind": hook.kind,
        "event": event_kind,
        "trigger": trigger,
        "run_as": database.user,
        "status": "running",
        "db_version": db_version,
        "requested_by": requested_by,
    }
    if run is None:
        run = SyncRun.objects.create(project=project, kind=SyncRun.Kind.HOOK, summary=summary)
    else:
        run.summary = {**(run.summary or {}), **summary}
        run.started_at = timezone.now()
        run.finished_at = None
        run.save()

    secrets = []
    for row in {project.database.pk: project.database, database.pk: database}.values():
        try:
            secrets.append(row.password)
        except Exception:  # noqa: BLE001 - masking is best effort if the key is gone
            pass
    try:
        secrets.append(project.mergin_account.secret)
    except Exception:  # noqa: BLE001
        pass

    work_dir.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(pgpass_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(pgpass_line(database))
        env = hook_environment(
            project, connections, event_kind, work_dir=work_dir, pgpass_path=pgpass_path,
            db_version=db_version, prev_db_version=prev_db_version, database=database,
        )
        result = execute(hook.command, cwd=work_dir, env=env, timeout_s=hook.timeout_s)
    except Exception as exc:  # noqa: BLE001 - e.g. /bin/sh missing; recorded, not raised
        result = HookResult(ok=False, exit_code=None, timed_out=False, stdout="", stderr=f"could not run hook: {exc}", duration_s=0.0)
    finally:
        try:
            pgpass_path.unlink()
        except FileNotFoundError:
            pass

    result.stdout = cap(mask(result.stdout, secrets))
    result.stderr = cap(mask(result.stderr, secrets))

    run.finished_at = timezone.now()
    run.ok = result.ok
    run.summary.update({
        "status": "done",
        "exit_code": result.exit_code,
        "timed_out": result.timed_out,
        "interrupted": result.interrupted,
        "duration_s": result.duration_s,
        "stdout": result.stdout,
        "stderr": result.stderr,
    })
    run.save()

    label = f"{hook.kind} hook '{hook.name}'"
    if result.ok:
        events.info(project, f"{label} finished ok in {result.duration_s}s", source="hook")
        log.info("%s for %s ok (%.1fs)", label, project, result.duration_s)
    else:
        why = "interrupted by supervisor shutdown" if result.interrupted else (
            "timed out" if result.timed_out else f"exit code {result.exit_code}")
        tail_lines = "\n".join(result.stderr.strip().splitlines()[-10:])
        message = f"{label} failed ({why}) after {result.duration_s}s"
        if tail_lines:
            message += "\n" + tail_lines
        events.error(project, message, source="hook")
        log.warning("%s for %s failed (%s)", label, project, why)
    return run, result


# --- requests from the UI ("run hook now") ---------------------------------------


def request_run(project, hook, user):
    """Create the row the supervisor picks up on its next tick."""
    return SyncRun.objects.create(
        project=project, kind=SyncRun.Kind.HOOK,
        summary={"hook": hook.name, "hook_kind": hook.kind, "event": hook.kind, "status": "requested",
                 "trigger": "manual", "requested_by": user.get_username() if user else None},
    )


def pending_requests(project):
    return list(
        SyncRun.objects.filter(project=project, kind=SyncRun.Kind.HOOK, finished_at__isnull=True,
                               summary__status="requested").order_by("started_at")
    )
