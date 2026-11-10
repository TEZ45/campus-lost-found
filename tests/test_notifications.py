"""Durable notification delivery, failure isolation, and retry guarantees."""

import io
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier
from unittest.mock import patch

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import close_old_connections, connections, transaction
from django.utils import timezone

from apps.reports import notifications
from apps.reports.models import ActionHistory, Notice, NotificationEvent
from tests.test_workflow import make_claim, make_report

pytestmark = pytest.mark.django_db(transaction=True)

APPROVE = {"evidence_reviewed": True, "custody_confirmed": True}
START = {"identity_verified": True, "custody_confirmed": True}
COMPLETE = {"transfer_confirmed": True}


def make_event(users, reason="More ownership proof is required"):
    report = make_report(users["owner"])
    claim = make_claim(report, users["claimant"])
    claim.request_evidence(users["service"], reason)
    return NotificationEvent.objects.get(history__claim=claim)


def test_workflow_commits_when_notice_delivery_fails(users):
    report = make_report(users["owner"], status="pending")
    with patch.object(Notice.objects, "create", side_effect=RuntimeError("Notice unavailable")):
        report.review(users["service"], "approve")
        claim = make_claim(report, users["claimant"])
        other = make_claim(report, users["other"])
        claim.approve(users["service"], confirmations=APPROVE)
        claim.start_handover(users["service"], confirmations=START)
        claim.complete_handover(users["service"], confirmations=COMPLETE)
        metrics = notifications.deliver_pending()

    report.refresh_from_db()
    claim.refresh_from_db()
    other.refresh_from_db()
    assert report.status == "closed"
    assert claim.status == "completed"
    assert claim.handover_completed_by == users["service"]
    assert claim.handover_completed_at is not None
    assert other.status == "invalidated"
    assert set(ActionHistory.objects.values_list("action", flat=True)) == {
        "report_approved", "approved", "handover_started", "handover_completed", "invalidated",
    }
    assert NotificationEvent.objects.count() == 5
    assert metrics["failed"] == metrics["pending"] == 5
    assert metrics["delivered"] == 0
    assert not Notice.objects.exists()
    assert not NotificationEvent.objects.filter(delivered_at__isnull=False).exists()


def test_failed_event_is_durable_and_retry_delivers_once(users, caplog):
    event = make_event(users)
    private_error = "private evidence: serial 9944, claimant@example.invalid"
    with patch.object(Notice.objects, "create", side_effect=RuntimeError(private_error)):
        assert notifications.deliver_event(event.pk) is False

    event.refresh_from_db()
    assert event.delivered_at is None
    assert event.attempts == 1
    assert event.last_error == "RuntimeError"
    assert private_error not in caplog.text
    assert not Notice.objects.exists()

    close_old_connections()
    assert notifications.deliver_pending()["delivered"] == 1
    event.refresh_from_db()
    assert event.delivered_at is not None
    assert event.attempts == 2
    assert event.last_error == ""
    notice = Notice.objects.get(event=event)
    assert (notice.user_id, notice.title, notice.body, notice.link) == (
        event.user_id, event.title, event.body, event.link,
    )

    delivered_at = event.delivered_at
    assert notifications.deliver_event(event.pk) is True
    assert notifications.deliver_pending()["delivered"] == 0
    event.refresh_from_db()
    assert event.delivered_at == delivered_at
    assert event.attempts == 2
    assert Notice.objects.filter(event=event).count() == 1


def test_crash_before_delivery_marker_rolls_back_notice(users):
    event = make_event(users)

    def crash_before_marker(instance, *args, **kwargs):
        assert instance.delivered_at is not None
        assert Notice.objects.filter(event=instance).count() == 1
        raise RuntimeError("Simulated failure before delivery marker is saved")

    with patch.object(NotificationEvent, "save", crash_before_marker):
        assert notifications.deliver_event(event.pk) is False

    event.refresh_from_db()
    assert event.delivered_at is None
    assert event.attempts == 1
    assert event.last_error == "RuntimeError"
    assert not Notice.objects.filter(event=event).exists()
    assert notifications.deliver_event(event.pk) is True
    assert Notice.objects.filter(event=event).count() == 1


def test_concurrent_workers_deliver_one_notice(users, monkeypatch):
    event = make_event(users)
    selected_same_event = Barrier(2)
    deliver_event = notifications.deliver_event

    def deliver_together(event_id):
        assert event_id == event.pk
        selected_same_event.wait(timeout=10)
        return deliver_event(event_id)

    def worker():
        close_old_connections()
        try:
            return notifications.deliver_pending(limit=1)
        finally:
            connections.close_all()

    monkeypatch.setattr(notifications, "deliver_event", deliver_together)
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(worker) for _ in range(2)]
        results = [future.result(timeout=20) for future in futures]
    monkeypatch.setattr(notifications, "deliver_event", deliver_event)

    assert any(result["delivered"] == 1 for result in results)
    assert Notice.objects.filter(event=event).count() == 1
    assert notifications.deliver_event(event.pk) is True
    event.refresh_from_db()
    assert event.delivered_at is not None
    assert Notice.objects.filter(event=event).count() == 1


def test_poison_event_does_not_starve_fresh_event_with_limit_one(users):
    poison = make_event(users)
    fresh = make_event(users)
    create_notice = Notice.objects.create

    def fail_poison(**kwargs):
        if kwargs["event"].pk == poison.pk:
            raise RuntimeError("Cannot deliver this event")
        return create_notice(**kwargs)

    with patch.object(Notice.objects, "create", side_effect=fail_poison):
        first = notifications.deliver_pending(limit=1)
        second = notifications.deliver_pending(limit=1)

    poison.refresh_from_db()
    fresh.refresh_from_db()
    assert first["failed"] == 1
    assert first["pending"] == 2
    assert second["delivered"] == 1
    assert second["pending"] == 1
    assert poison.delivered_at is None
    assert poison.attempts == 1
    assert fresh.delivered_at is not None
    assert Notice.objects.filter(event=fresh).count() == 1
    assert not Notice.objects.filter(event=poison).exists()


def test_outer_transaction_rollback_leaves_no_event_or_workflow_change(users):
    report = make_report(users["owner"], status="pending")
    claim = make_claim(report, users["claimant"])

    with pytest.raises(RuntimeError, match="Abort business transaction"), transaction.atomic():
        report.review(users["service"], "approve")
        claim.approve(users["service"], confirmations=APPROVE)
        assert NotificationEvent.objects.count() == 2
        raise RuntimeError("Abort business transaction")

    report.refresh_from_db()
    claim.refresh_from_db()
    assert report.status == "pending"
    assert claim.status == "pending"
    assert not ActionHistory.objects.exists()
    assert not NotificationEvent.objects.exists()
    assert not Notice.objects.exists()


def test_command_once_outputs_delivery_and_pending_metrics(users):
    first = make_event(users)
    pending = make_event(users)
    NotificationEvent.objects.filter(pk=first.pk).update(
        created_at=timezone.now() - timedelta(minutes=3),
    )
    NotificationEvent.objects.filter(pk=pending.pk).update(
        created_at=timezone.now() - timedelta(minutes=2),
    )
    output = io.StringIO()
    with patch("apps.reports.management.commands.deliver_notifications.time.sleep") as sleep:
        call_command("deliver_notifications", "--once", "--limit", "1", stdout=output)
    sleep.assert_not_called()

    lines = output.getvalue().splitlines()
    assert len(lines) == 1
    metrics = json.loads(lines[0])
    assert metrics.keys() == {
        "delivered", "failed", "pending", "oldest_pending_age_seconds",
    }
    assert metrics["delivered"] == 1
    assert metrics["failed"] == 0
    assert metrics["pending"] == 1
    assert metrics["oldest_pending_age_seconds"] >= 120
    assert Notice.objects.filter(event=first).exists()
    assert not Notice.objects.filter(event=pending).exists()


def test_command_once_reports_failed_delivery(users):
    make_event(users)
    output = io.StringIO()
    with patch.object(Notice.objects, "create", side_effect=RuntimeError("Unavailable")):
        call_command("deliver_notifications", "--once", stdout=output)
    metrics = json.loads(output.getvalue())
    assert metrics["delivered"] == 0
    assert metrics["failed"] == metrics["pending"] == 1
    assert metrics["oldest_pending_age_seconds"] >= 0


def test_command_once_empty_queue_reports_zero_age():
    output = io.StringIO()
    call_command("deliver_notifications", "--once", stdout=output)
    assert json.loads(output.getvalue()) == {
        "delivered": 0, "failed": 0, "pending": 0, "oldest_pending_age_seconds": 0,
    }


@pytest.mark.parametrize("option,value", [
    ("--limit", "0"), ("--limit", "-1"),
    ("--interval", "0"), ("--interval", "-1"),
    ("--interval", "nan"), ("--interval", "inf"), ("--interval", "-inf"),
])
def test_command_rejects_invalid_options_before_delivery(option, value):
    with (
        patch("apps.reports.management.commands.deliver_notifications.deliver_pending") as deliver,
        pytest.raises(CommandError),
    ):
        call_command("deliver_notifications", "--once", f"{option}={value}")
    deliver.assert_not_called()


@pytest.mark.parametrize("limit", [0, -1, True, False, 1.5, "1", None])
def test_worker_rejects_invalid_batch_limits(limit):
    with pytest.raises(ValueError, match="positive integer"):
        notifications.deliver_pending(limit=limit)


def test_notification_payload_omits_private_evidence_and_contact_details(users):
    secret = "EVIDENCE-SECRET-8a713"
    private_email = "private-owner@example.invalid"
    private_phone = "PRIVATE-PHONE-99331"
    attachment_path = "claims/private-evidence-8a713.png"
    reason = f"Inspected {secret}; attachment {attachment_path}"
    report = make_report(
        users["owner"], status="pending", description=secret,
        contact_email=private_email, contact_phone=private_phone,
    )
    report.review(users["service"], "approve", reason)
    users["claimant"].email = private_email
    users["claimant"].save(update_fields=["email"])
    claim = make_claim(
        report, users["claimant"], evidence=secret,
        claimant_phone=private_phone,
    )
    claim.request_evidence(users["service"], reason)

    events = list(NotificationEvent.objects.all())
    assert len(events) == 2
    assert ActionHistory.objects.filter(reason=reason).count() == 2
    for event in events:
        payload = json.dumps({
            "title": event.title, "body": event.body, "link": event.link,
            "last_error": event.last_error,
        })
        for sensitive in (secret, private_email, private_phone, attachment_path, reason):
            assert sensitive not in payload
    assert notifications.deliver_pending()["delivered"] == 2
    for event in events:
        notice = Notice.objects.get(event=event)
        assert (notice.title, notice.body, notice.link) == (event.title, event.body, event.link)
