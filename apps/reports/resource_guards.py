import math
import secrets
import time
from contextlib import contextmanager
from datetime import timedelta

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from .models import ResourceLease


class BusinessBusy(Exception):
    def __init__(self, message, retry_after=30):
        super().__init__(message)
        self.retry_after = max(1, math.ceil(retry_after))


@contextmanager
def work_budget(key):
    maximum = getattr(settings, "MATCH_MAX_SECONDS", 10)
    lease_seconds = max(120, maximum * 3)
    token = secrets.token_hex(16)
    with transaction.atomic():
        lease, _ = ResourceLease.objects.select_for_update().get_or_create(
            key=key, defaults={"token": "", "expires_at": timezone.now()},
        )
        now = timezone.now()
        if lease.expires_at > now:
            raise BusinessBusy("Matching is running or cooling down.",
                               (lease.expires_at - now).total_seconds())
        lease.token = token
        lease.expires_at = now + timedelta(seconds=lease_seconds)
        lease.save(update_fields=["token", "expires_at"])
    started = time.monotonic()

    def check_budget():
        if time.monotonic() - started >= maximum:
            raise BusinessBusy("Matching exceeded its work budget.")

    try:
        yield check_budget
    finally:
        ResourceLease.objects.filter(key=key, token=token).update(
            token="", expires_at=timezone.now() + timedelta(
                seconds=getattr(settings, "MATCH_COOLDOWN_SECONDS", 30)),
        )
