"""
Rebase conflicts (slice 17, A) without a database: geodiff's JSON into the
event text, the move-and-keep-50 rule, the collection step with its
two-way (warning, optional mail) and one-way (condition) outcomes, and
the supervisor calling it from the observe step and before a start.
"""

import json
import tempfile
from datetime import datetime
from pathlib import Path
from unittest import mock

from django.test import SimpleTestCase, override_settings
from django.utils import timezone

from registry.models import Database, MerginAccount, SyncConnection, SyncStatus

from . import conflicts
from .core import Runtime, Supervisor
from .tests import StubChild, running_project

GEODIFF_FILE = {
    "geodiff": [
        {"table": "punkter", "type": "conflict", "fid": "17",
         "changes": [{"column": 3, "base": "a", "old": "field", "new": "db"}]},
        {"table": "punkter", "type": "conflict", "fid": "41",
         "changes": [{"column": 3, "base": "x", "old": "y", "new": "z"},
                     {"column": 4, "base": 1, "old": 2, "new": 3}]},
        {"table": "punkter", "type": "conflict", "fid": "50",
         "changes": [{"column": 3, "base": "k", "new": "updated in db"}]},   # the field deleted the row
        {"table": "linjer", "type": "conflict", "fid": "2",
         "changes": [{"column": 1, "base": None, "old": "s", "new": "d"}]},
    ]
}


class ParseTests(SimpleTestCase):
    def test_parse_and_summarize(self):
        parsed = conflicts.parse(json.dumps(GEODIFF_FILE))
        self.assertEqual([(c.table, c.fid, c.deleted_in_field) for c in parsed],
                         [("punkter", "17", False), ("punkter", "41", False), ("punkter", "50", True), ("linjer", "2", False)])
        self.assertEqual(parsed[0].columns, [{"column": 3, "base": "a", "field": "field", "database": "db"}])
        summary = conflicts.summarize(parsed)
        self.assertEqual(list(summary), ["linjer", "punkter"])
        self.assertEqual(summary["punkter"], {"count": 3, "fids": ["17", "41", "50"], "fids_deleted": ["50"],
                                              "deleted_in_field": 1, "columns": [3, 4]})
        self.assertEqual(conflicts.parse("not json"), [])
        self.assertEqual(conflicts.parse('{"geodiff": []}'), [])

    def test_describe_names_columns_and_lists_fids(self):
        summary = conflicts.summarize(conflicts.parse(json.dumps(GEODIFF_FILE)))
        conflicts.name_columns(summary, {"punkter": ["fid", "uuid", "geom", "namn", "typ"]})
        text = conflicts.describe(summary, "20261027T101502-1-punkter.gpkg.json")
        # The direction as the two-way test found it (2026-10-26e): the database wins.
        self.assertIn("1 field value in linjer (column #1) overwritten by the database value, fid 2", text)
        self.assertIn("2 field values in punkter (namn, typ) overwritten by the database values, fid 17, 41", text)
        self.assertIn("1 database update of row deleted in the field in punkter dropped with the row, fid 50", text)
        self.assertTrue(text.startswith("rebase: "))
        self.assertIn("The overwritten field values are in conflicts/20261027T101502-1-punkter.gpkg.json", text)
        self.assertNotIn("server", text)
        many = conflicts.summarize([conflicts.Conflict("t", str(i)) for i in range(20)])
        self.assertIn("fid 0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11 and 8 more", conflicts.describe(many, "x.json"))
        only_deleted = conflicts.summarize([conflicts.Conflict("t", "9", deleted_in_field=True)])
        self.assertTrue(conflicts.describe(only_deleted, "x.json").endswith(". Record: conflicts/x.json"))


class FilesTests(SimpleTestCase):
    def test_save_names_by_time_and_label_and_keeps_fifty(self):
        with tempfile.TemporaryDirectory() as tmp:
            work = Path(tmp)
            (work / "tmp").mkdir()
            for i in range(53):
                path = work / "tmp" / "p-dbsync-pull-conflicts"
                path.write_text(json.dumps(GEODIFF_FILE))
                at = timezone.make_aware(datetime(2026, 9, 1, 8, 0, i), timezone.get_default_timezone())
                target = conflicts.save(path, work, "1-punkter.gpkg", at=at)
                self.assertFalse(path.exists())
                self.assertTrue(conflicts.safe_name(target.name))
            saved = conflicts.list_saved(work)
            self.assertEqual(len(saved), conflicts.KEEP)
            self.assertTrue(saved[0]["name"].endswith("-1-punkter.gpkg.json"))
            self.assertEqual(saved[0]["name"][:15], "20260901T080052")       # newest first
            self.assertEqual(saved[-1]["name"][:15], "20260901T080003")      # the three oldest are gone
            self.assertEqual(saved[0]["connection"], "1-punkter.gpkg")
            self.assertEqual(saved[0]["count"], 4)
            self.assertEqual(saved[0]["tables"]["punkter"]["fids"], ["17", "41", "50"])
            self.assertEqual(saved[0]["tables"]["punkter"]["deleted_in_field"], 1)
            self.assertEqual(saved[0]["tables"]["punkter"]["deleted_on_server"], 1)   # v1 alias, kept
            overview = conflicts.overview(work)
            self.assertEqual(overview["last_at"].strftime("%Y%m%dT%H%M%S"), "20260901T080052")
            self.assertEqual(overview["count_30d"], 0)                        # 2026-09-01 is older than 30 days
            # Two files in one second: a suffix, never an overwrite.
            path = work / "tmp" / "p-dbsync-pull-conflicts"
            path.write_text("{}")
            self.assertTrue(conflicts.save(path, work, "1-punkter.gpkg", at=at).name.endswith("-1-punkter.gpkg-2.json"))
            self.assertFalse(conflicts.safe_name("../etc/passwd"))
            self.assertEqual(conflicts.pending_files(work / "nope"), [])


class CollectTests(SimpleTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.work = Path(self.tmp.name)
        (self.work / "tmp").mkdir()
        self.project = running_project()
        self.conn = SyncConnection(pk=3, project=self.project, sync_file="1-punkter.gpkg",
                                   modified_schema="y_s_punkter", base_schema="y_b_punkter")
        self.events = mock.patch("supervisor.conflicts.events").start()
        self.conditions = mock.patch("supervisor.conflicts.conditions").start()
        self.alerts = mock.patch("supervisor.conflicts.alerts").start()
        mock.patch("supervisor.conflicts._column_names", return_value={"punkter": ["fid", "uuid", "geom", "namn", "typ"]}).start()
        self.addCleanup(mock.patch.stopall)

    def write(self, data=GEODIFF_FILE):
        path = self.work / "tmp" / "y-dbsync-pull-conflicts"
        path.write_text(json.dumps(data))
        return path

    def test_two_way_conflict_is_a_warning_event_and_the_file_is_kept(self):
        self.write()
        with mock.patch("supervisor.conflicts._connection_for", return_value=(self.conn, SyncStatus.Direction.TWO_WAY)):
            saved = conflicts.collect(self.project, self.work / "tmp", self.work)
        self.assertEqual(len(saved), 1)
        self.assertEqual(conflicts.pending_files(self.work / "tmp"), [])
        self.assertTrue(saved[0]["path"].name.endswith("-1-punkter.gpkg.json"))
        self.events.warning.assert_called_once()
        message = self.events.warning.call_args.args[1]
        self.assertIn("2 field values in punkter (namn, typ) overwritten by the database values, fid 17, 41", message)
        self.assertIn("deleted in the field in punkter dropped with the row, fid 50", message)
        self.conditions.activate.assert_not_called()
        self.alerts.notify.assert_not_called()                 # mail_on_conflict off
        # Empty TMPDIR: nothing happens.
        self.assertEqual(conflicts.collect(self.project, self.work / "tmp", self.work), [])

    def test_mail_on_conflict_sends_one_mail_per_file(self):
        self.project.mail_on_conflict = True
        self.write()
        with mock.patch("supervisor.conflicts._connection_for", return_value=(self.conn, SyncStatus.Direction.TWO_WAY)):
            conflicts.collect(self.project, self.work / "tmp", self.work)
        self.alerts.notify.assert_called_once()
        self.assertEqual(self.alerts.notify.call_args.args[1], "rebase_conflict")

    def test_one_way_rebase_is_the_content_divergence_condition(self):
        self.write()
        with mock.patch("supervisor.conflicts._connection_for", return_value=(self.conn, SyncStatus.Direction.ONE_WAY)):
            conflicts.collect(self.project, self.work / "tmp", self.work)
        self.events.warning.assert_not_called()
        keys = sorted(call.args[1] for call in self.conditions.activate.call_args_list)
        self.assertEqual(keys, ["content_divergence:1-punkter.gpkg:linjer", "content_divergence:1-punkter.gpkg:punkter"])
        self.assertEqual(self.conditions.activate.call_args.kwargs["level"], "error")
        self.assertIn("one-way project", self.conditions.activate.call_args.args[2])

    def test_unknown_connection_uses_the_project_name_and_empty_files_are_dropped(self):
        self.write({"geodiff": []})
        with mock.patch("supervisor.conflicts._connection_for", return_value=(None, None)):
            self.assertEqual(conflicts.collect(self.project, self.work / "tmp", self.work), [])
        self.assertEqual(conflicts.pending_files(self.work / "tmp"), [])
        self.write()
        with mock.patch("supervisor.conflicts._connection_for", return_value=(None, None)):
            saved = conflicts.collect(self.project, self.work / "tmp", self.work)
        self.assertTrue(saved[0]["path"].name.endswith("-y.json"))
        self.events.warning.assert_called_once()


class SupervisorStepTests(SimpleTestCase):
    """The collection runs from the observe step every tick and before a
    start, which empties TMPDIR (processes.private_tmpdir)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        override = override_settings(DBSYNC_DATA_DIR=Path(self.tmp.name))
        override.enable()
        self.addCleanup(override.disable)
        for target in (mock.patch.object(Supervisor, "_maybe_check"),
                       mock.patch.object(Supervisor, "_still_wanted", return_value=True),
                       mock.patch.object(Supervisor, "_pump_log"),
                       mock.patch("supervisor.core.hooks.pending_requests", return_value=[]),
                       mock.patch("supervisor.core.events")):
            target.start()
            self.addCleanup(target.stop)

    @mock.patch("supervisor.core.conflicts.collect")
    def test_observe_collects_every_tick(self, collect):
        project = running_project()
        supervisor = Supervisor(tick_seconds=1)
        supervisor.runtimes[project.pk] = Runtime(child=StubChild())
        supervisor._reconcile(project)
        collect.assert_called_once()
        self.assertEqual(collect.call_args.args[1], project.work_dir / "tmp")
        self.assertEqual(collect.call_args.args[2], project.work_dir)

    @mock.patch("supervisor.core.processes.spawn", side_effect=AssertionError("stop here"))
    @mock.patch("supervisor.core.render_config", return_value="x")
    @mock.patch("supervisor.core.write_config")
    @mock.patch.object(Supervisor, "_set_state")
    def test_start_collects_before_emptying_tmpdir(self, set_state, write_config, render, spawn):
        project = running_project()
        project.mergin_account = MerginAccount(name="svc", username="svc")
        project.database = Database(name="d", host="h", dbname="x", user="u")
        tmpdir = project.work_dir / "tmp"
        tmpdir.mkdir(parents=True)
        (tmpdir / "y-dbsync-pull-conflicts").write_text(json.dumps(GEODIFF_FILE))
        (tmpdir / "leftover.diff").write_text("x")
        supervisor = Supervisor(tick_seconds=1)
        with mock.patch("supervisor.conflicts._connection_for", return_value=(None, None)), \
                mock.patch("supervisor.conflicts.events"), \
                self.assertRaisesRegex(AssertionError, "stop here"):
            supervisor._start(project, Runtime(), 0.0)
        self.assertEqual(len(conflicts.list_saved(project.work_dir)), 1)   # saved before the emptying
        self.assertFalse((tmpdir / "leftover.diff").exists())             # and TMPDIR was emptied as before
