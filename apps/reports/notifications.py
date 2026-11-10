import logging

from django.db import DatabaseError, transaction
from django.db.models import F
from django.utils import timezone

from .models import Notice, NotificationEvent

logger = logging.getLogger(__name__)


def deliver_event(event_id):
    try:
        with transaction.atomic():
            event = NotificationEvent.objects.select_for_update().get(pk=event_id)
            if event.delivered_at is not None:
                return True
            Notice.objects.create(
                event=event, user_id=event.user_id, title=event.title,
                body=event.body, link=event.link,
            )
            event.delivered_at = timezone.now()
            event.attempts += 1
            event.last_error = ""
            event.save(update_fields=["delivered_at", "attempts", "last_error"])
        return True
    except Exception as exc:  # noqa: BLE001 - Worker failures must remain retryable.
        error = type(exc).__name__[:100]
        logger.warning("Notification event %s failed: %s", event_id, error)
        try:
            NotificationEvent.objects.filter(pk=event_id, delivered_at__isnull=True).update(
                attempts=F("attempts") + 1, last_error=error,
            )
        except DatabaseError:
            logger.warning("Notification event %s retry status could not be stored", event_id)
        return False


def deliver_pending(limit=100):
    if not isinstance(limit, int) or isinstance(limit, bool) or limit <= 0:
        raise ValueError("The batch limit must be a positive integer.")
    pending = NotificationEvent.objects.filter(delivered_at__isnull=True)
    event_ids = list(pending.order_by("attempts", "created_at", "pk").values_list("pk", flat=True)[:limit])
    delivered = sum(deliver_event(event_id) for event_id in event_ids)
    oldest = pending.values_list("created_at", flat=True).first()
    return {
        "delivered": delivered, "failed": len(event_ids) - delivered,
        "pending": pending.count(),
        "oldest_pending_age_seconds": max(0, (timezone.now() - oldest).total_seconds()) if oldest else 0,
    }
