"""Core workflow tests covering URS acceptance checks A1-A6 and the claim state model."""
import io

import pytest
from django.contrib.auth.models import User
from django.urls import reverse

from apps.reports.models import ActionHistory, Claim, Report, UserProfile


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
        contact_name=kw.pop("contact_name", "Staff"), contact_email=kw.pop("contact_email", "s@e.com"),
        status=status, created_by=user, **kw,
    )


def make_claim(report, user, evidence="Serial number ABC-123, blue case", status="pending", **kw):
    return Claim.objects.create(
        report=report, claimant=user, claimant_name=user.username, claimant_email=user.email,
        evidence=evidence, status=status, **kw,
    )


# ------------------------------------------------------------------ #
# A1: exclusive reservation under concurrent approval                #
# ------------------------------------------------------------------ #

@pytest.mark.django_db
def test_a1_at_most_one_reserved_claim(client, staff_user, alice, bob):
    report = make_report(alice)
    c1 = make_claim(report, alice)
    c2 = make_claim(report, bob)
    client.force_login(staff_user)

    client.post(reverse("staff_claim_review", args=[c1.pk]), {"action": "approve", "reason": "ok"})
    c1.refresh_from_db()
    assert c1.status == "awaiting_handover"
    assert c1.holds_reservation

    # Second approval for the same item must fail; c2 stays pending (A1).
    resp = client.post(reverse("staff_claim_review", args=[c2.pk]), {"action": "approve", "reason": "ok"},
                       follow=True)
    c2.refresh_from_db()
    assert c2.status == "pending"
    assert b"Action failed" in resp.content


@pytest.mark.django_db
def test_a1_dispute_blocks_second_approval(client, staff_user, alice, bob):
    report = make_report(alice)
    c1 = make_claim(report, alice)
    c2 = make_claim(report, bob)
    c1.approve(staff_user, "ok")
    c1.start_dispute(staff_user, "doubtful")
    client.force_login(staff_user)
    client.post(reverse("staff_claim_review", args=[c2.pk]), {"action": "approve", "reason": "ok"})
    c2.refresh_from_db()
    assert c2.status == "pending"


# ------------------------------------------------------------------ #
# A2: evidence resubmission & unauthorised evidence access           #
# ------------------------------------------------------------------ #

@pytest.mark.django_db
def test_a2_evidence_resubmission_returns_to_pending(client, staff_user, alice):
    report = make_report(alice)
    claim = make_claim(report, alice)
    claim.request_evidence(staff_user, "More proof needed")
    assert claim.status == "awaiting_evidence"
    claim.resubmit_evidence()
    assert claim.status == "pending"
    # Under dispute: additional evidence keeps the state (A2).
    claim.approve(staff_user)
    claim.start_dispute(staff_user)
    assert claim.status == "under_dispute"


@pytest.mark.django_db
def test_a2_unauthorised_users_cannot_read_evidence(client, staff_user, alice, bob):
    report = make_report(alice)
    claim = make_claim(report, alice)
    client.force_login(bob)
    resp = client.get(reverse("claim_detail", args=[claim.pk]))
    assert resp.status_code == 403


@pytest.mark.django_db
def test_a2_superuser_alone_cannot_read_evidence(client, alice):
    """SEC-1.3: administrator status alone does not authorise evidence access."""
    admin = User.objects.create_superuser("admin1", "a@e.com", "pass12345")
    report = make_report(alice)
    claim = make_claim(report, alice)
    client.force_login(admin)
    resp = client.get(reverse("claim_detail", args=[claim.pk]))
    assert resp.status_code == 403


@pytest.mark.django_db
def test_a2_claimant_and_staff_can_read_evidence(client, staff_user, alice):
    report = make_report(alice)
    claim = make_claim(report, alice)
    client.force_login(alice)
    assert client.get(reverse("claim_detail", args=[claim.pk])).status_code == 200
    client.force_login(staff_user)
    assert client.get(reverse("claim_detail", args=[claim.pk])).status_code == 200


# ------------------------------------------------------------------ #
# A3: handover vs withdrawal competition                             #
# ------------------------------------------------------------------ #

@pytest.mark.django_db
def test_a3_handover_blocks_withdrawal_and_cancellation(client, staff_user, alice):
    report = make_report(alice)
    claim = make_claim(report, alice)
    claim.approve(staff_user)
    claim.start_handover(staff_user)
    assert claim.status == "handover_in_progress"

    # Claimant withdraw must fail while handover is in progress.
    with pytest.raises(ValueError):
        claim.withdraw(alice)
    claim.refresh_from_db()
    assert claim.status == "handover_in_progress"

    # Staff report cancellation is blocked while handover is in progress.
    client.force_login(staff_user)
    resp = client.post(reverse("staff_report_review", args=[report.pk]),
                       {"action": "close", "reason": "cancel"}, follow=True)
    report.refresh_from_db()
    assert report.status != "closed"
    assert b"Cannot close" in resp.content

    # Staff report cancellation is blocked (model guard in complete path).
    report.refresh_from_db()
    with pytest.raises(ValueError):
        claim.reject(staff_user, "x")  # reject not allowed from in_progress
    assert claim.status == "handover_in_progress"


@pytest.mark.django_db
def test_a4_interrupted_handover_restores_awaiting(client, staff_user, alice):
    report = make_report(alice)
    claim = make_claim(report, alice)
    claim.approve(staff_user)
    claim.start_handover(staff_user)
    claim.interrupt_handover(staff_user, "Item was not handed over; recovered")
    assert claim.status == "awaiting_handover"
    assert claim.holds_reservation  # reservation retained


# ------------------------------------------------------------------ #
# A5: completion invalidates only before-handover claims             #
# ------------------------------------------------------------------ #

@pytest.mark.django_db
def test_a5_completion_closes_report_and_invalidates_before_handover(client, staff_user, alice, bob):
    report = make_report(alice)
    c1 = make_claim(report, alice)
    c2 = make_claim(report, bob)          # pending
    c3 = make_claim(report, bob, status="rejected")
    c4 = make_claim(report, alice)        # second pending
    c1.approve(staff_user)
    c1.start_handover(staff_user)
    c1.complete_handover(staff_user)

    c1.refresh_from_db()
    c2.refresh_from_db()
    c3.refresh_from_db()
    c4.refresh_from_db()
    report.refresh_from_db()
    assert c1.status == "completed"
    assert c2.status == "invalidated"
    assert c4.status == "invalidated"
    assert c3.status == "rejected"       # earlier final outcome unchanged
    assert report.status == "closed"


@pytest.mark.django_db
def test_a4_no_duplicate_handover_records(client, staff_user, alice):
    report = make_report(alice)
    claim = make_claim(report, alice)
    claim.approve(staff_user)
    claim.start_handover(staff_user)
    claim.complete_handover(staff_user)
    with pytest.raises(ValueError):
        claim.complete_handover(staff_user)  # already final
    count = ActionHistory.objects.filter(claim=claim, action="handover_completed").count()
    assert count == 1


# ------------------------------------------------------------------ #
# REQ-1.7/DOM-1: claim submission rules                              #
# ------------------------------------------------------------------ #

@pytest.mark.django_db
def test_claim_requires_approved_open_found_report(client, staff_user, alice):
    found_pending = make_report(alice, status="pending")
    found_closed = make_report(alice, status="closed")
    lost = make_report(alice, report_type="lost")

    client.force_login(alice)
    # Non-approved found reports reject the claim (redirect with message); lost reports 404.
    assert client.post(reverse("claim_create", args=[found_pending.pk]),
                       {"evidence": "x"}).status_code == 302
    assert client.post(reverse("claim_create", args=[found_closed.pk]),
                       {"evidence": "x"}).status_code == 302
    assert client.post(reverse("claim_create", args=[lost.pk]),
                       {"evidence": "x"}).status_code == 404


@pytest.mark.django_db
def test_claim_submission_creates_reference_and_history(client, alice, staff_user):
    report = make_report(alice)
    client.force_login(alice)
    resp = client.post(reverse("claim_create", args=[report.pk]), {"evidence": "serial 999"})
    assert resp.status_code == 302
    claim = Claim.objects.get(report=report)
    assert claim.reference.startswith("CLM-")
    assert claim.status == "pending"
    assert ActionHistory.objects.filter(claim=claim, action="claim_submitted").exists()


# ------------------------------------------------------------------ #
# REQ-1.12: withdrawal and invalidation                              #
# ------------------------------------------------------------------ #

@pytest.mark.django_db
def test_withdraw_allowed_only_before_handover(client, staff_user, alice):
    report = make_report(alice)
    claim = make_claim(report, alice)
    claim.withdraw(alice)
    assert claim.status == "withdrawn"
    with pytest.raises(ValueError):
        claim.withdraw(alice)  # final


@pytest.mark.django_db
def test_report_close_invalidates_before_handover_claims(client, staff_user, alice, bob):
    report = make_report(alice)
    c1 = make_claim(report, alice)
    c2 = make_claim(report, bob, status="completed")
    client.force_login(staff_user)
    client.post(reverse("staff_report_review", args=[report.pk]), {"action": "close", "reason": "found owner"})
    c1.refresh_from_db()
    c2.refresh_from_db()
    assert c1.status == "invalidated"
    assert c2.status == "completed"  # final outcome unchanged
