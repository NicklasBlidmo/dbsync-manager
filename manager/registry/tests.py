"""
Unit tests that need no database (design §13, unit).

Run with: python manage.py test registry
"""

from django.core.exceptions import ImproperlyConfigured
from django.test import SimpleTestCase, override_settings

from . import crypto
from .models import Database, MerginAccount

KEY_A = crypto.generate_key()
KEY_B = crypto.generate_key()


@override_settings(DBSYNC_MANAGER_SECRET_KEY=KEY_A)
class CryptoTests(SimpleTestCase):
    def test_roundtrip(self):
        token = crypto.encrypt("hemligt lösenord åäö")
        self.assertNotIn("hemligt", token)
        self.assertEqual(crypto.decrypt(token), "hemligt lösenord åäö")

    def test_empty_token_decrypts_to_empty(self):
        self.assertEqual(crypto.decrypt(""), "")

    def test_wrong_key_raises_secret_error(self):
        token = crypto.encrypt("x")
        with override_settings(DBSYNC_MANAGER_SECRET_KEY=KEY_B):
            with self.assertRaises(crypto.SecretError):
                crypto.decrypt(token)

    def test_missing_key_is_configuration_error(self):
        with override_settings(DBSYNC_MANAGER_SECRET_KEY=""):
            with self.assertRaises(ImproperlyConfigured):
                crypto.encrypt("x")

    def test_invalid_key_is_configuration_error(self):
        with override_settings(DBSYNC_MANAGER_SECRET_KEY="not-a-fernet-key"):
            with self.assertRaises(ImproperlyConfigured):
                crypto.encrypt("x")


@override_settings(DBSYNC_MANAGER_SECRET_KEY=KEY_A)
class SecretPropertyTests(SimpleTestCase):
    def test_mergin_account_secret_is_stored_encrypted(self):
        account = MerginAccount(name="svc", username="svc", url="https://app.merginmaps.com")
        account.secret = "pw"
        self.assertNotEqual(account.secret_enc, "pw")
        self.assertEqual(account.secret, "pw")

    def test_database_conn_info_quotes_and_hides_password(self):
        db = Database(name="gis", host="db.example.org", port=5433, dbname="gis", user="ak_dbsync")
        db.password = "p'w d"
        self.assertEqual(
            db.conn_info(with_password=False),
            "host=db.example.org port=5433 dbname=gis user=ak_dbsync",
        )
        self.assertEqual(
            db.conn_info(),
            "host=db.example.org port=5433 dbname=gis user=ak_dbsync password='p\\'w d'",
        )


# --- deployment helpers (review note 2026-09-21b) ---------------------------

from django.conf import settings  # noqa: E402

import generate_keys  # noqa: E402
import preflight  # noqa: E402

GOOD_ENV = {
    "DJANGO_SECRET_KEY": "x" * 50,
    "DBSYNC_MANAGER_SECRET_KEY": KEY_A,
    "DATABASE_URL": "postgres://role:pw@192.0.2.10:5432/gis",
}


class HookValidationTests(SimpleTestCase):
    def test_scheduled_hook_needs_run_as_and_a_time(self):
        from datetime import time as dtime

        from django.core.exceptions import ValidationError

        from .models import Hook

        with self.assertRaises(ValidationError) as ctx:
            Hook(name="backup", kind="scheduled", command="pg_dump").clean()
        self.assertEqual(sorted(ctx.exception.message_dict), ["database", "run_at"])
        Hook(name="grants", kind="post_init", command="true").clean()          # project hooks: nothing required
        hook = Hook(name="backup", kind="scheduled", command="pg_dump", run_at=dtime(2, 15))
        hook.database_id = 1
        hook.clean()


class GenerateKeysTests(SimpleTestCase):
    VALUES = {"DJANGO_SECRET_KEY": "django-new", "DBSYNC_MANAGER_SECRET_KEY": "fernet-new"}

    def test_generated_values_are_env_file_safe(self):
        for value in generate_keys.new_values().values():
            self.assertRegex(value, r"^[A-Za-z0-9_=-]+$")  # no $, #, quotes or spaces

    def test_fernet_key_is_accepted_by_crypto(self):
        with override_settings(DBSYNC_MANAGER_SECRET_KEY=generate_keys.fernet_key()):
            self.assertEqual(crypto.decrypt(crypto.encrypt("x")), "x")

    def test_fills_the_template_lines_in_place(self):
        text = "A=1\nDJANGO_SECRET_KEY=\n# note\nDBSYNC_MANAGER_SECRET_KEY=\nB=2\n"
        new, _ = generate_keys.fill_env_text(text, self.VALUES)
        self.assertEqual(new, "A=1\nDJANGO_SECRET_KEY=django-new\n# note\nDBSYNC_MANAGER_SECRET_KEY=fernet-new\nB=2\n")

    def test_duplicate_lines_collapse_and_keep_the_filled_value(self):
        # The installation failure: keys pasted above the empty template lines.
        text = "DJANGO_SECRET_KEY=pasted\nDJANGO_SECRET_KEY=\nDBSYNC_MANAGER_SECRET_KEY=\n"
        new, report = generate_keys.fill_env_text(text, self.VALUES)
        self.assertEqual(new.count("DJANGO_SECRET_KEY="), 1)
        self.assertIn("DJANGO_SECRET_KEY=pasted\n", new)
        self.assertIn("duplicate", report["DJANGO_SECRET_KEY"])

    def test_existing_fernet_key_is_never_replaced_without_force(self):
        text = "DJANGO_SECRET_KEY=a\nDBSYNC_MANAGER_SECRET_KEY=in-use\n"
        new, report = generate_keys.fill_env_text(text, self.VALUES)
        self.assertIn("DBSYNC_MANAGER_SECRET_KEY=in-use\n", new)
        self.assertEqual(report["DBSYNC_MANAGER_SECRET_KEY"], "kept")
        forced, _ = generate_keys.fill_env_text(text, self.VALUES, force=True)
        self.assertIn("DBSYNC_MANAGER_SECRET_KEY=fernet-new\n", forced)

    def test_missing_lines_are_appended(self):
        new, _ = generate_keys.fill_env_text("A=1\n", self.VALUES)
        self.assertTrue(new.endswith("DJANGO_SECRET_KEY=django-new\nDBSYNC_MANAGER_SECRET_KEY=fernet-new\n"))


class PreflightEnvironmentTests(SimpleTestCase):
    def problems(self, **changes):
        return preflight.check_environment({**GOOD_ENV, **changes})

    def test_good_environment_has_no_problems(self):
        self.assertEqual(self.problems(), [])

    def test_empty_key_is_named(self):
        (problem,) = self.problems(DJANGO_SECRET_KEY="")
        self.assertIn("DJANGO_SECRET_KEY is empty", problem)

    def test_invalid_fernet_key(self):
        (problem,) = self.problems(DBSYNC_MANAGER_SECRET_KEY="abc")
        self.assertIn("not a valid Fernet key", problem)

    def test_variable_name_pasted_into_value(self):
        (problem,) = self.problems(DATABASE_URL="DATABASE_URL=postgres://r:p@h/db")
        self.assertIn("pasted into the value", problem)

    def test_password_that_breaks_the_url_is_named(self):
        # "!?" at the start of a generated password: ? begins the query string
        # and the URL parses without a host (clean-room walk, 2026-09-29).
        (problem,) = self.problems(DATABASE_URL="postgres://dbsync_manager:!?abc123@postgis:5432/gis")
        self.assertIn("password in DATABASE_URL contains characters", problem)
        (problem,) = self.problems(DATABASE_URL="postgres://dbsync_manager:pw@postgis:5432")
        self.assertIn("needs a host and a database", problem)
        self.assertIn("host goes after the @", problem)
        self.assertEqual(self.problems(DATABASE_URL="postgres://r:a-b_c@h:5432/db"), [])

    def test_localhost_is_explained_only_when_the_connection_fails(self):
        # Valid with network_mode: host, so not a configuration error.
        self.assertEqual(self.problems(DATABASE_URL="postgres://r:p@localhost:5432/db"), [])
        line = preflight.explain_connection_error("connection refused", "r", "localhost", 5432, "db")
        self.assertIn("container itself", line)

    def test_unknown_time_zone(self):
        (problem,) = self.problems(TZ="Europe/Stokholm")
        self.assertIn("not a known time zone", problem)
        self.assertEqual(self.problems(TZ="Europe/Stockholm"), [])

    def test_auth_failure_mentions_missing_role(self):
        line = preflight.explain_connection_error(
            'FATAL:  password authentication failed for user "m"', "m", "h", 5432, "db"
        )
        self.assertIn("role that does not exist", line)


class SettingsTests(SimpleTestCase):
    def test_loopback_is_always_allowed(self):
        for host in ("localhost", "127.0.0.1"):
            self.assertIn(host, settings.ALLOWED_HOSTS)

    def test_cookie_names_do_not_collide_with_other_django_apps(self):
        self.assertNotEqual(settings.SESSION_COOKIE_NAME, "sessionid")
        self.assertNotEqual(settings.CSRF_COOKIE_NAME, "csrftoken")


class PreflightConsistencyTests(SimpleTestCase):
    """preflight.py must not import the settings, so it carries its own copy
    of what it needs from them (review note 2026-09-21b). Keep them in step."""

    def test_registry_schema_name_matches_settings(self):
        self.assertEqual(preflight.REGISTRY_SCHEMA, settings.REGISTRY_SCHEMA)

    def test_preflight_and_generate_keys_do_not_import_django(self):
        for module in (preflight, generate_keys):
            with open(module.__file__, encoding="utf-8") as handle:
                source = handle.read()
            self.assertNotIn("import django", source, module.__name__)
            self.assertNotIn("from django", source, module.__name__)
