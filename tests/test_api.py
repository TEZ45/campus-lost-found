import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse
from rest_framework.test import APIClient

from apps.reports.models import Claim, ClaimAttachment, Notice, Report

from .test_workflow import make_claim, make_png_bytes, make_report

pytestmark = pytest.mark.django_db


@pytest.fixture
def api_client():
    return APIClient()


def url(name, *args):
    return reverse(f"reports_api:{name}", args=args)


def report_data(**overrides):
    return {
        "report_type": "found", "item_name": "Blue wallet", "category": "Accessories",
        "description": "Near the main entrance", "location": "Library", "event_date": "2026-10-01",
        "contact_name": "Owner", "contact_email": "owner@example.com", "contact_phone": "123456",
        **overrides,
    }


def test_public_api_filters_pages_and_hides_contact(api_client, users):
    public = make_report(users["owner"], item="Public wallet", contact_phone="secret-phone")
    make_report(users["owner"], item="Private wallet", status="pending")
    response = api_client.get(url("reports"), {"q": "wallet", "page_size": 1})
    assert response.status_code == 200
    assert response.json()["count"] == 1
    item = response.json()["results"][0]
    assert item["id"] == public.pk
    assert "contact" not in item
    assert "created_by" not in item
    assert b"secret-phone" not in response.content
    invalid_date = api_client.get(url("reports"), {"date_from": "invalid"})
    assert invalid_date.status_code == 400
    assert "date_from" in invalid_date.json()["error"]["details"]
    assert api_client.get(url("reports"), {"unknown": "x"}).status_code == 400


@pytest.mark.parametrize("role,expected", [(None, 404), ("other", 404), ("owner", 200), ("service", 200)])
def test_private_report_api_visibility(api_client, users, role, expected):
    report = make_report(users["owner"], status="pending")
    if role:
        api_client.force_login(users[role])
    response = api_client.get(url("report", report.pk))
    assert response.status_code == expected
    if expected == 200:
        assert response.json()["contact"]["email"] == report.contact_email


def test_report_write_api_rejects_forged_fields_and_supports_patch(api_client, users):
    api_client.force_login(users["owner"])
    invalid = api_client.post(url("reports"), report_data(status="approved"), format="json")
    assert invalid.status_code == 400
    assert invalid.json()["error"]["code"] == "invalid"
    assert not Report.objects.exists()
    created = api_client.post(url("reports"), report_data(), format="json")
    assert created.status_code == 201
    report = Report.objects.get(pk=created.json()["id"])
    assert report.created_by == users["owner"]
    assert report.status == "pending"
    patched = api_client.patch(url("report", report.pk), {"item_name": "Green wallet"}, format="json")
    assert patched.status_code == 200
    assert patched.json()["item_name"] == "Green wallet"
    assert patched.json()["location"] == "Library"
    assert api_client.get(url("own-reports")).json()["count"] == 1
    api_client.force_login(users["other"])
    assert api_client.patch(url("report", report.pk), {"item_name": "Stolen"}, format="json").status_code == 403


def test_report_photo_create_keep_and_clear(api_client, users):
    api_client.force_login(users["owner"])
    image = SimpleUploadedFile("photo.png", make_png_bytes(), content_type="image/png")
    response = api_client.post(url("reports"), report_data(photo=image), format="multipart")
    assert response.status_code == 201
    report = Report.objects.get(pk=response.json()["id"])
    saved = report.photo.name
    assert saved
    assert api_client.patch(url("report", report.pk), {"location": "Canteen"}, format="json").status_code == 200
    report.refresh_from_db()
    assert report.photo.name == saved
    assert api_client.patch(url("report", report.pk), {"photo": None}, format="json").status_code == 200
    report.refresh_from_db()
    assert not report.photo


def test_claim_workflow_through_api(api_client, users):
    report = make_report(users["owner"])
    api_client.force_login(users["claimant"])
    response = api_client.post(url("create-claim", report.pk), {"evidence": "Serial ABC"}, format="json")
    assert response.status_code == 201
    claim = Claim.objects.get(pk=response.json()["id"])
    assert claim.claimant == users["claimant"]
    assert api_client.get(url("claims")).json()["count"] == 1
    api_client.force_login(users["service"])
    assert api_client.get(url("staff-claims")).json()["count"] == 1
    response = api_client.post(url("claim-action", claim.pk), {"action": "request_evidence", "reason": "Photo needed"})
    assert response.status_code == 200
    assert response.json()["status"] == "awaiting_evidence"
    assert "approve" not in {action["key"] for action in response.json()["allowed_actions"]}
    assert api_client.post(url("claim-action", claim.pk), {"action": "approve"}).status_code == 409
    claim.refresh_from_db()
    assert claim.status == "awaiting_evidence"
    api_client.force_login(users["claimant"])
    assert api_client.post(url("resubmit", claim.pk), {"evidence": "Serial ABC and scratch"}).status_code == 200
    api_client.force_login(users["service"])
    for action, status in [("approve", "awaiting_handover"), ("start_handover", "handover_in_progress"),
                           ("complete_handover", "completed")]:
        response = api_client.post(url("claim-action", claim.pk), {"action": action})
        assert response.status_code == 200
        assert response.json()["status"] == status
    assert api_client.post(url("claim-action", claim.pk), {"action": "complete_handover"}).status_code == 409
    report.refresh_from_db()
    assert report.status == "closed"
    assert api_client.get(url("history")).json()["count"] >= 5


@pytest.mark.parametrize("resolution", ["resolve_keep", "resolve_reject"])
def test_api_dispute_resolution_and_safe_interruption(api_client, users, resolution):
    claim = make_claim(make_report(users["owner"]), users["claimant"])
    api_client.force_login(users["service"])
    action_url = url("claim-action", claim.pk)
    for action, status in [("approve", "awaiting_handover"), ("start_dispute", "under_dispute")]:
        response = api_client.post(action_url, {"action": action})
        assert response.status_code == 200
        assert response.json()["status"] == status
    if resolution == "resolve_reject":
        missing_reason = api_client.post(action_url, {"action": resolution})
        assert missing_reason.status_code == 400
        assert "reason" in missing_reason.json()["error"]["details"]
    resolved = api_client.post(action_url, {"action": resolution, "reason": "Evidence reviewed"})
    assert resolved.status_code == 200
    if resolution == "resolve_keep":
        assert resolved.json()["status"] == "awaiting_handover"
        assert api_client.post(action_url, {"action": "start_handover"}).json()["status"] == "handover_in_progress"
        missing_reason = api_client.post(action_url, {"action": "interrupt_handover"})
        assert missing_reason.status_code == 400
        claim.refresh_from_db()
        assert claim.status == "handover_in_progress"
        interrupted = api_client.post(action_url, {
            "action": "interrupt_handover", "reason": "Item recovered; no transfer occurred",
        })
        assert interrupted.status_code == 200
        assert interrupted.json()["status"] == "awaiting_handover"
    else:
        assert resolved.json()["status"] == "rejected"
    api_client.force_login(users["claimant"])
    withdrawn = api_client.post(url("withdraw", claim.pk), {})
    assert withdrawn.status_code == (200 if resolution == "resolve_keep" else 409)
    claim.refresh_from_db()
    assert claim.status == ("withdrawn" if resolution == "resolve_keep" else "rejected")


@pytest.mark.parametrize("role,expected", [(None, 401), ("owner", 403), ("claimant", 200), ("service", 200), ("admin", 403)])
def test_claim_and_attachment_api_are_private(api_client, users, role, expected):
    claim = make_claim(make_report(users["owner"]), users["claimant"])
    image = SimpleUploadedFile("proof.png", make_png_bytes(), content_type="image/png")
    attachment = ClaimAttachment.objects.create(claim=claim, image=image)
    if role:
        api_client.force_login(users[role])
    assert api_client.get(url("claim", claim.pk)).status_code == expected
    response = api_client.get(url("attachment", attachment.pk))
    assert response.status_code == expected
    if response.streaming:
        assert b"".join(response.streaming_content) == make_png_bytes()
        assert response["Cache-Control"] == "private, no-store"


def test_claim_api_rejects_invalid_images_and_server_owned_fields(api_client, users):
    report = make_report(users["owner"])
    api_client.force_login(users["claimant"])
    fake = SimpleUploadedFile("fake.png", b"not an image", content_type="image/png")
    assert api_client.post(url("create-claim", report.pk), {"evidence": "proof", "images": [fake]}, format="multipart").status_code == 400
    response = api_client.post(url("create-claim", report.pk), {"evidence": "proof", "claimant": users["other"].pk}, format="json")
    assert response.status_code == 400
    assert not Claim.objects.exists()


@pytest.mark.parametrize("content_type", ["image/jpeg", "image/gif"])
@pytest.mark.parametrize("field", ["photo", "images"])
def test_upload_api_rejects_real_png_with_wrong_mime(api_client, users, content_type, field):
    report = make_report(users["owner"])
    api_client.force_login(users["claimant"])
    image = SimpleUploadedFile("proof.png", make_png_bytes(), content_type=content_type)
    if field == "photo":
        response = api_client.post(url("reports"), report_data(photo=image), format="multipart")
    else:
        response = api_client.post(url("create-claim", report.pk), {
            "evidence": "Proof", "images": [image],
        }, format="multipart")
    assert response.status_code == 400
    assert Report.objects.count() == 1
    assert not Claim.objects.exists()


def test_notices_are_owned_and_only_post_marks_read(api_client, users):
    own = Notice.objects.create(user=users["claimant"], title="Your claim")
    other = Notice.objects.create(user=users["other"], title="Other claim")
    assert api_client.get(url("notices")).status_code == 401
    api_client.force_login(users["claimant"])
    assert api_client.get(url("notices")).json()["count"] == 1
    wrong_method = api_client.get(url("notice-read", own.pk))
    assert wrong_method.status_code == 405
    assert wrong_method.json()["error"]["code"] == "method_not_allowed"
    assert api_client.post(url("notice-read", other.pk), {}).status_code == 404
    assert api_client.post(url("notice-read", own.pk), {}).json()["is_read"] is True


@pytest.mark.parametrize("endpoint", ["dashboard", "staff-reports", "staff-claims", "handovers", "history"])
def test_staff_api_requires_business_role(api_client, users, endpoint):
    assert api_client.get(url(endpoint)).status_code == 401
    api_client.force_login(users["admin"])
    assert api_client.get(url(endpoint)).status_code == 403
    api_client.force_login(users["service"])
    assert api_client.get(url(endpoint)).status_code == 200


def test_staff_report_review_closure_and_matching_api(api_client, users):
    report = make_report(users["owner"], status="pending")
    api_client.force_login(users["owner"])
    assert api_client.post(url("regenerate"), {}).status_code == 403
    api_client.force_login(users["service"])
    assert api_client.post(url("review-report", report.pk), {"action": "approve"}).status_code == 200
    assert api_client.post(url("review-report", report.pk), {"action": "approve"}).status_code == 409
    make_report(users["owner"], report_type="lost")
    assert api_client.post(url("regenerate"), {}).status_code == 200
    assert api_client.get(url("matches")).json()["count"] == 1
    api_client.force_login(users["owner"])
    response = api_client.post(url("close", report.pk), {})
    assert response.status_code == 200
    assert response.json()["status"] == "close_requested"
    api_client.force_login(users["service"])
    assert api_client.post(url("review-report", report.pk), {"action": "close"}).json()["status"] == "closed"


def test_session_login_registration_and_mutations_require_csrf(users):
    client = APIClient(enforce_csrf_checks=True)
    credentials = {"username": "student", "password1": "Strong-Test-Password-92", "password2": "Strong-Test-Password-92"}
    assert client.post(url("register"), credentials, format="json").status_code == 403
    assert client.post(url("login"), {"username": "student", "password": "Strong-Test-Password-92"}, format="json").status_code == 403
    token = client.get(url("csrf")).json()["csrf_token"]
    created = client.post(url("register"), credentials, format="json", HTTP_X_CSRFTOKEN=token)
    assert created.status_code == 201
    token = created.json()["csrf_token"]
    assert client.get(url("me")).json()["username"] == "student"
    assert client.post(url("reports"), report_data(), format="json").status_code == 403
    assert client.post(url("reports"), report_data(), format="json", HTTP_X_CSRFTOKEN=token).status_code == 201
    assert client.get(url("logout")).status_code == 405
    assert client.post(url("logout"), {}, format="json", HTTP_X_CSRFTOKEN=token).status_code == 204
    assert client.get(url("me")).status_code == 401
    token = client.get(url("csrf")).json()["csrf_token"]
    response = client.post(url("login"), {"username": "student", "password": "Strong-Test-Password-92"}, format="json", HTTP_X_CSRFTOKEN=token)
    assert response.status_code == 200
    assert client.get(url("me")).json()["username"] == "student"
