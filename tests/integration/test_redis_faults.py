import json
import os
import subprocess
import time
from urllib.parse import urlsplit
from uuid import uuid4

import pytest
from django.http import HttpResponse
from django.test import RequestFactory
from redis import Redis
from redis.exceptions import RedisError

from apps.reports.request_limits import RequestProtectionMiddleware, consume_budget, guard_cache

pytestmark = pytest.mark.redis_integration


@pytest.fixture
def fault_redis(settings):
    url = os.getenv("REDIS_FAULT_TEST_URL")
    if not url:
        if os.getenv("REDIS_ALLOW_FAULT_TESTS") == "yes":
            pytest.fail("Enabled fault tests require REDIS_FAULT_TEST_URL.")
        pytest.skip("Set REDIS_FAULT_TEST_URL to a separate disposable Redis server.")
    if os.getenv("REDIS_ALLOW_FAULT_TESTS") != "yes":
        pytest.fail("Fault tests require REDIS_ALLOW_FAULT_TESTS=yes on a disposable server.")
    address = urlsplit(url)
    assert address.hostname in {"127.0.0.1", "localhost", "::1"}
    admin = Redis.from_url(url, socket_connect_timeout=1, socket_timeout=1)
    assert admin.ping()
    identity = admin.info("server")["run_id"]
    for name in ("REDIS_TEST_URL", "REQUEST_LIMITS_REDIS_URL"):
        normal_url = os.getenv(name)
        if normal_url:
            with Redis.from_url(normal_url, socket_connect_timeout=1, socket_timeout=1) as normal:
                assert normal.info("server")["run_id"] != identity, "Fault server must be separate."
    assert admin.info("replication")["role"] == "master"
    assert admin.info("keyspace") == {}, "Fault server must be empty across all databases."
    assert admin.config_get("maxmemory-policy")["maxmemory-policy"] == "noeviction"
    maximum = admin.config_get("maxmemory")["maxmemory"]
    snapshots = admin.config_get("save")["save"]
    prefix = f"campus-fault-{uuid4().hex}"
    settings.DEBUG = False
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
        yield admin, prefix
    finally:
        try:
            admin.config_set("maxmemory", maximum)
            admin.config_set("save", snapshots)
            for key in admin.scan_iter(match=f"{prefix}:*"):
                admin.delete(key)
        finally:
            admin.close()


def test_noeviction_memory_failure_denies_writes_preserves_counters_and_recovers(fault_redis):
    admin, prefix = fault_redis
    backend = guard_cache()
    assert consume_budget(backend, "memory", "test-user", 1, 3600, now=125) == 0
    keys = list(admin.scan_iter(match=f"{prefix}:*"))
    assert len(keys) == 1
    value = admin.get(keys[0])
    evicted = admin.info("stats")["evicted_keys"]
    maximum = admin.config_get("maxmemory")["maxmemory"]
    reached = []

    def view(request):
        reached.append(True)
        return HttpResponse("ok")

    middleware = RequestProtectionMiddleware(view)
    admin.config_set("maxmemory", 1)
    response = middleware(RequestFactory().post("/api/v1/reports/"))
    assert response.status_code == 503
    assert response["Retry-After"] == "5"
    assert b"request_guard_unavailable" in response.content
    assert reached == []
    assert admin.get(keys[0]) == value
    assert admin.info("stats")["evicted_keys"] == evicted
    admin.config_set("maxmemory", maximum)
    assert middleware(RequestFactory().post("/api/v1/reports/")).status_code == 200
    assert reached == [True]
    assert consume_budget(backend, "memory", "test-user", 1, 3600, now=125) > 0


def test_volatile_restart_denies_during_outage_reconnects_and_resets_budget(fault_redis):
    commands = []
    for name in ("REDIS_FAULT_STOP_COMMAND", "REDIS_FAULT_START_COMMAND"):
        value = os.getenv(name)
        if not value:
            pytest.fail(f"Set {name} for the disposable Redis lifecycle test.")
        command = json.loads(value)
        assert isinstance(command, list) and command and all(isinstance(arg, str) for arg in command)
        commands.append(command)
    admin, _ = fault_redis
    assert admin.config_get("appendonly")["appendonly"] == "no"
    admin.config_set("save", "")
    backend = guard_cache()
    previous_run = admin.info("server")["run_id"]
    assert consume_budget(backend, "restart", "test-user", 1, 3600, now=125) == 0
    assert consume_budget(backend, "restart", "test-user", 1, 3600, now=125) > 0
    reached = []

    def view(request):
        reached.append(True)
        return HttpResponse("ok")

    middleware = RequestProtectionMiddleware(view)
    try:
        subprocess.run(commands[0], check=True, timeout=30, capture_output=True)
        response = middleware(RequestFactory().post("/api/v1/reports/"))
        assert response.status_code == 503
        assert reached == []
    finally:
        subprocess.run(commands[1], check=True, timeout=30, capture_output=True)
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            try:
                if admin.ping():
                    break
            except RedisError:
                pass
            time.sleep(0.1)
        else:
            pytest.fail("Disposable Redis did not recover after restart.")
    assert admin.info("server")["run_id"] != previous_run
    assert backend is guard_cache()
    assert consume_budget(backend, "restart", "test-user", 1, 3600, now=125) == 0
    assert middleware(RequestFactory().post("/api/v1/reports/")).status_code == 200
    assert reached == [True]
