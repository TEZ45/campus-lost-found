from django.db import models


class StorageLedger(models.Model):
    """One locked row accounts for all managed public and private files."""

    id = models.PositiveSmallIntegerField(primary_key=True, default=1, editable=False)
    used_bytes = models.PositiveBigIntegerField(default=0)
    initialized = models.BooleanField(default=False)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [models.CheckConstraint(condition=models.Q(id=1), name="one_storage_ledger")]


class FileDeletion(models.Model):
    """Durable deletion outbox; a failed delete remains charged and retryable."""

    storage = models.CharField(max_length=10, choices=[("public", "Public"), ("private", "Private")])
    name = models.CharField(max_length=500)
    size_bytes = models.PositiveBigIntegerField(default=0)
    attempts = models.PositiveIntegerField(default=0)
    last_error = models.CharField(max_length=100, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    completed_at = models.DateTimeField(null=True, blank=True, db_index=True)

    class Meta:
        ordering = ["created_at", "pk"]
        constraints = [
            models.UniqueConstraint(fields=["storage", "name"], name="one_file_deletion_per_path"),
        ]
