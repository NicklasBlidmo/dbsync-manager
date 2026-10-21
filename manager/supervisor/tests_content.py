"""
Content divergence base <-> modified (slice 15, A): the statement, its
shaping, the skip rule and the sample cap without a database; the
evaluation rules (one-way raises, two-way shows); and, tagged `db`, the
comparison against the real PostgreSQL of the test profile.
"""

import sqlite3
import tempfile
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path

from django.db import connection
from django.test import SimpleTestCase, TestCase, tag

from registry.models import SyncProject, SyncStatus

from . import content
from .check import Observed, evaluate

H = SyncProject.Health
T0 = datetime(2026, 10, 21, 8, 0, tzinfo=timezone.utc)


class SqlTests(SimpleTestCase):
    def test_tables_to_compare_skips_count_differences_and_missing_tables(self):
        tables = ["a", "b", "c", "d"]
        self.assertEqual(content.tables_to_compare(tables, {"a": 5, "b": 5, "c": 1}, {"a": 5, "b": 6, "d": 1}), ["a"])

    def test_plan_needs_the_key_and_finds_geometry_and_uuid(self):
        base = [("fid", "int8"), ("name", "text"), ("uuid", "uuid"), ("geom", "geometry")]
        modified = [("fid", "int8"), ("name", "text"), ("uuid", "uuid"), ("geom", "geometry"), ("extra", "text")]
        spec = content.plan("punkter", "fid", base, modified)
        self.assertEqual(spec["columns"], ["name", "uuid", "geom"])   # extra is not in both
        self.assertEqual(spec["geometry"], ["geom"])
        self.assertEqual(spec["uuid"], "uuid")
        self.assertIsNone(content.plan("punkter", "id", base, modified))

    def test_diff_sql_joins_on_the_key_and_compares_geometry_as_ewkb(self):
        spec = {"table": "pun\"kter", "key": "fid", "columns": ["name", "geom"], "geometry": ["geom"], "uuid": "uuid"}
        sql = content.diff_sql("p_b", "p_m", spec)
        self.assertIn('FROM "p_b"."pun""kter" b FULL OUTER JOIN "p_m"."pun""kter" m ON b."fid" = m."fid"', sql)
        self.assertIn('b."name" IS DISTINCT FROM m."name"', sql)
        self.assertIn('ST_AsEWKB(b."geom") IS DISTINCT FROM ST_AsEWKB(m."geom")', sql)
        self.assertIn('COALESCE(m."uuid", b."uuid")::text AS uuid', sql)
        self.assertIn("count(*) FILTER (WHERE kind = 'changed') OVER ()", sql)
        self.assertTrue(sql.rstrip().endswith(f"LIMIT {content.SAMPLE_CAP}"))
        self.assertIn("CASE WHEN ST_AsEWKB", sql)
        # No columns beyond the key: the join still finds rows on one side only.
        sql = content.diff_sql("p_b", "p_m", {"table": "t", "key": "fid", "columns": [], "geometry": [], "uuid": None})
        self.assertIn("OR (FALSE)", sql)
        self.assertIn("NULL::text AS uuid", sql)

    def test_shape_counts_from_the_window_totals_and_keeps_the_sample(self):
        self.assertEqual(content.shape([]), {"only_base": 0, "only_modified": 0, "changed": 0, "sample": []})
        rows = [("changed", "3", "u3", ["name"], 1, 2, 3), ("only_base", "7", None, [], 1, 2, 3)]
        result = content.shape(rows)
        self.assertEqual((result["only_base"], result["only_modified"], result["changed"]), (1, 2, 3))
        self.assertEqual(result["sample"][0], {"key": "3", "uuid": "u3", "kind": "changed", "columns": ["name"]})
        self.assertEqual(content.total(result), 6)
        self.assertEqual(content.summarize(result), "3 changed, 2 only in modified, 1 only in base")

    def test_gpkg_primary_key_and_geometry_columns(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "t.gpkg"
            with closing(sqlite3.connect(path)) as conn:
                conn.execute("CREATE TABLE punkter (fid INTEGER PRIMARY KEY, name TEXT, geom BLOB)")
                conn.execute("CREATE TABLE lookup (id INTEGER PRIMARY KEY, label TEXT)")
                conn.execute("CREATE TABLE nokey (a TEXT)")
                conn.execute("CREATE TABLE gpkg_geometry_columns (table_name TEXT, column_name TEXT, geometry_type_name TEXT, srs_id INTEGER, z INTEGER, m INTEGER)")
                conn.execute("INSERT INTO gpkg_geometry_columns VALUES ('punkter', 'geom', 'POINT', 3006, 1, 0)")
                conn.commit()
            keys = content.gpkg_primary_keys(path, ["punkter", "lookup", "nokey"])
            self.assertEqual(keys, {"punkter": "fid", "lookup": "id", "nokey": "fid"})
            self.assertEqual(content.gpkg_geometry_columns(path), {"punkter": "geom"})


def observed(direction_permission="reader", writers=None, **kw):
    base = dict(server_version=10, local_version=10, db_version=10,
                gpkg_count={"punkter": 100}, base_count={"punkter": 100}, modified_count={"punkter": 100},
                mergin_permission=direction_permission, db_writers=writers or [], content_checked=True,
                content_diff={}, tables=[{"name": "punkter", "geometry_column": "geom"}])
    base.update(kw)
    return Observed(**base)


class EvaluateTests(SimpleTestCase):
    def check(self, status, obs, minutes=0):
        return evaluate(status, obs, now=T0 + timedelta(minutes=minutes), check_interval=300, stuck_after=1800,
                        process_running=True, label="1-punkter.gpkg")

    def test_one_way_project_raises_the_condition_at_error_level_two_way_only_shows(self):
        diff = {"punkter": {"only_base": 0, "only_modified": 0, "changed": 2, "sample": []}}
        status = SyncStatus()
        health, conds, msgs = self.check(status, observed(content_diff=diff))
        self.assertEqual(health, H.DIVERGED)
        self.assertEqual(conds, {"content_divergence:1-punkter.gpkg:punkter"})
        self.assertIn("2 changed", msgs["content_divergence:1-punkter.gpkg:punkter"])
        self.assertEqual(status.content_diff, diff)
        self.assertEqual(status.content_checked_at, T0)
        self.assertEqual(status.tables, [{"name": "punkter", "geometry_column": "geom"}])
        # Reader with other writers: the edits can never be pushed, same condition.
        status = SyncStatus()
        health, conds, _ = self.check(status, observed(writers=["editor"], content_diff=diff))
        self.assertEqual(conds, {"content_divergence:1-punkter.gpkg:punkter"})
        # Two-way: normal between pushes.
        status = SyncStatus()
        health, conds, _ = self.check(status, observed(direction_permission="writer", content_diff=diff))
        self.assertEqual((health, conds), (H.OK, set()))
        self.assertEqual(status.content_diff, diff)

    def test_unchecked_content_keeps_the_previous_result_and_raises_nothing(self):
        status = SyncStatus(content_diff={"punkter": {"only_base": 1, "only_modified": 0, "changed": 0, "sample": []}})
        health, conds, _ = self.check(status, observed(content_checked=False, content_diff={}))
        self.assertEqual(conds, set())
        self.assertEqual(status.content_diff["punkter"]["only_base"], 1)   # untouched, not re-raised


@tag("db")
class PostgresCompareTests(TestCase):
    """The statement against a real PostgreSQL: attributes, a uuid, and a
    geometry when PostGIS is in the test database."""

    def setUp(self):
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1 FROM pg_extension WHERE extname = 'postgis'")
            self.postgis = cursor.fetchone() is not None
            cursor.execute("CREATE SCHEMA t_b")
            cursor.execute("CREATE SCHEMA t_m")
            geom = ", geom geometry(PointZ, 3006)" if self.postgis else ""
            for schema in ("t_b", "t_m"):
                cursor.execute(f"CREATE TABLE {schema}.punkter (fid integer PRIMARY KEY, name text, uuid uuid{geom})")
            point = ", ST_SetSRID(ST_MakePoint(1, 2, 3), 3006)" if self.postgis else ""
            rows = [(1, "a", "11111111-1111-1111-1111-111111111111"), (2, "b", "22222222-2222-2222-2222-222222222222"),
                    (3, "c", "33333333-3333-3333-3333-333333333333"), (4, "only base", None)]
            for fid, name, uuid in rows:
                cursor.execute(f"INSERT INTO t_b.punkter VALUES (%s, %s, %s{point})", [fid, name, uuid])
            for fid, name, uuid in rows[:3]:
                cursor.execute(f"INSERT INTO t_m.punkter VALUES (%s, %s, %s{point})", [fid, name, uuid])
            cursor.execute("UPDATE t_m.punkter SET name = 'changed' WHERE fid = 2")
            cursor.execute(f"INSERT INTO t_m.punkter VALUES (5, 'only modified', NULL{point})")
            if self.postgis:
                cursor.execute("UPDATE t_m.punkter SET geom = ST_SetSRID(ST_MakePoint(1, 2, 9), 3006) WHERE fid = 3")

    def tearDown(self):
        with connection.cursor() as cursor:
            cursor.execute("DROP SCHEMA t_b CASCADE")
            cursor.execute("DROP SCHEMA t_m CASCADE")

    def test_compare_table_reports_each_kind_with_columns_and_uuid(self):
        with connection.cursor() as cursor:
            base_cols = content.schema_columns(cursor, "t_b")
            mod_cols = content.schema_columns(cursor, "t_m")
            spec = content.plan("punkter", "fid", base_cols["punkter"], mod_cols["punkter"])
            self.assertEqual(spec["uuid"], "uuid")
            self.assertEqual(spec["geometry"], ["geom"] if self.postgis else [])
            result = content.compare_table(cursor, "t_b", "t_m", spec)
        self.assertEqual((result["only_base"], result["only_modified"]), (1, 1))
        self.assertEqual(result["changed"], 2 if self.postgis else 1)
        by_key = {row["key"]: row for row in result["sample"]}
        self.assertEqual(by_key["2"], {"key": "2", "uuid": "22222222-2222-2222-2222-222222222222", "kind": "changed", "columns": ["name"]})
        self.assertEqual(by_key["4"]["kind"], "only_base")
        self.assertEqual(by_key["5"]["kind"], "only_modified")
        if self.postgis:
            self.assertEqual(by_key["3"]["columns"], ["geom"])   # a Z change, same XY: EWKB differs
        self.assertNotIn("1", by_key)
