"""Access control, search, moderation, notices, attachments and matches."""
import io

import pytest
from django.contrib.auth.models import User
from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse

from apps.reports.models import ActionHistory, Claim, Match, Notice, Report, UserProfile


def make_png_bytes():
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (2, 2), (200, 30, 30)).save(buf, "PNG")
    return buf.getvalue()


@pytest.fixture
def client():
    from django.test import Client
    return Client()


@pytest.fixture
def staff_user(db):
    user = User.objects.create_user("staff1", "staff1@example.com", "pass12345")
    UserProfile.objects.create(user=user, is_service_staff=True)
    return user


@pytest.fixture
def alice(db):
    return User.objects.create_user("alice", "alice@example.com", "pass12345")


@pytest.fixture
def bob(db):
    return User.objects.create_user("bob", "bob@example.com", "pass12345")


def make_report(user, report_type="found", item="Black Wallet", status="approved", **kw):
    return Report.objects.create(
        report_type=report_type, item_name=item, category=kw.pop("category", "Electronics"),
        description=kw.pop("description", "Found near the library"),
        location=kw.pop("location", "Library"), event_date=kw.pop("event_date", "2026-10-01"),
        contact_name=kw.pop("contact_name", "Staff"), contact_email=kw.pop("contact_email", "hidden@x.com"),
        contact_phone=kw.pop("contact_phone", "123456"),
        status=status, created_by=user, **kw,
    )


def make_claim(report, user, evidence="serial ABC", status="pending"):
    return Claim.objects.create(
        report=report, claimant=user, claimant_name=user.username, claimant_email=user.email,
        evidence=evidence, status=status,
    )


# ------------------------------------------------------------------ #
# REQ-1.1: role restrictions                                         #
# ------------------------------------------------------------------ #

@pytest.mark.django_db
def test_staff_pages_require_staff_role(client, alice):
    client.force_login(alice)
    for url in ["staff_dashboard", "staff_report_queue", "staff_claim_queue", "staff_handover_queue"]:
        assert client.get(reverse(url)).status_code == 403


@pytest.mark.django_db
def test_staff_pages_require_login(client):
    assert client.get(reverse("staff_dashboard")).status_code == 302


# ------------------------------------------------------------------ #
# REQ-1.5 + SEC-1.3: search and public visibility                    #
# ------------------------------------------------------------------ #

@pytest.mark.django_db
def test_anonymous_only_sees_approved(client, alice):
    make_report(alice, item="Public Item", status="approved")
    make_report(alice, item="Hidden Item", status="pending")
    resp = client.get(reverse("report_list"))
    assert b"Public Item" in resp.content
    assert b"Hidden Item" not in resp.content


@pytest.mark.django_db
def test_search_combined_filters(client, alice, staff_user):
    make_report(alice, item="Black Wallet", report_type="found", category="Accessories",
                location="Library", event_date="2026-10-01")
    make_report(alice, item="Blue Backpack", report_type="lost", category="Bags",
                location="Gym", event_date="2026-10-05")
    make_report(alice, item="Black Backpack", report_type="found", category="Bags",
                location="Library", event_date="2026-10-10")

    # Keyword
    resp = client.get(reverse("report_list"), {"q": "Black"})
    assert b"Black Wallet" in resp.content and b"Black Backpack" in resp.content
    assert b"Blue Backpack" not in resp.content

    # Keyword + type
    resp = client.get(reverse("report_list"), {"q": "Backpack", "report_type": "found"})
    assert b"Black Backpack" in resp.content
    assert b"Blue Backpack" not in resp.content

    # Category + location + date range
    resp = client.get(reverse("report_list"),
                      {"category": "Bags", "location": "Library",
                       "date_from": "2026-10-01", "date_to": "2026-10-11"})
    assert b"Black Backpack" in resp.content
    assert b"Black Wallet" not in resp.content

    # Status filter is staff-only (public list never shows pending/rejected)
    client.force_login(staff_user)
    resp = client.get(reverse("report_list"), {"status": "approved"})
    assert b"Black Wallet" in resp.content


@pytest.mark.django_db
def test_public_pages_hide_contact_details(client, alice):
    make_report(alice, item="Private Contact Item", contact_email="secret@example.com",
                contact_phone="987654321")
    resp = client.get(reverse("report_list"))
    assert b"secret@example.com" not in resp.content
    resp = client.get(reverse("report_detail", args=[Report.objects.first().pk]))
    assert b"secret@example.com" not in resp.content
    assert b"987654321" not in resp.content


# ------------------------------------------------------------------ #
# REQ-1.3 / 1.4: moderation and owner editing                         #
# ------------------------------------------------------------------ #

@pytest.mark.django_db
def test_report_moderation_approve_and_reject(client, staff_user, alice):
    report = make_report(alice, status="pending")
    client.force_login(staff_user)

    # Rejection without a reason must fail.
    client.post(reverse("staff_report_review", args=[report.pk]), {"action": "reject"})
    report.refresh_from_db()
    assert report.status == "pending"

    client.post(reverse("staff_report_review", args=[report.pk]), {"action": "reject", "reason": "duplicate"})
    report.refresh_from_db()
    assert report.status == "rejected"
    assert ActionHistory.objects.filter(report=report, action="report_rejected",
                                        reason="duplicate").exists()


@pytest.mark.django_db
def test_owner_edit_returns_public_report_to_pending(client, alice, bob, staff_user):
    report = make_report(alice, item="Original Name", status="approved")
    # Bob cannot edit Alice's report (A6).
    client.force_login(bob)
    assert client.get(reverse("report_edit", args=[report.pk])).status_code == 403

    client.force_login(alice)
    client.post(reverse("report_edit", args=[report.pk]),
                {"report_type": "found", "item_name": "Renamed Item", "category": "Electronics",
                 "description": "d", "location": "Library", "event_date": "2026-10-01",
                 "contact_name": "A", "contact_email": "a@e.com"})
    report.refresh_from_db()
    assert report.status == "pending"  # changed public content requires another review (REQ-1.4)
    assert report.item_name == "Renamed Item"


@pytest.mark.django_db
def test_close_request_flow(client, staff_user, alice):
    report = make_report(alice, status="approved")
    client.force_login(alice)
    client.post(reverse("report_close_request", args=[report.pk]))
    report.refresh_from_db()
    assert report.status == "close_requested"

    client.force_login(staff_user)
    client.post(reverse("staff_report_review", args=[report.pk]), {"action": "close", "reason": "ok"})
    report.refresh_from_db()
    assert report.status == "closed"


# ------------------------------------------------------------------ #
# REQ-1.13 / 1.14: notices and audit history                         #
# ------------------------------------------------------------------ #

@pytest.mark.django_db
def test_staff_actions_generate_notices(client, staff_user, alice):
    report = make_report(alice)
    claim = make_claim(report, alice)
    client.force_login(staff_user)
    client.post(reverse("staff_claim_review", args=[claim.pk]), {"action": "approve", "reason": "ok"})
    notice = Notice.objects.get(user=alice)
    assert "approved" in notice.title.lower()
    assert notice.link == f"/claim/{claim.pk}/"


@pytest.mark.django_db
def test_history_records_actor_reason_and_result(client, staff_user, alice):
    report = make_report(alice)
    claim = make_claim(report, alice)
    claim.request_evidence(staff_user, "Need serial photo")
    h = ActionHistory.objects.filter(claim=claim).latest("created_at")
    assert h.actor == staff_user
    assert h.action == "evidence_requested"
    assert h.reason == "Need serial photo"
    assert h.previous_state == "pending"
    assert h.new_state == "awaiting_evidence"


# ------------------------------------------------------------------ #
# CAP-3.1: attachment limits                                         #
# ------------------------------------------------------------------ #

def img(name="a.png", content_type="image/png"):
    return SimpleUploadedFile(name, make_png_bytes(), content_type=content_type)


@pytest.mark.django_db
def test_claim_attachment_cap_three_per_submission(client, alice):
    report = make_report(alice)
    client.force_login(alice)
    resp = client.post(reverse("claim_create", args=[report.pk]),
                       {"evidence": "x", "images": [img("1.png"), img("2.png"), img("3.png"), img("4.png")]})
    assert resp.status_code == 200  # form re-rendered with error
    assert not Claim.objects.filter(report=report).exists()


@pytest.mark.django_db
def test_claim_attachment_rejects_non_jpg_png_and_oversize(client, alice):
    report = make_report(alice)
    client.force_login(alice)
    bad_type = SimpleUploadedFile("x.gif", make_png_bytes(), content_type="image/gif")
    resp = client.post(reverse("claim_create", args=[report.pk]), {"evidence": "x", "images": [bad_type]})
    assert resp.status_code == 200
    assert not Claim.objects.filter(report=report).exists()


@pytest.mark.django_db
def test_report_attachment_single_image(client, alice):
    client.force_login(alice)
    resp = client.post(reverse("report_create"),
                       {"report_type": "found", "item_name": "Umbrella", "category": "Other",
                        "description": "d", "location": "Canteen", "event_date": "2026-10-01",
                        "contact_name": "A", "contact_email": "a@e.com", "photo": img("p.png")})
    assert resp.status_code == 302
    report = Report.objects.get(item_name="Umbrella")
    assert report.photo
    assert report.status == "pending"


# ------------------------------------------------------------------ #
# REQ-1.6: suggested matches (Could)                                 #
# ------------------------------------------------------------------ #

@pytest.mark.django_db
def test_matches_generation_and_reason(client, alice):
    lost = make_report(alice, report_type="lost", item="Black Wallet", category="Accessories")
    found = make_report(alice, report_type="found", item="Wallet Black", category="Accessories")
    client.force_login(alice)
    client.post(reverse("matches"))
    match = Match.objects.filter(lost_report=lost, found_report=found).first()
    assert match is not None
    assert match.score > 0
    assert match.reason  # reason present
    # Suggested matches never approve claims automatically (DOM-1).
    assert match.status == "suggested"
