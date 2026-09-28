"""
Pull history grouping and the relative-time filter (slice 10 brief). Pure.
"""

from datetime import datetime, timedelta, timezone as tz
from unittest import mock

from django.test import SimpleTestCase, override_settings

from registry.models import SyncRun

from .runs import group_runs
from .templatetags.ui_extras import ago, isotime

T0 = datetime(2026, 9, 28, 11, 16, 0, tzinfo=tz.utc)


def run(kind="pull", version=11, seconds=0, ok=True, line="--- pull finished -- at version v11"):
    return SyncRun(kind=kind, checkout_version=version, started_at=T0 + timedelta(seconds=seconds), ok=ok,
                   summary={"line": line})


class GroupRunsTests(SimpleTestCase):
    def test_eight_uniform_rows_make_one_applied_group(self):
        runs = [run(seconds=8 - i) for i in range(8)]          # newest first, one second apart
        groups = group_runs(runs, window_seconds=60, total_connections=8)
        self.assertEqual(len(groups), 1)
        g = groups[0]
        self.assertEqual((g["kind"], g["version"], g["state"], g["files"]), ("pull", 11, "applied", "8 of 8"))
        self.assertTrue(g["uniform"])
        self.assertFalse(g["expanded"])
        self.assertEqual(g["started"], T0 + timedelta(seconds=1))   # the first of the group

    def test_one_failure_makes_the_group_failed_and_expanded(self):
        runs = [run(seconds=8 - i, ok=(i != 3)) for i in range(8)]
        (g,) = group_runs(runs, window_seconds=60, total_connections=8)
        self.assertEqual(g["state"], "failed")
        self.assertEqual(g["state_class"], "bad")
        self.assertFalse(g["uniform"])
        self.assertTrue(g["expanded"])
        self.assertEqual([r["state"] for r in g["rows"]].count("failed"), 1)

    def test_versions_and_kinds_split_groups_and_the_window_applies(self):
        runs = [
            run(version=12, seconds=200), run(version=12, seconds=199),          # v12 pull
            run(kind="push", version=12, seconds=150, ok=True),                 # a push
            run(version=11, seconds=100, ok=None), run(version=11, seconds=99, ok=None),   # v11, unconfirmed
            run(version=11, seconds=0),                                          # v11 but 99 s earlier: own group
        ]
        groups = group_runs(runs, window_seconds=60, total_connections=2)
        self.assertEqual([(g["kind"], g["version"], g["state"], g["files"]) for g in groups], [
            ("pull", 12, "applied", "2 of 2"),
            ("push", 12, "ok", "1 of 2"),
            ("pull", 11, "checkout only", "2 of 2"),
            ("pull", 11, "applied", "1 of 2"),
        ])

    def test_limit_counts_groups_not_rows(self):
        runs = [run(version=100 - i, seconds=1000 - 10 * i) for i in range(30)]
        self.assertEqual(len(group_runs(runs, window_seconds=5, total_connections=1, limit=20)), 20)


@override_settings(TIME_ZONE="UTC")   # the container may run with TZ from .env; these assert UTC
class AgoFilterTests(SimpleTestCase):
    def test_boundaries(self):
        now = datetime(2026, 9, 28, 12, 0, 0, tzinfo=tz.utc)
        with mock.patch("django.utils.timezone.now", return_value=now):
            self.assertEqual(ago(now - timedelta(seconds=59)), "59 s ago")
            self.assertEqual(ago(now - timedelta(seconds=60)), "1 min ago")
            self.assertEqual(ago(now - timedelta(minutes=61)), "1 h ago")
            self.assertEqual(ago(now - timedelta(hours=23, minutes=59)), "23 h ago")
            self.assertEqual(ago(now - timedelta(hours=25)), "2026-09-27 11:00")   # UTC == local in tests
            self.assertEqual(ago(None), "")
            self.assertEqual(ago((now - timedelta(minutes=3)).isoformat()), "3 min ago")   # ISO strings too
            # Naive strings (db-sync's log lines, UTC) went through django's
            # timezone.utc, removed in Django 5.0: a 500 on every project page
            # in the field (2026-09-28); the Windows run never hit it.
            self.assertEqual(ago("2026-09-28T11:57:00"), "3 min ago")

    def test_isotime_accepts_naive_and_aware_strings(self):
        self.assertEqual(isotime("2026-09-28T11:57:03"), "11:57:03")
        self.assertEqual(isotime("2026-09-28T11:57:03+00:00"), "11:57:03")
        self.assertEqual(isotime("not a time"), "not a time")
        self.assertEqual(isotime(""), "")
