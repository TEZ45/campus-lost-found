import json
from datetime import timedelta
from io import StringIO
from unittest.mock import patch

import pytest
from django.core.files.base import ContentFile
from django.core.management import call_command
from django.db import models
from django.utils import timezone

from apps.reports.lifecycle import cleanup_retention
from apps.reports.models import (
    ActionHistory,
    Claim,
    ClaimAttachment,
    FileDeletion,
    Notice,
    NotificationEvent,
    Report,
    StorageLedger,
)
from apps.reports.storage_quota import storage_for

from .test_workflow import make_claim, make_report

pytestmark = pytest.mark.django_db


def final_claim(users, now, days=15):
    report = make_report(users["owner"])
    claim = make_claim(report, users["claimant"], status="rejected")
    Claim.objects.filter(pk=claim.pk).update(finalized_at=now - timedelta(days=days))
    claim.refresh_from_db()
    return claim


def attachment(claim, name="claims/proof.png"):
    name = storage_for("private").save(name, ContentFile(b"private proof"))
    storage_for("public").save(name, ContentFile(b"legacy proof"))
    return ClaimAttachment.objects.create(claim=claim, image=name)


def history(users, claim, **kwargs):
    return ActionHistory.objects.create(
        actor=users["service"], report=claim.report, claim=claim,
        action="rejected", previous_state="pending", new_state="rejected",
        reason="secret evidence", result="copied secret", confirmations={"evidence_reviewed": True},
        **kwargs,
    )


def test_dry_run_changes_neither_database_nor_files(users):
    now = timezone.now()
    claim = final_claim(users, now)
    image = attachment(claim)
    log = history(users, claim)
    result = cleanup_retention(now=now)
    assert result["evidence"] == 1
    claim.refresh_from_db()
    log.refresh_from_db()
    assert claim.evidence and log.reason
    assert ClaimAttachment.objects.filter(pk=image.pk).exists()
    assert storage_for("private").exists(image.image.name)
    assert not FileDeletion.objects.exists()
    assert not StorageLedger.objects.exists()


def test_evidence_boundary_scrubs_text_snapshots_and_both_file_copies(users):
    now = timezone.now()
    eligible = final_claim(users, now)
    young = final_claim(users, now, days=14)
    image = attachment(eligible)
    log = history(users, eligible)
    result = cleanup_retention(apply=True, now=now)
    eligible.refresh_from_db()
    young.refresh_from_db()
    log.refresh_from_db()
    assert result["evidence"] == 1
    assert eligible.evidence == "" and eligible.evidence_purged_at == now
    assert young.evidence and young.evidence_purged_at is None
    assert not eligible.attachments.exists()
    assert not storage_for("private").exists(image.image.name)
    assert not storage_for("public").exists(image.image.name)
    assert (log.reason, log.result) == ("", "")
    assert (log.actor_id, log.action, log.new_state) == (users["service"].pk, "rejected", "rejected")
    assert log.confirmations == {"evidence_reviewed": True}
    with pytest.raises(ValueError, match="append-only"):
        ActionHistory.objects.filter(pk=log.pk).update(reason="changed")


@pytest.mark.parametrize("status", ["pending", "awaiting_evidence", "under_dispute", "handover_in_progress"])
def test_active_cases_retain_evidence(users, status):
    now = timezone.now()
    claim = final_claim(users, now, days=60)
    Claim.objects.filter(pk=claim.pk).update(status=status)
    image = attachment(claim)
    cleanup_retention(apply=True, now=now)
    claim.refresh_from_db()
    assert claim.evidence
    assert claim.attachments.filter(pk=image.pk).exists()


def test_uncertain_handover_holds_other_final_evidence(users):
    now = timezone.now()
    claim = final_claim(users, now, days=60)
    make_claim(claim.report, users["other"], status="handover_in_progress")
    cleanup_retention(apply=True, now=now)
    claim.refresh_from_db()
    assert claim.evidence


def test_business_deletion_detaches_retained_audit_then_history_expiry_removes_outbox(users):
    now = timezone.now()
    claim = final_claim(users, now, days=30)
    report = claim.report
    Report.objects.filter(pk=report.pk).update(status="closed", closed_at=now - timedelta(days=30))
    log = history(users, claim)
    event = NotificationEvent.objects.create(history=log, user=users["claimant"], title="Decision")
    notice = Notice.objects.create(user=users["claimant"], event=event, title="Decision")
    claim_reference, report_reference = claim.reference, report.reference
    cleanup_retention(apply=True, now=now)
    assert not Claim.objects.filter(pk=claim.pk).exists()
    assert not Report.objects.filter(pk=report.pk).exists()
    log.refresh_from_db()
    assert log.claim_id is None and log.report_id is None
    assert (log.claim_reference, log.report_reference) == (claim_reference, report_reference)
    assert Notice.objects.filter(pk=notice.pk).exists()
    models.QuerySet(model=ActionHistory).filter(pk=log.pk).update(created_at=now - timedelta(days=45))
    cleanup_retention(apply=True, now=now)
    assert not ActionHistory.objects.filter(pk=log.pk).exists()
    assert not NotificationEvent.objects.filter(pk=event.pk).exists()
    assert not Notice.objects.filter(pk=notice.pk).exists()


def test_history_expires_without_deleting_active_case_or_evidence(users):
    now = timezone.now()
    claim = make_claim(make_report(users["owner"]), users["claimant"])
    log = history(users, claim)
    models.QuerySet(model=ActionHistory).filter(pk=log.pk).update(created_at=now - timedelta(days=90))
    cleanup_retention(apply=True, now=now)
    assert not ActionHistory.objects.filter(pk=log.pk).exists()
    claim.refresh_from_db()
    assert claim.evidence and claim.status == "pending"


def test_missing_legacy_timestamps_are_reported_and_skipped(users):
    claim = make_claim(make_report(users["owner"], status="closed"), users["claimant"], status="rejected")
    result = cleanup_retention(apply=True)
    assert result["legacy_claims_missing_finalized_at"] == 1
    assert result["legacy_reports_missing_closed_at"] == 1
    assert Claim.objects.filter(pk=claim.pk).exists()
    assert Report.objects.filter(pk=claim.report_id).exists()


def test_legacy_notice_bodies_are_scrubbed_with_final_evidence(users):
    now = timezone.now()
    claim = final_claim(users, now)
    Claim.objects.filter(pk=claim.pk).update(current_reason="Copied private evidence")
    report = claim.report
    Report.objects.filter(pk=report.pk).update(
        status="closed", closed_at=now - timedelta(days=15), review_reason="Copied report proof",
    )
    notices = [
        Notice.objects.create(user=users["claimant"], title="Claim update",
                              link=f"/claim/{claim.pk}/", body="Copied private evidence"),
        Notice.objects.create(user=users["owner"], title="Report update",
                              link=f"/report/{report.pk}/", body="Copied report proof"),
    ]
    cleanup_retention(apply=True, now=now)
    assert Notice.objects.filter(pk__in=[notice.pk for notice in notices], body="").count() == 2
    claim.refresh_from_db()
    report.refresh_from_db()
    assert not claim.current_reason and not report.review_reason


def test_deletion_failure_is_durable_and_retryable(users):
    now = timezone.now()
    claim = final_claim(users, now)
    image = attachment(claim)
    with patch("django.core.files.storage.FileSystemStorage.delete", side_effect=OSError("busy")):
        result = cleanup_retention(apply=True, now=now)
    assert result["files_failed"] == 2
    assert FileDeletion.objects.filter(completed_at=None, attempts=1).count() == 2
    assert StorageLedger.objects.get(pk=1).used_bytes == 25
    cleanup_retention(apply=True, now=now)
    assert not storage_for("private").exists(image.image.name)
    assert StorageLedger.objects.get(pk=1).used_bytes == 0
    assert FileDeletion.objects.filter(completed_at=None).count() == 0


def test_command_defaults_to_preview_and_batches_are_repeatable(users):
    now = timezone.now()
    first = final_claim(users, now)
    second = final_claim(users, now)
    output = StringIO()
    call_command("cleanup_retention", stdout=output, batch_size=1)
    assert json.loads(output.getvalue())["apply"] is False
    assert not StorageLedger.objects.exists()
    cleanup_retention(apply=True, batch_size=1, now=now)
    assert Claim.objects.filter(pk__in=[first.pk, second.pk], evidence="").count() == 1
    cleanup_retention(apply=True, batch_size=1, now=now)
    assert Claim.objects.filter(pk__in=[first.pk, second.pk], evidence="").count() == 2


def test_large_audit_is_scrubbed_and_detached_in_repeatable_bounded_batches(users):
    now = timezone.now()
    claim = final_claim(users, now, days=30)
    logs = [history(users, claim) for _ in range(4)]
    cleanup_retention(apply=True, batch_size=1, now=now)
    assert Claim.objects.filter(pk=claim.pk).exists()
    assert ActionHistory.objects.filter(claim=claim).exclude(reason="").count() == 2
    for _ in range(5):
        cleanup_retention(apply=True, batch_size=1, now=now)
    assert not Claim.objects.filter(pk=claim.pk).exists()
    assert ActionHistory.objects.filter(pk__in=[log.pk for log in logs], claim_id=None).count() == 4
