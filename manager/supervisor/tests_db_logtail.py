"""
Log history written to the registry (design §6, §13): a replay of the POC
segment yields pull runs with ok null, never true, since no comment is
observed; the check confirms them; repeated errors are counted. Tagged `db`.
"""

from datetime import timedelta

from django.test import TestCase, tag
from django.utils import timezone

from registry.models import Database, Event, MerginAccount, SyncProject, SyncRun

from . import housekeeping, logtail
from .logtail import TailState, apply, classify, parse
from .tests_logtail import POC_SEGMENT


def make_project():
    account = MerginAccount(name="svc-log", username="svc")
    account.secret = "x"
    account.save()
    database = Database(name="log-db", host="db", dbname="ak", user="u")
    database.password = "pw"
    database.save()
    return SyncProject.objects.create(mergin_project="ak/log", mergin_account=account, database=database, schema_prefix="l")


@tag("db")
class ApplyTests(TestCase):
    def test_poc_replay_never_marks_a_pull_ok_from_the_log(self):
        project = make_project()
        state = TailState()
        actions = [a for a in (classify(r) for r in parse(POC_SEGMENT)) if a]
        counts = apply(project, actions, state)
        pulls = list(SyncRun.objects.filter(project=project, kind="pull").order_by("started_at"))
        self.assertEqual([p.checkout_version for p in pulls], [4177, 4178])
        self.assertEqual([p.ok for p in pulls], [None, False])      # the second failed on GEODIFF ERROR
        push = SyncRun.objects.get(project=project, kind="push")
        self.assertEqual((push.ok, push.checkout_version), (True, 4179))
        infos = Event.objects.filter(project=project, source="dbsync", level="info")
        self.assertEqual(infos.count(), 1)
        self.assertIn("AK-VALIDERING", infos.get().message)
        errors = list(Event.objects.filter(project=project, source="dbsync", level="error").order_by("at"))
        # 403 once (second suppressed), GEODIFF ERROR with the suppression note, pulling-data error, traceback
        self.assertEqual(len(errors), 4)
        self.assertIn("HTTP 403", errors[0].message)
        self.assertIn("repeated 1 more times", errors[1].message)
        self.assertEqual(counts["suppressed"], 1)
        # The check confirms pulls up to the database version.
        self.assertEqual(logtail.close_confirmed_pulls(project, 4177), 1)
        self.assertTrue(SyncRun.objects.get(checkout_version=4177).ok)
        self.assertFalse(SyncRun.objects.get(checkout_version=4178).ok)   # failed stays failed

    def test_prune_keeps_audit_events(self):
        project = make_project()
        old = timezone.now() - timedelta(days=100)
        Event.objects.create(project=project, at=old, level="info", source="dbsync", message="old machine event")
        from django.contrib.auth.models import User

        user = User.objects.create_user("audit", password="pw")
        Event.objects.create(project=project, at=old, level="info", source="user", message="old user action", user=user)
        Event.objects.create(project=project, level="info", source="dbsync", message="new")
        SyncRun.objects.create(project=project, kind="pull", started_at=old)
        SyncRun.objects.create(project=project, kind="pull")
        result = housekeeping.prune(days=90)
        self.assertEqual(result, {"events": 1, "runs": 1})
        self.assertEqual(set(Event.objects.filter(project=project).values_list("message", flat=True)), {"old user action", "new"})
        self.assertEqual(SyncRun.objects.filter(project=project).count(), 1)
