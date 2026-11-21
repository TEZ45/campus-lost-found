import os
from uuid import uuid4

import pytest
from django.core.cache.backends.redis import RedisCacheClient
from redis import Redis
from redis.exceptions import RedisError


def pytest_configure():
    backend = os.getenv("TEST_REQUEST_LIMITS_BACKEND", "locmem")
    if backend not in {"locmem", "redis"}:
        raise pytest.UsageError("TEST_REQUEST_LIMITS_BACKEND must be locmem or redis.")
    if backend == "redis":
        url = os.getenv("REDIS_TEST_URL")
        if not url:
            raise pytest.UsageError("Redis test mode requires REDIS_TEST_URL.")
        try:
            with Redis.from_url(url, socket_connect_timeout=1, socket_timeout=1) as connection:
                connection.ping()
                policy = connection.config_get("maxmemory-policy")["maxmemory-policy"]
                if policy != "noeviction":
                    raise pytest.UsageError("Redis tests require maxmemory-policy=noeviction.")
        except (RedisError, ValueError, OSError) as exc:
            raise pytest.UsageError(f"Redis test service is unavailable: {type(exc).__name__}.") from None


@pytest.fixture(autouse=True)
def redis_pool_cleanup(monkeypatch):
    clients = []
    original = RedisCacheClient.__init__

    def track(client, *args, **kwargs):
        original(client, *args, **kwargs)
        clients.append(client)

    monkeypatch.setattr(RedisCacheClient, "__init__", track)
    try:
        yield
    finally:
        # Django's cache close hook does not disconnect pools abandoned by settings overrides.
        for client in clients:
            for pool in client._pools.values():
                pool.disconnect()


@pytest.fixture(autouse=True)
def request_counter_isolation(settings, request, redis_pool_cleanup):
    if request.node.get_closest_marker("redis_integration"):
        yield
        return
    if os.getenv("TEST_REQUEST_LIMITS_BACKEND") == "redis":
        prefix = f"campus-suite-{uuid4().hex}"
        url = os.environ["REDIS_TEST_URL"]
        settings.CACHES = {
            **settings.CACHES,
            "request_limits": {
                "BACKEND": "django.core.cache.backends.redis.RedisCache",
                "LOCATION": url,
                "KEY_PREFIX": prefix,
                "OPTIONS": {"socket_connect_timeout": 1, "socket_timeout": 1},
            },
        }
        try:
            yield
        finally:
            with Redis.from_url(url, socket_connect_timeout=1, socket_timeout=1) as connection:
                for key in connection.scan_iter(match=f"{prefix}:*"):
                    connection.delete(key)
        return
    settings.CACHES = {
        **settings.CACHES,
        "request_limits": {
            "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
            "LOCATION": "isolated-request-limits",
        },
    }
    from django.core.cache import caches

    counter = caches["request_limits"]
    counter.clear()
    yield
    counter.clear()


@pytest.fixture(scope="session")
def django_db_modify_db_settings(django_db_modify_db_settings_parallel_suffix, tmp_path_factory):
    from django.conf import settings

    database = settings.DATABASES["default"]
    if database["ENGINE"] == "django.db.backends.sqlite3":
        database.setdefault("TEST", {})["NAME"] = str(
            tmp_path_factory.mktemp("database") / "test.sqlite3"
        )


@pytest.fixture(autouse=True)
def media_roots(settings, tmp_path):
    settings.MEDIA_ROOT = tmp_path / "public"
    settings.PRIVATE_MEDIA_ROOT = tmp_path / "private"


@pytest.fixture
def users(db):
    from django.contrib.auth.models import User

    from apps.reports.models import UserProfile

    users = {name: User.objects.create_user(name) for name in (
        "owner", "claimant", "other", "service", "admin", "service_admin",
    )}
    for name in ("service", "service_admin"):
        UserProfile.objects.create(user=users[name], is_service_staff=True)
    for name in ("admin", "service_admin"):
        users[name].is_staff = True
        users[name].is_superuser = True
        users[name].save()
    return users
