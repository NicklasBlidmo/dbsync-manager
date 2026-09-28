"""
Guards for the deployment files (slice 12 brief, clean-room findings of
2026-09-28). Pure: they read docker-compose.yml, .env.example, tools/setup.sh
and the settings; no database.
"""

import os
from pathlib import Path

import yaml
from django.conf import settings
from django.test import SimpleTestCase

ROOT = Path(__file__).resolve().parents[2]


class ComposeTests(SimpleTestCase):
    def setUp(self):
        with open(ROOT / "docker-compose.yml", encoding="utf-8") as fh:
            self.compose = yaml.safe_load(fh)

    def test_init_scripts_are_mounted_as_files_not_a_directory(self):
        # A directory mount over /docker-entrypoint-initdb.d masks the postgis
        # image's own 10_postgis.sh: no extension, db-sync cannot start.
        for name, service in self.compose["services"].items():
            for volume in service.get("volumes", []):
                target = volume.split(":")[1] if isinstance(volume, str) else volume.get("target", "")
                self.assertNotEqual(target.rstrip("/"), "/docker-entrypoint-initdb.d",
                                    f"{name} mounts a directory over the init directory")

    def test_bundled_postgis_gets_the_manager_script_and_its_variables(self):
        postgis = self.compose["services"]["postgis"]
        sources = [v.split(":")[0] for v in postgis["volumes"]]
        self.assertIn("./docker/postgis-init/20-manager.sh", sources)
        self.assertTrue((ROOT / "docker/postgis-init/20-manager.sh").is_file())
        for var in ("MANAGER_DB_PASSWORD", "SYNC_DB_ROLE", "SYNC_DB_PASSWORD"):
            self.assertIn(var, postgis["environment"], var)


class EnvExampleTests(SimpleTestCase):
    def setUp(self):
        self.values = {}
        for line in (ROOT / ".env.example").read_text(encoding="utf-8").splitlines():
            if "=" in line and not line.startswith("#"):
                key, _, value = line.partition("=")
                self.values[key.strip()] = value.strip()   # the last line wins, as in Compose

    def test_bundled_database_url_carries_the_registry_password(self):
        # Env files are not interpolated, so the password is written twice and
        # must be the same on both lines.
        url = self.values["DATABASE_URL"]
        self.assertTrue(url.startswith("postgres://dbsync_manager:"), url)
        password = url.split("dbsync_manager:", 1)[1].split("@", 1)[0]
        self.assertEqual(password, self.values["MANAGER_DB_PASSWORD"])
        self.assertIn("@postgis:5432/", url)

    def test_sync_role_lines_exist(self):
        self.assertEqual(self.values["SYNC_DB_ROLE"], "dbsync_sync")
        self.assertIn("SYNC_DB_PASSWORD", self.values)

    def test_no_value_breaks_an_env_file(self):
        for key, value in self.values.items():
            for ch in "$#'\" ":
                self.assertNotIn(ch, value, f"{key} contains {ch!r}")


class CookieNameTests(SimpleTestCase):
    def test_cookie_names_carry_the_port(self):
        # Cookies belong to the host name, not the port: two instances on one
        # host (8090 and 8091) logged each other out.
        port = os.environ.get("HTTP_PORT", "8080")
        self.assertEqual(settings.SESSION_COOKIE_NAME, f"dbsync_manager_{port}_sessionid")
        self.assertEqual(settings.CSRF_COOKIE_NAME, f"dbsync_manager_{port}_csrftoken")


class SetupScriptTests(SimpleTestCase):
    def test_setup_script_is_posix_sh_and_names_every_answer(self):
        text = (ROOT / "tools/setup.sh").read_text(encoding="utf-8")
        self.assertTrue(text.startswith("#!/bin/sh\n"))
        for var in ("SETUP_PROFILE", "SETUP_HTTP_PORT", "SETUP_ALLOWED_HOSTS", "SETUP_TZ", "SETUP_PROJECT_NAME",
                    "SETUP_DATABASE_URL", "SETUP_POSTGRES_PASSWORD", "SETUP_MANAGER_DB_PASSWORD", "SETUP_SYNC_DB_PASSWORD"):
            self.assertIn(var, text, var)
        self.assertNotIn("[[", text)          # bash-only test syntax
        self.assertNotIn("function ", text)   # bash-only keyword
