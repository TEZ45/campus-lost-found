from datetime import timedelta
from unittest.mock import patch

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone
from rest_framework.test import APIClient

from apps.reports.models import ActionHistory, ClaimAttachment, Report

from .test_workflow import make_claim, make_report

pytestmark = pytest.mark.django_db


@pytest.fixture
def api_client():
    client = APIClient()
    client.default_format = "json"
    return client


@pytest.fixture
def claim(users):
    return make_claim(make_report(users["owner"]), users["claimant"])


def url(name, *args):
    return reverse(f"reports_api:{name}", args=args)


def add_history(claim, count, created_at=None):
    with patch("django.utils.timezone.now", return_value=created_at or timezone.now()):
        return ActionHistory.objects.bulk_create([
            ActionHistory(
                claim=claim, report=claim.report, actor=claim.claimant,
                action="evidence_resubmitted", reason=f"Evidence revision {index}",
                previous_state="under_dispute", new_state="under_dispute",
            )
            for index in range(count)
        ])


def selects(queries, model):
    table = connection.ops.quote_name(model._meta.db_table)
    return [query["sql"] for query in queries
            if query["sql"].lstrip().upper().startswith("SELECT") and table in query["sql"]]


@pytest.mark.parametrize("role,expected", [
    (None, 401), ("owner", 403), ("other", 403), ("admin", 403),
    ("claimant", 200), ("service", 200), ("service_admin", 200),
])
@pytest.mark.parametrize("endpoint", ["claim", "claim-history"])
def test_claim_reads_authorize_before_loading_related_data(
    api_client, users, claim, role, expected, endpoint,
):
    add_history(claim, 3)
    ClaimAttachment.objects.create(claim=claim, image="private-proof.png")
    if role:
        api_client.force_login(users[role])
    with CaptureQueriesContext(connection) as queries:
        response = api_client.get(url(endpoint, claim.pk))
    assert response.status_code == expected
    assert response["Cache-Control"] == "private, no-store"
    if expected != 200:
        assert response.json()["error"]["code"] == (
            "not_authenticated" if expected == 401 else "permission_denied"
        )
        for model in (ActionHistory, ClaimAttachment, Report):
            assert not selects(queries, model)
    elif endpoint == "claim-history":
        assert response.json()["count"] == 3
        assert {item["claim"] for item in response.json()["results"]} == {claim.pk}
        assert not selects(queries, ClaimAttachment)
        assert not selects(queries, Report)


@pytest.mark.parametrize("endpoint", ["claim", "claim-history"])
def test_missing_claim_is_not_found_after_authentication(api_client, users, claim, endpoint):
    path = url(endpoint, claim.pk + 1)
    assert api_client.get(path).status_code == 401
    api_client.force_login(users["claimant"])
    response = api_client.get(path)
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"


@pytest.mark.parametrize("role,endpoint,state", [
    ("claimant", "claim", "pending"),
    ("claimant", "claims", "pending"),
    ("service", "staff-claims", "pending"),
    ("service", "handovers", "awaiting_handover"),
])
def test_claim_responses_do_not_grow_with_history(api_client, users, role, endpoint, state):
    claim = make_claim(make_report(users["owner"]), users["claimant"], status=state)
    api_client.force_login(users[role])
    path = url(endpoint, claim.pk) if endpoint == "claim" else url(endpoint)
    before = api_client.get(path, {"page_size": 1})
    assert before.status_code == 200
    add_history(claim, 1005)
    with CaptureQueriesContext(connection) as queries:
        after = api_client.get(path, {"page_size": 1})
    assert after.status_code == 200
    assert after.content == before.content
    assert not selects(queries, ActionHistory)
    item = after.json() if endpoint == "claim" else after.json()["results"][0]
    assert "history" not in item
    assert item["history_url"] == url("claim-history", claim.pk)


def test_history_pages_are_bounded_complete_and_stably_ordered(api_client, users, claim):
    now = timezone.now()
    newest = add_history(claim, 1004, now)
    oldest = add_history(claim, 1, now - timedelta(days=1))
    other_claim = make_claim(claim.report, users["other"])
    add_history(other_claim, 1, now)
    ActionHistory.objects.create(report=claim.report, action="report_reviewed")
    expected = [item.pk for item in reversed(newest)] + [oldest[0].pk]
    api_client.force_login(users["claimant"])
    path = url("claim-history", claim.pk)
    with CaptureQueriesContext(connection) as queries:
        response = api_client.get(path, {"page_size": 10000})
    assert response.status_code == 200
    page = response.json()
    assert page["count"] == 1005
    assert page["previous"] is None
    assert len(page["results"]) == 100
    history_reads = [query for query in selects(queries, ActionHistory) if "COUNT(" not in query]
    assert len(history_reads) == 1
    assert "LIMIT 100" in history_reads[0]
    ids = [item["id"] for item in page["results"]]
    while page["next"]:
        response = api_client.get(page["next"])
        assert response.status_code == 200
        page = response.json()
        assert page["count"] == 1005
        assert page["previous"] is not None
        assert 1 <= len(page["results"]) <= 100
        assert {item["claim"] for item in page["results"]} == {claim.pk}
        ids.extend(item["id"] for item in page["results"])
    assert ids == expected
    assert len(set(ids)) == 1005


def test_history_default_page_size_and_empty_page(api_client, users, claim):
    api_client.force_login(users["claimant"])
    path = url("claim-history", claim.pk)
    empty = api_client.get(path)
    assert empty.status_code == 200
    assert empty.json() == {"count": 0, "next": None, "previous": None, "results": []}
    add_history(claim, 21)
    page = api_client.get(path).json()
    assert page["count"] == 21
    assert len(page["results"]) == 20
    assert page["next"] is not None
    assert page["previous"] is None
    single = api_client.get(path, {"page_size": 1}).json()
    assert len(single["results"]) == 1


@pytest.mark.parametrize("page", [0, -1, "invalid", 9999])
def test_invalid_history_page_is_not_found(api_client, users, claim, page):
    add_history(claim, 1)
    api_client.force_login(users["claimant"])
    response = api_client.get(url("claim-history", claim.pk), {"page": page})
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"


@pytest.mark.parametrize("role,endpoint,state,payload", [
    ("claimant", "resubmit", "under_dispute", {"evidence": "Additional serial number"}),
    ("claimant", "withdraw", "pending", {"reason": "No longer needed"}),
    ("service", "claim-action", "pending", {"action": "request_evidence", "reason": "Serial needed"}),
])
def test_claim_actions_do_not_load_history(api_client, users, role, endpoint, state, payload):
    claim = make_claim(make_report(users["owner"]), users["claimant"], status=state)
    add_history(claim, 1005)
    api_client.force_login(users[role])
    with CaptureQueriesContext(connection) as queries:
        response = api_client.post(url(endpoint, claim.pk), payload)
    assert response.status_code == 200
    assert response.json()["history_url"] == url("claim-history", claim.pk)
    assert "history" not in response.json()
    assert not selects(queries, ActionHistory)
    assert ActionHistory.objects.filter(claim=claim).count() == 1006


def test_created_claim_links_to_paginated_history(api_client, users):
    report = make_report(users["owner"])
    api_client.force_login(users["claimant"])
    with CaptureQueriesContext(connection) as queries:
        response = api_client.post(url("create-claim", report.pk), {"evidence": "Serial ABC"})
    assert response.status_code == 201
    assert "history" not in response.json()
    assert not selects(queries, ActionHistory)
    history_url = url("claim-history", response.json()["id"])
    assert response.json()["history_url"] == history_url
    history = api_client.get(history_url).json()
    assert history["count"] == 1
    assert history["results"][0]["claim"] == response.json()["id"]
