import pytest
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client
from django.urls import reverse

from apps.reports import services
from apps.reports.models import ActionHistory, Claim, Match, Notice
from apps.reports.permissions import claim_actions, report_actions

from .test_workflow import make_claim, make_png_bytes, make_report

pytestmark = pytest.mark.django_db


def report_data(report, **changes):
    return {
        "report_type": report.report_type, "item_name": report.item_name,
        "category": report.category, "description": report.description,
        "location": report.location, "event_date": "2026-10-01",
        "contact_name": report.contact_name, "contact_email": report.contact_email,
        "contact_phone": report.contact_phone, **changes,
    }


@pytest.mark.parametrize("change,expected", [
    ({}, "approved"), ({"contact_phone": "12345"}, "approved"),
    ({"item_name": "New name"}, "pending"),
])
def test_approved_report_only_rechecks_changed_public_content(client, users, change, expected):
    report = make_report(users["owner"])
    client.force_login(users["owner"])
    response = client.post(reverse("report_edit", args=[report.pk]), report_data(report, **change))
    assert response.status_code == 302
    report.refresh_from_db()
    assert report.status == expected
    history = report.history.get()
    assert (history.previous_state, history.new_state) == ("approved", expected)


def test_rejected_report_with_existing_photo_can_reenter_review(client, users):
    report = make_report(users["owner"], status="rejected")
    report.photo.save("original.png", SimpleUploadedFile("original.png", make_png_bytes(), content_type="image/png"))
    original = report.photo.name
    client.force_login(users["owner"])
    response = client.post(reverse("report_edit", args=[report.pk]), report_data(report, item_name="Corrected"))
    assert response.status_code == 302
    report.refresh_from_db()
    assert report.status == "pending"
    assert report.photo.name == original
    client.force_login(users["service"])
    assert b"Corrected" in client.get(reverse("staff_report_queue")).content
    assert client.post(reverse("staff_report_review", args=[report.pk]), {"action": "approve"}).status_code == 302
    report.refresh_from_db()
    assert report.status == "approved"


def test_notices_require_login_and_post_to_mark_read(client, users):
    assert client.get(reverse("notices")).status_code == 302
    notice = Notice.objects.create(user=users["owner"], title="Owned notice", link="//example.invalid/")
    other = Notice.objects.create(user=users["other"], title="Private notice")
    client.force_login(users["owner"])
    content = client.get(reverse("notices")).content
    assert b"Owned notice" in content and b"Private notice" not in content
    assert client.get(reverse("notice_read", args=[notice.pk])).status_code == 405
    notice.refresh_from_db()
    assert not notice.is_read
    assert client.post(reverse("notice_read", args=[other.pk])).status_code == 404
    response = client.post(reverse("notice_read", args=[notice.pk]))
    assert response.url == reverse("notices")
    notice.refresh_from_db()
    assert notice.is_read


def test_logout_requires_post_and_csrf(users):
    client = Client(enforce_csrf_checks=True)
    client.force_login(users["owner"])
    assert client.get(reverse("logout")).status_code == 405
    assert client.post(reverse("logout")).status_code == 403
    assert "_auth_user_id" in client.session
    client.get(reverse("report_list"))
    token = client.cookies["csrftoken"].value
    assert client.post(reverse("logout"), {"csrfmiddlewaretoken": token}).status_code == 302
    assert "_auth_user_id" not in client.session


@pytest.mark.parametrize("destination", [
    "https://example.invalid/", "//example.invalid/", r"/\example.invalid/",
    "javascript:alert(1)", "unknown-route", "http:example.invalid",
])
def test_login_redirects_stay_local(client, users, destination):
    users["owner"].set_password("Secure-test-password-51")
    users["owner"].save()
    response = client.post(reverse("login"), {
        "username": "owner", "password": "Secure-test-password-51", "next": destination,
    })
    assert response.status_code == 302
    assert response.url == reverse("report_list")


def test_matching_write_requires_service_role(client, users):
    make_report(users["owner"], report_type="lost")
    make_report(users["owner"])
    assert b"Regenerate suggestions" not in client.get(reverse("matches")).content
    assert client.post(reverse("matches")).status_code == 302
    for name in ("owner", "admin"):
        client.force_login(users[name])
        assert client.post(reverse("matches")).status_code == 403
    assert not Match.objects.exists()
    client.force_login(users["service"])
    assert b"Regenerate suggestions" in client.get(reverse("matches")).content
    assert client.post(reverse("matches")).status_code == 200
    assert Match.objects.count() == 1


def test_staff_template_and_service_share_claim_actions(client, users):
    claim = make_claim(make_report(users["owner"]), users["claimant"])
    claim.request_evidence(users["service"], "More proof")
    client.force_login(users["service"])
    response = client.get(reverse("staff_claim_queue"))
    assert b'value="approve"' not in response.content
    assert b'value="reject"' in response.content
    assert {item["key"] for item in claim_actions(users["service"], claim)} == {"reject"}
    with pytest.raises(ValueError):
        services.act_on_claim(users["service"], claim.pk, "approve", confirmations={'evidence_reviewed': True, 'custody_confirmed': True})
    with pytest.raises(ValidationError):
        services.act_on_claim(users["service"], claim.pk, "reject", " ")
    claim.refresh_from_db()
    assert claim.status == "awaiting_evidence"


def test_reserved_report_action_contract_blocks_edit_and_second_approval(users):
    report = make_report(users["owner"])
    first = make_claim(report, users["claimant"])
    second = make_claim(report, users["other"])
    services.act_on_claim(users["service"], first.pk, "approve", confirmations={'evidence_reviewed': True, 'custody_confirmed': True})
    assert "edit" not in {item["key"] for item in report_actions(users["owner"], report)}
    assert "approve" not in {item["key"] for item in claim_actions(users["service"], second)}
    services.act_on_claim(users["service"], first.pk, "start_handover", confirmations={'identity_verified': True, 'custody_confirmed': True})
    assert "close" not in {item["key"] for item in report_actions(users["service"], report)}
    assert "request_close" not in {item["key"] for item in report_actions(users["owner"], report)}


def test_closed_report_cannot_be_reopened_through_any_service(users):
    report = make_report(users["owner"], status="closed")
    for action in ("approve", "reject", "close"):
        with pytest.raises(ValueError):
            services.review_report(users["service"], report.pk, action, "reason")
    with pytest.raises(ValueError):
        services.update_report(users["owner"], report.pk, {"item_name": "Reopen"})
    with pytest.raises(ValueError):
        services.request_report_closure(users["owner"], report.pk)
    assert not ActionHistory.objects.exists()


def test_matching_updates_existing_scores_without_changing_claims(users):
    lost = make_report(users["owner"], report_type="lost", item="Black blue wallet")
    found = make_report(users["owner"], item="Black blue wallet")
    claim = make_claim(found, users["claimant"])
    services.regenerate_matches(users["service"])
    match = Match.objects.get(lost_report=lost, found_report=found)
    assert match.score == 1.0
    lost.item_name = "Other"
    lost.save()
    services.regenerate_matches(users["service"])
    match.refresh_from_db()
    assert match.score == 0.5
    claim.refresh_from_db()
    assert claim.status == "pending"
    assert not Claim.objects.filter(status="awaiting_handover").exists()
