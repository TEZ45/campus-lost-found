from pathlib import Path

import pytest
from django.core.exceptions import ValidationError

from apps.reports import services
from apps.reports.models import ActionHistory, Claim, ClaimAttachment, Notice, Report
from apps.reports.notifications import deliver_pending

from .test_uploads import upload
from .test_workflow import make_claim, make_report

pytestmark = pytest.mark.django_db


def snapshot(root):
    return {str(path.relative_to(root)): path.read_bytes()
            for path in root.rglob("*") if path.is_file()}


def report_data(**changes):
    return {
        "report_type": "found", "item_name": "Wallet", "location": "Library",
        "event_date": "2026-10-01", "contact_name": "Owner", **changes,
    }


def fail_history(monkeypatch):
    def fail(**kwargs):
        raise RuntimeError("History storage unavailable")

    monkeypatch.setattr(ActionHistory.objects, "create", fail)


def fail_attachment(monkeypatch, number=1):
    original = ClaimAttachment.save
    saved = 0

    def save_then_fail(self, *args, **kwargs):
        nonlocal saved
        original(self, *args, **kwargs)
        saved += 1
        if saved == number:
            raise RuntimeError("Attachment persistence failed")

    monkeypatch.setattr(ClaimAttachment, "save", save_then_fail)


def test_report_creation_cleans_new_file_after_history_failure(users, settings, monkeypatch):
    fail_history(monkeypatch)
    with pytest.raises(RuntimeError, match="History"):
        services.create_report(users["owner"], report_data(photo=upload()))
    assert not Report.objects.exists()
    assert snapshot(settings.MEDIA_ROOT) == {}


def test_report_replacement_failure_preserves_original_file(users, settings, monkeypatch):
    report = make_report(users["owner"])
    report.photo.save("original.png", upload())
    original_name = report.photo.name
    original_files = snapshot(settings.MEDIA_ROOT)
    fail_history(monkeypatch)
    with pytest.raises(RuntimeError, match="History"):
        services.update_report(users["owner"], report.pk, {
            "item_name": "Changed", "photo": upload("JPEG"),
        })
    report.refresh_from_db()
    assert report.photo.name == original_name
    assert report.item_name == "Black Wallet"
    assert report.status == "approved"
    assert snapshot(settings.MEDIA_ROOT) == original_files


def test_create_failure_does_not_delete_a_preexisting_file(users, settings, monkeypatch):
    report = make_report(users["owner"])
    report.photo.save("original.png", upload())
    original_files = snapshot(settings.MEDIA_ROOT)
    fail_history(monkeypatch)
    with pytest.raises((RuntimeError, ValidationError)):
        services.create_report(users["owner"], report_data(photo=report.photo))
    assert Report.objects.count() == 1
    assert snapshot(settings.MEDIA_ROOT) == original_files


def test_rejected_report_reuses_existing_photo_and_returns_to_review(users, settings):
    report = make_report(users["owner"], status="rejected")
    report.photo.save("original.png", upload())
    original_files = snapshot(settings.MEDIA_ROOT)
    updated = services.update_report(users["owner"], report.pk, {
        "description": "Corrected details", "photo": report.photo,
    })
    assert updated.status == "pending"
    assert updated.photo.name == report.photo.name
    assert snapshot(settings.MEDIA_ROOT) == original_files
    assert updated.history.get().previous_state == "rejected"


@pytest.mark.parametrize("failure", ["history", "attachment"])
def test_claim_creation_rolls_back_rows_and_uploaded_files(users, settings, monkeypatch, failure):
    report = make_report(users["owner"])
    if failure == "history":
        fail_history(monkeypatch)
    else:
        fail_attachment(monkeypatch, number=2)
    with pytest.raises(RuntimeError):
        services.create_claim(users["claimant"], report.pk, {"evidence": "Proof"},
                              [upload(), upload()])
    assert not Claim.objects.exists()
    assert not ClaimAttachment.objects.exists()
    assert not ActionHistory.objects.exists()
    assert snapshot(settings.PRIVATE_MEDIA_ROOT) == {}


@pytest.mark.parametrize("failure", ["history", "attachment"])
def test_resubmission_failure_restores_state_evidence_and_original_files(
    users, settings, monkeypatch, failure,
):
    claim = make_claim(make_report(users["owner"]), users["claimant"],
                       status="awaiting_evidence", evidence="Original proof")
    attachment = ClaimAttachment.objects.create(claim=claim, image=upload())
    original_files = snapshot(settings.PRIVATE_MEDIA_ROOT)
    if failure == "history":
        fail_history(monkeypatch)
    else:
        fail_attachment(monkeypatch)
    with pytest.raises(RuntimeError):
        services.resubmit_claim(users["claimant"], claim.pk, "Updated proof", [upload()])
    claim.refresh_from_db()
    assert (claim.status, claim.evidence) == ("awaiting_evidence", "Original proof")
    assert list(claim.attachments.values_list("pk", flat=True)) == [attachment.pk]
    assert not claim.history.exists()
    assert snapshot(settings.PRIVATE_MEDIA_ROOT) == original_files


@pytest.mark.parametrize("existing,accepted", [(2, True), (3, False)])
def test_service_enforces_total_evidence_limit_under_lock(users, settings, existing, accepted):
    claim = make_claim(make_report(users["owner"]), users["claimant"], status="awaiting_evidence")
    for _ in range(existing):
        ClaimAttachment.objects.create(claim=claim, image=upload())
    previous_files = snapshot(settings.PRIVATE_MEDIA_ROOT)
    images = [upload() for _ in range(3)]
    if accepted:
        services.resubmit_claim(users["claimant"], claim.pk, "New proof", images)
        claim.refresh_from_db()
        assert (claim.status, claim.attachments.count()) == ("pending", 5)
    else:
        with pytest.raises(ValidationError):
            services.resubmit_claim(users["claimant"], claim.pk, "New proof", images)
        claim.refresh_from_db()
        assert (claim.status, claim.attachments.count()) == ("awaiting_evidence", 3)
        assert snapshot(settings.PRIVATE_MEDIA_ROOT) == previous_files


def test_report_review_notice_failure_preserves_report_and_photo(users, settings, monkeypatch):
    report = make_report(users["owner"], status="pending")
    report.photo.save("original.png", upload())
    original_files = snapshot(settings.MEDIA_ROOT)

    def fail(**kwargs):
        raise RuntimeError("Notice storage unavailable")

    monkeypatch.setattr(Notice.objects, "create", fail)
    services.review_report(users["service"], report.pk, "approve")
    assert deliver_pending()["failed"] == 1
    report.refresh_from_db()
    assert report.status == "approved"
    assert report.history.filter(action="report_approved").exists()
    assert snapshot(settings.MEDIA_ROOT) == original_files


def test_claim_action_notice_failure_preserves_claim_and_evidence(users, monkeypatch):
    claim = make_claim(make_report(users["owner"]), users["claimant"])
    attachment = ClaimAttachment.objects.create(claim=claim, image=upload())
    original = Path(attachment.image.path).read_bytes()

    def fail(**kwargs):
        raise RuntimeError("Notice storage unavailable")

    monkeypatch.setattr(Notice.objects, "create", fail)
    services.act_on_claim(users["service"], claim.pk, "approve",
                          confirmations={"evidence_reviewed": True, "custody_confirmed": True})
    assert deliver_pending()["failed"] == 1
    claim.refresh_from_db()
    assert claim.status == "awaiting_handover"
    assert claim.history.filter(action="approved").exists()
    assert Path(attachment.image.path).read_bytes() == original
