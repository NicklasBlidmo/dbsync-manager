"""
Hook execution (design §8, §13 unit): environment, pgpass, masking,
capping, and the process itself (POSIX only). No registry database.
"""

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from django.test import SimpleTestCase, override_settings

from registry import crypto
from registry.models import Database, Hook, MerginAccount, SyncConnection, SyncProject

from . import alerts, hooks
from .core import PS, Runtime, Supervisor

KEY = crypto.generate_key()


def project_with_hook(hook=None):
    account = MerginAccount(pk=1, name="svc", username="svc")
    account.secret = "mergin-secret"
    database = Database(pk=1, name="gis", host="db.example", port=5433, dbname="ak", user="ak_dbsync")
    database.password = "db:pass\\word"
    project = SyncProject(
        pk=7, mergin_project="ak/bonsta", schema_prefix="p3922", mergin_account=account, database=database,
        desired_state="running", process_state=PS.INITIALIZING, post_init_hook=hook, notify_emails=["ops@example.org"],
    )
    conns = [
        SyncConnection(project=project, sync_file="1-punkter.gpkg", modified_schema="p3922_s_punkter", base_schema="p3922_b_punkter"),
        SyncConnection(project=project, sync_file="d-foto.gpkg", modified_schema="p3922_s_foto", base_schema="p3922_b_foto"),
    ]
    return project, conns


@override_settings(DBSYNC_MANAGER_SECRET_KEY=KEY)
class PureHookTests(SimpleTestCase):
    def test_environment_is_the_documented_one_without_the_password(self):
        project, conns = project_with_hook()
        env = hooks.hook_environment(project, conns, "post_init", work_dir="/data/projects/7",
                                     pgpass_path="/data/projects/7/hook.pgpass", db_version=42)
        self.assertEqual(env["DBSYNC_PROJECT"], "ak/bonsta")
        self.assertEqual(env["DBSYNC_PROJECT_ID"], "7")
        self.assertEqual(env["DBSYNC_SCHEMA_PREFIX"], "p3922")
        self.assertEqual(env["DBSYNC_MODIFIED_SCHEMAS"], "p3922_s_punkter,p3922_s_foto")
        self.assertEqual(env["DBSYNC_BASE_SCHEMAS"], "p3922_b_punkter,p3922_b_foto")
        self.assertEqual(env["DBSYNC_EVENT"], "post_init")
        self.assertEqual(env["DBSYNC_DB_VERSION"], "42")
        self.assertEqual((env["PGHOST"], env["PGPORT"], env["PGDATABASE"], env["PGUSER"]), ("db.example", "5433", "ak", "ak_dbsync"))
        self.assertEqual(env["PGPASSFILE"], "/data/projects/7/hook.pgpass")
        self.assertNotIn("PGPASSWORD", env)
        self.assertNotIn("DBSYNC_PREV_DB_VERSION", env)
        self.assertFalse(any("db:pass" in v for v in env.values()))
        env = hooks.hook_environment(project, conns, "post_pull", work_dir="/w", pgpass_path="/p", db_version=43, prev_db_version=42)
        self.assertEqual(env["DBSYNC_PREV_DB_VERSION"], "42")

    def test_hook_runs_as_its_own_database_row_when_set(self):
        project, conns = project_with_hook()
        admin_db = Database(pk=2, name="ak (admin)", host="db.example", port=5433, dbname="ak", user="ak_admin")
        admin_db.password = "admin-pw"
        hook = Hook(pk=3, name="grants", kind="post_init", command="true", database=admin_db)
        self.assertIs(hooks.run_as(project, hook), admin_db)
        self.assertIs(hooks.run_as(project, Hook(pk=4, name="x", kind="post_init", command="true")), project.database)
        env = hooks.hook_environment(project, conns, "post_init", work_dir="/w", pgpass_path="/p",
                                     database=hooks.run_as(project, hook))
        self.assertEqual(env["PGUSER"], "ak_admin")
        self.assertEqual(env["DBSYNC_SYNC_ROLE"], "ak_dbsync")   # grants target the sync role
        self.assertEqual(hooks.pgpass_line(admin_db), "db.example:5433:ak:ak_admin:admin-pw\n")

    def test_pgpass_line_escapes_colons_and_backslashes(self):
        project, _ = project_with_hook()
        self.assertEqual(hooks.pgpass_line(project.database), "db.example:5433:ak:ak_dbsync:db\\:pass\\\\word\n")

    def test_mask_and_cap(self):
        self.assertEqual(hooks.mask("pw=db:pass\\word and mergin-secret", ["db:pass\\word", "mergin-secret", ""]), "pw=*** and ***")
        text = "a" * 100 + "MIDDLE" + "b" * 100
        capped = hooks.cap(text, limit=40)
        self.assertTrue(capped.startswith("a" * 20))
        self.assertTrue(capped.endswith("b" * 20))
        self.assertIn("characters omitted", capped)
        self.assertNotIn("MIDDLE", capped)
        self.assertEqual(hooks.cap("short", limit=40), "short")


@unittest.skipUnless(os.name == "posix", "needs /bin/sh and process groups")
class ExecuteTests(SimpleTestCase):
    def test_exit_code_output_and_environment(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = dict(os.environ, DBSYNC_EVENT="post_init")
            result = hooks.execute('echo "event=$DBSYNC_EVENT"; echo oops >&2; exit 3', cwd=tmp, env=env, timeout_s=10)
        self.assertFalse(result.ok)
        self.assertEqual(result.exit_code, 3)
        self.assertEqual(result.stdout.strip(), "event=post_init")
        self.assertEqual(result.stderr.strip(), "oops")
        self.assertFalse(result.timed_out)

    def test_timeout_kills_the_process_group(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = hooks.execute("sleep 30 & wait", cwd=tmp, env=dict(os.environ), timeout_s=1)
        self.assertTrue(result.timed_out)
        self.assertFalse(result.ok)
        self.assertIsNone(result.exit_code)
        self.assertLess(result.duration_s, 10)


class AlertTextTests(SimpleTestCase):
    @override_settings(DBSYNC_BASE_URL="http://gis.example:8090/")
    def test_subject_and_body(self):
        project, _ = project_with_hook()
        self.assertEqual(alerts.subject_for(project, "db_behind:1-punkter.gpkg", False), "[dbsync-manager] ak/bonsta: alert db_behind:1-punkter.gpkg")
        self.assertEqual(alerts.subject_for(project, "stuck", True), "[dbsync-manager] ak/bonsta: resolved stuck")
        body = alerts.body_for(project, "stuck", "checkout not moving", resolved=False)
        self.assertIn("Condition: stuck (active)", body)
        self.assertIn("checkout not moving", body)
        self.assertIn("http://gis.example:8090/projects/7/", body)


# --- post_init through the loop (§6 step 4), registry writes stubbed ------------


class StubChild:
    def __init__(self):
        self.alive = True
        self.pid = 4242
        self.exit_code = None
        self.terminated = False

    def terminate(self):
        self.terminated = True
        self.alive = False
        self.exit_code = -15

    def kill(self):
        pass


def make_hook(name="grants", kind="post_init"):
    return Hook(pk=1, name=name, kind=kind, command="true", timeout_s=600)


@override_settings(DBSYNC_MANAGER_SECRET_KEY=KEY)
class PostInitFlowTests(SimpleTestCase):
    def setUp(self):
        for target in ("_maybe_check", "_set_state", "_mark_post_init_ok", "_project_db_version"):
            patcher = mock.patch.object(Supervisor, target)
            setattr(self, target, patcher.start())
            self.addCleanup(patcher.stop)
        for target in ("supervisor.core.events", "supervisor.core.hooks.pending_requests"):
            patcher = mock.patch(target)
            self.addCleanup(patcher.stop)
            patched = patcher.start()
            if target.endswith("pending_requests"):
                patched.return_value = []
        self.comments = mock.patch("supervisor.core.dbinspect.base_schema_comments",
                                   return_value={"p3922_b_punkter": {"version": "v5"}, "p3922_b_foto": {"version": "v5"}})
        self.comments.start()
        self.addCleanup(self.comments.stop)
        self.run_hook = mock.patch("supervisor.core.hooks.run_hook").start()
        self.addCleanup(mock.patch.stopall)
        self.rotate = mock.patch("supervisor.core.rotate_if_needed", return_value=False).start()

    def project(self, hook):
        project, conns = project_with_hook(hook)
        # connections.all() would hit the database; serve the stub list instead.
        patcher = mock.patch.object(SyncProject, "connections", new_callable=mock.PropertyMock,
                                    return_value=mock.Mock(all=mock.Mock(return_value=conns)))
        patcher.start()
        self.addCleanup(patcher.stop)
        return project

    def test_hook_success_marks_ok_and_running(self):
        hook = make_hook()
        project = self.project(hook)
        self.run_hook.return_value = (None, hooks.HookResult(True, 0, False, "", "", 1.0))
        supervisor = Supervisor(1)
        rt = Runtime(child=StubChild())
        supervisor.runtimes[project.pk] = rt
        supervisor._reconcile(project)
        self.run_hook.assert_called_once()
        self.assertEqual(self.run_hook.call_args.args[1], hook)
        self.assertEqual(self.run_hook.call_args.args[2], "post_init")
        self._mark_post_init_ok.assert_called_once_with(project)
        self._set_state.assert_called_with(project, PS.RUNNING, "")
        self.assertFalse(rt.child.terminated)

    def test_hook_failure_stops_the_child_and_blocks(self):
        hook = make_hook()
        project = self.project(hook)
        self.run_hook.return_value = (None, hooks.HookResult(False, 1, False, "", "permission denied", 0.2))
        supervisor = Supervisor(1)
        child = StubChild()
        rt = Runtime(child=child)
        supervisor.runtimes[project.pk] = rt
        supervisor._reconcile(project)             # runs the hook, terminates the child
        self.assertTrue(child.terminated)
        self._mark_post_init_ok.assert_not_called()
        self.assertIn("post_init hook 'grants' failed (exit code 1)", rt.block_after_exit)
        supervisor._reconcile(project)             # child gone -> blocked, not crashed
        self.assertIsNone(rt.child)
        self.assertEqual(rt.crashes, 0)
        state_calls = [c.args[1] for c in self._set_state.call_args_list]
        self.assertIn(PS.BLOCKED, state_calls)
        self.assertNotIn(PS.CRASHED, state_calls)

    def test_no_hook_goes_straight_to_running(self):
        project = self.project(None)
        supervisor = Supervisor(1)
        supervisor.runtimes[project.pk] = Runtime(child=StubChild())
        supervisor._reconcile(project)
        self.run_hook.assert_not_called()
        self._set_state.assert_called_with(project, PS.RUNNING, "")

    def test_start_is_blocked_while_post_init_not_ok_and_schemas_exist(self):
        hook = make_hook()
        project = self.project(hook)
        project.process_state = PS.STOPPED
        supervisor = Supervisor(1)
        supervisor.runtimes[project.pk] = Runtime()
        with mock.patch("supervisor.core.processes.spawn") as spawn, mock.patch("supervisor.core.write_config"):
            supervisor._reconcile(project)
            spawn.assert_not_called()
        self.assertEqual(self._set_state.call_args.args[1], PS.BLOCKED)
        self.assertIn("post_init hook has not succeeded", self._set_state.call_args.args[2])
