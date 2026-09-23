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
import signal
import threading
import time
from dataclasses import dataclass
from datetime import timedelta

from django.conf import settings
from django.db import DatabaseError, connection
from django.utils import timezone

from registry import events
from registry.crypto import SecretError
from registry.models import Event, SyncProject, SyncRun, SyncStatus

from . import check, conditions, dbinspect, hooks, processes
from .config_render import ConfigRenderError, render_config, write_config
from .logs import rotate_if_needed, tail

log = logging.getLogger("supervisor")

# Fixed key for pg_advisory_lock: "dbsync-manager supervisor". A second
# supervisor against the same registry blocks here (§6 process ownership).
LOCK_KEY = 0x6462_7379_6E63_7376

BACKOFF_FIRST = 30
BACKOFF_MAX = 600
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


class Supervisor:
    def __init__(self, tick_seconds: int):
        self.tick_seconds = tick_seconds
        # Set by SIGTERM/SIGINT to end the loop. Named so it cannot shadow the
        # _stop() method below: an earlier `self._stop = Event()` did, and
        # pause silently called the Event instead of stopping the daemon.
        self._stop_requested = threading.Event()
        self._lock_held = False
        self.runtimes: dict[int, Runtime] = {}

    # --- lifecycle -----------------------------------------------------------

    def install_signal_handlers(self):
        for sig in (signal.SIGTERM, signal.SIGINT):
            signal.signal(sig, self._on_signal)

    def _on_signal(self, signum, frame):
        log.info("received signal %s, stopping after the current tick", signum)
        self._stop_requested.set()

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

        for project in projects:
            try:
                self._reconcile(project)
            except DatabaseError:
                raise
            except Exception:  # noqa: BLE001 - one project must not stall the others
                log.exception("reconcile failed for %s", project)

        running = sum(1 for rt in self.runtimes.values() if rt.child and rt.child.alive)
        log.info("tick: %d projects, %d daemons running", len(projects), running)

    def _reconcile(self, project):
        rt = self.runtimes.setdefault(project.pk, Runtime())
        now = time.monotonic()

        if rt.child is not None and not rt.child.alive:
            self._handle_exit(project, rt, now)

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

    def _start(self, project, rt, now):
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
        child = processes.spawn(command, paths["work_dir"], paths["stderr"], paths["pidfile"], paths["config"])
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
        max_bytes = settings.DBSYNC_LOG_MAX_MB * 1024 * 1024
        for name in ("log", "stderr"):
            if rotate_if_needed(paths[name], max_bytes, settings.DBSYNC_LOG_KEEP):
                log.info("rotated %s for %s", paths[name].name, project)

        if project.process_state == PS.INITIALIZING:
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
        """Every base schema exists: run post_init if configured, then running."""
        hook = project.post_init_hook
        if hook is None or project.post_init_ok_at is not None:
            self._set_state(project, PS.RUNNING, "")
            return
        self._set_state(project, PS.INITIALIZING, f"running post_init hook '{hook.name}'")
        _, result = hooks.run_hook(project, hook, "post_init", db_version=self._project_db_version(project))
        if result.ok:
            self._mark_post_init_ok(project)
            self._set_state(project, PS.RUNNING, "")
            return
        # A sync without its grants, views and validation is worse than one
        # that waits (§8): stop the child; _handle_exit records blocked.
        why = "timed out" if result.timed_out else f"exit code {result.exit_code}"
        rt.block_after_exit = (
            f"post_init hook '{hook.name}' failed ({why}); fix it and use 'Run hook now', or re-initialise"
        )
        if rt.child is not None:
            rt.child.terminate()

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
        """'Run hook now' requests from the UI: sync_run rows with status requested."""
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
            _, result = hooks.run_hook(
                project, hook, kind, db_version=db_version, prev_db_version=prev,
                requested_by=(run.summary or {}).get("requested_by"), run=run, trigger="manual",
            )
            if kind == "post_init" and result.ok and project.post_init_ok_at is None:
                self._mark_post_init_ok(project)
                # A blocked project starts on the next tick; nothing else to do.

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
            prev = rt.last_post_pull_version
            rt.last_post_pull_version = db_version
            hooks.run_hook(project, hook, "post_pull", db_version=db_version, prev_db_version=prev)

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
        rt.next_start_at = now + delay
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
        log.info("check: %s health=%s conditions=%s", project, health, sorted(wanted) or "-")

    # --- helpers -------------------------------------------------------------

    @staticmethod
    def _set_state(project, state, reason):
        if project.process_state == state and project.process_reason == reason:
            return
        project.process_state = state
        project.process_reason = reason
        SyncProject.objects.filter(pk=project.pk).update(process_state=state, process_reason=reason)
