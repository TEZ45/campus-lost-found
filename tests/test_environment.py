import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
PRODUCTION = {
    "APP_ENV": "production",
    "SECRET_KEY": "test-only-secret-0123456789-abcdefghijklmnopqrstuvwxyz-ABCDEFGHIJKLM",
    "ALLOWED_HOSTS": "lost-found.example.edu",
    "DB_ENGINE": "postgresql",
    "DB_NAME": "campus_test",
    "DB_USER": "campus_runtime",
    "DB_PASSWORD": "test-only-database-password",
    "DB_HOST": "127.0.0.1",
    "REQUEST_LIMITS_REDIS_URL": "redis://127.0.0.1:6379/15",
    "RECOVERY_STATE_FILE": str(ROOT.parent / "test-control" / "recovery.json"),
    "OPS_STATE_DIR": str(ROOT.parent / "test-operations"),
}
READ_SETTINGS = """
import json
from unittest.mock import patch

with patch('dotenv.load_dotenv') as dotenv:
    from config import settings
database = settings.DATABASES['default'].copy()
database.pop('PASSWORD', None)
print(json.dumps({
    'profile': settings.APP_ENV, 'debug': settings.DEBUG,
    'database': database, 'hosts': settings.ALLOWED_HOSTS,
    'redirect': settings.SECURE_SSL_REDIRECT,
    'secure_session': settings.SESSION_COOKIE_SECURE,
    'secure_csrf': settings.CSRF_COOKIE_SECURE,
    'hsts': settings.SECURE_HSTS_SECONDS,
    'hsts_subdomains': settings.SECURE_HSTS_INCLUDE_SUBDOMAINS,
    'hsts_preload': settings.SECURE_HSTS_PRELOAD,
    'proxy': settings.SECURE_PROXY_SSL_HEADER,
    'redis': settings.CACHES['request_limits']['BACKEND'],
    'prefix': settings.CACHES['request_limits']['KEY_PREFIX'],
    'csrf_origins': settings.CSRF_TRUSTED_ORIGINS,
    'dotenv_loaded': dotenv.called,
}, default=str))
"""


def run_settings(values, code=READ_SETTINGS):
    names = {
        "APP_ENV", "DEBUG", "SECRET_KEY", "ALLOWED_HOSTS", "TRUST_PROXY_HTTPS",
        "CSRF_TRUSTED_ORIGINS", "DJANGO_SETTINGS_MODULE",
        "RECOVERY_STATE_FILE", "OPS_STATE_DIR", "BACKUP_ROOT", "MEDIA_ROOT", "PRIVATE_MEDIA_ROOT",
    }
    env = {key: value for key, value in os.environ.items()
           if key not in names and not key.startswith(("DB_", "REQUEST_LIMITS_", "SECURE_HSTS_"))}
    env.update(values, DJANGO_SETTINGS_MODULE="config.settings")
    return subprocess.run([sys.executable, "-c", code], cwd=ROOT, env=env,
                          capture_output=True, text=True, timeout=20, check=False)


def read_settings(values):
    result = run_settings(values)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


@pytest.mark.parametrize("profile,loads_dotenv", [("development", True), ("test", False)])
def test_local_profiles_keep_sqlite_and_skip_dotenv_in_tests(profile, loads_dotenv):
    result = read_settings({"APP_ENV": profile})
    assert result["profile"] == profile
    assert result["dotenv_loaded"] is loads_dotenv
    assert result["database"]["ENGINE"] == "django.db.backends.sqlite3"
    assert result["database"]["OPTIONS"]["transaction_mode"] == "IMMEDIATE"
    assert result["debug"] is True
    assert result["redirect"] is False
    assert result["proxy"] is None


def test_postgresql_uses_explicit_identity_and_bounded_database_calls():
    result = read_settings({
        "APP_ENV": "test", "DB_ENGINE": "postgresql", "DB_USER": "only_this_role",
        "DB_PORT": "55432", "DB_CONNECT_TIMEOUT": "3", "DB_STATEMENT_TIMEOUT_MS": "7000",
        "DB_LOCK_TIMEOUT_MS": "2000", "DB_SSLMODE": "disable",
    })
    database = result["database"]
    assert database["ENGINE"] == "django.db.backends.postgresql"
    assert database["USER"] == "only_this_role"
    assert database["PORT"] == 55432
    assert database["OPTIONS"] == {
        "connect_timeout": 3, "sslmode": "disable",
        "options": "-c statement_timeout=7000 -c lock_timeout=2000",
    }
    assert "transaction_mode" not in database["OPTIONS"]
    assert database["CONN_MAX_AGE"] == 0


def test_production_enables_https_without_implicitly_trusting_proxy_headers():
    result = read_settings(PRODUCTION)
    assert result["dotenv_loaded"] is False
    assert result["debug"] is False
    assert result["redirect"] and result["secure_session"] and result["secure_csrf"]
    assert result["hsts"] == 3600
    assert result["hsts_subdomains"] is False and result["hsts_preload"] is False
    assert result["proxy"] is None
    assert result["database"]["OPTIONS"]["sslmode"] == "verify-full"
    assert result["database"]["CONN_MAX_AGE"] == 60
    assert result["redis"] == "django.core.cache.backends.redis.RedisCache"


@pytest.mark.parametrize("value", ["-1", "301", "unlimited"])
def test_database_connection_reuse_is_bounded(value):
    result = run_settings({**PRODUCTION, "DB_CONN_MAX_AGE": value})
    assert result.returncode != 0
    assert "DB_CONN_MAX_AGE" in result.stderr


def test_database_connection_reuse_can_be_disabled():
    result = read_settings({**PRODUCTION, "DB_CONN_MAX_AGE": "0"})
    assert result["database"]["CONN_MAX_AGE"] == 0


def test_explicit_proxy_and_cache_namespace_settings():
    result = read_settings({
        **PRODUCTION, "TRUST_PROXY_HTTPS": "true", "REQUEST_LIMITS_TRUSTED_PROXIES": "127.0.0.1/32",
        "REQUEST_LIMITS_KEY_PREFIX": "campus-test:isolated",
        "CSRF_TRUSTED_ORIGINS": "https://frontend.example.edu",
    })
    assert result["proxy"] == ["HTTP_X_FORWARDED_PROTO", "https"]
    assert result["prefix"] == "campus-test:isolated"
    assert result["csrf_origins"] == ["https://frontend.example.edu"]


def test_hsts_domain_coverage_requires_explicit_opt_in():
    result = read_settings({
        **PRODUCTION, "SECURE_HSTS_SECONDS": "31536000",
        "SECURE_HSTS_INCLUDE_SUBDOMAINS": "true", "SECURE_HSTS_PRELOAD": "true",
    })
    assert result["hsts"] == 31536000
    assert result["hsts_subdomains"] and result["hsts_preload"]


def test_hsts_preload_rejects_short_duration_even_with_subdomains():
    result = run_settings({
        **PRODUCTION, "SECURE_HSTS_INCLUDE_SUBDOMAINS": "true", "SECURE_HSTS_PRELOAD": "true",
    })
    assert result.returncode != 0
    assert "SECURE_HSTS_PRELOAD" in result.stderr


def test_dotenv_cannot_switch_the_selected_profile():
    result = run_settings({"APP_ENV": "development"}, """
import os
from unittest.mock import patch

def load_dotenv(*args):
    os.environ['APP_ENV'] = 'production'

with patch('dotenv.load_dotenv', side_effect=load_dotenv):
    import config.settings
""")
    assert result.returncode != 0
    assert "Set APP_ENV in the process environment" in result.stderr


@pytest.mark.parametrize("name", [
    "SECRET_KEY", "ALLOWED_HOSTS", "DB_NAME", "DB_USER", "DB_PASSWORD", "DB_HOST",
    "REQUEST_LIMITS_REDIS_URL", "RECOVERY_STATE_FILE", "OPS_STATE_DIR",
])
def test_missing_production_inputs_fail_without_loading_dotenv(name):
    env = {key: value for key, value in PRODUCTION.items() if key != name}
    result = run_settings(env)
    assert result.returncode != 0
    assert "ImproperlyConfigured" in result.stderr and name in result.stderr


@pytest.mark.parametrize("name,value", [
    ("APP_ENV", "prodution"), ("DEBUG", "true"), ("DEBUG", "typo"),
    ("SECRET_KEY", "dev-insecure-key-change-in-production"), ("SECRET_KEY", "x" * 60),
    ("ALLOWED_HOSTS", "*"), ("ALLOWED_HOSTS", ".example.edu"),
    ("ALLOWED_HOSTS", "https://example.edu"), ("ALLOWED_HOSTS", "example.edu:443"),
    ("ALLOWED_HOSTS", "[[::1]]"),
    ("DB_ENGINE", "sqlite"), ("DB_ENGINE", "mysql"), ("DB_PASSWORD", "short"),
    ("DB_PORT", "65536"), ("DB_CONNECT_TIMEOUT", "0"), ("DB_STATEMENT_TIMEOUT_MS", "bad"),
    ("DB_LOCK_TIMEOUT_MS", "-1"), ("DB_SSLMODE", "disable"), ("DB_SSLMODE", "prefer"),
    ("DB_SSLMODE", "invalid"), ("REQUEST_LIMITS_REDIS_URL", "https://example.edu"),
    ("REQUEST_LIMITS_REDIS_URL", "redis://localhost:6379/0,redis://localhost:6380/0"),
    ("REQUEST_LIMITS_REDIS_URL", "redis://localhost:invalid/0"),
    ("REQUEST_LIMITS_REDIS_URL", "redis://localhost/not-a-database"),
    ("REQUEST_LIMITS_REDIS_URL", "redis://localhost/0?socket_timeout=0"),
    ("REQUEST_LIMITS_KEY_PREFIX", ""), ("REQUEST_LIMITS_KEY_PREFIX", "two namespaces"),
    ("REQUEST_LIMITS_TRUSTED_PROXIES", "not-a-network"),
    ("TRUST_PROXY_HTTPS", "true"), ("CSRF_TRUSTED_ORIGINS", "http://frontend.example.edu"),
    ("CSRF_TRUSTED_ORIGINS", "https://*.example.edu"),
    ("SECURE_HSTS_PRELOAD", "true"), ("SECURE_HSTS_SECONDS", "-1"),
    ("RECOVERY_STATE_FILE", "relative.json"), ("OPS_STATE_DIR", "relative"),
    ("RECOVERY_STATE_FILE", str(ROOT / "private_media" / "state.json")),
    ("OPS_STATE_DIR", str(ROOT / "media" / "operations")),
])
def test_invalid_configuration_fails_before_database_access(name, value):
    result = run_settings({**PRODUCTION, name: value})
    assert result.returncode != 0
    assert "ImproperlyConfigured" in result.stderr and name in result.stderr


def test_production_deployment_checks_need_no_database_connection():
    result = run_settings({
        **PRODUCTION, "SECURE_HSTS_SECONDS": "31536000",
        "SECURE_HSTS_INCLUDE_SUBDOMAINS": "true", "SECURE_HSTS_PRELOAD": "true",
    }, """
import django
from django.core.management import call_command
from unittest.mock import patch

with patch('django.db.backends.base.base.BaseDatabaseWrapper.connect',
           side_effect=AssertionError('Deployment checks must not connect to the database')):
    django.setup()
    call_command('check', deploy=True, fail_level='WARNING')
""")
    assert result.returncode == 0, result.stderr


def test_recovery_and_operations_state_cannot_share_a_directory():
    result = run_settings({**PRODUCTION, "OPS_STATE_DIR": str(ROOT.parent / "test-control")})
    assert result.returncode != 0
    assert "separate directories" in result.stderr
