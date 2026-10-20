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

from unittest import mock

from django.test import override_settings
from django.utils import timezone as dj_tz

from . import mergin_api
from .check import (PENDING_KEY, Observed, direction_of, evaluate, gpkg_counts, gpkg_tables, is_transient_api_error,
                    select_tables, worst)
from .core import PS, Runtime, Supervisor

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


class FailedHealthTests(SimpleTestCase):
    def test_deterministic_condition_makes_health_failed(self):
        from .check import health_with_conditions

        self.assertEqual(health_with_conditions(H.OK, {"checkout_inconsistent": "2026-10-20T13:07:53+00:00"}), H.FAILED)
        self.assertEqual(health_with_conditions(H.BEHIND, {"database_missing_postgis": "x"}), H.FAILED)
        self.assertEqual(health_with_conditions(H.OK, {"process_crashed": "x"}), H.OK)
        self.assertEqual(health_with_conditions(H.OK, {}), H.OK)
        self.assertEqual(worst([H.FAILED, H.DIVERGED, H.OK]), H.FAILED)


class MerginUnreachableTests(SimpleTestCase):
    """Review note 2026-10-20: outages are a condition with a delayed mail,
    not an error per cycle; configuration errors stay errors."""

    def test_transient_versus_configuration_errors(self):
        for text in ("HTTP Error: 503 Service Unavailable", "upstream connect error or disconnect/reset before headers",
                     "Connection refused", "connection reset by peer", "The read operation timed out", "502 Bad Gateway",
                     "504 Gateway Time-out", "Temporary failure in name resolution"):
            self.assertTrue(is_transient_api_error(Exception(text)), text)
        for text in ("HTTP Error: 401 Unauthorized", "403 Forbidden", "404 Not Found", "Detail: project not found",
                     "something odd happened"):
            self.assertFalse(is_transient_api_error(Exception(text)), text)

        class ClientError(Exception):
            def __init__(self, code):
                super().__init__("error")
                self.http_error = code

        self.assertTrue(is_transient_api_error(ClientError(503)))
        self.assertFalse(is_transient_api_error(ClientError(404)))

    def test_unreachable_keeps_the_last_server_facts(self):
        h = Harness()
        h.check(observed(), 0)
        health, conds, _ = h.check(observed(server_version=None, mergin_permission=None,
                                            mergin_unreachable="Mergin API: 503"), 5)
        self.assertEqual(health, H.OK)
        self.assertEqual(conds, set())
        self.assertEqual(h.status.server_version, 10)
        self.assertEqual(h.status.direction, D.ONE_WAY)
        self.assertEqual(h.status.last_ok_check_at, at(5))


@override_settings(DBSYNC_CHECK_INTERVAL=INTERVAL, DBSYNC_STUCK_AFTER=STUCK_AFTER, DBSYNC_MERGIN_DOWN_ALERT_AFTER=900)
class MerginUnreachableFlowTests(SimpleTestCase):
    """The condition through _reconcile, registry writes stubbed."""

    def setUp(self):
        self.conditions = mock.patch("supervisor.core.conditions").start()
        self.alerts = mock.patch("supervisor.core.alerts").start()
        self.check = mock.patch("supervisor.core.check.run_project_check").start()
        mock.patch("supervisor.core.logtail.close_confirmed_pulls", return_value=0).start()
        mock.patch("supervisor.core.hooks.pending_requests", return_value=[]).start()
        mock.patch("supervisor.core.events").start()
        for target in ("_set_state", "_maybe_post_pull", "_project_db_version"):
            mock.patch.object(Supervisor, target).start()
        self.addCleanup(mock.patch.stopall)
        self.project = SyncProject(pk=3, mergin_project="ak/x", schema_prefix="x",
                                   desired_state=SyncProject.DesiredState.PAUSED, process_state=PS.STOPPED,
                                   health=H.OK, active_conditions={})
        conns = [mock.Mock(sync_file="1-punkter.gpkg")]
        mock.patch.object(SyncProject, "connections", new_callable=mock.PropertyMock,
                          return_value=mock.Mock(all=mock.Mock(return_value=conns))).start()
        self.supervisor = Supervisor(1)
        self.rt = Runtime()
        self.supervisor.runtimes[self.project.pk] = self.rt
        # The conditions module is a mock; keep the in-memory rows honest, and
        # answer the registry query for the group from them.
        self.all_projects = [self.project]
        key = "mergin_unreachable"

        def activate(project, condition, message, level=None, *, mail=True):
            project.active_conditions = {**(project.active_conditions or {}),
                                         condition: dj_tz.now().isoformat(timespec="seconds")}
            return True

        def resolve(project, condition, message="", *, mail=True):
            project.active_conditions = {k: v for k, v in (project.active_conditions or {}).items() if k != condition}
            return True

        self.conditions.activate.side_effect = activate
        self.conditions.resolve.side_effect = resolve
        mock.patch.object(Supervisor, "_projects_with",
                          side_effect=lambda key_, url: [p for p in self.all_projects if key_ in (p.active_conditions or {})]).start()

    def tick(self, down, projects=None):
        # A fresh tuple per call: core discards the key from the returned set.
        self.check.side_effect = lambda *a, **k: (H.OK, {"mergin_unreachable"} if down else set(),
                                                  {"mergin_unreachable": "Mergin API: 503"} if down else {})
        for project in projects or [self.project]:
            self.supervisor.runtimes[project.pk].last_check_at = 0
            self.supervisor._reconcile(project)

    def since(self, seconds_ago):
        self.project.active_conditions = {"mergin_unreachable": (dj_tz.now() - timedelta(seconds=seconds_ago)).isoformat(timespec="seconds")}

    def test_set_without_mail_then_mail_after_threshold_then_resolve_with_duration(self):
        self.tick(down=True)
        self.conditions.activate.assert_called_once()
        self.assertEqual(self.conditions.activate.call_args.kwargs["mail"], False)
        self.assertIn("unreachable", self.conditions.activate.call_args.args[2])
        self.conditions.alert.assert_not_called()
        # Still down, 100 s later: no mail yet.
        self.since(100)
        self.tick(down=True)
        self.conditions.alert.assert_not_called()
        # Down for 16 minutes: one mail, and only one.
        self.since(960)
        self.tick(down=True)
        self.tick(down=True)
        self.conditions.alert.assert_called_once()
        self.assertIn("16 min", self.conditions.alert.call_args.args[2])
        self.alerts.notify.assert_called_once()
        self.assertEqual(self.alerts.notify.call_args.kwargs["resolved"], False)
        # Back: resolved with the duration and the recovery mail.
        self.tick(down=False)
        self.conditions.resolve.assert_called_once()
        self.assertIn("reachable again after 16 min", self.conditions.resolve.call_args.args[2])
        self.assertEqual(self.alerts.notify.call_count, 2)
        self.assertEqual(self.alerts.notify.call_args.kwargs["resolved"], True)
        self.assertIsNone(self.rt.mergin_alerted)

    def test_short_outage_resolves_without_any_mail(self):
        self.tick(down=True)
        self.since(300)
        self.tick(down=False)
        self.conditions.alert.assert_not_called()
        self.alerts.notify.assert_not_called()
        self.alerts.notify_group.assert_not_called()
        self.assertIn("5 min 0 s", self.conditions.resolve.call_args.args[2])
        # Nothing active: a good check changes nothing.
        self.project.active_conditions = {}
        self.tick(down=False)
        self.conditions.resolve.assert_called_once()

    def test_adopted_mid_outage_assumes_the_mail_went_out(self):
        self.since(2000)
        self.tick(down=True)                 # a previous supervisor set it 33 min ago
        self.conditions.alert.assert_not_called()
        self.tick(down=False)
        self.alerts.notify.assert_called_once()
        self.assertEqual(self.alerts.notify.call_args.kwargs["resolved"], True)
        self.conditions.apply.assert_called()   # the check's own conditions are still managed

    def test_five_projects_checked_in_five_ticks_get_one_mail_each_way(self):
        # Checks are staggered: each project keeps the phase of its first
        # check, so the projects cross the threshold in different ticks.
        from registry.models import MerginAccount

        account = MerginAccount(pk=1, name="svc", url="https://app.merginmaps.com", username="svc")
        projects = []
        for pk in range(11, 16):
            since = (dj_tz.now() - timedelta(seconds=900 + 20 * pk)).isoformat(timespec="seconds")
            project = SyncProject(pk=pk, mergin_project=f"ak/p{pk}", schema_prefix=f"p{pk}", mergin_account=account,
                                  desired_state=SyncProject.DesiredState.PAUSED, process_state=PS.STOPPED,
                                  health=H.OK, active_conditions={"mergin_unreachable": since},
                                  notify_emails=["ops@example.org"])
            projects.append(project)
            self.supervisor.runtimes[pk] = Runtime(mergin_alerted=False)
        self.all_projects = projects
        # Tick 1: the first project past the threshold mails the whole group.
        self.tick(down=True, projects=[projects[0]])
        self.alerts.notify.assert_not_called()
        self.alerts.notify_group.assert_called_once()
        items = self.alerts.notify_group.call_args.args[0]
        self.assertEqual(sorted(p.mergin_project for p, _ in items), [f"ak/p{pk}" for pk in range(11, 16)])
        self.assertEqual(self.alerts.notify_group.call_args.kwargs["resolved"], False)
        self.assertEqual(self.conditions.alert.call_count, 5)          # one event per project
        self.assertTrue(all(self.supervisor.runtimes[pk].mergin_alerted for pk in range(11, 16)))
        # Ticks 2-5: the others cross the threshold; no more mail.
        for project in projects[1:]:
            self.tick(down=True, projects=[project])
        self.alerts.notify_group.assert_called_once()
        self.alerts.notify.assert_not_called()
        # Recovery, one project per tick: nothing until the last one clears.
        for project in projects[:-1]:
            self.tick(down=False, projects=[project])
        self.assertEqual(self.alerts.notify_group.call_count, 1)
        self.assertEqual(self.conditions.resolve.call_count, 4)
        self.tick(down=False, projects=[projects[-1]])
        self.assertEqual(self.alerts.notify_group.call_count, 2)
        self.assertEqual(self.alerts.notify_group.call_args.kwargs["resolved"], True)
        self.assertEqual(sorted(p.mergin_project for p, _ in self.alerts.notify_group.call_args.args[0]),
                         [f"ak/p{pk}" for pk in range(11, 16)])
        self.alerts.notify.assert_not_called()
        self.assertEqual(self.supervisor._mergin_recovered, {})

    def test_late_entrant_during_an_announced_outage_is_not_mailed_again(self):
        from registry.models import MerginAccount

        account = MerginAccount(pk=1, name="svc", url="https://app.merginmaps.com", username="svc")
        first = SyncProject(pk=21, mergin_project="ak/a", schema_prefix="a", mergin_account=account,
                            desired_state=SyncProject.DesiredState.PAUSED, process_state=PS.STOPPED, health=H.OK,
                            active_conditions={"mergin_unreachable": (dj_tz.now() - timedelta(seconds=1000)).isoformat(timespec="seconds")})
        late = SyncProject(pk=22, mergin_project="ak/b", schema_prefix="b", mergin_account=account,
                           desired_state=SyncProject.DesiredState.PAUSED, process_state=PS.STOPPED, health=H.OK,
                           active_conditions={})
        self.all_projects = [first, late]
        self.supervisor.runtimes[21] = Runtime(mergin_alerted=False)
        self.supervisor.runtimes[22] = Runtime(mergin_alerted=False)
        self.tick(down=True, projects=[first])
        self.alerts.notify.assert_called_once()                # alone at the time: the single-project mail
        self.tick(down=True, projects=[late])                   # joins the outage
        late.active_conditions = {"mergin_unreachable": (dj_tz.now() - timedelta(seconds=1000)).isoformat(timespec="seconds")}
        self.tick(down=True, projects=[late])                   # crosses the threshold later
        self.alerts.notify.assert_called_once()
        self.alerts.notify_group.assert_not_called()
        self.assertTrue(self.supervisor.runtimes[22].mergin_alerted)
        # Both clear: one recovery mail naming both.
        self.tick(down=False, projects=[first])
        self.assertEqual(self.alerts.notify.call_count, 1)
        self.tick(down=False, projects=[late])
        self.alerts.notify_group.assert_called_once()
        self.assertEqual(len(self.alerts.notify_group.call_args.args[0]), 2)


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
