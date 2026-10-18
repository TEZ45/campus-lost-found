from django.contrib.auth.models import User
from django.db import models, transaction
from django.utils import timezone


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
    def reference(self):
        return f"RPT-{self.pk:06d}"


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

    def __str__(self):
        return f"Claim for {self.report.item_name} by {self.claimant_name} ({self.get_status_display()})"

    @property
    def reference(self):
        return f"CLM-{self.pk:06d}"

    @property
    def holds_reservation(self):
        return self.status in self.RESERVED_STATES

    def reservation_conflict_exists(self):
        """DOM-3: at most one reserved claim across the reserved states."""
        return (
            self.report.claims.filter(status__in=self.RESERVED_STATES)
            .exclude(pk=self.pk)
            .exists()
        )

    # ------------------------------------------------------------------ #
    # Shared transition machinery.                                       #
    # ------------------------------------------------------------------ #

    def _apply(self, new_status, actor, action, reason="", result="", extras=None):
        """Apply a state change inside a transaction: guard, save, history, notice."""
        with transaction.atomic():
            locked = Claim.objects.select_for_update().get(pk=self.pk)
            if new_status not in self.FINAL_STATES and locked.status in self.FINAL_STATES:
                raise ValueError(f"Cannot move final state {locked.status} to {new_status}")
            old_status = locked.status
            locked.status = new_status
            if actor is not None:
                locked.reviewed_by = actor
                locked.reviewed_at = timezone.now()
            for field, value in (extras or {}).items():
                setattr(locked, field, value)
            locked.save(update_fields=["status", "reviewed_by", "reviewed_at",
                                       "updated_at"] + list((extras or {}).keys()))
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

    # ------------------------------------------------------------------ #
    # Transitions.                                                       #
    # ------------------------------------------------------------------ #

    def request_evidence(self, staff, reason):
        """REQ-1.8: staff asks for more evidence. pending -> awaiting_evidence."""
        if self.status != "pending":
            raise ValueError(f"request_evidence only from pending, got {self.status}")
        return self._apply("awaiting_evidence", staff, "evidence_requested", reason,
                           "Additional evidence requested")

    def resubmit_evidence(self):
        """REQ-1.8: valid resubmission. awaiting_evidence -> pending."""
        if self.status != "awaiting_evidence":
            raise ValueError(f"resubmit only from awaiting_evidence, got {self.status}")
        return self._apply("pending", self.claimant, "evidence_resubmitted",
                           "", "Evidence resubmitted")

    def approve(self, staff, reason=""):
        """REQ-1.9 / DOM-3: approval needs no existing reservation."""
        if self.status != "pending":
            raise ValueError(f"approve only from pending, got {self.status}")
        if self.report.status != "approved":
            raise ValueError("Report must be approved and open to approve a claim")
        if self.reservation_conflict_exists():
            raise ValueError("Another claim already holds the reservation")
        return self._apply("awaiting_handover", staff, "approved",
                           reason, "Approved, awaiting handover")

    def reject(self, staff, reason):
        """REQ-1.8: rejection requires a reason."""
        if self.status not in ("pending", "awaiting_evidence", "awaiting_handover", "under_dispute"):
            raise ValueError(f"reject not allowed from {self.status}")
        if not reason:
            raise ValueError("Rejection requires a reason")
        return self._apply("rejected", staff, "rejected", reason, "Rejected")

    def start_dispute(self, staff, reason=""):
        """REQ-1.10: suspend collection, retain reservation."""
        if self.status != "awaiting_handover":
            raise ValueError(f"start_dispute only from awaiting_handover, got {self.status}")
        return self._apply("under_dispute", staff, "dispute_started", reason,
                           "Under dispute review, collection paused")

    def resolve_dispute(self, staff, approve, reason=""):
        """REQ-1.10: resolve keeps approval (-> awaiting_handover) or rejects."""
        if self.status != "under_dispute":
            raise ValueError(f"resolve_dispute only from under_dispute, got {self.status}")
        if approve:
            return self._apply("awaiting_handover", staff, "dispute_resolved", reason,
                               "Dispute resolved, awaiting handover")
        if not reason:
            raise ValueError("Rejection after dispute requires a reason")
        return self._apply("rejected", staff, "dispute_rejected", reason, "Dispute rejected")

    def start_handover(self, staff):
        """REQ-1.11 / DOM-4: record start before releasing the item."""
        if self.status != "awaiting_handover":
            raise ValueError(f"start_handover only from awaiting_handover, got {self.status}")
        return self._apply("handover_in_progress", staff, "handover_started", "",
                           "Handover in progress", extras={"handover_started_by": staff,
                                                            "handover_started_at": timezone.now()})

    def complete_handover(self, staff):
        """REQ-1.11-1.12 / DOM-5: close report, invalidate other before-handover claims."""
        if self.status != "handover_in_progress":
            raise ValueError(f"complete_handover only from handover_in_progress, got {self.status}")
        with transaction.atomic():
            locked = Claim.objects.select_for_update().get(pk=self.pk)
            other_before = locked.report.claims.filter(status__in=self.BEFORE_HANDOVER).exclude(pk=locked.pk)
            for other in other_before:
                other._apply("invalidated", staff, "invalidated",
                             "Found-item report completed", "Invalidated after completion")
            locked.report.status = "closed"
            locked.report.save(update_fields=["status", "updated_at"])
            self._apply("completed", staff, "handover_completed", "",
                        "Handover completed", extras={"handover_completed_by": staff,
                                                       "handover_completed_at": timezone.now()})
        return self

    def interrupt_handover(self, staff, reason):
        """REQ-1.11 / A4: safe interruption after staff confirms item not handed over."""
        if self.status != "handover_in_progress":
            raise ValueError(f"interrupt_handover only from handover_in_progress, got {self.status}")
        if not reason:
            raise ValueError("Interruption requires staff confirmation")
        return self._apply("awaiting_handover", staff, "handover_interrupted", reason,
                           "Interrupted safely, awaiting handover")

    def withdraw(self, user):
        """REQ-1.12: only before handover."""
        if self.status not in self.BEFORE_HANDOVER:
            raise ValueError(f"withdraw not allowed from {self.status}")
        return self._apply("withdrawn", user, "withdrawn", "", "Withdrawn by claimant")

    def invalidate(self, staff, reason):
        """REQ-1.12: verified report cancellation invalidates before-handover claims."""
        if self.status not in self.BEFORE_HANDOVER:
            raise ValueError(f"invalidate not allowed from {self.status}")
        return self._apply("invalidated", staff, "invalidated", reason, "Invalidated")


class ClaimAttachment(models.Model):
    """Evidence images for a claim (CAP-3.1: up to 3 per submission, 5 total, JPG/PNG, 5 MB)."""

    claim = models.ForeignKey(Claim, on_delete=models.CASCADE, related_name="attachments")
    image = models.ImageField(upload_to="claims/")
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
