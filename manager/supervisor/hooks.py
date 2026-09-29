"""
Hook execution (design §8).

A hook is a shell command run by the supervisor with a defined
environment, inside the container. The contract, in short:

- database access through PG* variables and a per-run pgpass file (mode
  600, deleted afterwards); the password is never in the environment;
- every run is a `sync_run` row created before the command starts and
  finished after, so an interrupted run is visible;
- stdout/stderr are capped at 64 KB per stream (head and tail kept) and
  masked: the stored secrets are replaced by *** before storage;
- a timeout kills the whole process group;
- hooks must be idempotent; the manager may run one more than once.

Since slice 13 every hook runs in a worker thread (`start_job`): the tick
starts it and reaps it on a later tick, so a long pg_dump no longer stalls
the loop. A hook still running when the supervisor stops gets SIGTERM,
SIGKILL after INTERRUPT_KILL_AFTER seconds, and its run is marked
`interrupted`.

Two kinds of hook share this machinery: project hooks (post_init,
pre_remove, post_pull; `run_hook`) with the project's variables, and
scheduled hooks (`run_scheduled`) that belong to the installation, run
daily at a time of day as their own run-as database, and get no project
variables.

Who reacts to the result (post_init blocks `running`, pre_remove stops
the operation, post_pull is only logged, a scheduled hook mails on a
transition) is the caller's business.
"""

import logging
import os
import signal
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from django.conf import settings
from django.db import connections
from django.utils import timezone

from registry import events
from registry.models import Hook, SyncRun

log = logging.getLogger("supervisor.hooks")

OUTPUT_CAP = 64 * 1024
PGPASS_NAME = "hook.pgpass"
MASK = "***"
POLL_SECONDS = 1.0            # how often a running hook is checked for interrupt/timeout
INTERRUPT_KILL_AFTER = 10     # seconds after SIGTERM (supervisor shutdown) before SIGKILL


@dataclass
class HookResult:
    ok: bool
    exit_code: int | None
    timed_out: bool
    stdout: str
    stderr: str
    duration_s: float
    interrupted: bool = False   # killed because the supervisor is shutting down


# Process groups of hooks running right now (pid -> start), so a SIGTERM to
# the supervisor can end them instead of waiting for Docker's SIGKILL.
_running: dict = {}
# pid -> monotonic time of the SIGTERM; `execute` escalates to SIGKILL.
_interrupted: dict = {}


def interrupt_running():
    now = time.monotonic()
    for pid in list(_running):
        _killpg(pid, signal.SIGTERM)
        _interrupted.setdefault(pid, now)


def _killpg(pid, sig):
    try:
        os.killpg(pid, sig)
    except (ProcessLookupError, PermissionError, AttributeError):
        try:
            os.kill(pid, sig)
        except (ProcessLookupError, PermissionError):
            pass


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


def hook_environment(project, connections_, event_kind, *, work_dir, pgpass_path,
                     db_version=None, prev_db_version=None, database=None) -> dict:
    """The documented environment (§8), on top of the supervisor's own.

    `database` is the connection the hook runs as; PG* point at it. The
    sync role's name is passed separately so grants can target it.
    """
    database = database or project.database
    env = dict(os.environ)
    tmpdir = str(Path(work_dir) / "tmp")
    env.update({
        # Same reason as for the daemon: a private temp dir per project.
        "TMPDIR": tmpdir, "TMP": tmpdir, "TEMP": tmpdir,
        "DBSYNC_PROJECT": project.mergin_project,
        "DBSYNC_PROJECT_ID": str(project.pk),
        "DBSYNC_SCHEMA_PREFIX": project.schema_prefix,
        "DBSYNC_MODIFIED_SCHEMAS": ",".join(c.modified_schema for c in connections_),
        "DBSYNC_BASE_SCHEMAS": ",".join(c.base_schema for c in connections_),
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


def scheduled_environment(hook, database, *, work_dir, pgpass_path, run_id) -> dict:
    """The environment of a scheduled hook: PG* of its run-as database and
    the hook's own variables; no project variables (slice 13 brief, A4)."""
    env = dict(os.environ)
    tmpdir = str(Path(work_dir) / "tmp")
    env.update({
        "TMPDIR": tmpdir, "TMP": tmpdir, "TEMP": tmpdir,
        "DBSYNC_EVENT": "scheduled",
        "DBSYNC_HOOK_NAME": hook.name,
        "DBSYNC_HOOK_ID": str(hook.pk),
        "DBSYNC_RUN_ID": str(run_id),
        "DBSYNC_DATA_DIR": str(settings.DBSYNC_DATA_DIR),
        "DBSYNC_WORK_DIR": str(work_dir),
        "PGHOST": database.host,
        "PGPORT": str(database.port),
        "PGDATABASE": database.dbname,
        "PGUSER": database.user,
        "PGPASSFILE": str(pgpass_path),
    })
    env.pop("PGPASSWORD", None)
    return env


def pgpass_line(database) -> str:
    def esc(value):
        return str(value).replace("\\", "\\\\").replace(":", "\\:")

    return f"{esc(database.host)}:{database.port}:{esc(database.dbname)}:{esc(database.user)}:{esc(database.password)}\n"


# --- execution ---------------------------------------------------------------------


def execute(command: str, *, cwd, env, timeout_s: int) -> HookResult:
    """Run `command` with sh -c in its own process group; kill the group on
    timeout, and INTERRUPT_KILL_AFTER seconds after a shutdown SIGTERM."""
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
    killed_after_interrupt = False
    _running[proc.pid] = started
    try:
        while True:
            remaining = timeout_s - (time.monotonic() - started)
            if remaining <= 0:
                timed_out = True
                _killpg(proc.pid, signal.SIGKILL)
                out, err = proc.communicate()
                break
            try:
                out, err = proc.communicate(timeout=min(remaining, POLL_SECONDS))
                break
            except subprocess.TimeoutExpired:
                since = _interrupted.get(proc.pid)
                if since is not None and not killed_after_interrupt and time.monotonic() - since >= INTERRUPT_KILL_AFTER:
                    killed_after_interrupt = True
                    _killpg(proc.pid, signal.SIGKILL)
    finally:
        _running.pop(proc.pid, None)
        interrupted = _interrupted.pop(proc.pid, None) is not None
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


# --- worker threads (slice 13, closes the slice 6 "hooks block the tick" item) ------


class HookJob:
    """A hook running in a worker thread. The tick starts it, checks `done`
    on later ticks and reads `run` and `result`."""

    def __init__(self, label: str):
        self.label = label
        self.run = None
        self.result: HookResult | None = None
        self.error = None
        self.started = time.monotonic()
        self.previous_status = ""     # scheduled hooks: last_status before this run
        self._thread: threading.Thread | None = None

    @property
    def done(self) -> bool:
        return self._thread is not None and not self._thread.is_alive()

    @property
    def seconds(self) -> int:
        return int(time.monotonic() - self.started)

    def join(self, timeout=None):
        if self._thread is not None:
            self._thread.join(timeout)


_jobs: set = set()


def start_job(label: str, fn, *args, **kwargs) -> HookJob:
    """Run `fn(*args, **kwargs)` (which returns (run, result)) in a daemon
    thread. The thread's registry connection is closed when it ends."""
    job = HookJob(label)

    def target():
        try:
            job.run, job.result = fn(*args, **kwargs)
        except Exception as exc:  # noqa: BLE001 - recorded, the loop must not see it
            job.error = exc
            log.exception("hook job %s failed", label)
            job.result = HookResult(ok=False, exit_code=None, timed_out=False, stdout="",
                                    stderr=f"hook job failed: {exc}", duration_s=0.0)
        finally:
            try:
                connections.close_all()
            except Exception:  # noqa: BLE001
                pass
            _jobs.discard(job)

    job._thread = threading.Thread(target=target, name=f"hook:{label}", daemon=True)
    _jobs.add(job)
    job._thread.start()
    return job


def wait_for_jobs(timeout: float) -> int:
    """Join running hook jobs for up to `timeout` seconds; returns how many are still running."""
    deadline = time.monotonic() + timeout
    for job in list(_jobs):
        job.join(max(0.0, deadline - time.monotonic()))
    return sum(1 for job in list(_jobs) if not job.done)


# --- recording a run ------------------------------------------------------------------


def _record(run, result: HookResult, secrets) -> None:
    result.stdout = cap(mask(result.stdout, secrets))
    result.stderr = cap(mask(result.stderr, secrets))
    run.finished_at = timezone.now()
    run.ok = result.ok
    run.summary.update({
        "status": "interrupted" if result.interrupted else "done",
        "exit_code": result.exit_code,
        "timed_out": result.timed_out,
        "interrupted": result.interrupted,
        "duration_s": result.duration_s,
        "stdout": result.stdout,
        "stderr": result.stderr,
    })
    run.save()


def _log_outcome(project, label: str, result: HookResult) -> None:
    who = project if project is not None else "installation"
    if result.ok:
        events.info(project, f"{label} finished ok in {result.duration_s}s", source="hook")
        log.info("%s for %s ok (%.1fs)", label, who, result.duration_s)
        return
    why = failure_reason(result)
    tail_lines = "\n".join(result.stderr.strip().splitlines()[-10:])
    message = f"{label} failed ({why}) after {result.duration_s}s"
    if tail_lines:
        message += "\n" + tail_lines
    events.error(project, message, source="hook")
    log.warning("%s for %s failed (%s)", label, who, why)


def failure_reason(result: HookResult) -> str:
    if result.interrupted:
        return "interrupted by supervisor shutdown"
    if result.timed_out:
        return "timed out"
    return f"exit code {result.exit_code}"


def _write_pgpass(path: Path, database) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(pgpass_line(database))


# --- project hooks ----------------------------------------------------------------------


def run_hook(project, hook, event_kind, *, connections=None, db_version=None, prev_db_version=None,
             requested_by=None, run=None, trigger="auto") -> tuple:
    """Run `hook` for `project`; return (SyncRun, HookResult). Never raises for a failing hook.

    `trigger` records which path ran it: "auto" (after init, post_pull, an
    operation) or "manual" ("Run hook now"). The caller decides what a
    failure means; a manual run never blocks a project (§8).
    """
    connections_ = list(connections if connections is not None else project.connections.all())
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
        run = SyncRun.objects.create(project=project, hook=hook, kind=SyncRun.Kind.HOOK, summary=summary)
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
    (work_dir / "tmp").mkdir(exist_ok=True)
    try:
        _write_pgpass(pgpass_path, database)
        env = hook_environment(
            project, connections_, event_kind, work_dir=work_dir, pgpass_path=pgpass_path,
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

    _record(run, result, secrets)
    _log_outcome(project, f"{hook.kind} hook '{hook.name}'", result)
    return run, result


# --- requests from the UI ("run hook now") ---------------------------------------


def request_run(project, hook, user):
    """Create the row the supervisor picks up on its next tick."""
    return SyncRun.objects.create(
        project=project, hook=hook, kind=SyncRun.Kind.HOOK,
        summary={"hook": hook.name, "hook_kind": hook.kind, "event": hook.kind, "status": "requested",
                 "trigger": "manual", "requested_by": user.get_username() if user else None},
    )


def pending_requests(project):
    return list(
        SyncRun.objects.filter(project=project, kind=SyncRun.Kind.HOOK, finished_at__isnull=True,
                               summary__status="requested").order_by("started_at")
    )


# --- scheduled hooks (slice 13) ---------------------------------------------------------


def scheduled_work_dir(hook) -> Path:
    """Per-hook directory under /data: pgpass and a private TMPDIR (2026-09-28b)."""
    return settings.DBSYNC_DATA_DIR / "hooks" / str(hook.pk)


def run_scheduled(hook, *, run=None, requested_by=None) -> tuple:
    """Run a scheduled hook as its run-as database; return (SyncRun, HookResult)."""
    database = hook.database
    work_dir = scheduled_work_dir(hook)
    pgpass_path = work_dir / PGPASS_NAME
    summary = {
        "hook": hook.name,
        "hook_kind": Hook.Kind.SCHEDULED,
        "event": "scheduled",
        "trigger": "manual" if requested_by else "schedule",
        "run_as": database.user if database is not None else "",
        "status": "running",
        "requested_by": requested_by,
    }
    if run is None:
        run = SyncRun.objects.create(project=None, hook=hook, kind=SyncRun.Kind.HOOK, summary=summary)
    else:
        run.summary = {**(run.summary or {}), **summary}
        run.started_at = timezone.now()
        run.finished_at = None
        run.save()

    secrets = []
    if database is None:
        result = HookResult(ok=False, exit_code=None, timed_out=False, stdout="",
                            stderr="scheduled hook has no run-as database; set one in Settings -> Hooks", duration_s=0.0)
    else:
        try:
            secrets.append(database.password)
        except Exception:  # noqa: BLE001
            pass
        work_dir.mkdir(parents=True, exist_ok=True)
        (work_dir / "tmp").mkdir(exist_ok=True)
        try:
            _write_pgpass(pgpass_path, database)
            env = scheduled_environment(hook, database, work_dir=work_dir, pgpass_path=pgpass_path, run_id=run.pk)
            result = execute(hook.command, cwd=work_dir, env=env, timeout_s=hook.timeout_s)
        except Exception as exc:  # noqa: BLE001
            result = HookResult(ok=False, exit_code=None, timed_out=False, stdout="", stderr=f"could not run hook: {exc}", duration_s=0.0)
        finally:
            try:
                pgpass_path.unlink()
            except FileNotFoundError:
                pass

    _record(run, result, secrets)
    _log_outcome(None, f"scheduled hook '{hook.name}'", result)
    return run, result


def request_scheduled_run(hook, user):
    """'Run now' on a scheduled hook: the supervisor starts it on its next tick."""
    return SyncRun.objects.create(
        project=None, hook=hook, kind=SyncRun.Kind.HOOK,
        summary={"hook": hook.name, "hook_kind": Hook.Kind.SCHEDULED, "event": "scheduled", "status": "requested",
                 "trigger": "manual", "requested_by": user.get_username() if user else None},
    )


def pending_scheduled_request(hook):
    return (
        SyncRun.objects.filter(hook=hook, project__isnull=True, kind=SyncRun.Kind.HOOK, finished_at__isnull=True,
                               summary__status="requested").order_by("started_at").first()
    )


def mark_orphaned_runs() -> int:
    """At supervisor start: runs left 'running' by a previous supervisor are
    over, whatever happened to their process; say so instead of showing
    them as running forever."""
    now = timezone.now()
    count = 0
    for run in SyncRun.objects.filter(kind=SyncRun.Kind.HOOK, finished_at__isnull=True, summary__status="running"):
        run.finished_at = now
        run.ok = False
        run.summary = {**(run.summary or {}), "status": "interrupted", "interrupted": True,
                       "stderr": ((run.summary or {}).get("stderr") or "") + "\n[interrupted: supervisor restarted]"}
        run.save()
        count += 1
    Hook.objects.filter(kind=Hook.Kind.SCHEDULED, last_status="running").update(last_status="interrupted")
    if count:
        log.warning("%d hook run(s) left running by a previous supervisor marked interrupted", count)
    return count
