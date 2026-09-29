"""
The supervisor loop (design §6).

Slices 2–3: loop steps 1–3 (start, stop, exit with backoff), the
initializing → running transition from the base schema comments, log
rotation, adoption of daemons left running by a previous supervisor, and
step 5, the consistency check with health and alert conditions.
Operations (step 0), the post_init hook (step 4) and post_pull (step 6)
come in later slices.

One writer per state field (§6 State model): this module writes
`process_state` and `process_reason`; the check (check.py) writes
`health`; nothing here writes `desired_state`.
"""

import logging
import os
import random
import signal
import threading
import time
from dataclasses import dataclass
from datetime import timedelta

from django.conf import settings
from django.db import DatabaseError, connection
from django.utils import timezone

import json
import shutil

from registry import events
from registry.crypto import SecretError
from registry.models import Event, Hook, Operation, SyncConnection, SyncProject, SyncRun, SyncStatus

from . import alerts, check, conditions, dbinspect, hooks, housekeeping, logtail, operations, processes, scheduling
from .config_render import ConfigRenderError, render_config, write_config
from .logs import rotate_if_needed, tail

log = logging.getLogger("supervisor")

# Fixed key for pg_advisory_lock: "dbsync-manager supervisor". A second
# supervisor against the same registry blocks here (§6 process ownership).
LOCK_KEY = 0x6462_7379_6E63_7376

BACKOFF_FIRST = 30
BACKOFF_MAX = 600
BACKOFF_JITTER = 5           # seconds of random extra delay, so crashed daemons do not restart in step
SPAWN_STAGGER = 1.1          # seconds between two daemon starts (geodiff seeds its temp names by the second)
STABLE_UPTIME = 600          # a run this long resets the crash counter
CRASH_ALERT_AFTER = 3        # consecutive crashes before condition process_crashed
STOP_GRACE = 60              # seconds between SIGTERM and SIGKILL
SHUTDOWN_GRACE = 10          # seconds to wait for children at supervisor shutdown

PS = SyncProject.ProcessState


def backoff_delay(consecutive_crashes: int) -> int:
    """30 s, 60 s, 120 s ... capped at 10 min (§6 step 3)."""
    if consecutive_crashes < 1:
        return 0
    return min(BACKOFF_FIRST * 2 ** (consecutive_crashes - 1), BACKOFF_MAX)


@dataclass
class Runtime:
    """What the supervisor knows about one project between ticks (in memory)."""

    child: processes.Child | None = None
    stopping_since: float | None = None
    restart_requested: bool = False   # set by the stuck rule; one restart, not a crash
    restarting: bool = False
    crashes: int = 0
    next_start_at: float = 0.0
    last_start_at: float = 0.0
    blocked_reason: str = ""
    block_after_exit: str = ""        # set when a failed post_init hook stops the child
    last_check_at: float = 0.0
    stuck_restart_at: float | None = None
    last_post_pull_version: int | None = None
    tail: logtail.TailState | None = None   # log position and error de-duplication
    # A project hook running in a worker thread (slice 13); the tick reaps it.
    hook_job: hooks.HookJob | None = None
    hook_job_kind: str = ""                 # "post_init", "post_pull" or "manual:<kind>"


class Supervisor:
    def __init__(self, tick_seconds: int):
        self.tick_seconds = tick_seconds
        # Set by SIGTERM/SIGINT to end the loop. Named so it cannot shadow the
        # _stop() method below: an earlier `self._stop = Event()` did, and
        # pause silently called the Event instead of stopping the daemon.
        self._stop_requested = threading.Event()
        self._lock_held = False
        self.runtimes: dict[int, Runtime] = {}
        self.open_operations: dict[int, Operation] = {}
        self.scheduled_jobs: dict[int, hooks.HookJob] = {}        # hook id -> running scheduled hook
        self.operation_hook_jobs: dict[int, hooks.HookJob] = {}   # project id -> running pre_remove hook
        self.io = SupervisorIO(self)
        self._last_prune_day = None
        self._last_spawn_at: float | None = None

    # --- lifecycle -----------------------------------------------------------

    def install_signal_handlers(self):
        for sig in (signal.SIGTERM, signal.SIGINT):
            signal.signal(sig, self._on_signal)

    def _on_signal(self, signum, frame):
        log.info("received signal %s, stopping after the current tick", signum)
        self._stop_requested.set()
        # A hook may be running inside this tick (a long pg_dump); without
        # this the container's grace period expires and Docker kills us.
        hooks.interrupt_running()

    def ensure_lock(self):
        """Hold a session-level advisory lock for the life of this process.

        The lock lives on the Django connection, which a management command
        keeps open. If the connection is lost the lock is gone with it, so
        it is re-taken after a database error.
        """
        if self._lock_held:
            return
        with connection.cursor() as cursor:
            cursor.execute("SELECT pg_try_advisory_lock(%s)", [LOCK_KEY])
            (acquired,) = cursor.fetchone()
            if not acquired:
                log.warning(
                    "another supervisor holds the lock on this registry; "
                    "waiting instead of starting children"
                )
                cursor.execute("SELECT pg_advisory_lock(%s)", [LOCK_KEY])
        self._lock_held = True
        log.info("supervisor lock acquired")

    def run(self):
        self.install_signal_handlers()
        self.ensure_lock()
        self.adopt_orphans()
        hooks.mark_orphaned_runs()
        log.info("supervisor running, tick every %ss", self.tick_seconds)
        while not self._stop_requested.is_set():
            try:
                self.ensure_lock()
                self.tick()
            except DatabaseError:
                log.exception("registry database error; reconnecting on the next tick")
                connection.close()
                self._lock_held = False
            except Exception:  # noqa: BLE001 - the loop must survive a bad tick
                log.exception("tick failed")
            self._stop_requested.wait(self.tick_seconds)
        self.shutdown()

    def shutdown(self):
        """Stop children so they do not outlive the container's grace period."""
        live = [(pid, rt) for pid, rt in self.runtimes.items() if rt.child and rt.child.alive]
        for _, rt in live:
            rt.child.terminate()
        survivors = processes.wait_for_exit([rt.child for _, rt in live], SHUTDOWN_GRACE)
        for child in survivors:
            child.kill()
        # Hooks got SIGTERM in _on_signal and SIGKILL after INTERRUPT_KILL_AFTER;
        # wait for their threads so the runs are recorded as interrupted.
        still = hooks.wait_for_jobs(hooks.INTERRUPT_KILL_AFTER + 5)
        if still:
            log.warning("%d hook job(s) still running at shutdown", still)
        try:
            for project_id, rt in live:
                SyncProject.objects.filter(pk=project_id).update(
                    process_state=PS.STOPPED, process_reason="supervisor shut down"
                )
                processes.remove_pidfile(self._paths(project_id)["pidfile"])
        except DatabaseError:
            log.warning("could not record shutdown in the registry")
        log.info("supervisor stopped (%d children stopped)", len(live))

    # --- paths ---------------------------------------------------------------

    @staticmethod
    def _paths(project_id):
        work_dir = settings.DBSYNC_DATA_DIR / "projects" / str(project_id)
        return {
            "work_dir": work_dir,
            "config": work_dir / processes.CONFIG_NAME,
            "log": work_dir / processes.LOG_NAME,
            "stderr": work_dir / processes.STDERR_NAME,
            "pidfile": work_dir / processes.PIDFILE_NAME,
        }

    # --- startup: re-attach to daemons a previous supervisor left running ----

    def adopt_orphans(self):
        for project in SyncProject.objects.filter(removed_at__isnull=True):
            child = processes.adopt(self._paths(project.pk)["pidfile"])
            if child is None:
                continue
            rt = self.runtimes.setdefault(project.pk, Runtime())
            rt.child = child
            rt.last_start_at = time.monotonic()
            log.info("adopted running db-sync for %s (pid %s)", project, child.pid)
            events.info(project, f"supervisor restarted; adopted running db-sync (pid {child.pid})")

    # --- one tick ------------------------------------------------------------

    def tick(self):
        projects = list(
            SyncProject.objects.filter(removed_at__isnull=True)
            .select_related("mergin_account", "database")
            .prefetch_related("connections")
        )
        seen = {project.pk for project in projects}

        # Projects deleted from the registry while their daemon runs.
        for project_id in list(self.runtimes):
            if project_id not in seen:
                rt = self.runtimes.pop(project_id)
                if rt.child and rt.child.alive:
                    log.warning("project %s vanished from the registry; stopping its daemon", project_id)
                    rt.child.terminate()

        # Step 0: operations (§6). Executed before reconcile so that a
        # stop_child step's SIGTERM is seen as a stop, not a crash.
        self.open_operations = {}
        touched = set()   # projects whose registry rows an operation changed this tick
        for operation in Operation.objects.filter(status__in=Operation.OPEN_STATUSES).select_related("project"):
            self.open_operations[operation.project_id] = operation
            if operation.status == Operation.Status.FAILED:
                continue
            touched.add(operation.project_id)
            try:
                outcome = operations.advance(operation, self.io)
            except DatabaseError:
                raise
            except Exception:  # noqa: BLE001
                log.exception("operation %s failed unexpectedly", operation.pk)
                continue
            if outcome == "done":
                self.open_operations.pop(operation.project_id, None)
                events.info(operation.project, f"{operation.kind} operation {operation.pk} completed")
            elif outcome == "failed":
                events.error(operation.project, f"{operation.kind} operation {operation.pk} failed at "
                                                f"step '{operation.step}': {operation.error}")

        for project in projects:
            if project.pk in touched:
                # The object (and its prefetched connections) was loaded before
                # the operation ran and is stale: after mark_removed it still
                # says desired running with connections, and reconciling it
                # would start a daemon that re-creates the schemas just
                # dropped (field finding 2026-09-24). Next tick re-reads.
                continue
            try:
                self._reconcile(project)
            except DatabaseError:
                raise
            except Exception:  # noqa: BLE001 - one project must not stall the others
                log.exception("reconcile failed for %s", project)

        try:
            self._run_scheduled_hooks()
        except DatabaseError:
            raise
        except Exception:  # noqa: BLE001 - a bad hook row must not stall the projects
            log.exception("scheduled hooks failed")

        running = sum(1 for rt in self.runtimes.values() if rt.child and rt.child.alive)
        log.info("tick: %d projects, %d daemons running", len(projects), running)

        today = timezone.localdate()
        if self._last_prune_day != today:
            self._last_prune_day = today
            housekeeping.prune()

    def _reconcile(self, project):
        rt = self.runtimes.setdefault(project.pk, Runtime())
        now = time.monotonic()

        if rt.child is not None and not rt.child.alive:
            self._handle_exit(project, rt, now)

        if rt.hook_job is not None and rt.hook_job.done:
            self._finish_hook_job(project, rt)
            if rt.block_after_exit:
                return   # the daemon is being stopped; _handle_exit records blocked next tick

        operation = self.open_operations.get(project.pk)
        if operation is not None:
            # The operation owns the project: no start, no hooks, no check.
            # Its stop_child step signals the daemon; the exit above records it.
            if rt.child is None:
                reason = f"operation {operation.pk} ({operation.kind}) {operation.status} at step '{operation.step}'"
                self._set_state(project, PS.BLOCKED, reason)
            return

        self._run_requested_hooks(project, rt)

        wants_running = project.desired_state == SyncProject.DesiredState.RUNNING

        if wants_running:
            if rt.child is None:
                if now < rt.next_start_at:
                    pass  # crashed, waiting out the backoff
                else:
                    self._start(project, rt, now)
            elif rt.restart_requested:
                rt.restart_requested = False
                rt.restarting = True
                rt.child.terminate()
                log.info("restarting db-sync for %s (pid %s)", project, rt.child.pid)
            else:
                self._observe(project, rt)
        else:
            if rt.child is not None:
                self._stop(project, rt, now)
            elif project.process_state != PS.STOPPED:
                self._set_state(project, PS.STOPPED, "")

        self._maybe_check(project, rt, now)

    # --- step 1: start -------------------------------------------------------

    def _stagger(self):
        """At least SPAWN_STAGGER seconds between two daemon starts."""
        now = time.monotonic()
        if self._last_spawn_at is not None:
            wait = SPAWN_STAGGER - (now - self._last_spawn_at)
            if wait > 0:
                time.sleep(wait)
        self._last_spawn_at = time.monotonic()

    @staticmethod
    def _still_wanted(project) -> bool:
        """Re-read the registry right before a start: the in-memory project
        may be stale (an operation in this tick, an edit in the admin)."""
        fresh = SyncProject.objects.filter(pk=project.pk).values("desired_state", "removed_at").first()
        if fresh is None or fresh["removed_at"] is not None:
            log.warning("not starting %s: removed from the registry", project)
            return False
        if fresh["desired_state"] != SyncProject.DesiredState.RUNNING:
            return False
        return True

    def _start(self, project, rt, now):
        if not self._still_wanted(project):
            return
        paths = self._paths(project.pk)
        try:
            text = render_config(
                project,
                project.connections.all(),
                project.mergin_account,
                project.database,
                paths["work_dir"],
            )
        except (ConfigRenderError, SecretError) as exc:
            reason = f"cannot start: {exc}"
            if rt.blocked_reason != reason:
                rt.blocked_reason = reason
                events.error(project, reason)
            self._set_state(project, PS.BLOCKED, reason)
            return
        rt.blocked_reason = ""

        # §6 step 1 / §8: a configured post_init hook that has not succeeded
        # blocks the start once the schemas exist (init already happened).
        if project.post_init_hook_id and project.post_init_ok_at is None:
            state, _ = self._initial_state(project)
            if state == PS.RUNNING:
                self._set_state(project, PS.BLOCKED,
                                "post_init hook has not succeeded; use 'Run hook now' or re-initialise")
                return

        write_config(paths["config"], text)
        command = processes.daemon_command(settings.DBSYNC_DAEMON_DIR, paths["config"], paths["log"])
        # Own temp dir per daemon and a pause between starts: two daemons
        # started in the same second otherwise share geodiff's temp file
        # names in /tmp and crash each other (review note 2026-09-28b).
        tmpdir = processes.private_tmpdir(paths["work_dir"])
        env = dict(os.environ, TMPDIR=str(tmpdir), TMP=str(tmpdir), TEMP=str(tmpdir))
        self._stagger()
        child = processes.spawn(command, paths["work_dir"], paths["stderr"], paths["pidfile"], paths["config"], env=env)
        rt.child = child
        rt.stopping_since = None
        rt.last_start_at = now

        state, reason = self._initial_state(project)
        self._set_state(project, state, reason)
        attempt = f" (restart {rt.crashes})" if rt.crashes else ""
        events.info(project, f"started db-sync (pid {child.pid}){attempt}")
        log.info("started db-sync for %s, pid %s%s", project, child.pid, attempt)

    def _initial_state(self, project):
        """initializing until every base schema carries db-sync's comment."""
        schemas = [conn.base_schema for conn in project.connections.all()]
        try:
            comments = dbinspect.base_schema_comments(project.database, schemas)
        except Exception as exc:  # noqa: BLE001 - reported, not fatal
            return PS.INITIALIZING, f"could not read base schema comments: {exc}"
        missing = [name for name, comment in comments.items() if not comment]
        if missing:
            return PS.INITIALIZING, "waiting for base schema: " + ", ".join(missing)
        return PS.RUNNING, ""

    # --- running: observe ----------------------------------------------------

    def _observe(self, project, rt):
        paths = self._paths(project.pk)
        # Read new log lines before rotating, or copy-and-truncate would
        # discard lines not yet ingested.
        self._pump_log(project, rt, paths)
        max_bytes = settings.DBSYNC_LOG_MAX_MB * 1024 * 1024
        for name in ("log", "stderr"):
            if rotate_if_needed(paths[name], max_bytes, settings.DBSYNC_LOG_KEEP):
                log.info("rotated %s for %s", paths[name].name, project)

        if project.process_state == PS.INITIALIZING:
            if rt.hook_job is not None:
                return   # post_init running in its worker; _finish_hook_job takes over
            state, reason = self._initial_state(project)
            if state == PS.RUNNING:
                events.info(project, "initialised: every base schema has db-sync's comment")
                self._after_init(project, rt)
            elif reason != project.process_reason:
                self._set_state(project, PS.INITIALIZING, reason)
        elif project.process_state not in (PS.RUNNING,):
            # e.g. adopted after a supervisor restart, or state left stale
            state, reason = self._initial_state(project)
            self._set_state(project, state, reason)

    # --- step 4: post_init hook (§6, §8) --------------------------------------

    def _after_init(self, project, rt):
        """Every base schema exists: start post_init if configured, else running.

        The hook runs in a worker thread (slice 13); _finish_hook_job applies
        its result on a later tick, so a long hook never stalls the loop.
        """
        hook = project.post_init_hook
        if hook is None or project.post_init_ok_at is not None:
            self._set_state(project, PS.RUNNING, "")
            return
        if rt.hook_job is not None:
            return   # a hook of this project is running; its result is applied when it ends
        self._set_state(project, PS.INITIALIZING, f"running post_init hook '{hook.name}'")
        rt.hook_job = hooks.start_job(f"{project.pk}:post_init", hooks.run_hook, project, hook, "post_init",
                                      db_version=self._project_db_version(project))
        rt.hook_job_kind = "post_init"

    def _finish_hook_job(self, project, rt):
        """Apply the result of a project hook that ended in its worker thread."""
        job, rt.hook_job = rt.hook_job, None
        kind, rt.hook_job_kind = rt.hook_job_kind, ""
        result = job.result
        if kind == "post_init":
            hook = project.post_init_hook
            name = hook.name if hook is not None else "?"
            if result.ok:
                self._mark_post_init_ok(project)
                if project.process_state == PS.INITIALIZING and rt.child is not None and rt.child.alive:
                    self._set_state(project, PS.RUNNING, "")
                return
            if result.interrupted:
                # Shutdown, not a hook failure: stay initializing; the next
                # supervisor runs the hook again through _observe.
                self._set_state(project, PS.INITIALIZING, "post_init hook interrupted by supervisor shutdown; re-run on restart")
                return
            # A sync without its grants, views and validation is worse than one
            # that waits (§8): stop the child; _handle_exit records blocked.
            reason = (f"post_init hook '{name}' failed ({hooks.failure_reason(result)}); "
                      "fix it and use 'Run hook now', or re-initialise")
            if rt.child is not None and rt.child.alive:
                rt.block_after_exit = reason
                rt.child.terminate()
            else:
                self._set_state(project, PS.BLOCKED, reason)
        elif kind == "manual:post_init":
            if result.ok and project.post_init_ok_at is None:
                self._mark_post_init_ok(project)
                # A blocked project starts on the next tick; nothing else to do.

    @staticmethod
    def _mark_post_init_ok(project):
        now = timezone.now()
        project.post_init_ok_at = now
        SyncProject.objects.filter(pk=project.pk).update(post_init_ok_at=now)

    @staticmethod
    def _project_db_version(project):
        """The lowest db_version across the project's connections, or None."""
        versions = [v for v in SyncStatus.objects.filter(project=project).values_list("db_version", flat=True) if v is not None]
        return min(versions) if versions else None

    def _run_requested_hooks(self, project, rt):
        """'Run hook now' requests from the UI: sync_run rows with status
        requested, started one at a time in a worker thread."""
        if rt.hook_job is not None:
            return
        for run in hooks.pending_requests(project):
            kind = (run.summary or {}).get("hook_kind", "")
            hook = getattr(project, f"{kind}_hook", None) if kind in ("post_init", "pre_remove", "post_pull") else None
            if hook is None:
                run.finished_at = timezone.now()
                run.ok = False
                run.summary = {**(run.summary or {}), "status": "done", "stderr": f"no {kind} hook is configured"}
                run.save()
                continue
            db_version = self._project_db_version(project)
            prev = rt.last_post_pull_version if kind == "post_pull" else None
            rt.hook_job = hooks.start_job(
                f"{project.pk}:manual:{kind}", hooks.run_hook, project, hook, kind, db_version=db_version,
                prev_db_version=prev, requested_by=(run.summary or {}).get("requested_by"), run=run, trigger="manual",
            )
            rt.hook_job_kind = f"manual:{kind}"
            return

    # --- step 6: post_pull hook (§6, §8) --------------------------------------

    def _maybe_post_pull(self, project, rt):
        hook = project.post_pull_hook
        if hook is None:
            return
        db_version = self._project_db_version(project)
        if db_version is None:
            return
        if rt.last_post_pull_version is None:
            last = (
                SyncRun.objects.filter(project=project, kind=SyncRun.Kind.HOOK, ok=True,
                                       summary__hook_kind="post_pull")
                .order_by("-started_at").values_list("summary__db_version", flat=True).first()
            )
            rt.last_post_pull_version = last if last is not None else db_version
            return
        if db_version > rt.last_post_pull_version:
            if rt.hook_job is not None:
                return   # another hook of this project is running; try again at the next check
            prev = rt.last_post_pull_version
            rt.last_post_pull_version = db_version
            rt.hook_job = hooks.start_job(f"{project.pk}:post_pull", hooks.run_hook, project, hook, "post_pull",
                                          db_version=db_version, prev_db_version=prev)
            rt.hook_job_kind = "post_pull"

    # --- scheduled hooks (slice 13, design §8) ----------------------------------

    def _run_scheduled_hooks(self):
        now_local = timezone.localtime()
        for hook in Hook.objects.filter(kind=Hook.Kind.SCHEDULED).select_related("database"):
            self._tick_scheduled_hook(hook, now_local)

    def _tick_scheduled_hook(self, hook, now_local):
        """Reap a finished run, then start one when due or requested; never two at once."""
        job = self.scheduled_jobs.get(hook.pk)
        if job is not None:
            if not job.done:
                return
            self.scheduled_jobs.pop(hook.pk, None)
            self._finish_scheduled(hook, job)
        request = hooks.pending_scheduled_request(hook)
        due = bool(hook.enabled) and scheduling.is_due(hook.run_at, hook.last_started_at, now_local)
        if request is None and not due:
            return
        if hook.last_status == "running":
            log.warning("scheduled hook '%s' is marked running elsewhere; not starting a second instance", hook.name)
            return
        now = timezone.now()
        fields = {"last_status": "running"}
        if due:
            fields["last_started_at"] = now   # a manual run does not count as today's scheduled run
        Hook.objects.filter(pk=hook.pk).update(**fields)
        previous = hook.last_status
        hook.last_status = "running"
        if due:
            hook.last_started_at = now
        requested_by = (request.summary or {}).get("requested_by") if request is not None else None
        job = hooks.start_job(f"scheduled:{hook.pk}", hooks.run_scheduled, hook, run=request, requested_by=requested_by)
        job.previous_status = previous
        self.scheduled_jobs[hook.pk] = job
        log.info("scheduled hook '%s' started (%s)", hook.name,
                 "requested" if request is not None else f"due at {hook.run_at:%H:%M}")

    def _finish_scheduled(self, hook, job):
        result = job.result
        status = "interrupted" if result.interrupted else ("ok" if result.ok else "failed")
        Hook.objects.filter(pk=hook.pk).update(last_finished_at=timezone.now(), last_status=status)
        hook.last_status = status
        if result.interrupted:
            return
        change = scheduling.transition(job.previous_status, result.ok)
        if change:
            alerts.notify_hook(hook, change, result, job.run)

    # --- log history (§6 classification) ---------------------------------------

    def _pump_log(self, project, rt, paths):
        state_path = paths["work_dir"] / logtail.STATE_NAME
        if rt.tail is None:
            rt.tail = logtail.TailState.load(state_path)
        try:
            counts = logtail.pump(project, paths["log"], state_path, rt.tail)
        except DatabaseError:
            raise
        except Exception:  # noqa: BLE001 - history must never break the loop
            log.exception("log ingest failed for %s", project)
            return
        if counts.get("runs") or counts.get("events"):
            log.info("log for %s: %s", project, counts)

    # --- step 2: stop --------------------------------------------------------

    def _stop(self, project, rt, now):
        if rt.stopping_since is None:
            rt.child.terminate()
            rt.stopping_since = now
            self._set_state(project, PS.STOPPING, "")
            log.info("stopping db-sync for %s (pid %s)", project, rt.child.pid)
        elif now - rt.stopping_since > STOP_GRACE:
            log.warning("db-sync for %s ignored SIGTERM for %ss; killing", project, STOP_GRACE)
            rt.child.kill()

    # --- step 3: exit --------------------------------------------------------

    def _handle_exit(self, project, rt, now):
        child, rt.child = rt.child, None
        paths = self._paths(project.pk)
        processes.remove_pidfile(paths["pidfile"])
        code = child.exit_code

        if rt.stopping_since is not None:
            rt.stopping_since = None
            operation = self.open_operations.get(project.pk)
            if operation is not None:
                self._set_state(project, PS.BLOCKED, f"operation {operation.pk} ({operation.kind}) running")
            else:
                self._set_state(project, PS.STOPPED, "")
            events.info(project, f"db-sync stopped (pid {child.pid})")
            log.info("db-sync for %s stopped", project)
            return

        if rt.restarting:
            rt.restarting = False
            self._set_state(project, PS.STOPPED, "restarting")
            events.info(project, f"db-sync stopped for restart (pid {child.pid})")
            return

        if rt.block_after_exit:
            reason, rt.block_after_exit = rt.block_after_exit, ""
            self._set_state(project, PS.BLOCKED, reason)
            events.error(project, f"db-sync stopped: {reason}")
            return

        if now - rt.last_start_at >= STABLE_UPTIME:
            rt.crashes = 0
        rt.crashes += 1
        delay = backoff_delay(rt.crashes)
        # Jitter breaks the lock-step of daemons that crashed together.
        rt.next_start_at = now + delay + random.uniform(0, BACKOFF_JITTER)
        retry_at = (timezone.now() + timedelta(seconds=delay)).strftime("%H:%M:%S")
        code_text = f"exit code {code}" if code is not None else "exit (adopted process, code unknown)"
        last_lines = tail(paths["stderr"], lines=15)
        reason = f"{code_text}; consecutive crashes: {rt.crashes}; restart at {retry_at}"
        if last_lines:
            reason += "\n--- last stderr lines ---\n" + last_lines
        self._set_state(project, PS.CRASHED, reason)

        message = f"db-sync exited unexpectedly ({code_text}); restarting in {delay}s"
        if last_lines:
            message += "\n" + last_lines
        if rt.crashes == CRASH_ALERT_AFTER:
            conditions.activate(project, "process_crashed", message, level=Event.Level.ERROR)
        else:
            events.error(project, message)
        log.warning("db-sync for %s exited (%s); restart in %ss", project, code_text, delay)

    # --- step 5: consistency check and health ---------------------------------

    def _maybe_check(self, project, rt, now):
        if now - rt.last_check_at < settings.DBSYNC_CHECK_INTERVAL:
            return
        if project.process_state == PS.INITIALIZING:
            # Checkouts and schemas are being created; a check now only
            # reports missing files. Retried every tick until initialised.
            return
        rt.last_check_at = now
        if not project.connections.all():
            return

        process_running = (
            rt.child is not None and rt.child.alive
            and project.process_state == PS.RUNNING
            and project.desired_state == SyncProject.DesiredState.RUNNING
        )
        health, wanted, messages = check.run_project_check(project, process_running=process_running)

        # Stuck: one automatic restart first; alert only if still stuck later.
        if "stuck" in wanted:
            if rt.stuck_restart_at is None:
                rt.stuck_restart_at = now
                rt.restart_requested = True
                events.warning(project, "checkout is not advancing while db-sync runs; restarting it once: "
                               + messages.get("stuck", ""))
                wanted.discard("stuck")
            elif now - rt.stuck_restart_at < settings.DBSYNC_STUCK_AFTER:
                wanted.discard("stuck")
        else:
            rt.stuck_restart_at = None

        conditions.apply(project, wanted, messages)

        # A crash loop resolves once the daemon has stayed up for a check interval.
        if process_running and now - rt.last_start_at >= settings.DBSYNC_CHECK_INTERVAL:
            conditions.resolve(project, "process_crashed", "resolved: db-sync has stayed up")

        self._maybe_post_pull(project, rt)
        # Pull runs are confirmed by the database, never by a log line (§6).
        confirmed = logtail.close_confirmed_pulls(project, self._project_db_version(project))
        if confirmed:
            log.info("check: %s confirmed %d pull run(s)", project, confirmed)
        log.info("check: %s health=%s conditions=%s", project, health, sorted(wanted) or "-")

    # --- helpers -------------------------------------------------------------

    @staticmethod
    def _set_state(project, state, reason):
        if project.process_state == state and project.process_reason == reason:
            return
        project.process_state = state
        project.process_reason = reason
        SyncProject.objects.filter(pk=project.pk).update(process_state=state, process_reason=reason)


class SupervisorIO:
    """The side effects of operation steps (§6 "Operations"), one method per step.

    Each method is idempotent: running it twice after a crash is safe.
    """

    def __init__(self, supervisor):
        self.sup = supervisor

    # -- shared steps ------------------------------------------------------------

    def block(self, operation):
        project = operation.project
        self.sup._set_state(project, PS.BLOCKED, f"operation {operation.pk} ({operation.kind}) running")
        return "project blocked; the supervisor will not start its daemon"

    def stop_child(self, operation):
        rt = self.sup.runtimes.setdefault(operation.project_id, Runtime())
        child = rt.child
        if child is None or not child.alive:
            return "no daemon running"
        now = time.monotonic()
        if rt.stopping_since is None:
            child.terminate()
            rt.stopping_since = now
            log.info("operation %s: SIGTERM to db-sync pid %s", operation.pk, child.pid)
            return operations.Wait(f"SIGTERM sent to db-sync pid {child.pid}")
        if now - rt.stopping_since > STOP_GRACE:
            child.kill()
            log.warning("operation %s: db-sync pid %s ignored SIGTERM; SIGKILL", operation.pk, child.pid)
            return operations.Wait(f"SIGKILL sent to pid {child.pid} after {STOP_GRACE}s")
        return operations.Wait(f"waiting for pid {child.pid} to exit")

    def pre_remove_hook(self, operation):
        """Start the hook in a worker thread and wait across ticks (slice 13)."""
        project = operation.project
        hook = project.pre_remove_hook
        if hook is None:
            return "no pre_remove hook configured"
        job = self.sup.operation_hook_jobs.get(project.pk)
        if job is None:
            job = hooks.start_job(f"{project.pk}:pre_remove", hooks.run_hook, project, hook, "pre_remove",
                                  db_version=self.sup._project_db_version(project), trigger="auto")
            self.sup.operation_hook_jobs[project.pk] = job
            return operations.Wait(f"pre_remove hook '{hook.name}' started")
        if not job.done:
            return operations.Wait(f"pre_remove hook '{hook.name}' running")
        self.sup.operation_hook_jobs.pop(project.pk, None)
        run, result = job.run, job.result
        run_id = run.pk if run is not None else "?"
        if result.interrupted:
            raise operations.Interrupted(f"pre_remove hook '{hook.name}' was stopped by supervisor shutdown (run {run_id})")
        if not result.ok:
            raise operations.StepFailed(
                f"pre_remove hook '{hook.name}' failed ({hooks.failure_reason(result)}); nothing has been dropped. "
                f"Fix the hook and retry, or cancel (run {run_id})"
            )
        return f"hook '{hook.name}' ok in {result.duration_s}s (run {run_id})"

    def drop_schemas(self, operation):
        project = operation.project
        if operation.kind == Operation.Kind.REMOVE and not operation.params.get("drop_schemas"):
            return "schemas kept (drop_schemas not requested)"
        names = []
        for conn in project.connections.all():
            names += [conn.base_schema, conn.modified_schema]
        if not names:
            return "no schemas registered"
        dropped = []
        with dbinspect.connect(project.database) as db, db.cursor() as cursor:
            for name in names:
                quoted = '"' + name.replace('"', '""') + '"'
                try:
                    cursor.execute(f"DROP SCHEMA IF EXISTS {quoted} CASCADE")
                except Exception as exc:  # noqa: BLE001
                    raise operations.StepFailed(
                        f"DROP SCHEMA {name} failed: {exc}. Dropped so far: {', '.join(dropped) or 'none'}. "
                        "Resolve it in PostgreSQL (dependent objects owned by another role?) and retry"
                    ) from exc
                dropped.append(name)
        return "dropped " + ", ".join(dropped)

    def delete_workdir(self, operation):
        work_dir = self.sup._paths(operation.project_id)["work_dir"]
        if not work_dir.exists():
            return "work dir already gone"
        shutil.rmtree(work_dir)
        return f"removed {work_dir}"

    # -- remove ---------------------------------------------------------------------

    def mark_removed(self, operation):
        project = operation.project
        now = timezone.now()
        SyncConnection.objects.filter(project=project).delete()
        SyncStatus.objects.filter(project=project).delete()
        SyncProject.objects.filter(pk=project.pk).update(
            desired_state=SyncProject.DesiredState.REMOVED, removed_at=now,
            process_state=PS.STOPPED, process_reason="", health=SyncProject.Health.UNKNOWN,
            active_conditions={},
        )
        project.removed_at = now
        self.sup.runtimes.pop(project.pk, None)
        events.info(project, "sync removed" + (" and schemas dropped" if operation.params.get("drop_schemas") else ""))
        return "project marked removed; connections and status rows deleted"

    # -- reinit ---------------------------------------------------------------------

    def snapshot_pending(self, operation):
        project = operation.project
        pending = {}
        for status in SyncStatus.objects.filter(project=project).select_related("connection"):
            for table, modified in (status.modified_count or {}).items():
                base = (status.base_count or {}).get(table)
                if base is not None and modified != base:
                    pending[f"{status.connection.sync_file}:{table}"] = modified - base
        detail = json.dumps(pending) if pending else "no pending database changes"
        if pending and not operation.params.get("confirm_pending_loss"):
            raise operations.StepFailed(
                f"pending database changes would be lost: {detail}. Confirm the loss and retry, or cancel"
            )
        return detail

    def reset(self, operation):
        project = operation.project
        SyncStatus.objects.filter(project=project).delete()
        init_from = operation.params.get("init_from") or project.init_from
        SyncProject.objects.filter(pk=project.pk).update(
            post_init_ok_at=None, health=SyncProject.Health.UNKNOWN, health_since=None,
            active_conditions={}, init_from=init_from,
            desired_state=SyncProject.DesiredState.RUNNING, process_state=PS.STOPPED, process_reason="",
        )
        self.sup.runtimes[project.pk] = Runtime()
        events.info(project, f"re-initialised: schemas dropped, will init from {init_from} on the next tick")
        return f"registry reset; init_from={init_from}"

