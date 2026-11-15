import pytest
from django.contrib.auth.models import User
from django.db import connection
from django.urls import reverse
from rest_framework.test import APIClient

from apps.reports.models import ActionHistory, Claim, NotificationEvent, Report

from .test_api import report_data, url
from .test_workflow import make_claim, make_report

pytestmark = pytest.mark.django_db

PAYLOADS = [
    "x' OR 1=1 --",
    "x'; DROP TABLE reports_report; --",
    "x'; ALTER TABLE reports_claim ADD COLUMN injected TEXT; --",
]


@pytest.fixture
def api_client():
    client = APIClient()
    client.default_format = "json"
    return client


@pytest.fixture(autouse=True)
def schema_guard(db):
    tables = set(connection.introspection.table_names())
    with connection.cursor() as cursor:
        columns = {
            model._meta.db_table: [column.name for column in
                                  connection.introspection.get_table_description(
                                      cursor, model._meta.db_table)]
            for model in (Report, Claim)
        }
    yield
    assert set(connection.introspection.table_names()) == tables
    with connection.cursor() as cursor:
        for table, expected in columns.items():
            assert [column.name for column in
                    connection.introspection.get_table_description(cursor, table)] == expected


@pytest.mark.parametrize("payload", PAYLOADS)
@pytest.mark.parametrize("field", ["q", "category", "location"])
@pytest.mark.parametrize("endpoint", ["api", "html"])
def test_search_treats_sql_as_literal_data(api_client, client, users, payload, field, endpoint):
    data = {"item" if field == "q" else field: payload}
    match = make_report(users["owner"], **data)
    make_report(users["owner"], item="Unrelated public item")
    make_report(users["owner"], status="pending", **data)
    before = list(Report.objects.values_list("pk", flat=True))
    if endpoint == "api":
        response = api_client.get(url("reports"), {field: payload})
        assert response.status_code == 200
        assert response.json()["count"] == 1
        assert [item["id"] for item in response.json()["results"]] == [match.pk]
    else:
        response = client.get(reverse("report_list"), {field: payload})
        assert response.status_code == 200
        assert list(response.context["reports"].values_list("pk", flat=True)) == [match.pk]
    assert list(Report.objects.values_list("pk", flat=True)) == before


@pytest.mark.parametrize("payload", PAYLOADS)
def test_login_payload_cannot_authenticate(api_client, users, payload):
    before = User.objects.count()
    response = api_client.post(url("login"), {"username": payload, "password": "wrong"})
    assert response.status_code == 401
    assert "_auth_user_id" not in api_client.session
    assert User.objects.count() == before


@pytest.mark.parametrize("payload", PAYLOADS)
def test_report_and_evidence_store_sql_as_literal_data(api_client, users, payload):
    api_client.force_login(users["owner"])
    response = api_client.post(url("reports"), report_data(item_name=payload, description=payload))
    assert response.status_code == 201
    report = Report.objects.get(pk=response.json()["id"])
    assert report.item_name == report.description == payload
    report.review(users["service"], "approve")
    api_client.force_login(users["claimant"])
    response = api_client.post(url("create-claim", report.pk), {"evidence": payload})
    assert response.status_code == 201
    claim = Claim.objects.get(pk=response.json()["id"])
    assert claim.evidence == payload
    assert api_client.get(url("claim", claim.pk)).json()["evidence"] == payload
    assert Report.objects.get(pk=report.pk).status == "approved"


@pytest.mark.parametrize("field", ["ordering", "sql", "item_name__regex"])
def test_search_rejects_query_structure_parameters(api_client, users, field):
    report = make_report(users["owner"])
    response = api_client.get(url("reports"), {field: PAYLOADS[1]})
    assert response.status_code == 400
    assert Report.objects.get(pk=report.pk).status == "approved"


def test_write_rejects_sql_instruction_fields(api_client, users):
    api_client.force_login(users["owner"])
    response = api_client.post(url("reports"), report_data(sql=PAYLOADS[1]))
    assert response.status_code == 400
    assert not Report.objects.exists()
    assert not ActionHistory.objects.exists()


def test_staff_action_cannot_execute_sql(api_client, users):
    claim = make_claim(make_report(users["owner"]), users["claimant"])
    api_client.force_login(users["service"])
    response = api_client.post(url("claim-action", claim.pk), {"action": PAYLOADS[1]})
    assert response.status_code == 400
    claim.refresh_from_db()
    assert claim.status == "pending"
    assert not ActionHistory.objects.exists()
    assert not NotificationEvent.objects.exists()
