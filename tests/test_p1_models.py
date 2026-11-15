from datetime import time

import pytest
from django.contrib import admin
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import IntegrityError, transaction
from django.db.models.deletion import ProtectedError
from django.test import RequestFactory
from django.utils import timezone

from apps.reports.admin import CategoryAdmin, ClaimAdmin
from apps.reports.forms import ReportForm, SearchForm, validate_image
from apps.reports.models import ActionHistory, Category, Claim, Report, ResourceLease
from apps.reports.permissions import can_read_evidence, can_resubmit, claim_actions, report_actions
from apps.reports.serializers import ActionInput, ClaimInput, ReportInput

from .test_workflow import make_claim, make_png_bytes, make_report

pytestmark = pytest.mark.django_db


def submission(**changes):
    return {
        "report_type": "found", "item_name": "Phone", "category": "Electronics",
        "description": "Blue case", "location": "Library", "event_date": "2026-10-01",
        "event_time": "14:30", "contact_name": "Owner", "contact_email": "owner@example.com",
        **changes,
    }


def test_new_business_submissions_require_category_and_time_but_storage_preserves_unknown(users):
    legacy = make_report(users["owner"], event_time=None)
    assert legacy.event_time is None
    for field, invalid in (("category", " "), ("event_time", "")):
        data = submission(**{field: invalid})
        form = ReportForm(data)
        serializer = ReportInput(data=data)
        assert not form.is_valid() and field in form.errors
        assert not serializer.is_valid() and field in serializer.errors
    report = Report(**submission(event_time=None), created_by=users["owner"])
    with pytest.raises(ValidationError) as error:
        report.full_clean()
    assert "event_time" in error.value.message_dict
    assert ReportForm(submission()).is_valid()
    valid = ReportInput(data=submission())
    assert valid.is_valid(), valid.errors
    assert valid.validated_data["event_time"] == time(14, 30)


def test_category_catalog_lists_active_names_and_preserves_legacy_choice(users):
    Category.objects.get_or_create(name="Active category")
    Category.objects.update_or_create(name="Retired category", defaults={"is_active": False})
    report = make_report(users["owner"], category="Historical custom category")
    choices = dict(ReportForm(instance=report).fields["category"].widget.choices)
    assert "Active category" in choices
    assert "Retired category" not in choices
    assert "Historical custom category" in choices
    report.full_clean()  # Existing nullable event time and category text remain valid.


def test_system_admin_manages_categories_without_receiving_evidence_access(users):
    request = RequestFactory().get("/admin/reports/category/")
    request.user = users["admin"]
    category_admin = CategoryAdmin(Category, admin.site)
    assert category_admin.has_module_permission(request)
    assert category_admin.has_view_permission(request)
    assert category_admin.has_add_permission(request)
    assert category_admin.has_change_permission(request)
    assert category_admin.has_delete_permission(request)
    claim = make_claim(make_report(users["owner"]), users["claimant"])
    assert not can_read_evidence(users["admin"], claim)
    assert not ClaimAdmin(Claim, admin.site).has_view_permission(request, claim)


def test_claimant_is_required_and_cannot_be_deleted(users):
    claim = make_claim(make_report(users["owner"]), users["claimant"])
    with pytest.raises(ProtectedError):
        users["claimant"].delete()
    with pytest.raises(IntegrityError), transaction.atomic():
        Claim.objects.filter(pk=claim.pk).update(claimant=None)
    claim.refresh_from_db()
    assert claim.claimant_id == users["claimant"].pk


def test_disputed_evidence_uses_locked_state_and_retains_exclusive_reservation(users):
    report = make_report(users["owner"])
    claim = make_claim(report, users["claimant"], status="awaiting_evidence")
    stale = Claim.objects.get(pk=claim.pk)
    Claim.objects.filter(pk=claim.pk).update(status="under_dispute")
    claim.refresh_from_db()
    assert can_resubmit(users["claimant"], claim)
    assert "resubmit" in {action["key"] for action in claim_actions(users["claimant"], claim)}
    stale.resubmit_evidence("Additional unique identifying details", actor=users["claimant"])
    assert stale.status == "under_dispute" and stale.holds_reservation
    assert stale.evidence == "Additional unique identifying details"
    history = stale.history.get()
    assert (history.previous_state, history.new_state) == ("under_dispute", "under_dispute")
    with pytest.raises(IntegrityError), transaction.atomic():
        make_claim(report, users["other"], status="awaiting_handover")


def test_evidence_resubmission_checks_locked_actor_and_state(users):
    claim = make_claim(make_report(users["owner"]), users["claimant"], status="under_dispute")
    with pytest.raises(PermissionDenied):
        claim.resubmit_evidence("New details", actor=users["other"])
    Claim.objects.filter(pk=claim.pk).update(status="handover_in_progress")
    with pytest.raises(ValueError):
        claim.resubmit_evidence("New details", actor=users["claimant"])
    assert not ActionHistory.objects.exists()


@pytest.mark.parametrize("status,method,args", [
    ("pending", "request_evidence", (" ",)),
    ("awaiting_handover", "start_dispute", (" ",)),
    ("under_dispute", "resolve_dispute", (True, " ")),
    ("under_dispute", "resolve_dispute", (False, " ")),
    ("pending", "invalidate", (" ",)),
])
def test_decision_methods_require_reasons_in_the_model(users, status, method, args):
    claim = make_claim(make_report(users["owner"]), users["claimant"], status=status)
    with pytest.raises(ValueError, match="reason"):
        getattr(claim, method)(users["service"], *args)
    claim.refresh_from_db()
    assert claim.status == status and claim.finalized_at is None
    assert not ActionHistory.objects.exists()


def test_withdrawal_and_report_closure_require_reasons_and_record_final_dates(users):
    report = make_report(users["owner"])
    claim = make_claim(report, users["claimant"])
    assert next(a for a in claim_actions(users["claimant"], claim) if a["key"] == "withdraw")["reason"]
    assert next(a for a in report_actions(users["owner"], report) if a["key"] == "request_close")["reason"]
    with pytest.raises(ValueError):
        claim.withdraw(users["claimant"])
    with pytest.raises(ValueError):
        report.review(users["service"], "close")
    before = timezone.now()
    claim.withdraw(users["claimant"], "I found my own item")
    report.review(users["service"], "close", "No further search is needed")
    assert before <= claim.finalized_at <= timezone.now()
    assert before <= report.closed_at <= timezone.now()
    assert claim.evidence_purged_at is None


def test_completed_handover_sets_claim_and_report_final_dates(users):
    report = make_report(users["owner"])
    claim = make_claim(report, users["claimant"], status="handover_in_progress")
    claim.complete_handover(users["service"], confirmations={"transfer_confirmed": True})
    report.refresh_from_db()
    assert claim.status == "completed" and report.status == "closed"
    assert claim.handover_completed_at is not None and claim.finalized_at is not None
    assert report.closed_at == claim.finalized_at


def test_audit_references_exist_for_regular_and_bulk_insert_and_remain_immutable(users):
    report = make_report(users["owner"])
    claim = make_claim(report, users["claimant"])
    history = ActionHistory.objects.create(actor=users["service"], action="reviewed", claim=claim)
    assert (history.report_reference, history.claim_reference) == (report.reference, claim.reference)
    bulk = ActionHistory(action="submitted", report=report, claim=claim)
    ActionHistory.objects.bulk_create([bulk])
    bulk.refresh_from_db()
    assert (bulk.report_reference, bulk.claim_reference) == (report.reference, claim.reference)
    for action in (
        lambda: history.save(), lambda: history.delete(),
        lambda: ActionHistory.objects.filter(pk=history.pk).update(reason="Changed"),
        lambda: ActionHistory.objects.filter(pk=history.pk).delete(),
    ):
        with pytest.raises(ValueError, match="append-only"):
            action()
    with pytest.raises(ProtectedError):
        report.delete()


def test_resource_lease_keys_are_unique():
    ResourceLease.objects.create(key="match-generation", token="a" * 64, expires_at=timezone.now())
    with pytest.raises(IntegrityError), transaction.atomic():
        ResourceLease.objects.create(key="match-generation", token="b" * 64, expires_at=timezone.now())


def test_input_lengths_and_decoded_image_dimensions_are_bounded(settings):
    report = ReportInput(data=submission(description="x" * 10001))
    assert not report.is_valid() and "description" in report.errors
    claim = ClaimInput(data={"evidence": "x" * 10001})
    assert not claim.is_valid() and "evidence" in claim.errors
    reason = ActionInput(data={"action": "reject", "reason": "x" * 2001})
    assert not reason.is_valid() and "reason" in reason.errors
    search = SearchForm({"q": "x" * 201, "category": "x" * 101, "location": "x" * 201})
    assert not search.is_valid() and set(search.errors) == {"q", "category", "location"}
    settings.MAX_IMAGE_PIXELS = 3
    image = SimpleUploadedFile("image.png", make_png_bytes(), content_type="image/png")
    with pytest.raises(ValidationError, match="pixel"):
        validate_image(image)
    assert image.tell() == 0
