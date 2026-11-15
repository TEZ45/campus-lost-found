from datetime import timedelta
from importlib import import_module
from types import SimpleNamespace

import pytest
from django.apps import apps
from django.db import connection, models
from django.utils import timezone

from apps.reports.lifecycle import cleanup_retention
from apps.reports.models import ActionHistory, Claim, Report

from .test_workflow import make_claim, make_report

pytestmark = pytest.mark.django_db


def backfill_reasons():
    migration = import_module("apps.reports.migrations.0009_current_decision_reasons")
    migration.backfill_current_reasons(apps, SimpleNamespace(connection=connection))


@pytest.mark.parametrize("disputed", [False, True])
def test_current_evidence_instructions_survive_resubmission_and_audit_expiry(users, disputed):
    now = timezone.now()
    report = make_report(users["owner"])
    claim = make_claim(
        report, users["claimant"], status="awaiting_handover" if disputed else "pending",
    )
    reason = "Please show the serial number engraved beneath the case."
    if disputed:
        claim.start_dispute(users["service"], reason, confirmations={"custody_confirmed": True})
    else:
        claim.request_evidence(users["service"], reason)
    Claim.objects.filter(pk=claim.pk).update(current_reason="")
    backfill_reasons()
    claim.refresh_from_db()
    assert claim.current_reason == reason
    claim.resubmit_evidence("The number is ABC-123", actor=users["claimant"])
    assert claim.current_reason == reason
    models.QuerySet(model=ActionHistory).update(created_at=now - timedelta(days=46))
    cleanup_retention(apply=True, now=now)
    claim.refresh_from_db()
    assert not ActionHistory.objects.exists()
    assert claim.current_reason == reason
    assert claim.status == ("under_dispute" if disputed else "pending")
    if disputed:
        claim.resolve_dispute(
            users["service"], True, "The identifying number was verified.",
            confirmations={"custody_confirmed": True},
        )
        assert claim.current_reason == "The identifying number was verified."
    else:
        claim.approve(
            users["service"], confirmations={"evidence_reviewed": True, "custody_confirmed": True},
        )
        assert claim.current_reason == ""


def test_final_reason_and_audit_text_are_both_erased_after_fifteen_days(users):
    now = timezone.now()
    report = make_report(users["owner"])
    claim = make_claim(report, users["claimant"])
    report.review(users["service"], "close", "Owner supplied private identifying information.")
    claim.refresh_from_db()
    assert claim.current_reason and report.review_reason
    Claim.objects.filter(pk=claim.pk).update(finalized_at=now - timedelta(days=16))
    Report.objects.filter(pk=report.pk).update(closed_at=now - timedelta(days=16))
    saved_claim_reason, saved_report_reason = claim.current_reason, report.review_reason
    Claim.objects.filter(pk=claim.pk).update(current_reason="")
    Report.objects.filter(pk=report.pk).update(review_reason="")
    backfill_reasons()
    claim.refresh_from_db()
    report.refresh_from_db()
    assert claim.current_reason == "" and report.review_reason == ""
    Claim.objects.filter(pk=claim.pk).update(current_reason=saved_claim_reason)
    Report.objects.filter(pk=report.pk).update(review_reason=saved_report_reason)
    cleanup_retention(apply=True, now=now)
    claim.refresh_from_db()
    report.refresh_from_db()
    assert claim.current_reason == "" and report.review_reason == ""
    assert claim.evidence == "" and claim.evidence_purged_at is not None
    assert ActionHistory.objects.exists()
    assert not ActionHistory.objects.exclude(reason="", result="").exists()


def test_current_report_rejection_reason_survives_audit_expiry_and_clears_on_approval(users):
    now = timezone.now()
    report = make_report(users["owner"], status="pending")
    reason = "Give a more precise location before this report can be published."
    report.review(users["service"], "reject", reason)
    Report.objects.filter(pk=report.pk).update(review_reason="")
    backfill_reasons()
    report.refresh_from_db()
    assert report.review_reason == reason
    models.QuerySet(model=ActionHistory).update(created_at=now - timedelta(days=46))
    cleanup_retention(apply=True, now=now)
    report.refresh_from_db()
    assert report.status == "rejected" and report.review_reason == reason
    assert not ActionHistory.objects.exists()
    # An owner correction re-enters review; staff approval replaces the old decision.
    Report.objects.filter(pk=report.pk).update(status="pending")
    report.review(users["service"], "approve")
    assert report.review_reason == ""
