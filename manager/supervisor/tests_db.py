"""
Supervisor tests that need the registry (tagged `db`): alert mails through
conditions, hook runs as sync_run rows, hook requests. Run with the
compose profile `test`.
"""

import os
import unittest

from django.core import mail
from django.test import TestCase, tag

from registry.models import Database, Event, Hook, MerginAccount, SyncConnection, SyncProject, SyncRun

from . import conditions, hooks


def make_project(**kw):
    account = MerginAccount(name="svc", username="svc")
    account.secret = "mergin-secret"
    account.save()
    database = Database(name="gis", host="db", dbname="ak", user="ak_dbsync")
    database.password = "sup3r-secret"
    database.save()
    fields = dict(mergin_project="ak/bonsta", mergin_account=account, database=database, schema_prefix="p3922")
    fields.update(kw)
    project = SyncProject.objects.create(**fields)
    SyncConnection.objects.create(project=project, sync_file="1-punkter.gpkg", modified_schema="p3922_s_punkter", base_schema="p3922_b_punkter")
    return project


@tag("db")
class AlertTests(TestCase):
    def test_mail_on_activate_and_resolve_only(self):
        project = make_project(notify_emails=["ops@example.org", "gis@example.org"])
        self.assertTrue(conditions.activate(project, "db_behind:1-punkter.gpkg", "database is at v10 while the checkout is at v11"))
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, ["ops@example.org", "gis@example.org"])
        self.assertIn("alert db_behind:1-punkter.gpkg", mail.outbox[0].subject)
        self.assertIn("v10 while the checkout is at v11", mail.outbox[0].body)
        # Still active: nothing more.
        self.assertFalse(conditions.activate(project, "db_behind:1-punkter.gpkg", "again"))
        self.assertEqual(len(mail.outbox), 1)
        self.assertTrue(conditions.resolve(project, "db_behind:1-punkter.gpkg"))
        self.assertEqual(len(mail.outbox), 2)
        self.assertIn("resolved db_behind:1-punkter.gpkg", mail.outbox[1].subject)
        self.assertEqual(Event.objects.filter(project=project, condition="db_behind:1-punkter.gpkg").count(), 2)

    def test_no_recipients_no_mail_but_events_still_written(self):
        project = make_project(notify_emails=[])
        conditions.activate(project, "stuck", "checkout not moving")
        self.assertEqual(len(mail.outbox), 0)
        self.assertEqual(Event.objects.filter(project=project, condition="stuck").count(), 1)


@tag("db")
class HookRunTests(TestCase):
    @unittest.skipUnless(os.name == "posix", "needs /bin/sh")
    def test_run_hook_records_masked_output_and_removes_pgpass(self):
        project = make_project()
        hook = Hook.objects.create(
            name="show", kind="post_init", timeout_s=30,
            command='echo "event=$DBSYNC_EVENT schemas=$DBSYNC_MODIFIED_SCHEMAS"; cat "$PGPASSFILE"; echo mergin-secret',
        )
        run, result = hooks.run_hook(project, hook, "post_init", db_version=5)
        self.assertTrue(result.ok)
        run.refresh_from_db()
        self.assertTrue(run.ok)
        self.assertIsNotNone(run.finished_at)
        self.assertEqual(run.summary["status"], "done")
        self.assertIn("event=post_init schemas=p3922_s_punkter", run.summary["stdout"])
        self.assertNotIn("sup3r-secret", run.summary["stdout"])   # pgpass content masked
        self.assertNotIn("mergin-secret", run.summary["stdout"])
        self.assertIn("***", run.summary["stdout"])
        from django.conf import settings

        self.assertFalse((settings.DBSYNC_DATA_DIR / "projects" / str(project.pk) / hooks.PGPASS_NAME).exists())
        self.assertTrue(Event.objects.filter(project=project, source="hook", level="info").exists())

    @unittest.skipUnless(os.name == "posix", "needs /bin/sh")
    def test_failed_hook_writes_error_event_with_stderr_tail(self):
        project = make_project()
        hook = Hook.objects.create(name="bad", kind="post_init", command="echo nope >&2; exit 2")
        run, result = hooks.run_hook(project, hook, "post_init")
        self.assertFalse(result.ok)
        self.assertEqual(run.summary["exit_code"], 2)
        event = Event.objects.get(project=project, source="hook", level="error")
        self.assertIn("exit code 2", event.message)
        self.assertIn("nope", event.message)

    def test_request_and_pending(self):
        project = make_project()
        hook = Hook.objects.create(name="grants", kind="post_init", command="true")
        project.post_init_hook = hook
        project.save()
        from django.contrib.auth.models import User

        user = User.objects.create_user("op", password="pw")
        run = hooks.request_run(project, hook, user)
        self.assertEqual(run.summary["status"], "requested")
        self.assertEqual(run.summary["requested_by"], "op")
        self.assertEqual([r.pk for r in hooks.pending_requests(project)], [run.pk])
        run.finished_at = run.started_at
        run.summary = {**run.summary, "status": "done"}
        run.save()
        self.assertEqual(hooks.pending_requests(project), [])
        self.assertEqual(SyncRun.objects.filter(project=project, kind="hook").count(), 1)
