"""
Log history written to the registry (design §6, §13): a replay of the POC
segment yields pull runs with ok null, never true, since no comment is
observed; the check confirms them; repeated errors are counted. Tagged `db`.
"""

import json
import tempfile
from datetime import timedelta
from pathlib import Path
from unittest import mock

from django.test import TestCase, override_settings, tag
from django.utils import timezone

from registry.models import Database, Event, MerginAccount, SyncConnection, SyncProject, SyncRun, SyncStatus

from . import conflicts, housekeeping, logtail
from .core import Supervisor
from .tests_conflicts import GEODIFF_FILE
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


@tag("db")
class ConflictCollectionTests(TestCase):
    """Slice 17, A: a conflict file in the daemon's TMPDIR is moved to the
    project's conflicts dir and becomes one warning event naming the
    connection's layer and rows; in a one-way project it is the
    content_divergence condition instead."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        override = override_settings(DBSYNC_DATA_DIR=Path(self.tmp.name))
        override.enable()
        self.addCleanup(override.disable)
        self.project = make_project()
        self.conn = SyncConnection.objects.create(project=self.project, sync_file="1-punkter.gpkg",
                                                  modified_schema="l_s_punkter", base_schema="l_b_punkter")
        self.status = SyncStatus.objects.create(
            project=self.project, connection=self.conn, direction="two_way", mergin_permission="writer",
            tables=[{"name": "punkter", "geometry_column": "geom"}, {"name": "linjer", "geometry_column": "geom"}],
        )
        self.tmpdir = self.project.work_dir / "tmp"
        self.tmpdir.mkdir(parents=True)
        self.sup = Supervisor(1)

    def test_file_is_moved_and_the_event_written(self):
        (self.tmpdir / "log-dbsync-pull-conflicts").write_text(json.dumps(GEODIFF_FILE))
        with mock.patch("supervisor.conflicts._column_names", return_value={}):
            self.sup._collect_conflicts(self.project, self.sup._paths(self.project.pk))
        self.assertEqual(list(self.tmpdir.iterdir()), [])
        saved = conflicts.list_saved(self.project.work_dir)
        self.assertEqual(len(saved), 1)
        self.assertEqual(saved[0]["connection"], "1-punkter.gpkg")
        event = Event.objects.get(project=self.project, level="warning")
        self.assertEqual(event.source, "dbsync")
        self.assertIn("3 conflicts in punkter (column #3, column #4), 1 deleted on the server; fid 17, 41, 50", event.message)
        self.assertIn(f"see conflicts/{saved[0]['name']}", event.message)
        self.project.refresh_from_db()
        self.assertEqual(self.project.active_conditions, {})
        # Second tick, nothing new: no second event.
        self.sup._collect_conflicts(self.project, self.sup._paths(self.project.pk))
        self.assertEqual(Event.objects.filter(project=self.project).count(), 1)

    def test_one_way_rebase_raises_the_condition_with_a_mail(self):
        SyncStatus.objects.filter(pk=self.status.pk).update(direction="one_way", mergin_permission="reader")
        (self.tmpdir / "log-dbsync-pull-conflicts").write_text(json.dumps(GEODIFF_FILE))
        with mock.patch("supervisor.conflicts._column_names", return_value={}), \
                mock.patch("supervisor.conditions.alerts.notify", return_value=True) as notify:
            self.sup._collect_conflicts(self.project, self.sup._paths(self.project.pk))
        self.project.refresh_from_db()
        self.assertEqual(sorted(self.project.active_conditions),
                         ["content_divergence:1-punkter.gpkg:linjer", "content_divergence:1-punkter.gpkg:punkter"])
        self.assertEqual(self.project.health, "failed")
        self.assertEqual(Event.objects.filter(project=self.project, level="error").count(), 2)
        self.assertEqual(notify.call_count, 2)
