"""
Django settings for dbsync-manager.

Everything that differs between installations comes from the environment
(design §10). Nothing in this file should need editing for a deployment.
"""

import os
from pathlib import Path

import dj_database_url
from django.core.exceptions import ImproperlyConfigured

BASE_DIR = Path(__file__).resolve().parent.parent


# --- environment helpers ---------------------------------------------------


def env(name, default=None, required=False):
    value = os.environ.get(name, default)
    if required and not value:
        raise ImproperlyConfigured(f"environment variable {name} is required")
    return value


def env_int(name, default):
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ImproperlyConfigured(f"{name} must be an integer, got {raw!r}") from exc


def env_bool(name, default=False):
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def env_list(name, default=""):
    raw = os.environ.get(name)
    if raw is None:
        raw = default
    return [item.strip() for item in raw.split(",") if item.strip()]


# --- core ------------------------------------------------------------------

SECRET_KEY = env("DJANGO_SECRET_KEY", required=True)
DEBUG = env_bool("DJANGO_DEBUG", False)
# The loopback names are always allowed, whatever .env says: the container's
# own healthcheck calls http://127.0.0.1:8080/healthz, and an ALLOWED_HOSTS
# that lists only the public address would otherwise mark a working container
# unhealthy and fill the log with DisallowedHost tracebacks.
ALLOWED_HOSTS = list(dict.fromkeys(env_list("ALLOWED_HOSTS") + ["localhost", "127.0.0.1", "[::1]"]))
CSRF_TRUSTED_ORIGINS = env_list("CSRF_TRUSTED_ORIGINS")

INSTALLED_APPS = [
    # ui first: its templates/admin/base_site.html must be found before
    # Django's own (app template dirs are searched in this order).
    "ui.apps.UiConfig",
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "django.contrib.postgres",
    "registry.apps.RegistryConfig",
    "supervisor.apps.SupervisorConfig",
    "api.apps.ApiConfig",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    # Serves the collected static files (admin CSS/JS) from gunicorn. Without
    # it the admin is unstyled whenever DJANGO_DEBUG is false, since Django
    # only serves static files itself under runserver with DEBUG on.
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "dbsync_manager.urls"
WSGI_APPLICATION = "dbsync_manager.wsgi.application"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]


# --- registry database (design §3, §5, §12) --------------------------------
#
# The role in DATABASE_URL owns the dbsync_manager schema and nothing else.
# Syncs run with the roles stored in registry.Database, never with this one.

REGISTRY_SCHEMA = "dbsync_manager"

_db = dj_database_url.config(env="DATABASE_URL", conn_max_age=60, conn_health_checks=True)
if not _db:
    raise ImproperlyConfigured("environment variable DATABASE_URL is required")
_db.setdefault("OPTIONS", {})
_db["OPTIONS"]["options"] = f"-c search_path={REGISTRY_SCHEMA},public"
DATABASES = {"default": _db}

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"


# --- auth ------------------------------------------------------------------

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

LOGIN_URL = "/login/"
LOGIN_REDIRECT_URL = "/"
LOGOUT_REDIRECT_URL = "/login/"

# Behind the organisation's TLS proxy (design §12). SECURE_COOKIES is off by
# default so a plain-HTTP evaluation on port 8080 can log in.
USE_X_FORWARDED_HOST = True
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
SESSION_COOKIE_SECURE = env_bool("SECURE_COOKIES", False)
# Own cookie names. Cookies belong to the host name, not to the port, so every
# Django application on the same server shares "csrftoken" and "sessionid":
# they log each other out, and a Secure csrftoken set by a neighbour over
# HTTPS cannot be overwritten from plain HTTP, which makes every form here
# fail with "CSRF cookie not set" until the browser's site data is cleared.
# Two installations of the manager on one host (production on 8090, a test
# instance on 8091) shared a name too and logged each other out on every
# login (2026-09-28), so the port is part of the name. Changing HTTP_PORT
# logs everyone out once.
_cookie_port = "".join(ch for ch in env("HTTP_PORT", "8080") if ch.isalnum()) or "8080"
SESSION_COOKIE_NAME = f"dbsync_manager_{_cookie_port}_sessionid"
CSRF_COOKIE_NAME = f"dbsync_manager_{_cookie_port}_csrftoken"
CSRF_COOKIE_SECURE = SESSION_COOKIE_SECURE
SESSION_COOKIE_HTTPONLY = True
X_FRAME_OPTIONS = "DENY"


# --- i18n / static ---------------------------------------------------------

# en-gb: 24-hour times and "30 September 2026" in the admin instead of
# "2:15 a.m." and "Sept. 30, 2026" (field test 2026-09-30). The manager's
# own templates use explicit formats.
LANGUAGE_CODE = "en-gb"
# One variable for the whole container. Django exports TIME_ZONE as TZ for its
# own process only; gunicorn's master and db-sync follow the container's TZ.
# Reading both from TZ keeps every log line on the same clock. Storage is UTC
# regardless (USE_TZ); this is display and logging only.
TIME_ZONE = env("TZ", "UTC") or "UTC"
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    # Compressed but not manifest-hashed: works without a collectstatic run
    # in development and needs no manifest at start-up.
    "staticfiles": {"BACKEND": "whitenoise.storage.CompressedStaticFilesStorage"},
}


# --- e-mail (design §6 alerts) --------------------------------------------

EMAIL_HOST = env("SMTP_HOST", "")
EMAIL_PORT = env_int("SMTP_PORT", 587)
EMAIL_HOST_USER = env("SMTP_USER", "")
EMAIL_HOST_PASSWORD = env("SMTP_PASSWORD", "")
EMAIL_USE_TLS = env_bool("SMTP_TLS", True)
# The supervisor sends alert mail inside its tick; without a timeout an
# unreachable SMTP host would stall the loop (docs/m365-direct-send.md).
EMAIL_TIMEOUT = env_int("SMTP_TIMEOUT", 30)
DEFAULT_FROM_EMAIL = env("SMTP_FROM", "dbsync-manager@localhost")
EMAIL_BACKEND = (
    "django.core.mail.backends.smtp.EmailBackend"
    if EMAIL_HOST
    else "django.core.mail.backends.console.EmailBackend"
)


# --- dbsync-manager (design §4, §6, §10) -----------------------------------

# Fernet key for secrets at rest. Required: without it nothing can be stored.
DBSYNC_MANAGER_SECRET_KEY = env("DBSYNC_MANAGER_SECRET_KEY", required=True)

# Where db-sync is installed in the image and where per-project state lives.
DBSYNC_DAEMON_DIR = Path(env("DBSYNC_DAEMON_DIR", "/mergin-db-sync"))
DBSYNC_DATA_DIR = Path(env("DBSYNC_DATA_DIR", "/data"))

# Supervisor timing, all in seconds.
DBSYNC_TICK = env_int("DBSYNC_TICK", 30)
DBSYNC_CHECK_INTERVAL = env_int("DBSYNC_CHECK_INTERVAL", 300)
DBSYNC_STUCK_AFTER = env_int("DBSYNC_STUCK_AFTER", 1800)
# A Mergin outage (5xx, refused, timeout) mails only once it has lasted this long.
DBSYNC_MERGIN_DOWN_ALERT_AFTER = env_int("DBSYNC_MERGIN_DOWN_ALERT_AFTER", 900)
# Compare base and modified content (not only counts) at every check (slice 15).
DBSYNC_CONTENT_CHECK = env_bool("DBSYNC_CONTENT_CHECK", True)

# Public address of the UI, for links in alert mails (optional).
DBSYNC_BASE_URL = env("DBSYNC_BASE_URL", "")

# Housekeeping.
DBSYNC_LOG_MAX_MB = env_int("DBSYNC_LOG_MAX_MB", 20)
DBSYNC_LOG_KEEP = env_int("DBSYNC_LOG_KEEP", 5)
DBSYNC_RETENTION_DAYS = env_int("DBSYNC_RETENTION_DAYS", 90)


# --- logging ---------------------------------------------------------------

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "plain": {"format": "%(asctime)s %(levelname)s %(name)s: %(message)s"},
    },
    "handlers": {
        "console": {"class": "logging.StreamHandler", "formatter": "plain"},
    },
    "root": {"handlers": ["console"], "level": "INFO"},
    "loggers": {
        "django": {"level": "INFO", "propagate": True},
        "registry": {"level": "INFO", "propagate": True},
        "supervisor": {"level": "INFO", "propagate": True},
    },
}
