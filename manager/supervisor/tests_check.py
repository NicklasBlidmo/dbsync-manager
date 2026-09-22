"""
Consistency check and health rules (design §6, §13 unit). Pure evaluation
over in-memory status rows; the GeoPackage reader against a real SQLite
file. No registry database needed.
"""

import sqlite3
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from django.test import SimpleTestCase

from registry.models import SyncProject, SyncStatus

from . import mergin_api
from .check import PENDING_KEY, Observed, direction_of, evaluate, gpkg_counts, gpkg_tables, select_tables, worst

H = SyncProject.Health
D = SyncStatus.Direction
INTERVAL = 300
STUCK_AFTER = 1800
T0 = datetime(2026, 9, 22, 12, 0, tzinfo=timezone.utc)


def at(minutes):
    return T0 + timedelta(minutes=minutes)


def observed(**kw):
    base = dict(
        server_version=10, local_version=10, db_version=10,
        gpkg_count={"punkter": 100}, base_count={"punkter": 100}, modified_count={"punkter": 100},
        mergin_permission="reader", db_writers=[],
    )
    base.update(kw)
    return Observed(**base)


class Harness:
    """Runs successive checks against one status row."""

    def __init__(self, process_running=True):
        self.status = SyncStatus()
        self.process_running = process_running

    def check(self, obs, minutes):
        return evaluate(
            self.status, obs, now=at(minutes), check_interval=INTERVAL, stuck_after=STUCK_AFTER,
            process_running=self.process_running, label="1-punkter.gpkg",
        )


class HealthTests(SimpleTestCase):
    def test_in_sync_is_ok_and_says_nothing(self):
        h = Harness()
        health, conds, _ = h.check(observed(), 0)
        self.assertEqual(health, H.OK)
        self.assertEqual(conds, set())
        self.assertEqual(h.status.direction, D.ONE_WAY)
        self.assertEqual(h.status.last_ok_check_at, at(0))

    def test_one_count_mismatch_is_ignored_two_raise_divergence(self):
        h = Harness()
        h.check(observed(), 0)
        health, conds, _ = h.check(observed(base_count={"punkter": 99}), 5)
        self.assertEqual(health, H.OK)
        self.assertEqual(conds, set())
        self.assertEqual(h.status.mismatch_streak["punkter"], 1)
        health, conds, msgs = h.check(observed(base_count={"punkter": 99}), 10)
        self.assertEqual(health, H.DIVERGED)
        self.assertEqual(conds, {"divergence:1-punkter.gpkg:punkter"})
        self.assertIn("100 rows in the GeoPackage but 99", msgs["divergence:1-punkter.gpkg:punkter"])
        # Converging resets the streak and the condition.
        health, conds, _ = h.check(observed(), 15)
        self.assertEqual((health, conds), (H.OK, set()))
        self.assertEqual(h.status.mismatch_streak["punkter"], 0)

    def test_counts_are_not_compared_while_versions_differ(self):
        h = Harness()
        h.check(observed(), 0)
        # Window between file pull and database apply: local ahead, counts differ.
        h.check(observed(local_version=11, gpkg_count={"punkter": 101}), 5)
        h.check(observed(local_version=11, gpkg_count={"punkter": 101}), 10)
        self.assertEqual(h.status.mismatch_streak["punkter"], 0)

    def test_interrupted_pull_raises_db_behind_after_two_intervals(self):
        h = Harness()
        h.check(observed(), 0)
        # The POC incident: checkout advanced, comment did not, modified differs from base.
        stuck = observed(local_version=11, server_version=11, modified_count={"punkter": 100}, base_count={"punkter": 100})
        health, conds, _ = h.check(stuck, 5)
        self.assertEqual(health, H.CATCHING_UP)     # within the pull window
        self.assertEqual(conds, set())
        health, conds, msgs = h.check(observed(local_version=11, server_version=11, modified_count={"punkter": 101}), 16)
        self.assertEqual(health, H.BEHIND)
        self.assertEqual(conds, {"db_behind:1-punkter.gpkg"})
        self.assertIn("interrupted pull", msgs["db_behind:1-punkter.gpkg"])
        self.assertIn("must not be pushed", msgs["db_behind:1-punkter.gpkg"])
        # The pull applies: resolved, and last_pull_at recorded.
        health, conds, _ = h.check(observed(local_version=11, server_version=11, db_version=11), 21)
        self.assertEqual((health, conds), (H.OK, set()))
        self.assertEqual(h.status.last_pull_at, at(21))

    def test_stranded_edits_need_reader_writers_and_two_checks(self):
        h = Harness()
        pending = observed(db_writers=["qgis_editor"], modified_count={"punkter": 102})
        health, conds, _ = h.check(pending, 0)
        self.assertEqual(h.status.direction, D.DB_EDITS_CANNOT_PUSH)
        self.assertEqual(conds, set())
        self.assertEqual(h.status.mismatch_streak[PENDING_KEY], 1)
        health, conds, msgs = h.check(pending, 5)
        self.assertEqual(health, H.DIVERGED)
        self.assertEqual(conds, {"stranded_db_edits:1-punkter.gpkg"})
        self.assertIn("qgis_editor", msgs["stranded_db_edits:1-punkter.gpkg"])
        # Same pending edits with a Writer account are normal two-way.
        h2 = Harness()
        writer = observed(mergin_permission="writer", modified_count={"punkter": 102})
        h2.check(writer, 0)
        health, conds, _ = h2.check(writer, 5)
        self.assertEqual((health, conds), (H.OK, set()))
        self.assertEqual(h2.status.direction, D.TWO_WAY)

    def test_stuck_only_when_running_and_after_stuck_after(self):
        h = Harness(process_running=True)
        h.check(observed(), 0)
        ahead = observed(server_version=12)
        health, conds, _ = h.check(ahead, 5)
        self.assertEqual((health, conds), (H.CATCHING_UP, set()))
        health, conds, _ = h.check(ahead, 25)
        self.assertEqual((health, conds), (H.CATCHING_UP, set()))
        health, conds, msgs = h.check(ahead, 31)
        self.assertEqual((health, conds), (H.STUCK, {"stuck"}))
        self.assertIn("v12", msgs["stuck"])
        # Progress clears it.
        health, conds, _ = h.check(observed(server_version=12, local_version=12, db_version=12), 36)
        self.assertEqual((health, conds), (H.OK, set()))

    def test_stuck_is_not_evaluated_while_paused(self):
        h = Harness(process_running=False)
        h.check(observed(), 0)
        health, conds, _ = h.check(observed(server_version=12), 60)
        self.assertEqual((health, conds), (H.CATCHING_UP, set()))

    def test_idle_project_never_alarms(self):
        h = Harness()
        for minutes in range(0, 24 * 60, 5):
            health, conds, _ = h.check(observed(), minutes)
            self.assertEqual((health, conds), (H.OK, set()))

    def test_failed_check_keeps_previous_result_then_goes_stale(self):
        h = Harness()
        h.check(observed(), 0)
        health, conds, _ = h.check(observed(error="Mergin API: timeout"), 5)
        self.assertIsNone(health)                    # caller keeps the previous health
        self.assertEqual(conds, set())
        self.assertEqual(h.status.check_error, "Mergin API: timeout")
        self.assertEqual(h.status.server_version, 10)  # previous values kept
        health, conds, msgs = h.check(observed(error="Mergin API: timeout"), 16)
        self.assertEqual((health, conds), (H.STALE, {"check_failed"}))
        self.assertIn("failing since", msgs["check_failed"])

    def test_never_succeeded_is_unknown(self):
        h = Harness()
        health, conds, _ = h.check(observed(error="database: refused"), 0)
        self.assertEqual((health, conds), (H.UNKNOWN, set()))

    def test_direction_mapping(self):
        self.assertEqual(direction_of("reader", []), D.ONE_WAY)
        self.assertEqual(direction_of("reader", ["x"]), D.DB_EDITS_CANNOT_PUSH)
        self.assertEqual(direction_of("writer", ["x"]), D.TWO_WAY)
        self.assertEqual(direction_of("owner", []), D.TWO_WAY)
        self.assertIsNone(direction_of(None, []))

    def test_worst_health(self):
        self.assertEqual(worst([H.OK, H.CATCHING_UP]), H.CATCHING_UP)
        self.assertEqual(worst([H.OK, H.STUCK, H.BEHIND]), H.BEHIND)
        self.assertEqual(worst([H.DIVERGED, H.BEHIND]), H.DIVERGED)
        self.assertEqual(worst([None, H.OK]), H.OK)
        self.assertEqual(worst([]), H.UNKNOWN)


class GeoPackageTests(SimpleTestCase):
    def test_tables_and_counts_from_gpkg_contents(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "1-punkter.gpkg"
            conn = sqlite3.connect(path)
            try:
                conn.execute("CREATE TABLE gpkg_contents (table_name TEXT, data_type TEXT)")
                conn.executemany("INSERT INTO gpkg_contents VALUES (?, ?)", [
                    ("punkter", "features"), ("attributes_tbl", "attributes"), ("tiles", "tiles"),
                ])
                conn.execute('CREATE TABLE punkter (fid INTEGER)')
                conn.executemany("INSERT INTO punkter VALUES (?)", [(i,) for i in range(7)])
                conn.execute('CREATE TABLE attributes_tbl (fid INTEGER)')
                conn.commit()
            finally:
                conn.close()
            tables = gpkg_tables(path)
            self.assertEqual(tables, ["attributes_tbl", "punkter"])
            self.assertEqual(gpkg_counts(path, tables), {"attributes_tbl": 0, "punkter": 7})
            self.assertEqual(select_tables(tables, skip=["attributes_tbl"], include=[]), ["punkter"])
            self.assertEqual(select_tables(tables, skip=[], include=["punkter"]), ["punkter"])


class MerginApiParsingTests(SimpleTestCase):
    def test_version_and_permission(self):
        self.assertEqual(mergin_api.version_number({"version": "v4213"}), 4213)
        self.assertIsNone(mergin_api.version_number({"version": "x"}))
        self.assertIsNone(mergin_api.version_number({}))
        self.assertEqual(mergin_api.permission({"role": "Reader"}), "reader")
        self.assertEqual(mergin_api.permission({"role": "editor"}), "writer")
        self.assertEqual(mergin_api.permission({"permissions": {"upload": True}}), "writer")
        self.assertEqual(mergin_api.permission({"permissions": {"upload": False}}), "reader")
        self.assertIsNone(mergin_api.permission({}))

    def test_project_list_items_use_access_names_before_flags(self):
        # The paginated project list has no `role`; an owner would otherwise
        # show as writer (field finding 2026-09-22 on NicklasBlidmo/p3922_test).
        item = {
            "permissions": {"upload": True},
            "access": {"ownersnames": ["nicke"], "writersnames": ["anna"], "readersnames": ["bo"]},
        }
        self.assertEqual(mergin_api.permission(item, "nicke"), "owner")
        self.assertEqual(mergin_api.permission(item, "anna"), "writer")
        self.assertEqual(mergin_api.permission(item, "bo"), "reader")
        self.assertEqual(mergin_api.permission(item, "workspace-owner-not-listed"), "writer")  # flags fallback
        self.assertEqual(mergin_api.permission(item), "writer")
        self.assertEqual(mergin_api.permission_with_source(item, "nicke"), ("owner", "access"))
        self.assertEqual(mergin_api.permission_with_source(item, "someone"), ("writer", "flags"))
        self.assertEqual(mergin_api.permission_with_source({"role": "owner"}, "x"), ("owner", "role"))


class BestEffortWritersTests(SimpleTestCase):
    def test_writers_failure_does_not_fail_the_check(self):
        h = Harness()
        health, conds, _ = h.check(observed(db_writers=None, warning="writers: ACL arrays must be one-dimensional"), 0)
        self.assertEqual((health, conds), (H.OK, set()))
        self.assertEqual(h.status.server_version, 10)
        self.assertEqual(h.status.mergin_permission, "reader")
        self.assertIsNone(h.status.db_writers)
        self.assertIsNone(h.status.direction)            # Reader with unknown writers: no verdict
        self.assertEqual(h.status.check_error, "writers: ACL arrays must be one-dimensional")
        self.assertEqual(h.status.last_ok_check_at, at(0))
        # With writers known again the note clears and the verdict returns.
        health, _, _ = h.check(observed(), 5)
        self.assertEqual(h.status.check_error, "")
        self.assertEqual(h.status.direction, D.ONE_WAY)

    def test_owner_is_two_way_even_with_unknown_writers(self):
        self.assertEqual(direction_of("owner", None), D.TWO_WAY)
        self.assertIsNone(direction_of("reader", None))
