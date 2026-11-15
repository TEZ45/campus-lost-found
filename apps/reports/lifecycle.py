"""Internal retention maintenance, called only by the explicit management command.

Ordinary audit writes remain append-only. The narrowly scoped base QuerySet
updates below implement the retention exception: erase freeform reason/result
at 15 days because either can contain copied evidence, detach stable references
at 30 days, and remove eligible history with its notices at 45 days. Decision
action, actor, time, states and confirmations remain intact until history expiry.
Missing legacy final timestamps are reported and never inferred from updated_at.
"""

from datetime import timedelta

from django.db import models, transaction
from django.db.models import CharField, Exists, OuterRef, Q, Value
from django.db.models.functions import Cast, Concat
from django.utils import timezone

from .lifecycle_models import FileDeletion
from .models import ActionHistory, Claim, ClaimAttachment, Notice, NotificationEvent, Report
from .storage_quota import (
    process_file_deletions,
    queue_file_deletion,
    storage_for,
)

EVIDENCE_DAYS = 15
BUSINESS_DAYS = 30
HISTORY_DAYS = 45
UNCERTAIN_STATES = ("under_dispute", "handover_in_progress")


def _history_update(ids, **values):
    # This is deliberately internal: no manager/model API exposes mutable audit.
    if ids:
        models.QuerySet(model=ActionHistory).filter(pk__in=ids).update(**values)


def _scrub_history(queryset, batch_size):
    freeform = queryset.exclude(reason="", result="")
    ids = list(freeform.order_by("pk").values_list("pk", flat=True)[:batch_size])
    _history_update(ids, reason="", result="")
    return not freeform.exists()


def _uncertain(report_id):
    return Claim.objects.filter(report_id=report_id, status__in=UNCERTAIN_STATES).exists()


def _scrub_notices(link, batch_size):
    bodies = Notice.objects.filter(link=link).exclude(body="")
    ids = list(bodies.order_by("pk").values_list("pk", flat=True)[:batch_size])
    Notice.objects.filter(pk__in=ids).update(body="")
    return not bodies.exists()


def _purge_evidence(claim, now, batch_size):
    attachments = list(claim.attachments.order_by("pk")[:batch_size])
    for attachment in attachments:
        # Pre-migration evidence could still have a public copy or fallback.
        queue_file_deletion(storage_for("private"), attachment.image.name)
        queue_file_deletion(storage_for("public"), attachment.image.name)
    ClaimAttachment.objects.filter(pk__in=[attachment.pk for attachment in attachments]).delete()
    claim.evidence = ""
    claim.current_reason = ""
    notices_scrubbed = _scrub_notices(f"/claim/{claim.pk}/", batch_size)
    scrubbed = _scrub_history(ActionHistory.objects.filter(claim_id=claim.pk), batch_size)
    complete = scrubbed and notices_scrubbed and not claim.attachments.exists()
    claim.evidence_purged_at = now if complete else None
    claim.save(update_fields=["evidence", "evidence_purged_at", "current_reason"])
    return complete


def cleanup_retention(apply=False, batch_size=100, now=None):
    """Run one bounded, repeatable pass. Dry run performs only reads."""
    if not 1 <= batch_size <= 1000:
        raise ValueError("Batch size must be between 1 and 1000")
    now = now or timezone.now()
    evidence_cutoff = now - timedelta(days=EVIDENCE_DAYS)
    business_cutoff = now - timedelta(days=BUSINESS_DAYS)
    history_cutoff = now - timedelta(days=HISTORY_DAYS)
    result = {
        "apply": apply, "evidence": 0, "claims": 0, "reports": 0, "history": 0,
        "legacy_claims_missing_finalized_at": Claim.objects.filter(
            status__in=Claim.FINAL_STATES, finalized_at=None,
        ).count(),
        "legacy_reports_missing_closed_at": Report.objects.filter(
            status="closed", closed_at=None,
        ).count(),
        "uncertain_skipped": 0,
    }

    evidence_ids = list(Claim.objects.filter(
        status__in=Claim.FINAL_STATES, finalized_at__lte=evidence_cutoff,
        evidence_purged_at=None,
    ).filter(~Exists(Claim.objects.filter(
        report_id=OuterRef("report_id"), status__in=UNCERTAIN_STATES,
    ))).order_by("finalized_at", "pk").values_list("pk", flat=True)[:batch_size])
    for pk in evidence_ids:
        with transaction.atomic():
            candidate = Claim.objects.filter(pk=pk).first()
            if candidate is None:
                continue
            Report.objects.select_for_update().get(pk=candidate.report_id)
            claim = Claim.objects.select_for_update().get(pk=pk)
            if (claim.status not in Claim.FINAL_STATES or not claim.finalized_at
                    or claim.finalized_at > evidence_cutoff or claim.evidence_purged_at):
                continue
            if _uncertain(claim.report_id):
                result["uncertain_skipped"] += 1
                continue
            result["evidence"] += 1
            if apply:
                _purge_evidence(claim, now, batch_size)

    # Closed-report freeform audit may also contain evidence copied by staff.
    report_notice_bodies = Notice.objects.filter(link=Concat(
        Value("/report/"), Cast(OuterRef("pk"), output_field=CharField()), Value("/"),
    )).exclude(body="")
    report_scrub_ids = list(Report.objects.filter(
        status="closed", closed_at__lte=evidence_cutoff,
    ).filter(Q(review_reason__gt="") | Q(Exists(report_notice_bodies)) | Q(Exists(ActionHistory.objects.filter(
        report_id=OuterRef("pk"), claim_id=None,
    ).exclude(reason="", result="")))).filter(~Exists(Claim.objects.filter(
        report_id=OuterRef("pk"), status__in=UNCERTAIN_STATES,
    ))).order_by("closed_at", "pk")
        .values_list("pk", flat=True)[:batch_size])
    if apply:
        for pk in report_scrub_ids:
            with transaction.atomic():
                report = Report.objects.select_for_update().get(pk=pk)
                if (report.status == "closed" and report.closed_at
                        and report.closed_at <= evidence_cutoff and not _uncertain(pk)):
                    report.review_reason = ""
                    report.save(update_fields=["review_reason"])
                    _scrub_notices(f"/report/{pk}/", batch_size)
                    _scrub_history(
                        ActionHistory.objects.filter(report_id=pk, claim_id=None), batch_size,
                    )

    claim_ids = list(Claim.objects.filter(
        status__in=Claim.FINAL_STATES, finalized_at__lte=business_cutoff,
    ).filter(~Exists(Claim.objects.filter(
        report_id=OuterRef("report_id"), status__in=UNCERTAIN_STATES,
    ))).order_by("finalized_at", "pk").values_list("pk", flat=True)[:batch_size])
    for pk in claim_ids:
        with transaction.atomic():
            candidate = Claim.objects.filter(pk=pk).first()
            if candidate is None:
                continue
            Report.objects.select_for_update().get(pk=candidate.report_id)
            claim = Claim.objects.select_for_update().get(pk=pk)
            if (claim.status not in Claim.FINAL_STATES or not claim.finalized_at
                    or claim.finalized_at > business_cutoff or _uncertain(claim.report_id)):
                continue
            if apply:
                if not claim.evidence_purged_at and not _purge_evidence(claim, now, batch_size):
                    continue
                ids = list(ActionHistory.objects.filter(claim_id=pk).order_by("pk")
                           .values_list("pk", flat=True)[:batch_size])
                _history_update(ids, claim_id=None, claim_reference=claim.reference)
                if ActionHistory.objects.filter(claim_id=pk).exists():
                    continue
                claim.delete()
            result["claims"] += 1

    remaining_claims = Claim.objects.filter(report_id=OuterRef("pk"))
    if not apply:
        remaining_claims = remaining_claims.exclude(pk__in=claim_ids)
    report_ids = list(Report.objects.filter(
        status="closed", closed_at__lte=business_cutoff,
    ).filter(~Exists(remaining_claims)).order_by("closed_at", "pk")
        .values_list("pk", flat=True)[:batch_size])
    for pk in report_ids:
        with transaction.atomic():
            report = Report.objects.select_for_update().get(pk=pk)
            if (report.status != "closed" or not report.closed_at
                    or report.closed_at > business_cutoff):
                continue
            children = report.claims.all()
            # Dry run simulates final-claim deletion without cascading active/new cases.
            if apply:
                blocked = children.exists()
            else:
                blocked = children.exclude(pk__in=claim_ids).exists() or _uncertain(pk)
            if blocked:
                continue
            if apply:
                ids = list(ActionHistory.objects.filter(report_id=pk).order_by("pk")
                           .values_list("pk", flat=True)[:batch_size])
                _history_update(ids, report_id=None, report_reference=report.reference)
                if ActionHistory.objects.filter(report_id=pk).exists():
                    continue
                queue_file_deletion(report.photo.storage, report.photo.name)
                report.delete()
            result["reports"] += 1

    # History has its own 45-day clock. Deleting an expired audit record never
    # cascades into business objects, whose handover facts remain on the models.
    history_ids = list(ActionHistory.objects.filter(
        created_at__lte=history_cutoff,
    ).order_by("created_at", "pk").values_list("pk", flat=True)[:batch_size])
    result["history"] = len(history_ids)
    if apply:
        result["history"] = 0
        for pk in history_ids:
            with transaction.atomic():
                candidate = ActionHistory.objects.filter(pk=pk).first()
                if candidate is None:
                    continue
                report_id = candidate.report_id
                if not report_id and candidate.claim_id:
                    report_id = Claim.objects.filter(pk=candidate.claim_id).values_list(
                        "report_id", flat=True,
                    ).first()
                if report_id:
                    Report.objects.select_for_update().filter(pk=report_id).first()
                if candidate.claim_id:
                    Claim.objects.select_for_update().filter(pk=candidate.claim_id).first()
                log = ActionHistory.objects.select_for_update().filter(
                    pk=pk, created_at__lte=history_cutoff,
                ).first()
                if log is None:
                    continue
                event_ids = list(NotificationEvent.objects.select_for_update()
                                 .filter(history_id=pk).order_by("pk")
                                 .values_list("pk", flat=True)[:batch_size])
                Notice.objects.filter(event_id__in=event_ids).delete()
                NotificationEvent.objects.filter(pk__in=event_ids).delete()
                if NotificationEvent.objects.filter(history_id=pk).exists():
                    continue
                models.QuerySet(model=ActionHistory).filter(pk=pk).delete()
                result["history"] += 1
    if apply:
        result.update(process_file_deletions(batch_size=batch_size))
    completed_jobs = list(FileDeletion.objects.filter(completed_at__lte=history_cutoff)
                          .order_by("completed_at", "pk").values_list("pk", flat=True)[:batch_size])
    result["deletion_jobs"] = len(completed_jobs)
    if apply:
        FileDeletion.objects.filter(pk__in=completed_jobs, completed_at__lte=history_cutoff).delete()
    return result
