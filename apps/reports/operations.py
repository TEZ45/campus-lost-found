import json
import os
import tempfile
from datetime import timedelta
from pathlib import Path

from django.conf import settings
from django.core.cache.backends.redis import RedisCache
from django.db import connection
from django.db.models import CharField, Exists, Min, OuterRef, Q, Value
from django.db.models.functions import Cast, Concat
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from .lifecycle import BUSINESS_DAYS, EVIDENCE_DAYS, HISTORY_DAYS, UNCERTAIN_STATES
from .lifecycle_models import FileDeletion
from .models import ActionHistory, Claim, Notice, NotificationEvent, Report
from .request_limits import guard_cache
from .storage_quota import storage_status

HEARTBEAT_MAX_AGE = {"notifications": 30, "retention": 300, "deletions": 300, "storage": 7200}


def state_path(task):
    if task not in HEARTBEAT_MAX_AGE:
        raise ValueError("Unknown maintenance task.")
    directory = getattr(settings, "OPS_STATE_DIR", "")
    return Path(directory) / f"{task}.json" if directory else None


def read_state(path):
    if path.is_symlink() or path.stat().st_size > 16384:
        raise ValueError("Invalid heartbeat file.")
    state = json.loads(path.read_text(encoding="ascii"))
    if not isinstance(state, dict) or state.get("version") != 1:
        raise ValueError("Invalid heartbeat state.")
    return state


def write_state(path, state):
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink():
        raise ValueError("Heartbeat files cannot be symlinks.")
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, encoding="ascii", delete=False) as stream:
            temporary = stream.name
            json.dump(state, stream, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary and os.path.exists(temporary):
            os.unlink(temporary)


def run_tracked(task, operation):
    path = state_path(task)
    if path is None:
        if settings.APP_ENV == "production":
            raise ValueError("Production maintenance requires OPS_STATE_DIR.")
        return operation()
    try:
        previous = read_state(path)
    except (OSError, ValueError):
        previous = {}
    state = {
        "version": 1, "task": task, "status": "running", "started_at": timezone.now().isoformat(),
        "finished_at": None, "last_success_at": previous.get("last_success_at"),
    }
    write_state(path, state)
    try:
        result = operation()
    except Exception as exc:
        state.update(status="failed", error=type(exc).__name__, finished_at=timezone.now().isoformat())
        write_state(path, state)
        raise
    failed = any(result.get(key, 0) for key in ("failed", "files_failed", "files_referenced"))
    state.update(status="failed" if failed else "ok", result=result, finished_at=timezone.now().isoformat())
    if not failed:
        state["last_success_at"] = state["finished_at"]
    write_state(path, state)
    return result


def heartbeat_status(task, now):
    path = state_path(task)
    if path is None:
        return {"ok": False, "reason": "not_configured"}
    try:
        state = read_state(path)
        finished = parse_datetime(state.get("last_success_at") or "")
        if finished is None or timezone.is_naive(finished) or state.get("task") != task:
            raise ValueError("Invalid heartbeat timestamp.")
        age = (now - finished).total_seconds()
        ok = 0 <= age <= HEARTBEAT_MAX_AGE[task] and state.get("status") in {"ok", "running"}
        return {"ok": ok, "status": state.get("status"), "age_seconds": round(age, 3)}
    except (OSError, ValueError, TypeError):
        return {"ok": False, "reason": "missing_or_invalid"}


def oldest_age(queryset, field, now, delay_days=0):
    oldest = queryset.aggregate(value=Min(field))["value"]
    return max(0, (now - oldest - timedelta(days=delay_days)).total_seconds()) if oldest else 0


def retention_backlog(now):
    uncertain = Claim.objects.filter(report_id=OuterRef("report_id"), status__in=UNCERTAIN_STATES)
    final = Claim.objects.filter(status__in=Claim.FINAL_STATES).filter(~Exists(uncertain))
    reports = Report.objects.filter(status="closed").filter(~Exists(Claim.objects.filter(report_id=OuterRef("pk"))))
    report_notices = Notice.objects.filter(link=Concat(
        Value("/report/"), Cast(OuterRef("pk"), output_field=CharField()), Value("/"),
    )).exclude(body="")
    freeform_reports = Report.objects.filter(status="closed").filter(
        Q(review_reason__gt="") | Q(Exists(report_notices)) | Q(Exists(ActionHistory.objects.filter(
            report_id=OuterRef("pk"), claim_id=None,
        ).exclude(reason="", result=""))),
    ).filter(~Exists(Claim.objects.filter(report_id=OuterRef("pk"), status__in=UNCERTAIN_STATES)))
    ages = {
        "evidence": oldest_age(final.filter(evidence_purged_at=None), "finalized_at", now, EVIDENCE_DAYS),
        "claims": oldest_age(final, "finalized_at", now, BUSINESS_DAYS),
        "reports": oldest_age(reports, "closed_at", now, BUSINESS_DAYS),
        "history": oldest_age(ActionHistory.objects.all(), "created_at", now, HISTORY_DAYS),
        "report_freeform": oldest_age(freeform_reports, "closed_at", now, EVIDENCE_DAYS),
    }
    return {"oldest_due_seconds": ages, "warning": max(ages.values()) >= 6 * 86400,
            "overdue": max(ages.values()) >= 7 * 86400,
            "missing_final_clocks": final.filter(finalized_at=None).count()
            + Report.objects.filter(status="closed", closed_at=None).count()}


def health_status(now=None):
    from .recovery import recovery_status

    now = now or timezone.now()
    checks = {}
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
        pending = NotificationEvent.objects.filter(delivered_at=None)
        age = oldest_age(pending, "created_at", now)
        checks["notifications"] = {"ok": age <= 10, "pending": pending.count(), "oldest_seconds": age}
        storage = storage_status()
        checks["storage"] = {"ok": storage["initialized"] and not storage["warning"], **storage}
        deletions = FileDeletion.objects.filter(completed_at=None)
        deletion_age = oldest_age(deletions, "created_at", now)
        failed = deletions.exclude(last_error="").count()
        checks["deletions"] = {"ok": deletion_age < 6 * 86400 and not failed,
                               "pending": deletions.count(), "oldest_seconds": deletion_age, "failed": failed}
        backlog = retention_backlog(now)
        checks["retention"] = {"ok": not backlog["warning"] and not backlog["missing_final_clocks"], **backlog}
        checks["database"] = {"ok": True}
    except Exception as exc:  # noqa: BLE001 - Health output must not expose connection details.
        checks["database"] = {"ok": False, "error": type(exc).__name__}
    try:
        backend = guard_cache()
        if isinstance(backend, RedisCache):
            client = backend._cache.get_client(write=True)
            client.ping()
            policy = client.config_get("maxmemory-policy")["maxmemory-policy"]
            memory = client.info("memory")
            capacity, used = memory["maxmemory"], memory["used_memory"]
            checks["redis"] = {"ok": policy == "noeviction" and (not capacity or used < capacity),
                               "policy": policy, "memory_full": bool(capacity and used >= capacity)}
        else:
            backend.get("healthcheck")
            checks["redis"] = {"ok": settings.APP_ENV != "production", "mode": "development_cache"}
    except Exception as exc:  # noqa: BLE001 - Health output must not expose Redis credentials.
        checks["redis"] = {"ok": False, "error": type(exc).__name__}
    recovery = recovery_status()
    checks["recovery"] = {"ok": recovery["valid"] and not recovery["paused"],
                          "configured": recovery["configured"], "paused": recovery["paused"], "valid": recovery["valid"]}
    for task in HEARTBEAT_MAX_AGE:
        checks[f"heartbeat_{task}"] = heartbeat_status(task, now)
    return {"ok": all(check["ok"] for check in checks.values()), "checked_at": now.isoformat(), "checks": checks}
