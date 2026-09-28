"""
Two daemons started in one tick (review note 2026-09-28b): each gets its
own TMPDIR under its work dir, the directory is emptied first, and the
spawns are at least SPAWN_STAGGER seconds apart. Goes through _reconcile
with the registry writes stubbed.
"""

import os
import tempfile
from pathlib import Path
from unittest import mock

from django.test import SimpleTestCase, override_settings

from registry import crypto
from registry.models import Database, MerginAccount, SyncProject

from . import processes
from .core import PS, SPAWN_STAGGER, Runtime, Supervisor
from .processes import private_tmpdir

KEY = crypto.generate_key()


def make_project(pk):
    account = MerginAccount(pk=1, name="svc", username="svc")
    account.secret = "pw"
    database = Database(pk=1, name="gis", host="db", dbname="ak", user="u")
    database.password = "pw"
    return SyncProject(pk=pk, mergin_project=f"ak/p{pk}", schema_prefix=f"p{pk}", mergin_account=account,
                       database=database, desired_state="running", process_state=PS.STOPPED)


class PrivateTmpdirTests(SimpleTestCase):
    def test_created_private_and_emptied(self):
        with tempfile.TemporaryDirectory() as tmp:
            work = Path(tmp) / "projects" / "4"
            first = private_tmpdir(work)
            self.assertTrue(first.is_dir())
            if os.name == "posix":
                self.assertEqual(first.stat().st_mode & 0o777, 0o700)
            (first / "riLhmPSJ").write_bytes(b"")           # a leftover geodiff copy
            (first / "sub").mkdir()
            (first / "sub" / "x").write_bytes(b"")
            again = private_tmpdir(work)
            self.assertEqual(again, first)
            self.assertEqual(list(first.iterdir()), [])


@override_settings(DBSYNC_MANAGER_SECRET_KEY=KEY)
class TwoStartsInOneTickTests(SimpleTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        settings_patch = override_settings(DBSYNC_DATA_DIR=Path(self.tmp.name))
        settings_patch.enable()
        self.addCleanup(settings_patch.disable)
        for target, value in (("_maybe_check", None), ("_set_state", None), ("_still_wanted", True),
                              ("_initial_state", (PS.RUNNING, ""))):
            patcher = mock.patch.object(Supervisor, target, return_value=value)
            patcher.start()
            self.addCleanup(patcher.stop)
        for target, value in (("supervisor.core.events", None), ("supervisor.core.render_config", "config: 1\n"),
                              ("supervisor.core.write_config", None)):
            patcher = mock.patch(target, return_value=value) if value is not None else mock.patch(target)
            patcher.start()
            self.addCleanup(patcher.stop)
        mock.patch("supervisor.core.hooks.pending_requests", return_value=[]).start()
        self.addCleanup(mock.patch.stopall)

    def test_distinct_tmpdirs_and_staggered_spawns(self):
        supervisor = Supervisor(1)
        spawns = []

        def fake_spawn(command, cwd, stderr_path, pidfile, config_path, env=None):
            spawns.append(env)
            return processes.Child(pid=1000 + len(spawns), config_path=Path(config_path), popen=mock.Mock(poll=lambda: None))

        sleeps = []
        with mock.patch("supervisor.core.processes.spawn", side_effect=fake_spawn), \
             mock.patch("supervisor.core.time.sleep", side_effect=sleeps.append):
            for pk in (4, 5):
                project = make_project(pk)
                supervisor.runtimes[pk] = Runtime()
                supervisor._reconcile(project)

        self.assertEqual(len(spawns), 2)
        tmpdirs = [env["TMPDIR"] for env in spawns]
        self.assertNotEqual(tmpdirs[0], tmpdirs[1])
        self.assertTrue(tmpdirs[0].endswith(os.path.join("projects", "4", "tmp")))
        self.assertTrue(all(Path(t).is_dir() for t in tmpdirs))
        self.assertEqual(spawns[0]["TMP"], spawns[0]["TMPDIR"])
        # The first start is immediate, the second waits out the stagger
        # (time.monotonic did not advance, so the full interval is slept).
        self.assertEqual(len(sleeps), 1)
        self.assertGreaterEqual(sleeps[0], SPAWN_STAGGER - 0.05)
