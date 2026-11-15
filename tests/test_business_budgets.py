import pytest
from django.core.exceptions import ValidationError
from django.urls import reverse

from apps.reports import services
from apps.reports.models import Match, Notice, Report, ResourceLease
from apps.reports.resource_guards import BusinessBusy, work_budget

from .test_uploads import upload
from .test_workflow import CONFIRMATIONS, make_claim, make_report

pytestmark = pytest.mark.django_db


def test_html_search_pages_results_and_preserves_filters(client, users):
    for number in range(24):
        make_report(users["owner"], item=f"Wallet {number}")
    make_report(users["owner"], item="Wallet private", status="pending")
    response = client.get(reverse("report_list"), {"q": "Wallet", "page": 1})
    assert response.status_code == 200
    assert len(response.context["reports"]) == 20
    assert response.context["page_obj"].paginator.count == 24
    assert b"q=Wallet&amp;page=2" in response.content
    assert b"Wallet private" not in response.content
    second = client.get(reverse("report_list"), {"q": "Wallet", "page": 2})
    assert len(second.context["reports"]) == 4


def test_notices_page_is_bounded_and_private(client, users):
    Notice.objects.bulk_create([
        Notice(user=users["owner"], title=f"Notice {number}") for number in range(25)
    ])
    Notice.objects.create(user=users["other"], title="Private other notice")
    client.force_login(users["owner"])
    response = client.get(reverse("notices"))
    assert len(response.context["notices"]) == 20
    assert response.context["page_obj"].paginator.count == 25
    assert b"Private other notice" not in response.content


def test_matching_pair_budget_rejects_before_writes(users, settings):
    settings.MATCH_MAX_PAIRS = 2
    for _ in range(2):
        make_report(users["owner"], report_type="lost")
        make_report(users["owner"])
    with pytest.raises(ValidationError, match="Too many matching pairs"):
        services.regenerate_matches(users["service"])
    assert not Match.objects.exists()
    assert ResourceLease.objects.get(key="match-generation").token == ""


def test_matching_deadline_rolls_back_partial_work(users, settings, monkeypatch):
    settings.MATCH_MAX_SECONDS = 10
    make_report(users["owner"], report_type="lost")
    make_report(users["owner"])
    make_report(users["owner"])
    clock = {"now": 0}
    original_score = services.match_score

    def slow_score(first, second):
        clock["now"] += 6
        return original_score(first, second)

    monkeypatch.setattr("apps.reports.resource_guards.time.monotonic", lambda: clock["now"])
    monkeypatch.setattr(services, "match_score", slow_score)
    with pytest.raises(BusinessBusy, match="work budget"):
        services.regenerate_matches(users["service"])
    assert not Match.objects.exists()


def test_shared_matching_lease_rejects_duplicate_and_cools_down(settings):
    settings.MATCH_COOLDOWN_SECONDS = 30
    with work_budget("match-generation"):
        token = ResourceLease.objects.get(key="match-generation").token
        with pytest.raises(BusinessBusy), work_budget("match-generation"):
            pytest.fail("A duplicate matching job entered")
        assert ResourceLease.objects.get(key="match-generation").token == token
    with pytest.raises(BusinessBusy), work_budget("match-generation"):
        pytest.fail("The cooldown was bypassed")


def test_match_budget_is_enforced_in_html_and_api(client, users, settings):
    settings.MATCH_COOLDOWN_SECONDS = 30
    client.force_login(users["service"])
    first = client.post(reverse("reports_api:regenerate"), {}, content_type="application/json")
    assert first.status_code == 200
    for path in [reverse("reports_api:regenerate"), reverse("matches")]:
        response = client.post(path, {}, content_type="application/json")
        assert response.status_code == 429
        assert int(response["Retry-After"]) >= 1


def test_direct_service_search_has_input_bounds(users):
    with pytest.raises(ValidationError):
        services.report_queryset(users["other"], {"q": "x" * 201})


def test_disputed_evidence_upload_via_api_preserves_reservation(client, users):
    claim = make_claim(make_report(users["owner"]), users["claimant"])
    client.force_login(users["service"])
    for action in ["approve", "start_dispute"]:
        response = client.post(reverse("reports_api:claim-action", args=[claim.pk]), {
            "action": action, "reason": "Ownership reviewed", "confirmations": CONFIRMATIONS[action],
        }, content_type="application/json")
        assert response.status_code == 200
    client.force_login(users["claimant"])
    response = client.post(reverse("reports_api:resubmit", args=[claim.pk]), {
        "evidence": "Additional private proof", "images": [upload()],
    })
    assert response.status_code == 200
    assert response.json()["status"] == "under_dispute"
    claim.refresh_from_db()
    assert claim.holds_reservation and claim.evidence == "Additional private proof"
    assert claim.attachments.count() == 1
    client.force_login(users["other"])
    response = client.post(reverse("reports_api:resubmit", args=[claim.pk]), {
        "evidence": "Forged update",
    }, content_type="application/json")
    assert response.status_code == 403
    claim.refresh_from_db()
    assert claim.evidence == "Additional private proof"


def test_closure_requires_and_records_reason(client, users):
    report = make_report(users["owner"])
    client.force_login(users["owner"])
    path = reverse("reports_api:close", args=[report.pk])
    assert client.post(path, {}, content_type="application/json").status_code == 400
    assert not report.history.exists()
    response = client.post(path, {"reason": "Recovered elsewhere"}, content_type="application/json")
    assert response.status_code == 200
    assert report.history.get().reason == "Recovered elsewhere"


def test_report_review_reason_is_private_to_managers(client, users):
    report = make_report(users["owner"])
    Report.objects.filter(pk=report.pk).update(review_reason="Private service instruction")
    path = reverse("reports_api:report", args=[report.pk])
    assert "review_reason" not in client.get(path).json()
    assert b"Private service instruction" not in client.get(reverse("report_detail", args=[report.pk])).content
    client.force_login(users["owner"])
    assert client.get(path).json()["review_reason"] == "Private service instruction"


def test_quota_blocks_real_report_upload_without_business_writes(client, users, settings):
    from .test_api import report_data

    settings.STORAGE_CAPACITY_BYTES = 1
    client.force_login(users["owner"])
    response = client.post(reverse("reports_api:reports"), report_data(photo=upload()))
    assert response.status_code == 400
    assert not Report.objects.exists()
    assert not list(settings.MEDIA_ROOT.rglob("*"))


def test_storage_warning_is_visible_only_to_staff(client, users, settings):
    from apps.reports.lifecycle_models import StorageLedger

    settings.STORAGE_CAPACITY_BYTES = 100
    StorageLedger.objects.create(used_bytes=80, initialized=True)
    client.force_login(users["service"])
    response = client.get(reverse("reports_api:dashboard"))
    assert response.json()["storage"]["warning"] is True
    assert b"80 percent full" in client.get(reverse("staff_dashboard")).content
    client.force_login(users["owner"])
    assert client.get(reverse("reports_api:dashboard")).status_code == 403
