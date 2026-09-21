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
