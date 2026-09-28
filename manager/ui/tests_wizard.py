"""
Add wizard: naming rule and target decision (design §9, §15). Pure, no
database.
"""

from types import SimpleNamespace
from unittest import mock

from django.test import SimpleTestCase

from registry.models import Database

from . import naming
from .wizard import Row, decide, inspect_targets


class FakeCursor:
    """Answers the two catalogue queries inspect_targets makes."""

    def __init__(self, postgis=True, existing=None):
        self.postgis = postgis
        self.existing = existing or {}
        self.last = ""

    def execute(self, sql, params=None):
        self.last = sql

    def fetchone(self):
        if "pg_extension" in self.last:
            return (1,) if self.postgis else None
        return None

    def fetchall(self):
        return list(self.existing.items())

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeConnection:
    def __init__(self, cursor):
        self._cursor = cursor

    def cursor(self):
        return self._cursor

    def rollback(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class InspectTargetsTests(SimpleTestCase):
    def data(self):
        return SimpleNamespace(
            database=Database(name="gis", host="postgis", dbname="gis", user="dbsync_sync"),
            rows=[Row("1-punkter.gpkg", "p_s_punkter", "p_b_punkter")],
            init_from="gpkg", project="ak/x",
        )

    def test_refuses_a_database_without_postgis_before_looking_at_schemas(self):
        # The daemon would otherwise crash after the project exists
        # (clean-room install 2026-09-28: no extension in the bundled PostGIS).
        cursor = FakeCursor(postgis=False)
        with mock.patch("ui.wizard.dbinspect.connect", return_value=FakeConnection(cursor)):
            checks, error = inspect_targets(self.data())
        self.assertEqual(checks, [])
        self.assertEqual(error, "PostGIS is not installed in database 'gis' on postgis: "
                                "run CREATE EXTENSION postgis there as a superuser first")
        self.assertIn("pg_extension", cursor.last)   # nothing else was asked

    def test_with_postgis_the_rows_are_checked(self):
        cursor = FakeCursor(postgis=True)
        with mock.patch("ui.wizard.dbinspect.connect", return_value=FakeConnection(cursor)):
            checks, error = inspect_targets(self.data())
        self.assertEqual(error, "")
        self.assertEqual([(c.sync_file, c.state) for c in checks], [("1-punkter.gpkg", "new")])


class NamingTests(SimpleTestCase):
    def test_stem_drops_extension_and_ordering_token(self):
        self.assertEqual(naming.stem_of("1-punkter.gpkg"), "punkter")
        self.assertEqual(naming.stem_of("d-foto.gpkg"), "foto")
        self.assertEqual(naming.stem_of("3-terrangobjekt-punkter.gpkg"), "terrangobjekt_punkter")
        self.assertEqual(naming.stem_of("12-Schakt Ytor.GPKG"), "schakt_ytor")
        self.assertEqual(naming.stem_of("data/ak-data.gpkg"), "ak_data")   # two letters: not an ordering token
        self.assertEqual(naming.stem_of("Anläggningar.gpkg"), "anlaggningar")
        self.assertEqual(naming.stem_of("-.gpkg"), "data")

    def test_derived_names(self):
        self.assertEqual(naming.derive_schema_names("p3922_test", "1-punkter.gpkg"),
                         ("p3922_test_s_punkter", "p3922_test_b_punkter"))

    def test_default_prefix_from_project_name(self):
        self.assertEqual(naming.default_prefix("ak/3922-Bönsta"), "p3922_bonsta")
        self.assertEqual(naming.default_prefix("ak/bonsta"), "bonsta")
        self.assertEqual(naming.default_prefix("ak/"), "project")

    def test_identifier_validation_and_length(self):
        self.assertTrue(naming.is_valid_identifier("p3922_test_s_punkter"))
        self.assertFalse(naming.is_valid_identifier("3922"))
        self.assertFalse(naming.is_valid_identifier("Has-Dash"))
        self.assertFalse(naming.is_valid_identifier("x" * 64))
        long_prefix = "p" * 60
        modified, base = naming.derive_schema_names(long_prefix, "1-punkter.gpkg")
        self.assertEqual(len(modified), 63)
        self.assertEqual(len(base), 63)

    def test_conflict_copies(self):
        self.assertTrue(naming.is_conflict_copy("1-punkter (edit conflict, anna v3).gpkg"))
        self.assertFalse(naming.is_conflict_copy("1-punkter.gpkg"))
        self.assertEqual(naming.conflict_original("sub/1-punkter (edit conflict, anna v3).gpkg"), "sub/1-punkter.gpkg")


class DecideTests(SimpleTestCase):
    P = "ak/bonsta"

    def test_fresh_schemas_from_gpkg(self):
        self.assertEqual(decide("gpkg", False, False, None, self.P)[0], "new")

    def test_adopt_when_comment_names_this_project(self):
        state, reason = decide("gpkg", True, True, {"name": self.P, "version": "v4213"}, self.P)
        self.assertEqual(state, "adopt")
        self.assertIn("v4213", reason)
        self.assertEqual(decide("db", True, True, {"name": self.P}, self.P)[0], "adopt")

    def test_reject_other_projects_and_broken_inits(self):
        self.assertEqual(decide("gpkg", True, True, {"name": "ak/other"}, self.P)[0], "reject")
        self.assertEqual(decide("gpkg", True, True, None, self.P)[0], "reject")
        self.assertEqual(decide("gpkg", True, False, None, self.P)[0], "reject")
        self.assertEqual(decide("gpkg", False, True, None, self.P)[0], "reject")

    def test_init_from_database(self):
        self.assertEqual(decide("db", True, False, None, self.P)[0], "new")
        self.assertEqual(decide("db", False, False, None, self.P)[0], "reject")
        self.assertEqual(decide("db", False, True, None, self.P)[0], "reject")
