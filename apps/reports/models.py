from django.contrib.auth.models import User
from django.core.exceptions import PermissionDenied
from django.db import models, transaction
from django.utils import timezone

from .permissions import is_service_staff
from .storage import PrivateEvidenceStorage


def _staff_actions(instance):
    return [
        {"key": key, "label": instance.ACTION_LABELS[key],
         "reason": key in instance.REASON_REQUIRED}
        for key in instance.STAFF_ACTIONS.get(instance.status, ())
    ]


class UserProfile(models.Model):
    """Business authorisation separate from Django admin/superuser status (URS SEC-1.3).
    Being a system administrator alone does NOT grant claim-evidence access;
    lost-property operations require this explicit service-staff authorisation."""

    user = models.OneToOneField(User, on_delete=models.CASCADE, related_name="profile")
    is_service_staff = models.BooleanField(
        default=False,
        help_text="Authorises lost-property staff operations (moderation, evidence, handover).",
    )

    def __str__(self):
        return f"{self.user.username}: {'service staff' if self.is_service_staff else 'user'}"


class Report(models.Model):
    """A lost or found item report (URS REQ-1.2)."""

    REPORT_TYPES = [
        ("lost", "Lost Item"),
        ("found", "Found Item"),
    ]
    STATUS_CHOICES = [
        ("pending", "Pending Review"),
        ("approved", "Approved / Public"),
        ("rejected", "Rejected"),
        ("close_requested", "Closure Requested"),
        ("closed", "Closed / Resolved"),
    ]
    STAFF_ACTIONS = {
        "pending": ("approve", "reject"),
        "close_requested": ("close",),
    }
    ACTION_LABELS = {
        "approve": "Approve and publish",
        "reject": "Reject",
        "close": "Approve closure",
    }
    REASON_REQUIRED = {"reject"}

    report_type = models.CharField(max_length=10, choices=REPORT_TYPES)
    item_name = models.CharField(max_length=200)
    category = models.CharField(max_length=100, blank=True, default="")
    description = models.TextField(blank=True)
    location = models.CharField(max_length=200)
    event_date = models.DateField(help_text="Date the item was lost or found")
    contact_name = models.CharField(max_length=100)
    contact_email = models.EmailField(blank=True)
    contact_phone = models.CharField(max_length=50, blank=True)
    photo = models.ImageField(upload_to="reports/", blank=True, null=True)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default="pending")
    created_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"[{self.get_report_type_display()}] {self.item_name} ({self.get_status_display()})"

    def is_public(self):
        return self.status == "approved"

    @property
    def staff_actions(self):
        return _staff_actions(self)

    @property
    def reference(self):
        return f"RPT-{self.pk:06d}"

    def review(self, staff, action, reason=""):
        if not is_service_staff(staff):
            raise PermissionDenied
        reason = reason.strip()
        with transaction.atomic():
            report = Report.objects.select_for_update().get(pk=self.pk)
            previous = report.status
            if action in ("approve", "reject"):
                if previous != "pending":
                    raise ValueError("Only pending reports may be approved or rejected")
                if action == "reject" and not reason:
                    raise ValueError("A rejection reason is required")
                report.status = "approved" if action == "approve" else "rejected"
            elif action == "close":
                if previous == "closed":
                    raise ValueError("This report is already closed")
                if report.claims.filter(status="handover_in_progress").exists():
                    raise ValueError("Cannot close: a handover is in progress for this report")
                for claim in report.claims.filter(status__in=Claim.BEFORE_HANDOVER):
                    claim.invalidate(staff, "Report closed by staff")
                report.status = "closed"
            else:
                raise ValueError("Unknown action")
            report.save(update_fields=["status", "updated_at"])
            ActionHistory.objects.create(
                actor=staff, action=f"report_{report.status}", report=report,
                previous_state=previous, new_state=report.status, reason=reason,
            )
            if report.created_by_id:
                Notice.objects.create(
                    user_id=report.created_by_id, title=f"Report {report.reference} {report.status}",
                    body=reason or f"Your report is now {report.status}.",
                    link=f"/report/{report.pk}/",
                )
        self.refresh_from_db()


class Claim(models.Model):
    """A claim for a found item, implementing the URS claim state model.

    States (URS 6.4 / Figure 2):
      pending -> awaiting_evidence -> pending
      pending -> awaiting_handover           (approval, gains reservation)
      awaiting_handover <-> under_dispute    (reservation retained)
      awaiting_handover -> handover_in_progress -> completed | awaiting_handover
      pending / awaiting_evidence / awaiting_handover / under_dispute
                 -> withdrawn | invalidated
      rejected / withdrawn / invalidated / completed are final.
    """

    STATUS_CHOICES = [
        ("pending", "Pending Review"),
        ("awaiting_evidence", "Awaiting Evidence"),
        ("awaiting_handover", "Awaiting Handover"),
        ("under_dispute", "Under Dispute Review"),
        ("handover_in_progress", "Handover in Progress"),
        ("completed", "Completed"),
        ("rejected", "Rejected"),
        ("withdrawn", "Withdrawn"),
        ("invalidated", "Invalidated"),
    ]
    # Claimant action allowed from these states (withdrawal, invalidation).
    BEFORE_HANDOVER = ["pending", "awaiting_evidence", "awaiting_handover", "under_dispute"]
    # States that hold the exclusive reservation (DOM-3).
    RESERVED_STATES = ["awaiting_handover", "under_dispute", "handover_in_progress"]
    FINAL_STATES = ["completed", "rejected", "withdrawn", "invalidated"]
    STAFF_ACTIONS = {
        "pending": ("approve", "reject", "request_evidence"),
        "awaiting_evidence": ("reject",),
        "awaiting_handover": ("start_handover", "start_dispute"),
        "under_dispute": ("resolve_keep", "resolve_reject"),
        "handover_in_progress": ("complete_handover", "interrupt_handover"),
    }
    ACTION_LABELS = {
        "approve": "Approve",
        "reject": "Reject",
        "request_evidence": "Request evidence",
        "start_handover": "Start handover",
        "start_dispute": "Start dispute review",
        "resolve_keep": "Resolve - keep approval",
        "resolve_reject": "Resolve - reject",
        "complete_handover": "Complete handover",
        "interrupt_handover": "Interrupt (safe)",
    }
    REASON_REQUIRED = {"reject", "resolve_reject", "interrupt_handover"}

    report = models.ForeignKey(Report, on_delete=models.CASCADE, related_name="claims")
    claimant = models.ForeignKey(
        User, on_delete=models.CASCADE, null=True, blank=True, related_name="claims"
    )
    claimant_name = models.CharField(max_length=100)
    claimant_email = models.EmailField()
    claimant_phone = models.CharField(max_length=50, blank=True)
    evidence = models.TextField(help_text="Description proving ownership")
    status = models.CharField(max_length=25, choices=STATUS_CHOICES, default="pending")
    reviewed_by = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True, blank=True, related_name="reviewed_claims"
    )
    reviewed_at = models.DateTimeField(null=True, blank=True)
    handover_started_by = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True, blank=True, related_name="handovers_started"
    )
    handover_started_at = models.DateTimeField(null=True, blank=True)
    handover_completed_by = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True, blank=True, related_name="handovers_completed"
    )
    handover_completed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["report"],
                condition=models.Q(status__in=[
                    "awaiting_handover", "under_dispute", "handover_in_progress",
                ]),
                name="one_reserved_claim_per_report",
            ),
        ]

    def __str__(self):
        return f"Claim for {self.report.item_name} by {self.claimant_name} ({self.get_status_display()})"

    @property
    def reference(self):
        return f"CLM-{self.pk:06d}"

    @property
    def holds_reservation(self):
        return self.status in self.RESERVED_STATES

    @property
    def staff_actions(self):
        return _staff_actions(self)

    def reservation_conflict_exists(self):
        """DOM-3: at most one reserved claim across the reserved states."""
        return (
            self.report.claims.filter(status__in=self.RESERVED_STATES)
            .exclude(pk=self.pk)
            .exists()
        )

    def _apply(self, new_status, actor, action, allowed_from, reason="", result="", extras=None):
        with transaction.atomic():
            # All claim and report writes take the report lock first.
            report = Report.objects.select_for_update().get(pk=self.report_id)
            locked = Claim.objects.select_for_update().get(pk=self.pk)
            if action in ("withdrawn", "evidence_resubmitted"):
                if not actor or not actor.is_active or locked.claimant_id != actor.pk:
                    raise PermissionDenied
            elif not is_service_staff(actor):
                raise PermissionDenied
            if locked.status not in allowed_from:
                raise ValueError(f"{action} not allowed from {locked.status}")
            if action in ("approved", "handover_started") and (
                report.status != "approved" or report.report_type != "found"
            ):
                raise ValueError("Report must be approved and open for this action")
            if new_status in self.RESERVED_STATES and locked.reservation_conflict_exists():
                raise ValueError("Another claim already holds the reservation")
            old_status = locked.status
            locked.status = new_status
            if actor is not None:
                locked.reviewed_by = actor
                locked.reviewed_at = timezone.now()
            for field, value in (extras or {}).items():
                setattr(locked, field, value)
            locked.save(update_fields=["status", "reviewed_by", "reviewed_at",
                                       "updated_at"] + list((extras or {}).keys()))
            if new_status == "completed":
                for other in report.claims.filter(status__in=self.BEFORE_HANDOVER):
                    other.invalidate(actor, "Found-item report completed")
                report.status = "closed"
                report.save(update_fields=["status", "updated_at"])
            ActionHistory.objects.create(
                actor=actor, action=action, claim=locked, report=locked.report,
                previous_state=old_status, new_state=new_status,
                reason=reason or "", result=result or "",
            )
            if actor is not None and locked.claimant is not None and actor != locked.claimant:
                Notice.objects.create(
                    user=locked.claimant,
                    title=f"Claim {locked.reference}: {action}",
                    body=result or reason or action,
                    link=f"/claim/{locked.pk}/",
                )
        self.refresh_from_db()
        return self

    def request_evidence(self, staff, reason):
        return self._apply("awaiting_evidence", staff, "evidence_requested", ("pending",), reason,
                           "Additional evidence requested")

    def resubmit_evidence(self, evidence=None):
        return self._apply("pending", self.claimant, "evidence_resubmitted",
                           ("awaiting_evidence",), result="Evidence resubmitted",
                           extras={"evidence": evidence} if evidence is not None else None)

    def approve(self, staff, reason=""):
        return self._apply("awaiting_handover", staff, "approved", ("pending",),
                           reason, "Approved, awaiting handover")

    def reject(self, staff, reason):
        if not reason.strip():
            raise ValueError("Rejection requires a reason")
        return self._apply("rejected", staff, "rejected", self.BEFORE_HANDOVER, reason, "Rejected")

    def start_dispute(self, staff, reason=""):
        return self._apply("under_dispute", staff, "dispute_started", ("awaiting_handover",), reason,
                           "Under dispute review, collection paused")

    def resolve_dispute(self, staff, approve, reason=""):
        if approve:
            return self._apply("awaiting_handover", staff, "dispute_resolved", ("under_dispute",), reason,
                               "Dispute resolved, awaiting handover")
        if not reason.strip():
            raise ValueError("Rejection after dispute requires a reason")
        return self._apply("rejected", staff, "dispute_rejected", ("under_dispute",), reason,
                           "Dispute rejected")

    def start_handover(self, staff):
        return self._apply("handover_in_progress", staff, "handover_started", ("awaiting_handover",), "",
                           "Handover in progress", extras={"handover_started_by": staff,
                                                            "handover_started_at": timezone.now()})

    def complete_handover(self, staff):
        return self._apply("completed", staff, "handover_completed", ("handover_in_progress",), "",
                           "Handover completed", extras={"handover_completed_by": staff,
                                                        "handover_completed_at": timezone.now()})

    def interrupt_handover(self, staff, reason):
        if not reason.strip():
            raise ValueError("Interruption requires staff confirmation")
        return self._apply("awaiting_handover", staff, "handover_interrupted",
                           ("handover_in_progress",), reason, "Interrupted safely, awaiting handover")

    def withdraw(self, user):
        return self._apply("withdrawn", user, "withdrawn", self.BEFORE_HANDOVER,
                           result="Withdrawn by claimant")

    def invalidate(self, staff, reason):
        return self._apply("invalidated", staff, "invalidated", self.BEFORE_HANDOVER,
                           reason, "Invalidated")


class ClaimAttachment(models.Model):
    """Evidence images for a claim (CAP-3.1: up to 3 per submission, 5 total, JPG/PNG, 5 MB)."""

    claim = models.ForeignKey(Claim, on_delete=models.CASCADE, related_name="attachments")
    image = models.ImageField(upload_to="claims/", storage=PrivateEvidenceStorage())
    uploaded_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["uploaded_at"]


class Match(models.Model):
    """A suggested match between a lost and a found report (REQ-1.6, Could)."""

    STATUS_CHOICES = [
        ("suggested", "Suggested"),
        ("confirmed", "Confirmed"),
        ("rejected", "Rejected"),
    ]

    lost_report = models.ForeignKey(Report, on_delete=models.CASCADE, related_name="lost_matches")
    found_report = models.ForeignKey(Report, on_delete=models.CASCADE, related_name="found_matches")
    score = models.FloatField(default=0.0, help_text="Match confidence score 0-1")
    reason = models.CharField(max_length=300, blank=True, default="")
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default="suggested")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ("lost_report", "found_report")

    def __str__(self):
        return f"Match: {self.lost_report.item_name} <-> {self.found_report.item_name} ({self.score:.0%})"


class Notice(models.Model):
    """In-app notice for a user (REQ-1.13)."""

    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="notices")
    title = models.CharField(max_length=200)
    body = models.TextField(blank=True)
    link = models.CharField(max_length=300, blank=True, default="")
    is_read = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]


class ActionHistory(models.Model):
    """Immutable audit history of key actions (REQ-1.14, SEC-1.4)."""

    actor = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True)
    action = models.CharField(max_length=50)
    claim = models.ForeignKey(Claim, on_delete=models.CASCADE, null=True, blank=True, related_name="history")
    report = models.ForeignKey(Report, on_delete=models.CASCADE, null=True, blank=True, related_name="history")
    previous_state = models.CharField(max_length=25, blank=True, default="")
    new_state = models.CharField(max_length=25, blank=True, default="")
    reason = models.TextField(blank=True, default="")
    result = models.TextField(blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.created_at:%Y-%m-%d %H:%M} {self.actor} {self.action}"
