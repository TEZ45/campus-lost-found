import multiprocessing
import os
import socket
import time
from concurrent.futures import ProcessPoolExecutor
from types import SimpleNamespace
from uuid import uuid4

import pytest
from django.core.cache.backends.redis import RedisCache
from django.http import HttpResponse
from django.test import RequestFactory
from redis import Redis

from apps.reports import request_limits

pytestmark = pytest.mark.redis_integration


def _initialize_worker(barrier):
    global worker_barrier
    worker_barrier = barrier


def _spend_budget(url, prefix):
    backend = RedisCache(url, {
        "KEY_PREFIX": prefix,
        "OPTIONS": {"socket_connect_timeout": 1, "socket_timeout": 1},
    })
    worker_barrier.wait(timeout=20)
    waits = [
        request_limits.consume_budget(backend, "shared", "user:7", 13, 60, now=125)
        for _ in range(20)
    ]
    backend.close()
    return os.getpid(), waits


@pytest.fixture
def real_redis(settings, monkeypatch):
    url = os.getenv("REDIS_TEST_URL")
    if not url:
        pytest.skip("Set REDIS_TEST_URL to an isolated test Redis service.")
    prefix = f"campus-test-{uuid4().hex}"
    connection = Redis.from_url(url, socket_connect_timeout=1, socket_timeout=1)
    assert connection.ping()
    assert connection.config_get("maxmemory-policy")["maxmemory-policy"] == "noeviction"
    settings.DEBUG = False
    settings.SECURE_SSL_REDIRECT = False
    settings.REQUEST_LIMITS_IP_ENDPOINT_MULTIPLIER = 1
    settings.REQUEST_LIMITS_TRUSTED_PROXIES = []
    settings.CACHES = {
        **settings.CACHES,
        "request_limits": {
            "BACKEND": "django.core.cache.backends.redis.RedisCache",
            "LOCATION": url,
            "KEY_PREFIX": prefix,
            "OPTIONS": {"socket_connect_timeout": 1, "socket_timeout": 1},
        },
    }
    monkeypatch.setattr(request_limits, "time", SimpleNamespace(time=lambda: 125))
    assert isinstance(request_limits.guard_cache(), RedisCache)
    yield url, prefix
    try:
        for key in connection.scan_iter(match=f"{prefix}:*"):
            connection.delete(key)
    finally:
        connection.close()


def test_real_redis_shares_exact_admission_across_spawned_processes(real_redis):
    url, prefix = real_redis
    context = multiprocessing.get_context("spawn")
    with ProcessPoolExecutor(
        max_workers=4, mp_context=context, initializer=_initialize_worker,
        initargs=(context.Barrier(4),),
    ) as pool:
        futures = [pool.submit(_spend_budget, url, prefix) for _ in range(4)]
        results = [future.result(timeout=30) for future in futures]
    assert len({pid for pid, _ in results}) == 4
    waits = [wait for _, values in results for wait in values]
    assert waits.count(0) == 13
    assert waits.count(55) == 67


def test_real_redis_rejects_html_and_api_before_login_parsing(real_redis, client, settings):
    settings.REQUEST_LIMITS = {**settings.REQUEST_LIMITS, "login": [(1, 60)]}
    response = client.post("/api/v1/auth/login/", data=b"{", content_type="application/json")
    assert response.status_code == 400
    response = client.post("/login/", data=b"{", content_type="application/json")
    assert response.status_code == 429
    assert response["Retry-After"] == "55"
    assert response["Cache-Control"] == "private, no-store"
    response = client.post("/api/v1/auth/login/", data=b"{", content_type="application/json")
    assert response.status_code == 429
    assert response.json()["error"]["code"] == "rate_limited"


def test_real_redis_expires_counters_without_extending_ttl_on_increment(real_redis):
    url, prefix = real_redis
    backend = request_limits.guard_cache()
    with Redis.from_url(url, socket_connect_timeout=1, socket_timeout=1) as connection:
        assert request_limits.consume_budget(backend, "expiry", "user:7", 1, 1, now=0.5) == 0
        keys = list(connection.scan_iter(match=f"{prefix}:*"))
        assert len(keys) == 1
        initial_ttl = connection.pttl(keys[0])
        assert 0 < initial_ttl <= 2000
        time.sleep(0.05)
        before_increment = connection.pttl(keys[0])
        assert 0 < before_increment < initial_ttl
        assert request_limits.consume_budget(backend, "expiry", "user:7", 1, 1, now=0.5) == 1
        assert 0 < connection.pttl(keys[0]) <= before_increment
        deadline = time.monotonic() + 5
        while connection.exists(keys[0]) and time.monotonic() < deadline:
            time.sleep(0.05)
        assert not connection.exists(keys[0])
        assert request_limits.consume_budget(backend, "expiry", "user:7", 1, 1, now=1.5) == 0


@pytest.mark.django_db
def test_real_redis_account_budget_follows_user_between_ips(real_redis, client, settings, users):
    settings.REQUEST_LIMITS = {**settings.REQUEST_LIMITS, "account_burst": [(1, 60)]}
    client.force_login(users["owner"])
    assert client.get("/api/v1/reports/", REMOTE_ADDR="192.0.2.1").status_code == 200
    response = client.get("/api/v1/reports/", REMOTE_ADDR="192.0.2.2")
    assert response.status_code == 429
    assert response.json()["error"]["code"] == "rate_limited"
    client.force_login(users["other"])
    assert client.get("/api/v1/reports/", REMOTE_ADDR="192.0.2.2").status_code == 200


def test_real_redis_connection_failure_denies_before_business_work(real_redis, settings):
    reached = []

    def business_view(request):
        reached.append(True)
        return HttpResponse("ok")

    with socket.socket() as unavailable:
        unavailable.bind(("127.0.0.1", 0))
        port = unavailable.getsockname()[1]
        configuration = settings.CACHES["request_limits"]
        settings.CACHES = {
            **settings.CACHES,
            "request_limits": {**configuration, "LOCATION": f"redis://127.0.0.1:{port}/0"},
        }
        request = RequestFactory().post("/api/v1/reports/")
        response = request_limits.RequestProtectionMiddleware(business_view)(request)
    assert response.status_code == 503
    assert response["Retry-After"] == "5"
    assert response["Cache-Control"] == "private, no-store"
    assert b"request_guard_unavailable" in response.content
    assert reached == []
    settings.CACHES = {**settings.CACHES, "request_limits": configuration}
    recovered = RequestFactory().post("/api/v1/reports/")
    response = request_limits.RequestProtectionMiddleware(business_view)(recovered)
    assert response.status_code == 200
    assert reached == [True]
