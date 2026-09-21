"""
The supervisor loop (design §6).

Slice 1 delivers the process frame only: exactly one supervisor per
registry (advisory lock), a tick loop with clean shutdown, and observation
of the registry. Steps 0–6 of the loop are added slice by slice (§14).
"""

import logging
import signal
import threading

from django.db import connection
from django.db.models import Count

from registry.models import SyncProject

log = logging.getLogger("supervisor")

# Fixed key for pg_advisory_lock: "dbsync-manager supervisor". Any second
# supervisor against the same registry blocks here (§6 process ownership).
LOCK_KEY = 0x6462_7379_6E63_7376


class Supervisor:
    def __init__(self, tick_seconds: int):
        self.tick_seconds = tick_seconds
        self._stop = threading.Event()

    # --- lifecycle -----------------------------------------------------------

    def install_signal_handlers(self):
        for sig in (signal.SIGTERM, signal.SIGINT):
            signal.signal(sig, self._on_signal)

    def _on_signal(self, signum, frame):
        log.info("received signal %s, stopping after the current tick", signum)
        self._stop.set()

    def acquire_lock(self):
        """Hold a session-level advisory lock for the life of this process.

        The lock lives on the Django connection, which a management command
        keeps open; nothing here may close it.
        """
        with connection.cursor() as cursor:
            cursor.execute("SELECT pg_try_advisory_lock(%s)", [LOCK_KEY])
            (acquired,) = cursor.fetchone()
            if acquired:
                log.info("supervisor lock acquired")
                return
            log.warning(
                "another supervisor holds the lock on this registry; "
                "waiting instead of starting children"
            )
            cursor.execute("SELECT pg_advisory_lock(%s)", [LOCK_KEY])
            log.info("supervisor lock acquired after waiting")

    def run(self):
        self.install_signal_handlers()
        self.acquire_lock()
        log.info("supervisor running, tick every %ss", self.tick_seconds)
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception:  # noqa: BLE001 - the loop must survive a bad tick
                log.exception("tick failed")
            self._stop.wait(self.tick_seconds)
        self.shutdown()

    def shutdown(self):
        log.info("supervisor stopped")

    # --- one tick ------------------------------------------------------------

    def tick(self):
        counts = (
            SyncProject.objects.filter(removed_at__isnull=True)
            .values("desired_state")
            .annotate(n=Count("id"))
        )
        summary = ", ".join(f"{row['desired_state']}={row['n']}" for row in counts) or "no projects"
        log.info("tick: %s", summary)
