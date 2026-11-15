"""
Django settings for Campus Lost and Found project.
"""
import os
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent

load_dotenv(BASE_DIR / ".env")

SECRET_KEY = os.getenv("SECRET_KEY", "dev-insecure-key-change-in-production")
DEBUG = os.getenv("DEBUG", "True") == "True"
ALLOWED_HOSTS = os.getenv("ALLOWED_HOSTS", "localhost,127.0.0.1").split(",")

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    # Third-party
    "rest_framework",
    # Local apps
    "apps.reports",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "apps.reports.request_limits.RequestProtectionMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "config.urls"

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
                "apps.reports.context_processors.unread_notices",
            ],
        },
    },
]

WSGI_APPLICATION = "config.wsgi.application"

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": BASE_DIR / "db.sqlite3",
        "OPTIONS": {"transaction_mode": "IMMEDIATE", "timeout": 20},
    }
}

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

LANGUAGE_CODE = "en-us"
TIME_ZONE = "Asia/Shanghai"
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"
STATICFILES_DIRS = []

MEDIA_URL = "media/"
MEDIA_ROOT = BASE_DIR / "media"
PRIVATE_MEDIA_ROOT = BASE_DIR / "private_media"
STORAGES = {
    "default": {"BACKEND": "apps.reports.managed_storage.ManagedFileSystemStorage"},
    "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
}

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

LOGIN_URL = "/login/"
LOGIN_REDIRECT_URL = "/"
LOGOUT_REDIRECT_URL = "/"

# Non-file form/JSON memory budget. Multipart file bytes are excluded by Django.
DATA_UPLOAD_MAX_MEMORY_SIZE = 6 * 1024 * 1024
FILE_UPLOAD_MAX_MEMORY_SIZE = 256 * 1024
FILE_UPLOAD_HANDLERS = [
    "apps.reports.upload_limits.AggregateUploadLimitHandler",
    "django.core.files.uploadhandler.MemoryFileUploadHandler",
    "django.core.files.uploadhandler.TemporaryFileUploadHandler",
]

# Three 5 MB files remain valid, with 2 MB reserved for multipart/form overhead.
REQUEST_MAX_BODY_BYTES = int(os.getenv("REQUEST_MAX_BODY_BYTES", str(17 * 1024 * 1024)))
REQUEST_MAX_UPLOAD_BYTES = int(os.getenv("REQUEST_MAX_UPLOAD_BYTES", str(15 * 1024 * 1024)))
MAX_IMAGE_PIXELS = int(os.getenv("MAX_IMAGE_PIXELS", "20000000"))
STORAGE_CAPACITY_BYTES = int(os.getenv("STORAGE_CAPACITY_BYTES", str(100 * 1024 ** 3)))
MATCH_MAX_PAIRS = int(os.getenv("MATCH_MAX_PAIRS", "2500"))
MATCH_MAX_SECONDS = int(os.getenv("MATCH_MAX_SECONDS", "10"))
MATCH_COOLDOWN_SECONDS = int(os.getenv("MATCH_COOLDOWN_SECONDS", "30"))
REQUEST_LIMITS_CACHE_ALIAS = "request_limits"
REQUEST_LIMITS_IP_ENDPOINT_MULTIPLIER = int(
    os.getenv("REQUEST_LIMITS_IP_ENDPOINT_MULTIPLIER", "20")
)
REQUEST_LIMITS_TRUSTED_PROXIES = [
    value.strip() for value in os.getenv("REQUEST_LIMITS_TRUSTED_PROXIES", "").split(",")
    if value.strip()
]
REQUEST_LIMITS_REDIS_URL = os.getenv("REQUEST_LIMITS_REDIS_URL", "")
CACHES = {
    "default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"},
    "request_limits": {
        "BACKEND": (
            "django.core.cache.backends.redis.RedisCache" if REQUEST_LIMITS_REDIS_URL
            else "django.core.cache.backends.locmem.LocMemCache"
        ),
        "LOCATION": REQUEST_LIMITS_REDIS_URL or "development-request-limits",
        # Avoid evicting active development counters under ordinary local use.
        "OPTIONS": (
            {"socket_connect_timeout": 1, "socket_timeout": 1}
            if REQUEST_LIMITS_REDIS_URL else {"MAX_ENTRIES": 100000}
        ),
        "KEY_PREFIX": "campus-lost-found",
    },
}

# Atomic fixed windows: each tuple is (requests, seconds). Adjacent windows can
# allow twice one window's limit; independent burst and sustained caps both apply.
# Endpoint caps below are per account; IP endpoint caps multiply these values to
# accommodate shared campus NATs. Measure actual traffic before production tuning.
REQUEST_LIMITS = {
    "ip_burst": [(3000, 60)],
    "ip_sustained": [(30000, 3600)],
    "account_burst": [(300, 60)],
    "account_sustained": [(3000, 3600)],
    "login": [(30, 60), (100, 3600)],
    "register": [(10, 60), (100, 3600)],
    "write": [(60, 60), (600, 3600)],
    "upload": [(20, 60), (200, 3600)],
    "search": [(120, 60), (1200, 3600)],
}

from apps.reports.request_limits import register_request_limit_checks

register_request_limit_checks()
