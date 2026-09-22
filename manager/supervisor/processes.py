"""
db-sync daemon processes (design §6, one subprocess per project).

A child is either spawned by this supervisor (a Popen we can wait on) or
adopted from a pidfile after the supervisor itself was restarted while the
container kept running: s6 restarts only the failed service, and the
orphaned daemons are reparented, not killed. Adopting them is what keeps
the invariant that two daemons never sync the same schemas.

Adoption is decided by the pidfile's pid *and* the config path in that
process's command line (/proc/<pid>/cmdline), so a recycled pid is not
mistaken for our daemon.
"""

import os
import signal
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

PIDFILE_NAME = "dbsync.pid"
CONFIG_NAME = "config.yaml"
LOG_NAME = "dbsync.log"
STDERR_NAME = "dbsync.stderr.log"


@dataclass
class Child:
    pid: int
    config_path: Path
    started_at: float = field(default_factory=time.monotonic)
    popen: subprocess.Popen | None = None
    adopted: bool = False

    @property
    def alive(self) -> bool:
        if self.popen is not None:
            return self.popen.poll() is None
        return pid_runs_config(self.pid, self.config_path)

    @property
    def exit_code(self):
        """Exit code once dead; None while alive or for an adopted child."""
        if self.popen is not None:
            return self.popen.poll()
        return None

    def terminate(self):
        _signal(self.pid, signal.SIGTERM)

    def kill(self):
        _signal(self.pid, signal.SIGKILL)


def _signal(pid, sig):
    try:
        os.kill(pid, sig)
    except ProcessLookupError:
        pass
    except PermissionError:
        pass


def daemon_command(daemon_dir, config_path, log_path) -> list[str]:
    return [
        sys.executable,
        str(Path(daemon_dir) / "dbsync_daemon.py"),
        str(config_path),
        "--log-file",
        str(log_path),
        "--log-verbosity",
        "INFO",
    ]


def spawn(command, cwd, stderr_path, pidfile, config_path) -> Child:
    """Start `command` detached in its own session, stderr appended to a file.

    stdout is discarded: db-sync prints only a timestamp per cycle there.
    stderr carries every log record at DEBUG level plus any traceback that
    escapes the daemon, which is what explains an unexpected exit.
    """
    cwd = Path(cwd)
    cwd.mkdir(parents=True, exist_ok=True)
    with open(stderr_path, "ab") as stderr:
        popen = subprocess.Popen(
            command,
            cwd=str(cwd),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=stderr,
            start_new_session=True,
            close_fds=True,
        )
    write_pidfile(pidfile, popen.pid, config_path)
    return Child(pid=popen.pid, config_path=Path(config_path), popen=popen)


def write_pidfile(pidfile, pid, config_path):
    Path(pidfile).write_text(f"{pid}\n{config_path}\n", encoding="utf-8")


def read_pidfile(pidfile):
    """Return (pid, config_path) or None."""
    try:
        lines = Path(pidfile).read_text(encoding="utf-8").splitlines()
        return int(lines[0]), Path(lines[1])
    except (OSError, ValueError, IndexError):
        return None


def remove_pidfile(pidfile):
    try:
        Path(pidfile).unlink()
    except FileNotFoundError:
        pass


def pid_runs_config(pid: int, config_path) -> bool:
    """True if `pid` is alive and its command line names `config_path`."""
    try:
        cmdline = Path(f"/proc/{pid}/cmdline").read_bytes()
    except FileNotFoundError:
        return False
    except OSError:
        # No /proc (not Linux): fall back to liveness only.
        try:
            os.kill(pid, 0)
        except OSError:
            return False
        return True
    args = cmdline.split(b"\0")
    return str(config_path).encode() in args


def adopt(pidfile) -> Child | None:
    """Re-attach to a daemon left running by a previous supervisor, or None."""
    found = read_pidfile(pidfile)
    if found is None:
        return None
    pid, config_path = found
    if not pid_runs_config(pid, config_path):
        remove_pidfile(pidfile)
        return None
    return Child(pid=pid, config_path=config_path, adopted=True)


def wait_for_exit(children, timeout: float) -> list:
    """Wait up to `timeout` seconds for all children to exit; return the survivors."""
    deadline = time.monotonic() + timeout
    survivors = list(children)
    while survivors and time.monotonic() < deadline:
        survivors = [child for child in survivors if child.alive]
        if survivors:
            time.sleep(0.2)
    return survivors
