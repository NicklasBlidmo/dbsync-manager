"""
Secrets at rest (design §4, §12).

Mergin passwords/tokens and database passwords are stored Fernet-encrypted
with the key from DBSYNC_MANAGER_SECRET_KEY. Only this module touches the
key. Decrypted values are handed to db-sync's generated config and to hook
pgpass files, never to the UI, the API, events or logs.
"""

from cryptography.fernet import Fernet, InvalidToken
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured


class SecretError(Exception):
    """A stored secret cannot be read with the configured key."""


def _fernet() -> Fernet:
    key = getattr(settings, "DBSYNC_MANAGER_SECRET_KEY", None)
    if not key:
        raise ImproperlyConfigured("DBSYNC_MANAGER_SECRET_KEY is not set")
    try:
        return Fernet(key.encode() if isinstance(key, str) else key)
    except (ValueError, TypeError) as exc:
        raise ImproperlyConfigured(
            "DBSYNC_MANAGER_SECRET_KEY is not a valid Fernet key; "
            "generate one with `manage.py generate_keys`"
        ) from exc


def encrypt(plaintext: str) -> str:
    """Return the Fernet token for *plaintext* as a str suitable for a TextField."""
    if plaintext is None:
        raise ValueError("cannot encrypt None")
    return _fernet().encrypt(plaintext.encode("utf-8")).decode("ascii")


def decrypt(token: str) -> str:
    """Return the plaintext for a stored token; SecretError if the key does not match."""
    if not token:
        return ""
    try:
        return _fernet().decrypt(token.encode("ascii")).decode("utf-8")
    except InvalidToken as exc:
        raise SecretError(
            "stored secret cannot be decrypted; DBSYNC_MANAGER_SECRET_KEY has changed"
        ) from exc


def generate_key() -> str:
    return Fernet.generate_key().decode("ascii")
