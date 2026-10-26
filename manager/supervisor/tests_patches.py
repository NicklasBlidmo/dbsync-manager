"""
The db-sync patches in patches/ (applied at image build, Dockerfile):

- every patch applies to the vendored upstream 2.4.0 sources exactly as
  the Dockerfile applies them, and gives patches/*.patched;
- the SIGTERM handling of 0002 behaves (review note 2026-10-20b, finding
  1; rebased on 2.4.0's retrying run_daemon in slice 16): a stop request
  during a connection pull lets that pull finish and skips the rest; a
  stop request during the sleep ends the daemon within a second, exit
  code 0; a stop request during the pull step ends the cycle before push,
  through 2.4.0's `except Exception` (SystemExit is not an Exception);
- the lost-update report of 0003 (two-way test 2026-10-26e, T6d): a row
  deleted in the database that the server's changeset updates is logged
  as a WARNING with the row's key and the field's values; other
  combinations are silent; a geodiff failure never breaks the pull.

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
SOURCES = ("dbsync.py", "dbsync_daemon.py")


class PatchesApplyTests(SimpleTestCase):
    def test_the_patches_apply_as_the_dockerfile_does(self):
        if GIT is None:
            self.skipTest("git not available")
        patches = sorted(PATCHES.glob("*.patch"))
        self.assertEqual([p.name for p in patches], [   # 0001 shipped in 2.4.0
            "0002-Stop-gracefully-on-SIGTERM.patch",
            "0003-Report-field-updates-lost-to-a-database-delete.patch",
        ])
        with tempfile.TemporaryDirectory() as tmp:
            for name in SOURCES:
                shutil.copy(PATCHES / "upstream" / name, Path(tmp) / name)
            for patch in patches:
                # -c core.autocrlf=false: the image is Linux; a Windows git
                # would otherwise write CRLF and the comparison below fails.
                result = subprocess.run(
                    [GIT, "-c", "core.autocrlf=false", "apply", "--include=*.py", str(patch)],
                    cwd=tmp, capture_output=True, text=True,
                )
                self.assertEqual(result.returncode, 0, f"{patch.name}: {result.stderr}")
            for name in SOURCES:
                applied = (Path(tmp) / name).read_bytes().replace(b"\r\n", b"\n")
                self.assertEqual(applied, (PATCHES / f"{name}.patched").read_bytes(), name)

    def test_upstream_sources_are_2_4_0(self):
        daemon = (PATCHES / "upstream" / "dbsync_daemon.py").read_text(encoding="utf-8")
        self.assertIn("def run_daemon(", daemon)                      # 2.4.0's retrying loop
        self.assertIn("DEFAULT_MAX_FAILED_RETRIES = 10", daemon)
        dbsync = (PATCHES / "upstream" / "dbsync.py").read_text(encoding="utf-8")
        self.assertIn("def _get_work_dir(conn_cfg):", dbsync)          # #187 as upstream merged it
        self.assertIn('f"{project_name}-{conn_cfg.modified}"', dbsync)
        self.assertIn("class AuthTokenStore:", dbsync)                 # #190


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

    def get(self, key, default=None, cast=None):
        return default

    def __contains__(self, key):
        return False

    def as_dict(self):
        return {}


def _stub_modules():
    mergin = StubModule("mergin")
    for exc in ("LoginError", "ClientError", "InvalidProject"):
        setattr(mergin, exc, type(exc, (Exception,), {}))
    mergin_client = StubModule("mergin.client")
    mergin_client.TokenError = type("TokenError", (Exception,), {})
    mergin_client.decode_token_data = lambda token: {}
    config = StubModule("config")
    config.config = StubConfig()
    config.ConfigError = type("ConfigError", (Exception,), {})
    config.validate_config = lambda cfg: None
    config.update_config_path = lambda path: None
    config.get_ignored_tables = lambda conn: []
    config.get_include_tables = lambda conn: []
    version = StubModule("version")
    version.__version__ = "2.4.0-test"
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
        "mergin": mergin, "mergin.client": mergin_client, "config": config, "version": version,
        "log_functions": log_functions, "smtp_functions": smtp, "psycopg2": psycopg2,
        "psycopg2.extensions": psycopg2.extensions,
    }


def _load(name, path):
    # An explicit loader: ".patched" is not a Python source suffix.
    spec = importlib.util.spec_from_loader(name, importlib.machinery.SourceFileLoader(name, str(path)))
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


class PatchedDaemonTests(SimpleTestCase):
    ARGV = ["dbsync_daemon.py", "config.yaml", "--skip-init"]

    def setUp(self):
        self.saved = {name: sys.modules.get(name) for name in
                      ("mergin", "mergin.client", "config", "version", "log_functions", "smtp_functions",
                       "psycopg2", "psycopg2.extensions", "dbsync", "dbsync_daemon")}
        sys.modules.update(_stub_modules())
        self.dbsync = _load("dbsync", PATCHES / "dbsync.py.patched")
        self.daemon = _load("dbsync_daemon", PATCHES / "dbsync_daemon.py.patched")
        self.dbsync._stop_requested = False
        far = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=1)
        self.dbsync.create_mergin_client = lambda: types.SimpleNamespace(_auth_session={"expire": far})

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
        dbsync.dbsync_pull(mc=None)
        self.assertEqual(pulled, ["c1", "c1 done"])
        self.assertTrue(dbsync.stop_requested())
        pushed = []
        dbsync.push = lambda conn, mc: pushed.append(conn)
        dbsync.dbsync_push(mc=None)
        self.assertEqual(pushed, [])             # nothing starts once a stop is requested

    def test_stop_during_the_sleep_exits_zero_within_a_second(self):
        dbsync, daemon = self.dbsync, self.daemon
        pulls, pushes = [], []
        dbsync.dbsync_pull = lambda mc: pulls.append(time.monotonic())

        def push(mc):
            pushes.append(time.monotonic())
            threading.Timer(0.3, dbsync.request_stop, args=(15, None)).start()   # arrives during the sleep

        dbsync.dbsync_push = push
        started = time.monotonic()
        with mock.patch.object(sys, "argv", self.ARGV), mock.patch("builtins.print"):
            with self.assertRaises(SystemExit) as ctx:
                daemon.main()                    # signal.signal needs the main thread
        elapsed = time.monotonic() - started
        self.assertEqual(ctx.exception.code, 0)
        self.assertEqual((len(pulls), len(pushes)), (1, 1))
        self.assertLess(elapsed, 3, "sleep_time is 30 s; the stop must end it within about a second")

    def test_stop_during_pull_exits_zero_before_push_through_the_retry_loop(self):
        # 2.4.0 wraps the cycle in `except Exception` and retries; the exit
        # after pull must pass through it (SystemExit is a BaseException).
        dbsync, daemon = self.dbsync, self.daemon
        pushes = []
        dbsync.dbsync_pull = lambda mc: dbsync.request_stop(15, None)
        dbsync.dbsync_push = lambda mc: pushes.append(1)
        with mock.patch.object(sys, "argv", self.ARGV), mock.patch("builtins.print"), \
                mock.patch.object(daemon.time, "sleep") as sleep:
            with self.assertRaises(SystemExit) as ctx:
                daemon.main()
        self.assertEqual(ctx.exception.code, 0)
        self.assertEqual(pushes, [])
        sleep.assert_not_called()                # no retry wait: this was not a failure

    def test_lost_field_updates_are_found_by_row_key_and_logged(self):
        dbsync = self.dbsync
        # as-json shape: an update carries old+new for changed columns and old only for the key;
        # a delete carries every old value.
        ours = [
            {"table": "punkter", "type": "delete",
             "changes": [{"column": 0, "old": 631}, {"column": 1, "old": "u-631"}, {"column": 3, "old": "x"}]},
            {"table": "punkter", "type": "delete",
             "changes": [{"column": 0, "old": 2}, {"column": 1, "old": "u-2"}, {"column": 3, "old": "y"}]},
            {"table": "punkter", "type": "update",
             "changes": [{"column": 0, "old": 5}, {"column": 3, "old": "a", "new": "b"}]},
        ]
        theirs = [
            {"table": "punkter", "type": "update",
             "changes": [{"column": 0, "old": 631}, {"column": 3, "old": "x", "new": "ny text"}]},   # lost
            {"table": "punkter", "type": "update",
             "changes": [{"column": 0, "old": 7}, {"column": 3, "old": "q", "new": "r"}]},           # other row
            {"table": "punkter", "type": "delete",
             "changes": [{"column": 0, "old": 2}, {"column": 1, "old": "u-2"}, {"column": 3, "old": "y"}]},  # both deleted
            {"table": "linjer", "type": "update",
             "changes": [{"column": 0, "old": 631}, {"column": 2, "old": 1, "new": 2}]},              # other table
        ]
        self.assertEqual(dbsync._lost_field_updates(ours, theirs),
                         [("punkter", {0: 631}, [(3, "ny text")])])
        self.assertEqual(dbsync._lost_field_updates([], theirs), [])
        dbsync._geodiff_list_changes_details = lambda path: ours if path.endswith("base2our") else theirs
        with self.assertLogs(level="WARNING") as logs:
            dbsync._report_lost_field_updates("/t/p-dbsync-pull-base2our", "/t/p-dbsync-pull-base2their")
        self.assertEqual(len(logs.output), 1)
        self.assertIn("field update lost: table punkter row (column 0 = 631) was deleted in the database; "
                      "the update from Mergin Maps (column 3: 'ny text') is dropped with the row", logs.output[0])

        def boom(path):
            raise RuntimeError("geodiff failed!")

        dbsync._geodiff_list_changes_details = boom
        with self.assertLogs(level="WARNING") as logs:
            dbsync._report_lost_field_updates("a", "b")          # reported, never raised
        self.assertIn("could not compare the changesets", logs.output[0])
        self.assertIn("_report_lost_field_updates(tmp_base2our, tmp_base2their)",
                      (PATCHES / "dbsync.py.patched").read_text(encoding="utf-8"))

    def test_sleep_unless_stopped_returns_early_and_the_retry_wait_uses_it(self):
        dbsync, daemon = self.dbsync, self.daemon
        with mock.patch.object(daemon.time, "sleep") as sleep:
            daemon.sleep_unless_stopped(3)
            self.assertEqual(sleep.call_count, 3)
            dbsync.request_stop(15, None)
            daemon.sleep_unless_stopped(600)
            self.assertEqual(sleep.call_count, 3)   # not one more second
        self.assertIn("sleep_unless_stopped(wait_time)", (PATCHES / "dbsync_daemon.py.patched").read_text(encoding="utf-8"))
        self.assertNotIn("time.sleep(wait_time)", (PATCHES / "dbsync_daemon.py.patched").read_text(encoding="utf-8"))
