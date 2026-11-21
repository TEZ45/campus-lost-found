"""Request budgets apply before authentication, body parsing and business views."""

from concurrent.futures import ThreadPoolExecutor
from io import BytesIO
from threading import Lock
from unittest.mock import Mock, patch

import pytest
from django.core.cache.backends.locmem import LocMemCache
from django.core.exceptions import RequestDataTooBig
from django.core.files.uploadedfile import SimpleUploadedFile
from django.http import HttpResponse, JsonResponse
from django.test import RequestFactory
from django.urls import path

from apps.reports.request_limits import (
    BodyLimitStream,
    RequestLimitUnavailable,
    RequestProtectionMiddleware,
    check_request_limit_configuration,
    client_ip,
    consume_budget,
    guard_cache,
)


def simple_view(request):
    return HttpResponse("ok")


def parsed_upload_view(request):
    files = request.FILES.getlist("images")
    return JsonResponse({"files": len(files), "bytes": sum(file.size for file in files)})


urlpatterns = [
    path("login/", simple_view),
    path("register/", simple_view),
    path("report/new/", simple_view),
    path("api/v1/auth/login/", simple_view),
    path("api/v1/auth/register/", simple_view),
    path("api/v1/reports/", simple_view),
    path("api/v1/reports/1/claims/", parsed_upload_view),
    path("", simple_view),
]


@pytest.fixture(autouse=True)
def isolated_guard(settings):
    settings.DEBUG = True
    settings.REQUEST_LIMITS_TRUSTED_PROXIES = []
    settings.REQUEST_LIMITS_IP_ENDPOINT_MULTIPLIER = 1
    settings.ROOT_URLCONF = __name__
    settings.REQUEST_LIMITS = {
        scope: [(1000, 60)] for scope in settings.REQUEST_LIMITS
    }


def set_budget(settings, scope, limit, seconds=60):
    settings.REQUEST_LIMITS = {**settings.REQUEST_LIMITS, scope: [(limit, seconds)]}


@pytest.mark.parametrize("endpoint", ["/login/", "/api/v1/auth/login/"])
def test_login_budget_returns_429_and_retry_after_before_view(client, settings, endpoint):
    set_budget(settings, "login", 1)
    assert client.post(endpoint).status_code == 200
    response = client.post(endpoint)
    assert response.status_code == 429
    assert 1 <= int(response["Retry-After"]) <= 60
    assert response["Cache-Control"] == "private, no-store"
    if endpoint.startswith("/api/"):
        assert response.json()["error"]["code"] == "rate_limited"
    else:
        assert response["Content-Type"].startswith("text/plain")


def test_login_budget_is_shared_between_html_and_api(client, settings):
    set_budget(settings, "login", 1)
    assert client.post("/login/").status_code == 200
    assert client.post("/api/v1/auth/login/").status_code == 429


@pytest.mark.parametrize("scope,endpoints,method", [
    ("register", ["/register/", "/api/v1/auth/register/"], "post"),
    ("write", ["/register/", "/report/new/"], "post"),
    ("upload", ["/report/new/", "/api/v1/reports/"], "post"),
    ("search", ["/", "/api/v1/reports/"], "get"),
])
def test_endpoint_budgets_cover_html_and_api(client, settings, scope, endpoints, method):
    set_budget(settings, scope, 1)
    send = getattr(client, method)
    assert send(endpoints[0]).status_code == 200
    assert send(endpoints[1]).status_code == 429


@pytest.mark.parametrize("scope", ["ip_burst", "ip_sustained"])
def test_ip_budgets_cover_all_routes(client, settings, scope):
    set_budget(settings, scope, 1)
    assert client.get("/login/").status_code == 200
    assert client.get("/api/v1/reports/").status_code == 429
    assert client.get("/api/v1/reports/", REMOTE_ADDR="192.0.2.2").status_code == 200


@pytest.mark.django_db
@pytest.mark.parametrize("scope", ["account_burst", "account_sustained", "write"])
def test_account_budget_follows_user_across_ips_and_routes(client, users, settings, scope):
    set_budget(settings, scope, 1)
    client.force_login(users["owner"])
    assert client.post("/report/new/", REMOTE_ADDR="192.0.2.1").status_code == 200
    assert client.post("/api/v1/reports/", REMOTE_ADDR="192.0.2.2").status_code == 429
    client.force_login(users["other"])
    assert client.post("/api/v1/reports/", REMOTE_ADDR="192.0.2.3").status_code == 200


@pytest.mark.django_db
def test_ip_gate_runs_before_session_lookup(client, users, settings, django_assert_num_queries):
    set_budget(settings, "ip_burst", 1)
    client.force_login(users["owner"])
    assert client.get("/").status_code == 200
    with django_assert_num_queries(0):
        assert client.post("/report/new/").status_code == 429


def test_untrusted_forwarded_header_cannot_rotate_ip_budget(client, settings):
    set_budget(settings, "ip_burst", 1)
    assert client.get("/", HTTP_X_FORWARDED_FOR="192.0.2.1").status_code == 200
    assert client.get("/", HTTP_X_FORWARDED_FOR="192.0.2.2").status_code == 429


@pytest.mark.parametrize("peer,forwarded,expected", [
    ("192.0.2.9", "198.51.100.1", "192.0.2.9"),
    ("10.0.0.1", "198.51.100.1, 10.0.0.2", "198.51.100.1"),
    ("10.0.0.1", "203.0.113.9, 198.51.100.1", "198.51.100.1"),
    ("10.0.0.1", "invalid, 10.0.0.2", "10.0.0.1"),
    ("10.0.0.1", "2001:0db8:0:0:0:0:0:1", "2001:db8::1"),
    ("::ffff:192.0.2.9", "198.51.100.1", "192.0.2.9"),
    ("10.0.0.1", "fe80::1%fake", "10.0.0.1"),
])
def test_forwarded_chain_uses_only_configured_proxy_hops(settings, peer, forwarded, expected):
    settings.REQUEST_LIMITS_TRUSTED_PROXIES = ["10.0.0.0/8"]
    request = RequestFactory().get("/", REMOTE_ADDR=peer, HTTP_X_FORWARDED_FOR=forwarded)
    assert client_ip(request) == expected


def test_atomic_counter_admits_exact_budget_with_concurrent_locmem_clients():
    backend = LocMemCache("guard-concurrent-locmem", {})
    backend.clear()
    with ThreadPoolExecutor(max_workers=16) as pool:
        waits = list(pool.map(
            lambda _: consume_budget(backend, "concurrent", "192.0.2.1", 12, 60, now=125),
            range(80),
        ))
    assert waits.count(0) == 12
    assert waits.count(55) == 68


def test_independent_workers_use_one_mock_shared_atomic_budget():
    shared = {}
    lock = Lock()

    class SharedCacheClient:
        def add(self, key, value, timeout):
            with lock:
                if key in shared:
                    return False
                shared[key] = value
                return True

        def incr(self, key):
            with lock:
                shared[key] += 1
                return shared[key]

    workers = [SharedCacheClient(), SharedCacheClient()]
    with ThreadPoolExecutor(max_workers=16) as pool:
        waits = list(pool.map(
            lambda i: consume_budget(workers[i % 2], "shared", "user:7", 8, 60, now=125),
            range(64),
        ))
    assert waits.count(0) == 8
    assert sum(shared.values()) == 64


def test_fixed_window_boundary_can_admit_two_adjacent_allowances():
    backend = LocMemCache("guard-boundaries", {})
    backend.clear()
    assert consume_budget(backend, "boundary", "ip", 1, 60, now=119.9) == 0
    assert consume_budget(backend, "boundary", "ip", 1, 60, now=119.9) == 1
    assert consume_budget(backend, "boundary", "ip", 1, 60, now=120) == 0
    assert consume_budget(backend, "boundary", "ip", 1, 60, now=120) == 60


@pytest.mark.parametrize("operation", ["add", "incr"])
def test_counter_errors_fail_closed(operation):
    backend = Mock()
    backend.add.return_value = False
    getattr(backend, operation).side_effect = ConnectionError("cache down")
    with pytest.raises(RequestLimitUnavailable):
        consume_budget(backend, "failure", "ip", 10, 60)


@pytest.mark.parametrize("endpoint", ["/login/", "/api/v1/auth/login/"])
def test_cache_outage_returns_503_before_view(client, endpoint):
    with patch("apps.reports.request_limits.guard_cache", side_effect=ConnectionError("down")):
        response = client.post(endpoint)
    assert response.status_code == 503
    assert response["Retry-After"] == "5"
    if endpoint.startswith("/api/"):
        assert response.json()["error"]["code"] == "request_guard_unavailable"


def test_production_refuses_local_cache_and_reports_system_check(client, settings):
    settings.CACHES = {
        **settings.CACHES,
        "request_limits": {
            "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
            "LOCATION": "unsupported-production-cache",
        },
    }
    settings.DEBUG = False
    with pytest.raises(RequestLimitUnavailable):
        guard_cache()
    assert "reports.E001" in {error.id for error in check_request_limit_configuration()}
    assert client.get("/").status_code == 503


def test_invalid_proxy_config_reports_system_check(settings):
    settings.REQUEST_LIMITS_TRUSTED_PROXIES = ["not-a-network"]
    assert "reports.E002" in {error.id for error in check_request_limit_configuration()}


@pytest.mark.parametrize("endpoint", ["/report/new/", "/api/v1/reports/"])
def test_content_length_rejected_before_view_or_body_read(settings, endpoint):
    settings.REQUEST_MAX_BODY_BYTES = 100
    request = RequestFactory().post(endpoint, data=b"x", content_type="application/octet-stream")
    request.META["CONTENT_LENGTH"] = "101"
    request._stream = Mock()
    view = Mock(return_value=HttpResponse("should not run"))
    response = RequestProtectionMiddleware(view)(request)
    assert response.status_code == 413
    view.assert_not_called()
    request._stream.read.assert_not_called()


def test_actual_stream_limit_rejects_missing_content_length_before_view_work(settings):
    settings.REQUEST_MAX_BODY_BYTES = 100
    request = RequestFactory().post("/api/v1/reports/", data=b"x" * 101,
                                    content_type="application/json")
    request.META.pop("CONTENT_LENGTH")
    completed = []

    def parse_then_work(incoming):
        _ = incoming.body
        completed.append(True)
        return HttpResponse("ok")

    response = RequestProtectionMiddleware(parse_then_work)(request)
    assert response.status_code == 413
    assert completed == []


def test_stream_readline_obeys_total_limit():
    request = RequestFactory().get("/")
    stream = BodyLimitStream(BytesIO(b"ab\ncde\n"), request, 5)
    assert stream.readline() == b"ab\n"
    with pytest.raises(RequestDataTooBig):
        stream.readline()
    assert request._request_limits_body_rejected


def test_aggregate_file_guard_rejects_before_upload_view_completes(client, settings):
    settings.REQUEST_MAX_UPLOAD_BYTES = 100
    files = [SimpleUploadedFile(f"{i}.png", b"x" * 40) for i in range(3)]
    response = client.post("/api/v1/reports/1/claims/", {"images": files})
    assert response.status_code == 413
    assert response.json()["error"]["code"] == "upload_too_large"


def test_three_five_megabyte_files_fit_memory_and_aggregate_guards(client):
    size = 5 * 1024 * 1024
    files = [SimpleUploadedFile(f"{i}.png", b"x" * size) for i in range(3)]
    response = client.post("/api/v1/reports/1/claims/", {"images": files})
    assert response.status_code == 200
    assert response.json() == {"files": 3, "bytes": 3 * size}


@pytest.mark.parametrize("endpoint", ["/login/", "/api/v1/auth/login/"])
def test_actual_application_login_routes_reject_before_parsing(client, settings, endpoint):
    settings.ROOT_URLCONF = "config.urls"
    set_budget(settings, "login", 1)
    # The first malformed submission spends the shared budget. The next body
    # never reaches authentication, form validation or the DRF JSON parser.
    first = client.post(endpoint, data=b"{", content_type="application/json")
    assert first.status_code in {200, 400}
    response = client.post(endpoint, data=b"{", content_type="application/json")
    assert response.status_code == 429
    assert int(response["Retry-After"]) > 0


def test_trusted_proxy_canonical_addresses_share_one_budget(client, settings):
    settings.REQUEST_LIMITS_TRUSTED_PROXIES = ["10.0.0.0/8"]
    set_budget(settings, "ip_burst", 1)
    assert client.get(
        "/", REMOTE_ADDR="10.0.0.1", HTTP_X_FORWARDED_FOR="2001:0db8:0:0:0:0:0:1",
    ).status_code == 200
    assert client.get(
        "/", REMOTE_ADDR="10.0.0.2", HTTP_X_FORWARDED_FOR="2001:db8::1",
    ).status_code == 429


@pytest.mark.parametrize("length", ["bad", "-1"])
def test_invalid_content_length_rejected_before_view(length):
    request = RequestFactory().post("/api/v1/reports/")
    request.META["CONTENT_LENGTH"] = length
    view = Mock(return_value=HttpResponse("ok"))
    assert RequestProtectionMiddleware(view)(request).status_code == 400
    view.assert_not_called()


def test_ip_endpoint_multiplier_allows_shared_nat_without_raising_account_budget(
    client, settings, users,
):
    settings.REQUEST_LIMITS_IP_ENDPOINT_MULTIPLIER = 3
    set_budget(settings, "write", 1)
    assert client.post("/report/new/").status_code == 200
    assert client.post("/api/v1/reports/").status_code == 200
    client.force_login(users["owner"])
    assert client.post("/report/new/").status_code == 200
    # Use another IP to ensure the rejection is the unchanged account budget.
    assert client.post("/api/v1/reports/", REMOTE_ADDR="192.0.2.8").status_code == 429


def test_missing_counter_between_add_and_increment_fails_closed():
    backend = Mock()
    backend.add.return_value = False
    backend.incr.side_effect = ValueError("counter was evicted")
    with pytest.raises(RequestLimitUnavailable):
        consume_budget(backend, "evicted", "ip", 10, 60)
