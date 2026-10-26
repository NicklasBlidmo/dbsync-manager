"""
The checkout layout of db-sync 2.4.0 (slice 16, B and C): one function
knows the path, the old tree decides whether a project needs the
migration, and the check reads a sparse checkout.
"""

import json
import tempfile
from pathlib import Path

from django.test import SimpleTestCase, override_settings

from registry.models import SyncConnection, SyncProject

from . import layout
from .check import local_version_of
from .config_render import WORKING_SUBDIR


class PathTests(SimpleTestCase):
    def test_checkout_dir_is_name_hyphen_modified_schema_under_work(self):
        path = layout.checkout_dir("/data/projects/3", "Arkeologikonsult/3922_Bönsta", "p3922_s_a_b")
        self.assertEqual(path, Path("/data/projects/3") / "work" / "3922_Bönsta-p3922_s_a_b")
        self.assertEqual(layout.legacy_checkout_tree("/data/projects/3", "Arkeologikonsult/3922_Bönsta"),
                         Path("/data/projects/3/work/3922_Bönsta"))
        self.assertEqual(layout.working_dir("/data/projects/3"), Path("/data/projects/3/work"))
        self.assertEqual(WORKING_SUBDIR, "work")
        self.assertEqual(layout.auth_token_path("/data/projects/3"), Path("/data/projects/3/work/.mergin_auth.json"))

    @override_settings(DBSYNC_DATA_DIR=Path("/data"))
    def test_connection_checkout_dir_goes_through_layout(self):
        project = SyncProject(pk=7, mergin_project="ak/x_y")
        conn = SyncConnection(project=project, sync_file="d/1-punkter.gpkg", modified_schema="x_s_punkter")
        self.assertEqual(conn.checkout_dir, Path("/data/projects/7/work/x_y-x_s_punkter"))

    def test_needs_migration_follows_the_old_tree_and_the_marker(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertFalse(layout.needs_migration(tmp, "ak/x"))          # never started
            (Path(tmp) / "work" / "x-x_s_a").mkdir(parents=True)            # new layout only
            self.assertFalse(layout.needs_migration(tmp, "ak/x"))
            (Path(tmp) / "work" / "x" / "x_s_a").mkdir(parents=True)        # the 2.3.0 tree
            self.assertTrue(layout.needs_migration(tmp, "ak/x"))
            marker = layout.mark_migrated(tmp, "operation 5")
            self.assertEqual(marker, Path(tmp) / "work" / ".layout-v2")
            self.assertIn("operation 5", marker.read_text(encoding="utf-8"))
            self.assertFalse(layout.needs_migration(tmp, "ak/x"))

    def test_local_version_of_a_sparse_checkout(self):
        with tempfile.TemporaryDirectory() as tmp:
            checkout = Path(tmp) / "work" / "x-x_s_a"
            (checkout / ".mergin").mkdir(parents=True)
            self.assertFalse(layout.is_downloaded(checkout))
            (checkout / "1-punkter.gpkg").write_bytes(b"")
            (checkout / ".mergin" / "mergin.json").write_text(
                json.dumps({"name": "x", "version": "v7359", "files": [{"path": "1-punkter.gpkg"}]}), encoding="utf-8")
            (checkout / ".mergin" / "file_filter.json").write_text('{"include": ["1-punkter.gpkg"], "exclude": null}')
            self.assertTrue(layout.is_downloaded(checkout))
            self.assertEqual(local_version_of(checkout), 7359)
