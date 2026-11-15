from contextlib import contextmanager

from django.conf import settings
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.files.storage import FileSystemStorage
from django.db import transaction
from django.db.models import Q
from django.db.models.fields.files import FieldFile
from django.http import Http404
from django.shortcuts import get_object_or_404

from .confirmations import REQUIRED
from .forms import MAX_IMAGES_PER_CLAIM, MAX_IMAGES_PER_SUBMISSION, ReportForm, validate_image
from .managed_storage import capture_upload_files
from .models import ActionHistory, Claim, ClaimAttachment, Match, Notice, Report
from .permissions import (
    can_manage_report,
    can_read_evidence,
    claim_actions,
    is_service_staff,
    report_actions,
)
from .resource_guards import work_budget
from .storage_quota import cleanup_failed_uploads, queue_file_deletion, reserve_uploads

PUBLIC_REPORT_FIELDS = (
    "report_type", "item_name", "category", "description", "location", "event_date", "event_time", "photo",
)


def require_user(actor):
    if not actor or not actor.is_authenticated or not actor.is_active:
        raise PermissionDenied


def require_staff(actor):
    if not is_service_staff(actor):
        raise PermissionDenied


def require_action(actions, action, reason=""):
    if not isinstance(reason, str) or len(reason) > 2000:
        raise ValidationError({"reason": "Use at most 2000 characters."})
    selected = next((item for item in actions if item["key"] == action), None)
    if selected is None:
        raise ValueError("Action is not available in the current state")
    if selected["reason"] and not reason.strip():
        raise ValidationError({"reason": "A reason is required."})


def record(action, actor, report, previous="", result="", claim=None, reason=""):
    ActionHistory.objects.create(
        actor=actor, action=action, report=report, claim=claim,
        previous_state=previous, new_state=claim.status if claim else report.status,
        result=result, reason=reason,
    )


@contextmanager
def stored_files():
    files = []
    try:
        with capture_upload_files(files):
            yield files
    except Exception:
        cleanup_failed_uploads(files)
        raise


def set_report_data(report, data, *, validate_photo=True):
    unknown = set(data) - set(ReportForm.Meta.fields)
    if unknown:
        raise ValidationError({key: "Unknown field." for key in unknown})
    for key, value in data.items():
        if key == "photo":
            if validate_photo and value and not isinstance(value, FieldFile):
                validate_image(value)
            value = value or ""
        setattr(report, key, value)
    report.full_clean()


def create_report(actor, data):
    require_user(actor)
    if not data.get("event_time"):
        raise ValidationError({"event_time": "The event time is required."})
    report = Report(created_by=actor, status="pending")
    set_report_data(report, data)
    with stored_files(), transaction.atomic():
        new_photo = bool(report.photo and not report.photo._committed)
        if new_photo:
            reserve_uploads([report.photo.file])
        report.save()
        record("report_submitted", actor, report)
        return report


def update_report(actor, report_id, data):
    require_user(actor)
    preview = get_object_or_404(Report, pk=report_id)
    if not can_manage_report(actor, preview):
        raise PermissionDenied
    photo = data.get("photo")
    if photo and not isinstance(photo, FieldFile):
        validate_image(photo)
    with stored_files(), transaction.atomic():
        report = get_object_or_404(Report.objects.select_for_update(), pk=report_id)
        if not can_manage_report(actor, report):
            raise PermissionDenied
        require_action(report_actions(actor, report), "edit")
        previous = report.status
        before = {key: getattr(report, key) for key in PUBLIC_REPORT_FIELDS}
        old_photo = (report.photo.storage, report.photo.name) if report.photo else None
        set_report_data(report, data, validate_photo=False)
        public_changed = any(before[key] != getattr(report, key) for key in PUBLIC_REPORT_FIELDS)
        new_photo = bool(report.photo and not report.photo._committed)
        if new_photo:
            reserve_uploads([report.photo.file])
        if previous == "rejected" or (previous == "approved" and (public_changed or new_photo)):
            report.status = "pending"
        report.save()
        if old_photo and (not report.photo or old_photo[1] != report.photo.name):
            queue_file_deletion(*old_photo)
        record("report_edited", actor, report, previous, "Saved")
        return report


@transaction.atomic
def request_report_closure(actor, report_id, reason=""):
    require_user(actor)
    report = get_object_or_404(Report.objects.select_for_update(), pk=report_id)
    if not can_manage_report(actor, report):
        raise PermissionDenied
    require_action(report_actions(actor, report), "request_close", reason)
    if not reason.strip():
        raise ValidationError({"reason": "A closure reason is required."})
    previous = report.status
    report.status = "close_requested"
    report.review_reason = reason.strip()
    report.save(update_fields=["status", "review_reason", "updated_at"])
    record("close_requested", actor, report, previous, reason=reason.strip())
    return report


def validate_evidence(evidence, images, existing=0):
    if not isinstance(evidence, str) or not evidence.strip() or len(evidence) > 10000:
        raise ValidationError({"evidence": "Evidence is required, up to 10000 characters."})
    if len(images) > MAX_IMAGES_PER_SUBMISSION or existing + len(images) > MAX_IMAGES_PER_CLAIM:
        raise ValidationError({"images": "At most 3 images per submission and 5 per claim."})
    for image in images:
        validate_image(image)


def save_attachments(claim, images):
    reserve_uploads(images)
    for image in images:
        attachment = ClaimAttachment(claim=claim, image=image)
        attachment.save()


def create_claim(actor, report_id, data, images=()):
    require_user(actor)
    if set(data) - {"evidence"}:
        raise ValidationError("Only evidence may be supplied for a claim.")
    images = list(images)
    validate_evidence(data.get("evidence"), images)
    with stored_files(), transaction.atomic():
        report = get_object_or_404(Report.objects.select_for_update(), pk=report_id, report_type="found")
        require_action(report_actions(actor, report), "claim")
        claim = Claim.objects.create(
            report=report, claimant=actor, claimant_name=actor.get_full_name() or actor.username,
            claimant_email=actor.email, evidence=data["evidence"].strip(), status="pending",
        )
        save_attachments(claim, images)
        record("claim_submitted", actor, report, claim=claim)
        return claim


def locked_claim(claim_id):
    claim = get_object_or_404(Claim, pk=claim_id)
    Report.objects.select_for_update().get(pk=claim.report_id)
    return Claim.objects.select_for_update().get(pk=claim_id)


def resubmit_claim(actor, claim_id, evidence, images=()):
    require_user(actor)
    images = list(images)
    validate_evidence(evidence, images)
    with stored_files(), transaction.atomic():
        claim = locked_claim(claim_id)
        if claim.claimant_id != actor.pk:
            raise PermissionDenied
        require_action(claim_actions(actor, claim), "resubmit")
        if claim.attachments.count() + len(images) > MAX_IMAGES_PER_CLAIM:
            raise ValidationError({"images": "At most 5 images per claim."})
        claim.resubmit_evidence(evidence.strip(), actor=actor)
        save_attachments(claim, images)
        return claim


@transaction.atomic
def review_report(actor, report_id, action, reason=""):
    require_staff(actor)
    report = get_object_or_404(Report.objects.select_for_update(), pk=report_id)
    if action == "close" and report.claims.filter(status="handover_in_progress").exists():
        raise ValueError("Cannot close: a handover is in progress for this report")
    require_action(report_actions(actor, report), action, reason)
    report.review(actor, action, reason)
    return report


@transaction.atomic
def act_on_claim(actor, claim_id, action, reason="", confirmations=None):
    require_user(actor)
    claim = locked_claim(claim_id)
    if action == "withdraw":
        if claim.claimant_id != actor.pk:
            raise PermissionDenied
    else:
        require_staff(actor)
    require_action(claim_actions(actor, claim), action, reason)
    if action not in REQUIRED and confirmations:
        raise ValidationError({"confirmations": "This action does not accept confirmations."})
    if action in ("resolve_keep", "resolve_reject"):
        return claim.resolve_dispute(actor, action == "resolve_keep", reason, confirmations=confirmations)
    if action in ("approve", "start_dispute", "interrupt_handover"):
        return getattr(claim, action)(actor, reason, confirmations=confirmations)
    if action in ("start_handover", "complete_handover"):
        return getattr(claim, action)(actor, confirmations=confirmations)
    if action in ("reject", "request_evidence"):
        return getattr(claim, action)(actor, reason)
    if action == "withdraw":
        return claim.withdraw(actor, reason)
    raise ValueError("Unknown action")


def mark_notice_read(actor, notice_id):
    require_user(actor)
    notice = get_object_or_404(Notice, pk=notice_id, user=actor)
    notice.is_read = True
    notice.save(update_fields=["is_read"])
    return notice


def open_claim_attachment(actor, attachment_id):
    require_user(actor)
    attachment = get_object_or_404(ClaimAttachment.objects.select_related("claim"), pk=attachment_id)
    if not can_read_evidence(actor, attachment.claim):
        raise PermissionDenied
    try:
        return attachment.image.open("rb")
    except FileNotFoundError:
        try:
            return FileSystemStorage(location=settings.MEDIA_ROOT).open(attachment.image.name)
        except FileNotFoundError:
            raise Http404 from None


def report_queryset(actor, filters):
    for field, maximum in {"q": 200, "category": 100, "location": 200}.items():
        value = filters.get(field)
        if value is not None and (not isinstance(value, str) or len(value) > maximum):
            raise ValidationError({field: f"Use at most {maximum} characters."})
    reports = Report.objects.all()
    if not is_service_staff(actor):
        reports = reports.filter(status="approved")
    if filters.get("q"):
        value = filters["q"]
        reports = reports.filter(Q(item_name__icontains=value) | Q(description__icontains=value)
                                 | Q(category__icontains=value))
    for key, lookup in {
        "report_type": "report_type", "category": "category__icontains",
        "location": "location__icontains", "date_from": "event_date__gte",
        "date_to": "event_date__lte", "status": "status",
    }.items():
        if filters.get(key):
            reports = reports.filter(**{lookup: filters[key]})
    return reports


def public_matches():
    return Match.objects.filter(
        lost_report__status="approved", found_report__status="approved",
    ).select_related("lost_report", "found_report").order_by("-score", "pk")


def match_score(first, second):
    score = 0.0
    reasons = []
    if first.category and first.category.lower() == second.category.lower():
        score += 0.5
        reasons.append("same category")
    words = [{word.lower() for word in item.item_name.split() if len(word) > 2}
             for item in (first, second)]
    shared = words[0] & words[1]
    if shared:
        score += 0.3 * min(len(shared), 3)
        reasons.append(f"keywords: {', '.join(sorted(shared))}")
    return min(round(score, 2), 1.0), "; ".join(reasons)


def regenerate_matches(actor):
    require_staff(actor)
    maximum = getattr(settings, "MATCH_MAX_PAIRS", 2500)
    with work_budget("match-generation") as check_budget, transaction.atomic():
        lost = Report.objects.filter(report_type="lost", status="approved")
        found = Report.objects.filter(report_type="found", status="approved")
        if lost.count() * found.count() > maximum:
            raise ValidationError("Too many matching pairs for one request.")
        check_budget()
        lost = list(lost[:maximum + 1])
        found = list(found[:maximum + 1])
        if len(lost) * len(found) > maximum:
            raise ValidationError("Too many matching pairs for one request.")
        check_budget()
        generated = 0
        for first in lost:
            for second in found:
                check_budget()
                score, reason = match_score(first, second)
                pair = {"lost_report": first, "found_report": second}
                if score > 0:
                    Match.objects.update_or_create(**pair, defaults={"score": score, "reason": reason})
                    generated += 1
                else:
                    Match.objects.filter(**pair, status="suggested").delete()
                check_budget()
        check_budget()
        return generated
