import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import RequestFactory

from apps.reports import request_limits
from config.settings import REQUEST_LIMITS


def effective_budgets(monkeypatch, request, identity, scopes):
    budgets = {}

    def consume(backend, scope, key, allowance, seconds):
        budgets.setdefault(scope.split(":")[1], []).append((allowance, seconds))
        return 0

    monkeypatch.setattr(request_limits, "guard_cache", object)
    monkeypatch.setattr(request_limits, "consume_budget", consume)
    middleware = request_limits.RequestProtectionMiddleware(lambda request: None)
    assert middleware._check(request, (identity, "test-client"), scopes) is None
    return budgets


def test_shared_campus_ip_can_sustain_the_largest_capacity_tier():
    for scope in ("ip_burst", "ip_sustained"):
        for allowance, seconds in REQUEST_LIMITS[scope]:
            assert allowance >= 40 * seconds


@pytest.mark.parametrize("path", [
    "/api/v1/reports/", "/api/v1/reports/1/claims/", "/api/v1/claims/1/resubmit/",
])
def test_json_writes_fit_capacity_without_spending_file_upload_budget(monkeypatch, path):
    request = RequestFactory().post(path, data="{}", content_type="application/json")
    scopes = request_limits.endpoint_budgets(request)
    assert scopes == ["write"]
    ip = effective_budgets(monkeypatch, request, "ip", scopes)
    for allowance, seconds in ip["write"]:
        assert 40 * 0.2 * seconds <= allowance <= 50 * seconds
    account = effective_budgets(monkeypatch, request, "account", scopes)
    assert account["write"] == [(60, 60), (600, 3600)]


@pytest.mark.parametrize("path", [
    "/api/v1/reports/", "/api/v1/reports/1/claims/", "/api/v1/claims/1/resubmit/",
])
def test_real_multipart_uploads_keep_account_and_shared_ip_caps(monkeypatch, path):
    request = RequestFactory().post(path, {"images": SimpleUploadedFile("proof.png", b"proof")})
    scopes = request_limits.endpoint_budgets(request)
    assert scopes == ["write", "upload"]
    ip = effective_budgets(monkeypatch, request, "ip", scopes)
    account = effective_budgets(monkeypatch, request, "account", scopes)
    assert ip["upload"] == [(400, 60), (4000, 3600)]
    assert account["upload"] == [(20, 60), (200, 3600)]
    assert account["write"] == [(60, 60), (600, 3600)]


@pytest.mark.parametrize("scope,account_limits,ip_limits", [
    ("login", [(30, 60), (100, 3600)], [(600, 60), (2000, 3600)]),
    ("register", [(10, 60), (100, 3600)], [(200, 60), (2000, 3600)]),
])
def test_authentication_limits_are_not_relaxed_by_write_capacity(
    monkeypatch, scope, account_limits, ip_limits,
):
    request = RequestFactory().post(f"/api/v1/auth/{scope}/", data="{}", content_type="application/json")
    scopes = request_limits.endpoint_budgets(request)
    assert effective_budgets(monkeypatch, request, "ip", scopes)[scope] == ip_limits
    assert effective_budgets(monkeypatch, request, "account", scopes)[scope] == account_limits


def test_global_ip_and_account_caps_remain_in_force(monkeypatch):
    request = RequestFactory().get("/api/v1/claims/")
    assert effective_budgets(monkeypatch, request, "ip", ["ip_burst", "ip_sustained"]) == {
        "ip_burst": [(3000, 60)], "ip_sustained": [(180000, 3600)],
    }
    assert effective_budgets(monkeypatch, request, "account", ["account_burst", "account_sustained"]) == {
        "account_burst": [(300, 60)], "account_sustained": [(3000, 3600)],
    }


def test_shared_ip_search_budget_supports_the_request_mix_without_relaxing_accounts(monkeypatch):
    request = RequestFactory().get("/api/v1/reports/", {"q": "wallet"})
    scopes = request_limits.endpoint_budgets(request)
    assert scopes == ["search"]
    for allowance, seconds in effective_budgets(monkeypatch, request, "ip", scopes)["search"]:
        assert 40 * 0.7 / 3 * seconds <= allowance <= 50 * seconds
    assert effective_budgets(monkeypatch, request, "account", scopes)["search"] == [(120, 60), (1200, 3600)]
