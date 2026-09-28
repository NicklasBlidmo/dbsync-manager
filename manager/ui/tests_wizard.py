"""
Add wizard: naming rule and target decision (design §9, §15). Pure, no
database.
"""

from django.test import SimpleTestCase

from . import naming
from .wizard import decide


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

    def test_deselect_patterns(self):
        self.assertEqual(naming.parse_patterns(" m-*.gpkg, d-*.gpkg ,, "), ["m-*.gpkg", "d-*.gpkg"])
        self.assertEqual(naming.parse_patterns(""), [])
        patterns = ["m-*.gpkg", "d-?oto.gpkg"]
        self.assertTrue(naming.matches_any("M-Huvudtyp.GPKG", patterns))        # case-insensitive
        self.assertTrue(naming.matches_any("sub/d-foto.gpkg", patterns))        # file name, not the directory
        self.assertFalse(naming.matches_any("1-punkter.gpkg", patterns))
        self.assertFalse(naming.matches_any("1-punkter.gpkg", []))

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
