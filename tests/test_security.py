from pathlib import Path

import pytest
from django.core.exceptions import PermissionDenied
from django.core.files.base import ContentFile
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import IntegrityError, transaction
from django.urls import reverse

from apps.reports.models import ActionHistory, Claim, ClaimAttachment, Match, Notice

from .test_workflow import make_claim, make_png_bytes, make_report

pytestmark = pytest.mark.django_db


@pytest.mark.parametrize("status", ["pending", "rejected", "close_requested", "closed", "approved"])
@pytest.mark.parametrize("role", ["anonymous", "owner", "claimant", "other", "service", "admin"])
def test_report_and_photo_visibility(client, users, status, role):
    report = make_report(users["owner"], status=status)
    report.photo.save("item.png", ContentFile(b"public image"))
    make_claim(report, users["claimant"])
    if role != "anonymous":
        client.force_login(users[role])
    expected = 200 if status == "approved" or role in ("owner", "claimant", "service") else 404
    assert client.get(reverse("report_detail", args=[report.pk])).status_code == expected
    response = client.get(report.photo.url)
    assert response.status_code == expected
    if response.streaming:
        list(response.streaming_content)


@pytest.mark.parametrize("role,expected", [
    ("anonymous", 302), ("owner", 403), ("claimant", 200),
    ("other", 403), ("service", 200), ("admin", 403),
])
@pytest.mark.parametrize("legacy", [False, True])
def test_evidence_download_permissions(client, users, settings, role, expected, legacy):
    report = make_report(users["owner"])
    claim = make_claim(report, users["claimant"])
    attachment = ClaimAttachment.objects.create(claim=claim, image="claims/proof.png")
    root = settings.MEDIA_ROOT if legacy else settings.PRIVATE_MEDIA_ROOT
    path = root / attachment.image.name
    path.parent.mkdir(parents=True)
    path.write_bytes(b"private evidence")
    if role != "anonymous":
        client.force_login(users[role])
    response = client.get(reverse("claim_attachment", args=[attachment.pk]))
    assert response.status_code == expected
    if expected == 200:
        assert b"".join(response.streaming_content) == b"private evidence"
        assert response["Cache-Control"] == "private, no-store"
        assert response["Content-Type"] == "application/octet-stream"
        assert response["Content-Disposition"].startswith("attachment;")
    assert client.get("/media/claims/proof.png").status_code == 404


def test_new_evidence_uses_private_storage_and_authenticated_link(client, users, settings):
    claim = make_claim(make_report(users["owner"]), users["claimant"])
    attachment = ClaimAttachment.objects.create(claim=claim, image=ContentFile(b"proof", "proof.png"))
    assert Path(attachment.image.path).is_relative_to(settings.PRIVATE_MEDIA_ROOT)
    with pytest.raises(ValueError):
        _ = attachment.image.url
    client.force_login(users["claimant"])
    content = client.get(reverse("claim_detail", args=[claim.pk])).content
    assert reverse("claim_attachment", args=[attachment.pk]).encode() in content
    assert b"/media/claims/" not in content


def test_legacy_storage_move_is_repeatable(settings, users):
    claim = make_claim(make_report(users["owner"]), users["claimant"])
    attachment = ClaimAttachment.objects.create(claim=claim, image="claims/proof.png")
    source = settings.MEDIA_ROOT / attachment.image.name
    source.parent.mkdir(parents=True)
    source.write_bytes(b"proof")
    call_command("move_private_evidence", verbosity=0)
    call_command("move_private_evidence", verbosity=0)
    assert not source.exists()
    assert Path(attachment.image.path).read_bytes() == b"proof"
    attachment.refresh_from_db()
    assert attachment.image.name == "claims/proof.png"


def test_legacy_storage_move_preserves_conflicting_files(settings):
    source = settings.MEDIA_ROOT / "claims/proof.png"
    target = settings.PRIVATE_MEDIA_ROOT / "claims/proof.png"
    source.parent.mkdir(parents=True)
    target.parent.mkdir(parents=True)
    source.write_bytes(b"original")
    target.write_bytes(b"different")
    with pytest.raises(CommandError):
        call_command("move_private_evidence", verbosity=0)
    assert source.read_bytes() == b"original"
    assert target.read_bytes() == b"different"


@pytest.mark.parametrize("status", ["pending", "rejected", "close_requested", "closed"])
def test_matches_hide_nonpublic_reports(client, users, status):
    lost = make_report(users["owner"], report_type="lost", item="Public wallet")
    found = make_report(users["owner"], status=status, item="Hidden detail 123")
    Match.objects.create(lost_report=lost, found_report=found, score=0.5)
    for url in (reverse("matches"), reverse("report_detail", args=[lost.pk])):
        assert b"Hidden detail 123" not in client.get(url).content


@pytest.mark.parametrize("role,expected", [("owner", 200), ("service", 200), ("admin", 403)])
def test_report_management_uses_business_role(client, users, role, expected):
    report = make_report(users["owner"])
    client.force_login(users[role])
    assert client.get(reverse("report_edit", args=[report.pk])).status_code == expected
    assert client.get(reverse("report_close_request", args=[report.pk])).status_code == expected
    response = client.post(reverse("report_close_request", args=[report.pk]))
    assert response.status_code == (302 if expected == 200 else 403)
    report.refresh_from_db()
    assert report.status == ("close_requested" if expected == 200 else "approved")


@pytest.mark.parametrize("role", ["admin", "service_admin"])
def test_business_admin_is_role_gated_and_read_only(client, users, role):
    report = make_report(users["owner"])
    claim = make_claim(report, users["claimant"])
    history = ActionHistory.objects.create(actor=users["service"], report=report, action="test")
    notice = Notice.objects.create(user=users["claimant"], title="Private result")
    lost = make_report(users["owner"], report_type="lost")
    match = Match.objects.create(lost_report=lost, found_report=report)
    client.force_login(users[role])
    for name, obj in (("report", report), ("claim", claim), ("actionhistory", history),
                      ("notice", notice), ("match", match)):
        prefix = f"/admin/reports/{name}/"
        expected = 200 if role == "service_admin" else 403
        assert client.get(prefix).status_code == expected
        assert client.get(f"{prefix}{obj.pk}/change/").status_code == expected
        assert client.post(f"{prefix}{obj.pk}/change/", {"status": "completed"}).status_code == 403
        assert client.post(f"{prefix}{obj.pk}/delete/", {"post": "yes"}).status_code == 403
        assert client.get(f"{prefix}add/").status_code == 403
    for action in ("approve_reports", "reject_reports", "close_reports", "delete_selected"):
        client.post("/admin/reports/report/", {"action": action, "_selected_action": [report.pk]})
    report.refresh_from_db()
    claim.refresh_from_db()
    assert report.status == "approved"
    assert claim.status == "pending"
    assert ActionHistory.objects.filter(pk=history.pk).exists()
    assert client.post(f"/admin/auth/user/{users['claimant'].pk}/delete/", {"post": "yes"}).status_code == 403


def test_model_operations_enforce_actor_permissions(users):
    report = make_report(users["owner"])
    claim = make_claim(report, users["claimant"])
    for role in ("owner", "other", "admin"):
        with pytest.raises(PermissionDenied):
            claim.approve(users[role])
        with pytest.raises(PermissionDenied):
            claim.withdraw(users[role])
        with pytest.raises(PermissionDenied):
            report.review(users[role], "close")


@pytest.mark.parametrize("status", Claim.RESERVED_STATES)
def test_database_rejects_second_reservation(users, status):
    report = make_report(users["owner"])
    make_claim(report, users["claimant"], status=status)
    with pytest.raises(IntegrityError), transaction.atomic():
        make_claim(report, users["other"], status="awaiting_handover")
    assert report.claims.filter(status__in=Claim.RESERVED_STATES).count() == 1


@pytest.mark.parametrize("action", ["withdraw", "reject", "invalidate", "start_dispute"])
def test_stale_claim_cannot_override_handover(users, action):
    claim = make_claim(make_report(users["owner"]), users["claimant"])
    claim.approve(users["service"])
    stale = Claim.objects.get(pk=claim.pk)
    claim.start_handover(users["service"])
    actor = users["claimant"] if action == "withdraw" else users["service"]
    args = (actor,) if action == "withdraw" else (actor, "reason")
    with pytest.raises(ValueError):
        getattr(stale, action)(*args)
    claim.refresh_from_db()
    assert claim.status == "handover_in_progress"
    assert claim.history.count() == 2


def test_stale_completion_cannot_repeat_or_undo_completion(users):
    claim = make_claim(make_report(users["owner"]), users["claimant"])
    claim.approve(users["service"])
    claim.start_handover(users["service"])
    stale = Claim.objects.get(pk=claim.pk)
    claim.complete_handover(users["service"])
    with pytest.raises(ValueError):
        stale.complete_handover(users["service"])
    with pytest.raises(ValueError):
        stale.interrupt_handover(users["service"], "retry")
    assert claim.history.filter(action="handover_completed").count() == 1


def test_approval_checks_latest_claim_and_report(users):
    report = make_report(users["owner"])
    claim = make_claim(report, users["claimant"])
    stale = Claim.objects.get(pk=claim.pk)
    claim.request_evidence(users["service"], "More proof")
    with pytest.raises(ValueError):
        stale.approve(users["service"])
    claim.resubmit_evidence()
    report.review(users["service"], "close")
    with pytest.raises(ValueError):
        claim.approve(users["service"])
    with pytest.raises(ValueError):
        report.review(users["service"], "approve")


def test_completion_rolls_back_all_changes_on_notice_failure(users, monkeypatch):
    report = make_report(users["owner"])
    claim = make_claim(report, users["claimant"])
    other = make_claim(report, users["other"])
    claim.approve(users["service"])
    claim.start_handover(users["service"])

    def fail(**kwargs):
        raise RuntimeError("Notice storage unavailable")

    monkeypatch.setattr(Notice.objects, "create", fail)
    with pytest.raises(RuntimeError):
        claim.complete_handover(users["service"])
    report.refresh_from_db()
    claim.refresh_from_db()
    other.refresh_from_db()
    assert (report.status, claim.status, other.status) == ("approved", "handover_in_progress", "pending")
    assert not ActionHistory.objects.filter(action__in=["handover_completed", "invalidated"]).exists()


def test_withdrawal_requires_post(client, users):
    claim = make_claim(make_report(users["owner"]), users["claimant"])
    client.force_login(users["claimant"])
    assert client.get(reverse("claim_withdraw", args=[claim.pk])).status_code == 405
    claim.refresh_from_db()
    assert claim.status == "pending"


def test_resubmission_saves_evidence_with_state(client, users):
    claim = make_claim(make_report(users["owner"]), users["claimant"])
    claim.request_evidence(users["service"], "More proof")
    client.force_login(users["claimant"])
    url = reverse("claim_resubmit", args=[claim.pk])
    assert client.post(url, {"evidence": "Updated proof"}).status_code == 302
    assert client.post(url, {"evidence": "Invalid retry"}).status_code == 302
    claim.refresh_from_db()
    assert (claim.status, claim.evidence) == ("pending", "Updated proof")


def test_resubmission_cannot_exceed_attachment_limit(client, users):
    claim = make_claim(make_report(users["owner"]), users["claimant"])
    for index in range(5):
        ClaimAttachment.objects.create(claim=claim, image=f"claims/{index}.png")
    claim.request_evidence(users["service"], "More proof")
    client.force_login(users["claimant"])
    image = SimpleUploadedFile("extra.png", make_png_bytes(), content_type="image/png")
    response = client.post(reverse("claim_resubmit", args=[claim.pk]), {
        "evidence": "New proof", "images": [image],
    })
    assert response.status_code == 200
    claim.refresh_from_db()
    assert claim.status == "awaiting_evidence"
    assert claim.attachments.count() == 5


def test_report_changes_cannot_interrupt_active_handover(client, users):
    report = make_report(users["owner"])
    claim = make_claim(report, users["claimant"])
    claim.approve(users["service"])
    claim.start_handover(users["service"])
    client.force_login(users["owner"])
    assert client.post(reverse("report_close_request", args=[report.pk])).status_code == 302
    assert client.post(reverse("report_edit", args=[report.pk]), {"item_name": "Changed"}).status_code == 302
    report.refresh_from_db()
    claim.refresh_from_db()
    assert report.status == "approved"
    assert claim.status == "handover_in_progress"


def test_report_closure_rolls_back_invalidations_on_failure(users, monkeypatch):
    report = make_report(users["owner"])
    claim = make_claim(report, users["claimant"])
    make_claim(report, users["other"])
    claim.approve(users["service"])
    original_create = Notice.objects.create
    count = 0

    def fail_second_notice(**kwargs):
        nonlocal count
        count += 1
        if count == 2:
            raise RuntimeError("Notice storage unavailable")
        return original_create(**kwargs)

    monkeypatch.setattr(Notice.objects, "create", fail_second_notice)
    with pytest.raises(RuntimeError):
        report.review(users["service"], "close")
    report.refresh_from_db()
    claim.refresh_from_db()
    assert report.status == "approved"
    assert claim.status == "awaiting_handover"
    assert report.claims.filter(status="pending").count() == 1
    assert not report.history.filter(action__in=["invalidated", "report_closed"]).exists()
