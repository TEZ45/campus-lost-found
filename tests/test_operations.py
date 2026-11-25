import io
import json
from datetime import timedelta
from unittest.mock import Mock, patch

import pytest
from django.core.cache.backends.redis import RedisCache
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from apps.reports import operations
from apps.reports.lifecycle_models import FileDeletion, StorageLedger
from apps.reports.models import ActionHistory, Claim, Notice, NotificationEvent, Report

from .test_notifications import make_event
from .test_workflow import make_claim, make_report

pytestmark = pytest.mark.django_db


@pytest.fixture
def ops(settings, tmp_path):
    settings.OPS_STATE_DIR = str(tmp_path / "ops")
    settings.RECOVERY_STATE_FILE = ""
    for task in operations.HEARTBEAT_MAX_AGE:
        operations.run_tracked(task, dict)
    StorageLedger.objects.create(pk=1, initialized=True)
    return tmp_path / "ops"


def command(name, *args, **kwargs):
    output = io.StringIO()
    call_command(name, *args, stdout=output, **kwargs)
    return json.loads(output.getvalue())


def test_health_is_read_only_and_empty_initialized_system_is_healthy(ops):
    with CaptureQueriesContext(connection) as queries:
        result = command("healthcheck")
    assert result["ok"]
    assert all(check["ok"] for check in result["checks"].values())
    assert all(query["sql"].lstrip().upper().startswith("SELECT") for query in queries)


def test_health_does_not_initialize_storage_ledger(ops):
    StorageLedger.objects.all().delete()
    output = io.StringIO()
    with pytest.raises(CommandError):
        call_command("healthcheck", stdout=output)
    result = json.loads(output.getvalue())
    assert not result["checks"]["storage"]["ok"]
    assert not StorageLedger.objects.exists()


def test_notifications_worker_tracks_success_and_failure_without_private_details(ops, users):
    event = make_event(users)
    with patch.object(Notice.objects, "create", side_effect=RuntimeError("private evidence secret")):
        result = command("deliver_notifications", "--once")
    assert result["failed"] == 1
    failed = operations.read_state(ops / "notifications.json")
    assert failed["status"] == "failed"
    assert "private evidence secret" not in (ops / "notifications.json").read_text()
    result = command("deliver_notifications", "--once")
    assert result["delivered"] == 1
    assert Notice.objects.filter(event=event).count() == 1
    assert operations.read_state(ops / "notifications.json")["status"] == "ok"


def test_crashed_task_leaves_failed_heartbeat_and_preserves_last_success(ops):
    before = operations.read_state(ops / "retention.json")
    with pytest.raises(OSError):
        operations.run_tracked("retention", Mock(side_effect=OSError("private path and password")))
    after = operations.read_state(ops / "retention.json")
    assert after["status"] == "failed"
    assert after["error"] == "OSError"
    assert after["last_success_at"] == before["last_success_at"]
    assert "private" not in json.dumps(after)
    assert not operations.heartbeat_status("retention", timezone.now())["ok"]


def test_unwritable_heartbeat_prevents_untracked_maintenance(ops):
    operation = Mock()
    with patch.object(operations, "write_state", side_effect=OSError), pytest.raises(OSError):
        operations.run_tracked("retention", operation)
    operation.assert_not_called()


@pytest.mark.parametrize("task", operations.HEARTBEAT_MAX_AGE)
@pytest.mark.parametrize("mode", ["missing", "stale", "future", "invalid", "running_stale"])
def test_heartbeat_loss_and_clock_errors_are_unhealthy(ops, task, mode):
    path = ops / f"{task}.json"
    now = timezone.now()
    state = operations.read_state(path)
    if mode == "missing":
        path.unlink()
    elif mode == "invalid":
        path.write_text('{"version":1,"last_success_at":42}', encoding="ascii")
    else:
        offset = timedelta(seconds=operations.HEARTBEAT_MAX_AGE[task] + 1)
        state["last_success_at"] = (now + offset if mode == "future" else now - offset).isoformat()
        if mode == "running_stale":
            state["status"] = "running"
        operations.write_state(path, state)
    assert not operations.heartbeat_status(task, now)["ok"]


def test_healthy_running_tick_uses_recent_last_success(ops):
    path = ops / "notifications.json"
    state = operations.read_state(path)
    state["status"] = "running"
    operations.write_state(path, state)
    assert operations.heartbeat_status("notifications", timezone.now())["ok"]


def test_two_missed_hourly_storage_runs_make_health_unhealthy(ops):
    now = timezone.now()
    path = ops / "storage.json"
    state = operations.read_state(path)
    state["last_success_at"] = (now - timedelta(hours=2, seconds=1)).isoformat()
    operations.write_state(path, state)
    result = operations.health_status(now)
    assert not result["ok"]
    assert not result["checks"]["heartbeat_storage"]["ok"]
    assert all(check["ok"] for name, check in result["checks"].items() if name != "heartbeat_storage")


def test_atomic_heartbeat_replace_failure_preserves_previous_state(ops):
    path = ops / "storage.json"
    previous = path.read_bytes()
    with patch.object(operations.os, "replace", side_effect=OSError), pytest.raises(OSError):
        operations.write_state(path, {"version": 1, "status": "bad"})
    assert path.read_bytes() == previous
    assert sorted(file.name for file in ops.iterdir()) == sorted(f"{task}.json" for task in operations.HEARTBEAT_MAX_AGE)


def test_old_notification_queue_reports_latency_violation_without_payload(ops, users):
    event = make_event(users)
    NotificationEvent.objects.filter(pk=event.pk).update(created_at=timezone.now() - timedelta(seconds=11))
    result = operations.health_status()
    assert not result["checks"]["notifications"]["ok"]
    assert result["checks"]["notifications"]["pending"] == 1
    assert result["checks"]["notifications"]["oldest_seconds"] >= 11
    assert event.body not in json.dumps(result)


def test_storage_eighty_percent_and_deletion_failures_are_visible(ops, settings):
    settings.STORAGE_CAPACITY_BYTES = 100
    StorageLedger.objects.filter(pk=1).update(used_bytes=80)
    job = FileDeletion.objects.create(storage="private", name="secret.bin", last_error="PermissionError")
    FileDeletion.objects.filter(pk=job.pk).update(created_at=timezone.now() - timedelta(days=7))
    result = operations.health_status()
    assert not result["checks"]["storage"]["ok"]
    assert not result["checks"]["deletions"]["ok"]
    assert result["checks"]["deletions"]["oldest_seconds"] >= 7 * 86400
    assert "secret.bin" not in json.dumps(result)


def test_cleanup_deadline_and_active_hold_are_distinguished(ops, users):
    now = timezone.now()
    report = make_report(users["owner"])
    held = make_claim(report, users["claimant"], status="rejected", finalized_at=now - timedelta(days=50))
    blocker = make_claim(report, users["other"], status="under_dispute")
    assert operations.retention_backlog(now)["oldest_due_seconds"]["evidence"] == 0
    blocker.status = "rejected"
    blocker.finalized_at = now
    blocker.save()
    backlog = operations.retention_backlog(now)
    assert backlog["overdue"]
    assert backlog["oldest_due_seconds"]["evidence"] == 35 * 86400
    assert Claim.objects.get(pk=held.pk).evidence


def test_report_freeform_cleanup_deadline_and_missing_clocks_are_visible(ops, users):
    now = timezone.now()
    report = make_report(users["owner"], status="closed", closed_at=now - timedelta(days=22))
    Notice.objects.create(user=users["owner"], title="Old", body="Private copied text", link=f"/report/{report.pk}/")
    make_report(users["owner"], status="closed")
    backlog = operations.retention_backlog(now)
    assert backlog["oldest_due_seconds"]["report_freeform"] == 7 * 86400
    assert backlog["missing_final_clocks"] == 1
    assert backlog["overdue"]


def test_retention_command_applies_policy_in_bounded_batches(ops, users):
    now = timezone.now()
    old = [make_claim(make_report(users["owner"]), users["claimant"], status="rejected",
                      finalized_at=now - timedelta(days=16)) for _ in range(2)]
    young = make_claim(make_report(users["owner"]), users["claimant"], status="rejected",
                       finalized_at=now - timedelta(days=14))
    result = command("run_maintenance", "--task", "retention", "--batch-size", "1")
    assert result["ok"]
    assert result["result"]["evidence"] == 1
    assert Claim.objects.filter(pk__in=[claim.pk for claim in old], evidence="").count() == 1
    assert Claim.objects.get(pk=young.pk).evidence
    command("run_maintenance", "--task", "retention", "--batch-size", "1")
    assert Claim.objects.filter(pk__in=[claim.pk for claim in old], evidence="").count() == 2
    assert operations.read_state(ops / "retention.json")["status"] == "ok"


def test_storage_and_deletion_commands_reconcile_and_retry(ops, settings):
    settings.PRIVATE_MEDIA_ROOT.mkdir(parents=True)
    orphan = settings.PRIVATE_MEDIA_ROOT / "orphan.bin"
    orphan.write_bytes(b"proof")
    assert command("run_maintenance", "--task", "storage")["result"]["queued"] == 1
    assert orphan.exists()
    with (
        patch("django.core.files.storage.FileSystemStorage.delete", side_effect=OSError("private path")),
        pytest.raises(CommandError),
    ):
        command("run_maintenance", "--task", "deletions")
    assert FileDeletion.objects.filter(completed_at=None).count() == 1
    assert operations.read_state(ops / "deletions.json")["status"] == "failed"
    assert command("run_maintenance", "--task", "deletions")["result"]["files_deleted"] == 1
    assert not orphan.exists()
    assert StorageLedger.objects.get(pk=1).used_bytes == 0


def test_command_failures_emit_safe_json_and_nonzero_exit(ops):
    output = io.StringIO()
    with (
        patch("apps.reports.management.commands.run_maintenance.cleanup_retention", side_effect=RuntimeError("password-secret")),
        pytest.raises(CommandError),
    ):
        call_command("run_maintenance", "--task", "retention", stdout=output)
    assert json.loads(output.getvalue()) == {"ok": False, "task": "retention", "error": "RuntimeError"}


@pytest.mark.parametrize("option,value", [("--batch-size", 0), ("--batch-size", 1001), ("--max-files", 0), ("--max-files", 1000001)])
def test_invalid_command_bounds_do_not_run_maintenance(ops, option, value):
    with patch("apps.reports.management.commands.run_maintenance.run_tracked") as run, pytest.raises(CommandError):
        command("run_maintenance", "--task", "retention", option, str(value))
    run.assert_not_called()


@pytest.mark.parametrize("policy,used,maximum,ok", [("noeviction", 10, 100, True), ("allkeys-lru", 10, 100, False), ("noeviction", 100, 100, False)])
def test_redis_policy_and_full_memory_are_checked(ops, policy, used, maximum, ok):
    client = Mock()
    client.config_get.return_value = {"maxmemory-policy": policy}
    client.info.return_value = {"used_memory": used, "maxmemory": maximum}
    backend = Mock(spec=RedisCache)
    backend._cache = Mock()
    backend._cache.get_client.return_value = client
    with patch.object(operations, "guard_cache", return_value=backend):
        result = operations.health_status()
    assert result["checks"]["redis"]["ok"] == ok
    client.ping.assert_called_once()


def test_backend_failures_and_recovery_pause_are_unhealthy_without_secrets(ops):
    with patch.object(connection, "cursor", side_effect=RuntimeError("database password")), patch.object(
        operations, "guard_cache", side_effect=RuntimeError("redis password"),
    ), patch("apps.reports.recovery.recovery_status", return_value={"configured": True, "paused": True, "valid": True}):
        result = operations.health_status()
    assert not result["ok"]
    assert not result["checks"]["database"]["ok"]
    assert not result["checks"]["redis"]["ok"]
    assert not result["checks"]["recovery"]["ok"]
    assert "password" not in json.dumps(result)


def test_production_missing_operations_state_fails_before_work(settings):
    settings.APP_ENV = "production"
    settings.OPS_STATE_DIR = ""
    work = Mock()
    with pytest.raises(ValueError, match="OPS_STATE_DIR"):
        operations.run_tracked("retention", work)
    work.assert_not_called()


def test_history_retention_deadline_is_visible(ops, users):
    now = timezone.now()
    with patch("django.utils.timezone.now", return_value=now - timedelta(days=52)):
        ActionHistory.objects.create(action="old_event")
    result = operations.retention_backlog(now)
    assert result["oldest_due_seconds"]["history"] == 7 * 86400
    assert result["overdue"]
    assert Report.objects.count() == 0
