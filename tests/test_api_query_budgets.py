import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from rest_framework.test import APIClient

from apps.reports.models import Match
from apps.reports.permissions import claim_actions, report_actions

from .test_workflow import make_claim, make_report

pytestmark = pytest.mark.django_db


@pytest.mark.parametrize("endpoint,role", [
    (endpoint, role) for endpoint in ("reports", "claims", "staff-claims", "matches")
    for role in ("other", "owner", "service") if endpoint != "staff-claims" or role == "service"
])
def test_page_queries_do_not_grow_per_row(users, role, endpoint):
    actor = users[role]
    for index in range(20):
        report = make_report(users["owner"], item=f"Wallet {index}")
        make_claim(report, actor)
        make_claim(report, users["claimant"], status="under_dispute")
        if endpoint == "matches":
            lost = make_report(users["owner"], report_type="lost")
            Match.objects.create(lost_report=lost, found_report=report, score=80)
    client = APIClient()
    client.force_login(actor)
    counts = []
    for size in (1, 20):
        with CaptureQueriesContext(connection) as queries:
            response = client.get(reverse(f"reports_api:{endpoint}"), {"page_size": size})
        assert response.status_code == 200
        assert len(response.json()["results"]) == size
        counts.append(len(queries))
    assert counts[1] == counts[0]


@pytest.mark.parametrize("role", ["other", "owner", "claimant", "service"])
@pytest.mark.parametrize("state", [None, "awaiting_handover", "under_dispute", "handover_in_progress"])
def test_batch_action_hints_match_live_permission_checks(users, role, state):
    report = make_report(users["owner"])
    claim = make_claim(report, users["claimant"])
    if state:
        make_claim(report, users["other"], status=state)
    client = APIClient()
    actor = users[role]
    client.force_login(actor)
    response = client.get(reverse("reports_api:reports"))
    assert response.json()["results"][0]["allowed_actions"] == report_actions(actor, report)
    if role in {"claimant", "service"}:
        response = client.get(reverse("reports_api:claim", args=[claim.pk]))
        assert response.json()["allowed_actions"] == claim_actions(actor, claim)
        assert response.json()["report"]["allowed_actions"] == report_actions(actor, report)
