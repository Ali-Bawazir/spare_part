"""
Django settings for mms project.
"""

import os
from datetime import timedelta
from pathlib import Path

# Build paths inside the project like this: BASE_DIR / 'subdir'.
BASE_DIR = Path(__file__).resolve().parent.parent


# SECURITY WARNING: keep the secret key used in production secret!
# Fail-closed: when DEBUG is False, SECRET_KEY must be set in the environment.
from django.core.exceptions import ImproperlyConfigured

DEBUG = os.environ.get("DEBUG", "False").lower() in ("true", "1", "yes")

if not DEBUG:
    _secret = os.environ.get("SECRET_KEY")
    if not _secret:
        raise ImproperlyConfigured(
            "SECRET_KEY env var is required when DEBUG=False."
        )
    SECRET_KEY = _secret
else:
    SECRET_KEY = os.environ.get(
        "SECRET_KEY",
        "django-insecure-dev-only-not-for-production-set-DEBUG-False-to-enforce",
    )
    if SECRET_KEY.startswith("django-insecure-"):
        import warnings
        warnings.warn(
            "MMS: SECRET_KEY not set; using insecure dev fallback. "
            "Set SECRET_KEY env var for any non-DEBUG deployment.",
            stacklevel=1,
        )

# SECURITY WARNING: don't run with debug turned on in production!
ALLOWED_HOSTS = [h.strip() for h in os.environ.get("ALLOWED_HOSTS", "").split(",") if h.strip()]
if not DEBUG and not ALLOWED_HOSTS:
    raise ImproperlyConfigured(
        "ALLOWED_HOSTS env var is required when DEBUG=False (comma-separated hostnames)."
    )


# Application definition

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "axes",
    "accounts",
    "maintenance",
    "inventory",
    "procurement",
]

AUTH_USER_MODEL = "accounts.User"
LOGIN_URL = "/accounts/login/"
LOGIN_REDIRECT_URL = "/"
LOGOUT_REDIRECT_URL = "/accounts/login/"

# django-axes — brute-force protection. AxesStandaloneBackend gates every
# authenticate() call; after 5 failures (per username OR per IP) the
# account/IP is locked for 15 minutes. A successful login resets the
# counter for that username.
AUTHENTICATION_BACKENDS = [
    "axes.backends.AxesStandaloneBackend",
    "django.contrib.auth.backends.ModelBackend",
]

AXES_FAILURE_LIMIT = 5
AXES_COOLOFF_TIME = timedelta(minutes=15)
AXES_LOCK_OUT_AT_FAILURE = True
AXES_LOCK_OUT_BY_USER_OR_IP = True
AXES_RESET_ON_SUCCESS = True
AXES_ENABLE_ACCESS_FAILURE_LOG = True
AXES_VERBOSE = True
# Read real client IP from the CranL proxy header first, then REMOTE_ADDR.
AXES_META_PRECEDENCE_ORDER = ("HTTP_X_FORWARDED_FOR", "REMOTE_ADDR")
# Don't reset the cool-off clock when the locked-out user keeps trying.
AXES_RESET_COOL_OFF_ON_FAILURE_DURING_LOCKOUT = False

# axes W002 ("you don't have AxesMiddleware in MIDDLEWARE") is informational
# only — AxesMiddleware is needed for DRF/TokenAuth flows. This MMS uses
# session auth via django.contrib.auth.urls + AxesStandaloneBackend, which
# gates every authenticate() call. No DRF endpoints are wired.
SILENCED_SYSTEM_CHECKS = ["axes.W002"]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.locale.LocaleMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "accounts.middleware.IdleSessionTimeoutMiddleware",  # must be after AuthenticationMiddleware
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "mms.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.debug",
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
                "django.template.context_processors.i18n",
                "maintenance.context_processors.mms_nav",
            ],
        },
    },
]

WSGI_APPLICATION = "mms.wsgi.application"


# Database
# https://docs.djangoproject.com/en/4.2/ref/settings/#databases

def _get_db_config():
    """Pick the database backend based on environment variables.

    Precedence (highest to lowest):
        1. ``MMS_USE_SQLITE=1`` — explicit SQLite fallback (CI, tests, local dev).
           Forces SQLite regardless of other DB_* vars.
        2. ``DATABASE_URL`` set — parses postgresql://user:pass@host:port/db
           (CranL auto-injects this when a DB is connected to the app).
        3. All of ``DB_USER``, ``DB_PASSWORD``, ``DB_NAME`` set — PostgreSQL.
        4. None of the above — SQLite (last-resort) with a warning in DEBUG only.

    In production, the docker compose stack always sets DB_PASSWORD, so the
    SQLite path is never hit. Tests run with ``MMS_USE_SQLITE=1``.
    """
    # 1) Explicit SQLite opt-in
    if os.environ.get("MMS_USE_SQLITE") == "1":
        return {
            "ENGINE": "django.db.backends.sqlite3",
            "NAME": BASE_DIR / "db.sqlite3",
        }

    # 2) DATABASE_URL (CranL auto-injects when DB is connected)
    database_url = os.environ.get("DATABASE_URL", "")
    if database_url.startswith(("postgresql://", "postgres://")):
        from urllib.parse import urlparse
        u = urlparse(database_url)
        return {
            "ENGINE": "django.db.backends.postgresql",
            "NAME": (u.path or "/").lstrip("/") or "postgres",
            "USER": u.username or "",
            "PASSWORD": u.password or "",
            "HOST": u.hostname or "localhost",
            "PORT": str(u.port or 5432),
            "CONN_MAX_AGE": 60,
            "OPTIONS": {
                "connect_timeout": 10,
            },
        }

    db_user = os.environ.get("DB_USER", "")
    db_password = os.environ.get("DB_PASSWORD", "")
    db_host = os.environ.get("DB_HOST", "localhost")
    db_port = os.environ.get("DB_PORT", "5432")
    db_name = os.environ.get("DB_NAME", "")

    # 3) PostgreSQL when all required vars are present
    if db_user and db_password and db_name:
        return {
            "ENGINE": "django.db.backends.postgresql",
            "NAME": db_name,
            "USER": db_user,
            "PASSWORD": db_password,
            "HOST": db_host,
            "PORT": db_port,
            "CONN_MAX_AGE": 60,
            "OPTIONS": {
                "connect_timeout": 10,
            },
        }

    # 3) Dev convenience: SQLite with warning (DEBUG only)
    if DEBUG:
        import warnings
        warnings.warn(
            "MMS: DB_PASSWORD/DB_USER/DB_NAME not set and MMS_USE_SQLITE is not '1'. "
            "Falling back to SQLite. For production, configure the docker compose stack.",
            stacklevel=1,
        )
        return {
            "ENGINE": "django.db.backends.sqlite3",
            "NAME": BASE_DIR / "db.sqlite3",
        }

    # 4) Production fail-closed: refuse to start without a real DB.
    raise ImproperlyConfigured(
        "DB_NAME/DB_USER/DB_PASSWORD env vars are required when DEBUG=False "
        "and MMS_USE_SQLITE is not '1'. Refusing to start with SQLite in production."
    )


DATABASES = {
    "default": _get_db_config()
}


# Password validation
# https://docs.djangoproject.com/en/4.2/ref/settings/#auth-password-validators

# Idle session timeout (seconds). accounts.middleware.IdleSessionTimeoutMiddleware
# reads this on startup and logs the user out after `SESSION_IDLE_TIMEOUT_SECONDS`
# of inactivity. Default 4h (14400s). Override via MMS_SESSION_TIMEOUT_SECONDS.
SESSION_IDLE_TIMEOUT_SECONDS = int(
    os.environ.get("MMS_SESSION_TIMEOUT_SECONDS", "14400")
)

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]


# Internationalization
# https://docs.djangoproject.com/en/4.2/topics/i18n/

LANGUAGE_CODE = "en"  # default; user-selectable via session/cookie
TIME_ZONE = "Asia/Riyadh"
USE_I18N = True
USE_L10N = True  # drives number/date formatting from LANGUAGE_CODE
USE_THOUSAND_SEPARATOR = True
NUMBER_GROUPING = 3
USE_TZ = True

LANGUAGES = [
    ("en", "English"),
    ("ar", "العربية"),
]
LANGUAGES_BIDI = ["ar"]  # declares Arabic as RTL; Django sets LANGUAGE_BIDI

LOCALE_PATHS = [BASE_DIR / "locale"]

LANGUAGE_COOKIE_NAME = "django_language"
LANGUAGE_COOKIE_AGE = 60 * 60 * 24 * 365  # 1 year
LANGUAGE_COOKIE_PATH = "/"
LANGUAGE_COOKIE_SAMESITE = "Lax"
LANGUAGE_COOKIE_SECURE = not DEBUG


# Static files (CSS, JavaScript, Images)
# https://docs.djangoproject.com/en/4.2/howto/static-files/

STATIC_URL = "static/"
STATICFILES_DIRS = [BASE_DIR / "static"]
STATIC_ROOT = BASE_DIR / "staticfiles"

# Media (uploads): FileSystemStorage for dev, S3 for production.
# Selection is automatic: if MMS_MEDIA_BUCKET is set, use S3; otherwise
# local FS. Dev works without any env vars.
_MEDIA_BUCKET = os.environ.get("MMS_MEDIA_BUCKET", "").strip()

if _MEDIA_BUCKET:
    # Production: S3-compatible object storage (CranL Object Storage /
    # Cloudflare R2 under the hood). MEDIA_URL is auto-derived from
    # AWS_S3_CUSTOM_DOMAIN (the CDN host), so users never see the raw
    # R2 endpoint.
    STORAGES = {
        "default": {"BACKEND": "storages.backends.s3.S3Storage"},
        "staticfiles": {
            "BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage"
        },
    }
    AWS_STORAGE_BUCKET_NAME      = _MEDIA_BUCKET
    AWS_S3_REGION_NAME            = os.environ.get("AWS_S3_REGION_NAME", "auto")
    AWS_S3_ENDPOINT_URL           = os.environ["AWS_S3_ENDPOINT_URL"]
    AWS_S3_CUSTOM_DOMAIN          = os.environ["AWS_S3_CUSTOM_DOMAIN"]
    AWS_DEFAULT_ACL               = os.environ.get("MMS_MEDIA_ACL", "public-read")
    AWS_QUERYSTRING_AUTH          = False
    AWS_S3_ADDRESSING_STYLE       = "path"
    # Uploaded media is immutable (filenames are uuid4 hexes), so cache
    # for a year. CDN honors Cache-Control from the bucket.
    AWS_S3_OBJECT_PARAMETERS      = {"CacheControl": "max-age=31536000"}
    # MEDIA_URL is set automatically by django-storages from CUSTOM_DOMAIN;
    # we leave it implicit so the bucket host stays the single source of truth.
else:
    # Dev / fallback: FileSystemStorage exactly as before. No env required.
    STORAGES = {
        "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
        "staticfiles": {
            "BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage"
        },
    }

# Top-level folder inside the bucket (or MEDIA_ROOT in dev). Uploaded
# files live at <MEDIA_ROOT_PREFIX>/<entity_type>/<id>/<filename> so a
# future entity needs no upload-path code change.
MEDIA_ROOT_PREFIX = os.environ.get("MMS_MEDIA_ROOT_PREFIX", "attachments")

# File Upload Settings
FILE_UPLOAD_MAX_MEMORY_SIZE = 100 * 1024 * 1024  # 100 MB upper bound; per-type limits enforced in views

# Per-content-type size limits (enforced in mms.utils.uploads.validate_uploaded_file).
MAX_IMAGE_SIZE = 10 * 1024 * 1024   # 10 MB — photos
MAX_AUDIO_SIZE = 25 * 1024 * 1024   # 25 MB — voice notes
MAX_VIDEO_SIZE = 100 * 1024 * 1024  # 100 MB — repair videos
MAX_PDF_SIZE   = 20 * 1024 * 1024   # 20 MB — invoices, quotations

# Local on-disk media root (dev only; production uses S3 via STORAGES above).
MEDIA_ROOT = BASE_DIR / "media"

# Default primary key field type
# https://docs.djangoproject.com/en/4.2/ref/settings/#default-auto-field

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"


# Celery Configuration (Phase 2 — background jobs)
# https://docs.celeryq.dev/en/stable/django/first-steps-with-django.html

CELERY_BROKER_URL = os.environ.get("REDIS_URL", "redis://localhost:6379/0")
CELERY_RESULT_BACKEND = "django-db"
CELERY_CACHE_BACKEND = "redis://localhost:6379/1"
CELERY_BEAT_SCHEDULER = "django_celery_beat.schedulers:DatabaseScheduler"
CELERY_ACCEPT_CONTENT = ["json"]
CELERY_TASK_SERIALIZER = "json"
CELERY_RESULT_SERIALIZER = "json"
CELERY_TIMEZONE = "UTC"


# File Upload Settings
FILE_UPLOAD_MAX_MEMORY_SIZE = 10 * 1024 * 1024  # 10 MB
DATA_UPLOAD_MAX_MEMORY_SIZE = 10 * 1024 * 1024  # 10 MB


# ---------------------------------------------------------------------------
# Phase 7.7: PO auto-fulfillment
# ---------------------------------------------------------------------------
# When a PO with linked PR(s) attached to a specific WO is received, the
# receive flow auto-calls `execute_warehouse_issue` for matching open
# PartIssueLines on that WO. This closes the loop between the supplier
# delivery and the WO consumption, so the user no longer needs to
# manually click "📤 Issue N from stock" on the WO page after every
# receive.
#
# Safety: when OFF, the receive flow keeps the existing behaviour
# (stock added, WO awaiting manual warehouse issue). When ON, the
# receive flow also issues the stock to the WO automatically.
# Stock-only PRs (work_order_id = NULL) are never auto-issued — they
# only replenish inventory.
PO_AUTO_ISSUE = os.environ.get("MMS_PO_AUTO_ISSUE", "True").lower() in (
    "true", "1", "yes", "on",
)


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
# Minimal logging config. stderr for everything; per-app loggers inherit.
# DEBUG=False hides Django's verbose request logging; tests/dev see INFO.
# Gunicorn --access-logfile / --error-logfile - forwards to stdout/stderr
# which is captured by Docker / CranL log shipping.
# Phase 1 v1.0.0 — structured JSON logging is deferred to v1.1.
LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "verbose": {
            "format": "{asctime} {levelname} {name} {message}",
            "style": "{",
        },
    },
    "handlers": {
        "console": {
            "class": "logging.StreamHandler",
            "formatter": "verbose",
        },
    },
    "root": {
        "handlers": ["console"],
        "level": "INFO",
    },
    "loggers": {
        "django": {
            "handlers": ["console"],
            "level": "INFO" if not DEBUG else "DEBUG",
            "propagate": False,
        },
        "maintenance": {
            "handlers": ["console"],
            "level": "INFO",
            "propagate": False,
        },
        "inventory": {
            "handlers": ["console"],
            "level": "INFO",
            "propagate": False,
        },
        "procurement": {
            "handlers": ["console"],
            "level": "INFO",
            "propagate": False,
        },
    },
}


# ---------------------------------------------------------------------------
# Production hardening (only when DEBUG=False AND not running tests)
# ---------------------------------------------------------------------------
# CranL terminates TLS at the edge, so Django trusts the X-Forwarded-Proto
# header from the proxy. Set TRUSTED_PROXY_CIDR on the host so
# --forwarded-allow-ips in gunicorn is locked down to the proxy.
#
# Tests run with DEBUG=False but over plain HTTP. Enabling
# SECURE_SSL_REDIRECT in tests causes every authenticated request to 301
# before tests can assert on 200, breaking the suite. We skip these
# settings when running under `manage.py test` so the test client can hit
# plain HTTP.
import sys as _sys
_RUNNING_TESTS = "test" in _sys.argv
if not DEBUG and not _RUNNING_TESTS:
    SECURE_SSL_REDIRECT = True
    SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
    SECURE_HSTS_SECONDS = 31536000
    SECURE_HSTS_INCLUDE_SUBDOMAINS = True
    SECURE_HSTS_PRELOAD = True
    SECURE_CONTENT_TYPE_NOSNIFF = True
    SECURE_REFERRER_POLICY = "same-origin"
    SESSION_COOKIE_SECURE = True
    SESSION_COOKIE_HTTPONLY = True
    SESSION_COOKIE_SAMESITE = "Lax"
    SESSION_EXPIRE_AT_BROWSER_CLOSE = True
    CSRF_COOKIE_SECURE = True
    CSRF_COOKIE_SAMESITE = "Lax"
    X_FRAME_OPTIONS = "DENY"