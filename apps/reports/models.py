from django.db import models
from django.contrib.auth.models import User


class Report(models.Model):
    """A lost or found item report."""

    REPORT_TYPES = [
        ("lost", "Lost Item"),
        ("found", "Found Item"),
    ]
    STATUS_CHOICES = [
        ("pending", "Pending Review"),
        ("approved", "Approved / Public"),
        ("rejected", "Rejected"),
        ("closed", "Closed / Resolved"),
    ]

    report_type = models.CharField(max_length=10, choices=REPORT_TYPES)
    item_name = models.CharField(max_length=200)
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


class Claim(models.Model):
    """A claim submission for a found item."""

    STATUS_CHOICES = [
        ("pending", "Pending Review"),
        ("approved", "Approved"),
        ("rejected", "Rejected"),
    ]

    report = models.ForeignKey(Report, on_delete=models.CASCADE, related_name="claims")
    claimant_name = models.CharField(max_length=100)
    claimant_email = models.EmailField()
    claimant_phone = models.CharField(max_length=50, blank=True)
    evidence = models.TextField(help_text="Description proving ownership")
    evidence_photo = models.ImageField(upload_to="claims/", blank=True, null=True)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default="pending")
    reviewed_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name="reviewed_claims")
    reviewed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"Claim for {self.report.item_name} by {self.claimant_name}"


class Match(models.Model):
    """A suggested match between a lost report and a found report."""

    STATUS_CHOICES = [
        ("suggested", "Suggested"),
        ("confirmed", "Confirmed"),
        ("rejected", "Rejected"),
    ]

    lost_report = models.ForeignKey(Report, on_delete=models.CASCADE, related_name="lost_matches")
    found_report = models.ForeignKey(Report, on_delete=models.CASCADE, related_name="found_matches")
    score = models.FloatField(default=0.0, help_text="Match confidence score 0-1")
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default="suggested")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ("lost_report", "found_report")

    def __str__(self):
        return f"Match: {self.lost_report.item_name} <-> {self.found_report.item_name} ({self.score:.0%})"
