"""
The db-sync patches in patches/ (applied at image build, Dockerfile):

- both patches apply to the vendored upstream 2.3.0 sources exactly as
  the Dockerfile applies them, and give patches/*.patched;
- the SIGTERM handling of 0002 behaves (review note 2026-10-20b, finding
  1): a stop request during a connection pull lets that pull finish and
  skips the rest; a stop request during the sleep ends the daemon within
  a second, exit code 0.

The patched modules are loaded from patches/ with their dependencies
(psycopg2, mergin, config, ...) stubbed. No database, no Mergin.
"""

import datetime
import importlib.machinery
import importlib.util
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import types
from pathlib import Path
from unittest import mock

from django.test import SimpleTestCase

ROOT = Path(__file__).resolve().parents[2]
PATCHES = ROOT / "patches"
GIT = shutil.which("git")


class PatchesApplyTests(SimpleTestCase):
    def test_both_patches_apply_as_the_dockerfile_does(self):
        if GIT is None:
            self.skipTest("git not available")
        with tempfile.TemporaryDirectory() as tmp:
            for name in ("dbsync.py", "dbsync_daemon.py"):
                shutil.copy(PATCHES / "upstream" / name, Path(tmp) / name)
            for patch in sorted(PATCHES.glob("*.patch")):
                # -c core.autocrlf=false: the image is Linux; a Windows git
                # would otherwise write CRLF and the comparison below fails.
                result = subprocess.run(
                    [GIT, "-c", "core.autocrlf=false", "apply", "--include=*.py", str(patch)],
                    cwd=tmp, capture_output=True, text=True,
                )
                self.assertEqual(result.returncode, 0, f"{patch.name}: {result.stderr}")
            for name in ("dbsync.py", "dbsync_daemon.py"):
                applied = (Path(tmp) / name).read_bytes().replace(b"\r\n", b"\n")
                self.assertEqual(applied, (PATCHES / f"{name}.patched").read_bytes(), name)


# --- loading the patched sources with stubs ----------------------------------------


class _Meta(type):
    def __getattr__(cls, name):
        return _Anything


class _Anything(metaclass=_Meta):
    def __init__(self, *args, **kwargs):
        pass

    def __getattr__(self, name):
        return _Anything


class StubModule(types.ModuleType):
    def __getattr__(self, name):
        if name.startswith("__"):
            raise AttributeError(name)
        return _Anything


class StubConfig:
    connections: list = []

    def as_int(self, key):
        return 30

    def __contains__(self, key):
        return False

    def as_dict(self):
        return {}


def _stub_modules():
    mergin = StubModule("mergin")
    for exc in ("LoginError", "ClientError", "InvalidProject"):
        setattr(mergin, exc, type(exc, (Exception,), {}))
    config = StubModule("config")
    config.config = StubConfig()
    config.ConfigError = type("ConfigError", (Exception,), {})
    config.validate_config = lambda cfg: None
    config.update_config_path = lambda path: None
    config.get_ignored_tables = lambda conn: []
    config.get_include_tables = lambda conn: []
    version = StubModule("version")
    version.__version__ = "2.3.0-test"
    log_functions = StubModule("log_functions")
    log_functions.setup_logger = lambda *a, **k: None

    def handle_error_and_exit(error):
        raise AssertionError(f"daemon error: {error}")

    log_functions.handle_error_and_exit = handle_error_and_exit
    smtp = StubModule("smtp_functions")
    smtp.send_email = lambda *a, **k: None
    psycopg2 = StubModule("psycopg2")
    psycopg2.extensions = StubModule("psycopg2.extensions")
    return {
        "mergin": mergin, "config": config, "version": version, "log_functions": log_functions,
        "smtp_functions": smtp, "psycopg2": psycopg2, "psycopg2.extensions": psycopg2.extensions,
    }


def _load(name, path):
    # An explicit loader: ".patched" is not a Python source suffix.
    spec = importlib.util.spec_from_loader(name, importlib.machinery.SourceFileLoader(name, str(path)))
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


class PatchedDaemonTests(SimpleTestCase):
    def setUp(self):
        self.saved = {name: sys.modules.get(name) for name in
                      ("mergin", "config", "version", "log_functions", "smtp_functions", "psycopg2",
                       "psycopg2.extensions", "dbsync", "dbsync_daemon")}
        sys.modules.update(_stub_modules())
        self.dbsync = _load("dbsync", PATCHES / "dbsync.py.patched")
        self.daemon = _load("dbsync_daemon", PATCHES / "dbsync_daemon.py.patched")

    def tearDown(self):
        for name, module in self.saved.items():
            if module is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = module

    def test_stop_during_a_connection_pull_finishes_it_and_skips_the_rest(self):
        dbsync = self.dbsync
        pulled = []

        def pull(conn, mc):
            pulled.append(conn)
            if conn == "c1":
                dbsync.request_stop(15, None)    # SIGTERM arrives while c1 is pulling
            pulled.append(f"{conn} done")        # ...and c1 still completes

        dbsync.pull = pull
        dbsync.config.connections = ["c1", "c2", "c3"]
        dbsync._stop_requested = False
        dbsync.dbsync_pull(mc=None)
        self.assertEqual(pulled, ["c1", "c1 done"])
        self.assertTrue(dbsync.stop_requested())
        pushed = []
        dbsync.push = lambda conn, mc: pushed.append(conn)
        dbsync.dbsync_push(mc=None)
        self.assertEqual(pushed, [])             # nothing starts once a stop is requested

    def test_stop_during_the_sleep_exits_zero_within_a_second(self):
        dbsync, daemon = self.dbsync, self.daemon
        dbsync._stop_requested = False
        far = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=1)
        dbsync.create_mergin_client = lambda: types.SimpleNamespace(_auth_session={"expire": far})
        pulls, pushes = [], []
        dbsync.dbsync_pull = lambda mc: pulls.append(time.monotonic())

        def push(mc):
            pushes.append(time.monotonic())
            threading.Timer(0.3, dbsync.request_stop, args=(15, None)).start()   # arrives during the sleep

        dbsync.dbsync_push = push
        started = time.monotonic()
        with mock.patch.object(sys, "argv", ["dbsync_daemon.py", "config.yaml", "--skip-init"]), \
                mock.patch("builtins.print"):
            with self.assertRaises(SystemExit) as ctx:
                daemon.main()                    # signal.signal needs the main thread
        elapsed = time.monotonic() - started
        self.assertEqual(ctx.exception.code, 0)
        self.assertEqual((len(pulls), len(pushes)), (1, 1))
        self.assertLess(elapsed, 3, "sleep_time is 30 s; the stop must end it within about a second")
