"""Measure the backend locally using isolated PostgreSQL, Redis and synthetic users."""

import argparse
import base64
import json
import math
import os
import random
import re
import signal
import socket
import subprocess
import sys
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from datetime import UTC, datetime
from http.client import HTTPConnection, HTTPException
from pathlib import Path
from urllib.error import URLError
from urllib.parse import urlsplit
from urllib.request import ProxyHandler, Request, build_opener
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
TIERS = {
    "small": {"users": 50, "rps": 10, "reports": 5000, "claims": 10000},
    "medium": {"users": 100, "rps": 20, "reports": 10000, "claims": 20000},
    "large": {"users": 200, "rps": 40, "reports": 20000, "claims": 40000},
}
PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAIAAAACCAIAAAD91JpzAAAAEklEQVR4nGMMyOthYGBgYgADAA9mAU4gURISAAAAAElFTkSuQmCC")


def validate_environment(env):
    if env.get("APP_ENV") != "test" or env.get("DB_ENGINE") != "postgresql":
        raise ValueError("Load tests require APP_ENV=test and DB_ENGINE=postgresql.")
    if not re.fullmatch(r"test_campus_load_[a-z0-9_]{1,40}", env.get("DB_NAME", "")):
        raise ValueError("Use a new test_campus_load_ database.")
    if env.get("DB_HOST") not in {"127.0.0.1", "localhost", "::1"}:
        raise ValueError("The test database must be on loopback.")
    redis = urlsplit(env.get("REQUEST_LIMITS_REDIS_URL", ""))
    if redis.scheme not in {"redis", "rediss"} or redis.hostname not in {"127.0.0.1", "localhost", "::1"}:
        raise ValueError("The test Redis service must be on loopback.")


def assert_empty_database():
    from django.db import connection

    reference_tables = {"django_migrations", "django_content_type", "auth_permission", "reports_category"}
    with connection.cursor() as cursor:
        for table in connection.introspection.table_names():
            if table not in reference_tables:
                cursor.execute(f"SELECT 1 FROM {connection.ops.quote_name(table)} LIMIT 1")
                if cursor.fetchone():
                    raise ValueError("Refusing a populated application database.")


def seed(tier, work):
    from django.contrib.auth.models import User
    from django.contrib.sessions.backends.db import SessionStore
    from django.core.management import call_command
    from django.db import connection

    from apps.reports.lifecycle_models import StorageLedger
    from apps.reports.models import Claim, ClaimAttachment, Report, UserProfile

    assert_empty_database()
    call_command("migrate", interactive=False, verbosity=0)
    paths = [work / "public" / "reports" / "load.png", work / "private" / "claims" / "a.png",
             work / "private" / "claims" / "b.png"]
    for path in paths:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(PNG)
    users = User.objects.bulk_create([
        User(username=f"load_{index}", email=f"load_{index}@example.invalid", password="!load-only")
        for index in range(tier["users"])
    ], batch_size=1000)
    staff = users[::10]
    UserProfile.objects.bulk_create([UserProfile(user=user, is_service_staff=True) for user in staff])
    reports = Report.objects.bulk_create([
        Report(report_type="found" if index % 2 == 0 else "lost", item_name=f"Load wallet {index}",
               category="Electronics", description="Synthetic load fixture", location="Library",
               event_date="2026-11-24", event_time="10:00", contact_name="Load user",
               contact_email="load@example.invalid", created_by=users[index % len(users)],
               photo="reports/load.png", status="approved")
        for index in range(tier["reports"])
    ], batch_size=1000)
    found = reports[::2]
    claims = Claim.objects.bulk_create([
        Claim(report=found[index % len(found)], claimant=users[index % len(users)],
              claimant_name="Load claimant", claimant_email="load@example.invalid",
              evidence="Synthetic serial number and distinguishing marks", status="pending")
        for index in range(tier["claims"])
    ], batch_size=1000)
    for offset in range(0, len(claims), 1000):
        ClaimAttachment.objects.bulk_create([
            ClaimAttachment(claim=claim, image=name)
            for claim in claims[offset:offset + 1000] for name in ("claims/a.png", "claims/b.png")
        ], batch_size=1000)
    StorageLedger.objects.update_or_create(pk=1, defaults={"initialized": True, "used_bytes": len(PNG) * 3})
    clients = []
    for user in users:
        session = SessionStore()
        session["_auth_user_id"] = str(user.pk)
        session["_auth_user_backend"] = "django.contrib.auth.backends.ModelBackend"
        session["_auth_user_hash"] = user.get_session_auth_hash()
        session.save()
        clients.append({"id": user.pk, "cookie": f"sessionid={session.session_key}",
                        "claims": [claim.pk for claim in claims if claim.claimant_id == user.pk]})
    with connection.cursor() as cursor:
        cursor.execute("ANALYZE")
    return {"clients": clients, "staff": [index for index in range(len(users)) if users[index] in staff],
            "reports": [report.pk for report in reports], "found": [report.pk for report in found],
            "claims": [claim.pk for claim in claims]}


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def spawn(arguments, log, env):
    options = {"creationflags": subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == "nt" else {"start_new_session": True}
    return subprocess.Popen([sys.executable, *arguments], cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT, **options)


def stop(process):
    if process.poll() is not None:
        return
    if os.name == "nt":
        subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"], check=False,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    else:
        os.killpg(process.pid, signal.SIGTERM)
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=10)


def bootstrap(base, process, clients):
    opener = build_opener(ProxyHandler({}))
    deadline = time.monotonic() + 30
    while True:
        if process.poll() is not None:
            raise RuntimeError("The owned test server exited before readiness.")
        try:
            with opener.open(f"{base}/api/v1/auth/csrf/", timeout=1) as response:
                if response.status == 200:
                    break
        except (URLError, TimeoutError):
            if time.monotonic() >= deadline:
                raise RuntimeError("The owned test server did not become ready.") from None
            time.sleep(0.1)
    for client in clients:
        request = Request(f"{base}/api/v1/auth/csrf/", headers={"Cookie": client["cookie"]})
        with opener.open(request, timeout=5) as response:
            client["csrf"] = json.loads(response.read())["csrf_token"]
            csrf_cookie = next(value.split(";", 1)[0] for value in response.headers.get_all("Set-Cookie", [])
                               if value.startswith("csrftoken="))
            client["cookie"] += f"; {csrf_cookie}"


def plan_request(sequence, data, rng, staff_sequence):
    client = data["clients"][rng.randrange(len(data["clients"]))]
    slot = sequence % 10
    payload = None
    if slot < 7:
        group = "reads"
        subtype = sequence % 3
        if subtype == 0:
            path = "/api/v1/reports/?q=wallet&location=Library&page_size=20"
        elif subtype == 1:
            path = f"/api/v1/reports/{rng.choice(data['reports'])}/"
        else:
            path = f"/api/v1/claims/{rng.choice(client['claims'])}/"
    elif slot < 9:
        group = "writes"
        subtype = (sequence // 10) % 3
        if subtype == 0:
            path = "/api/v1/reports/"
            payload = {"report_type": "found", "item_name": f"Load submitted {sequence}", "category": "Electronics",
                       "description": "Synthetic HTTP submission", "location": "Library", "event_date": "2026-11-24",
                       "event_time": "10:00", "contact_name": "Load user", "contact_email": "load@example.invalid"}
        elif subtype == 1:
            path = f"/api/v1/reports/{rng.choice(data['found'])}/claims/"
            payload = {"evidence": "Synthetic ownership marks"}
        else:
            client = data["clients"][rng.choice(data["staff"])]
            claim_id = data["claims"][next(staff_sequence)]
            path = f"/api/v1/staff/claims/{claim_id}/actions/"
            payload = {"action": "request_evidence", "reason": "Synthetic review needs another ownership mark"}
    else:
        group = "progress"
        path = "/api/v1/claims/" if (sequence // 10) % 2 else "/api/v1/notices/"
    return {"group": group, "path": path, "payload": payload, "client": client}


def request_once(base, planned, scheduled_at):
    started = time.monotonic()
    payload = planned["payload"]
    client = planned["client"]
    headers = {"Cookie": client["cookie"], "X-CSRFToken": client["csrf"], "Content-Type": "application/json"}
    target = urlsplit(base)
    if target.scheme != "http" or target.hostname != "127.0.0.1" or target.username or target.password:
        raise ValueError("Load requests require the owned loopback HTTP server.")
    connection = HTTPConnection(target.hostname, target.port, timeout=5)
    status, error, proof = 0, None, None
    try:
        connection.request("POST" if payload is not None else "GET", planned["path"],
                           body=json.dumps(payload).encode() if payload is not None else None, headers=headers)
        response = connection.getresponse()
        status = response.status
        body = response.read(2 * 1024 * 1024 + 1)
        if len(body) > 2 * 1024 * 1024:
            error = "ResponseTooLarge"
        elif 200 <= status < 300:
            if response.getheader("Content-Type", "").split(";", 1)[0] != "application/json":
                raise ValueError("Invalid response content type.")
            proof = validate_response(planned, json.loads(body), status)
    except (OSError, TimeoutError, HTTPException, TypeError, ValueError) as exc:
        error = type(exc).__name__
    finally:
        connection.close()
    finished = time.monotonic()
    return {"group": planned["group"], "status": status, "error": error, "user": client["id"],
            "seconds": finished - scheduled_at, "http_seconds": finished - started, "queue_seconds": started - scheduled_at,
            "proof": proof}


def validate_response(planned, value, status):
    if not isinstance(value, dict):
        raise TypeError("Expected a JSON object.")
    path, payload = planned["path"], planned["payload"]
    if payload is None:
        target = urlsplit(path).path.rstrip("/").split("/")[-1]
        if target.isdigit():
            if value.get("id") != int(target):
                raise ValueError("Unexpected resource identifier.")
        elif not isinstance(value.get("results"), list) or not isinstance(value.get("count"), int):
            raise ValueError("Invalid paginated response.")
        return None
    identifier = value.get("id")
    if type(identifier) is not int or identifier <= 0:
        raise ValueError("Invalid write identifier.")
    if path == "/api/v1/reports/":
        kind, expected_status = "report_submitted", "pending"
        expected = {"item_name": payload["item_name"]}
    elif path.startswith("/api/v1/staff/claims/"):
        kind, expected_status = "evidence_requested", "awaiting_evidence"
        expected = {"reason": payload["reason"]}
        if identifier != int(path.rstrip("/").split("/")[-2]):
            raise ValueError("Review returned a different claim.")
    else:
        kind, expected_status = "claim_submitted", "pending"
        expected = {"report_id": int(path.rstrip("/").split("/")[-2]), "evidence": payload["evidence"]}
    if status != (200 if kind == "evidence_requested" else 201) or value.get("status") != expected_status:
        raise ValueError("Unexpected write outcome.")
    return {"kind": kind, "id": identifier, "actor": planned["client"]["id"], **expected}


def verify_writes(proofs):
    from django.db.models import Q

    from apps.reports.models import ActionHistory, Claim, Report

    report_ids = [proof["id"] for proof in proofs if proof["kind"] == "report_submitted"]
    claim_ids = [proof["id"] for proof in proofs if proof["kind"] != "report_submitted"]
    reports = {row["id"]: row for row in Report.objects.filter(pk__in=report_ids).values("id", "created_by_id", "status", "item_name")}
    claims = {row["id"]: row for row in Claim.objects.filter(pk__in=claim_ids).values(
        "id", "claimant_id", "report_id", "status", "evidence", "current_reason", "reviewed_by_id",
    )}
    audits = set(ActionHistory.objects.filter(
        Q(report_id__in=report_ids, action="report_submitted") | Q(claim_id__in=claim_ids),
    ).values_list("action", "actor_id", "report_id", "claim_id"))
    failures, seen = 0, set()
    for proof in proofs:
        identity = (proof["kind"], proof["id"])
        valid = identity not in seen
        seen.add(identity)
        if proof["kind"] == "report_submitted":
            row = reports.get(proof["id"], {})
            valid &= row.get("created_by_id") == proof["actor"] and row.get("status") == "pending" and row.get("item_name") == proof["item_name"]
            audit = (proof["kind"], proof["actor"], proof["id"], None)
        else:
            row = claims.get(proof["id"], {})
            if proof["kind"] == "claim_submitted":
                valid &= (row.get("claimant_id") == proof["actor"] and row.get("status") == "pending"
                          and row.get("report_id") == proof["report_id"] and row.get("evidence") == proof["evidence"])
            else:
                valid &= (row.get("reviewed_by_id") == proof["actor"] and row.get("status") == "awaiting_evidence"
                          and row.get("current_reason") == proof["reason"])
            audit = (proof["kind"], proof["actor"], row.get("report_id"), proof["id"])
        failures += not (valid and audit in audits)
    return {"acknowledged": len(proofs), "mismatches": failures, "ok": bool(proofs) and not failures}


def percentile(values, fraction=0.95):
    if not values:
        return None
    return sorted(values)[max(0, math.ceil(len(values) * fraction) - 1)]


def summarize(results, duration, rps, elapsed=None, expected_users=None):
    failed = [result for result in results if result["error"] or not 200 <= result["status"] < 300]
    groups = {}
    for group in ("reads", "writes", "progress"):
        selected = [result for result in results if result["group"] == group]
        groups[group] = {"requests": len(selected), "p95_seconds": percentile([result["seconds"] for result in selected]),
                         "http_p95_seconds": percentile([result["http_seconds"] for result in selected]),
                         "queue_p95_seconds": percentile([result["queue_seconds"] for result in selected])}
    rate = len(failed) / len(results) if results else 1
    elapsed = max(duration, elapsed or duration)
    achieved = (len(results) - len(failed)) / elapsed
    active_users = len({row["user"] for row in results})
    rate_ok = achieved >= rps * 0.99
    users_ok = expected_users is None or active_users == expected_users
    latencies_ok = all(groups[group]["p95_seconds"] is not None and groups[group]["p95_seconds"] <= limit
                       for group, limit in (("reads", 2), ("writes", 3), ("progress", 2)))
    return {"scheduled_requests": len(results), "target_rps": rps, "duration_seconds": duration,
            "elapsed_seconds": elapsed, "achieved_rps": achieved, "active_users": active_users,
            "rate_target_met": rate_ok, "user_target_met": users_ok,
            "status_counts": dict(Counter(str(row["status"]) for row in results)),
            "error_counts": dict(Counter(row["error"] for row in results if row["error"])),
            "failure_rate": rate, "groups": groups, "targets_met": rate < 0.01 and latencies_ok and rate_ok and users_ok}


def run_phase(base, data, tier, seconds, phase, sequence_start, staff_sequence):
    rng = random.Random(20261124 + sequence_start)
    results, pending = [], set()
    started = time.monotonic()
    count = math.floor(seconds * tier["rps"])
    with ThreadPoolExecutor(max_workers=tier["users"]) as executor:
        for offset in range(count):
            scheduled = started + offset / tier["rps"]
            remaining = scheduled - time.monotonic()
            if remaining > 0:
                time.sleep(remaining)
            done = {future for future in pending if future.done()}
            results.extend(future.result() for future in done)
            pending -= done
            planned = plan_request(sequence_start + offset, data, rng, staff_sequence)
            if len(pending) >= tier["users"]:
                results.append({"group": planned["group"], "status": 0, "error": "ClientBackpressure",
                                "user": planned["client"]["id"], "seconds": 5, "http_seconds": 0,
                                "queue_seconds": max(0, time.monotonic() - scheduled)})
            else:
                pending.add(executor.submit(request_once, base, planned, scheduled))
            if offset and offset % (tier["rps"] * 60) == 0:
                print(json.dumps({"phase": phase, "scheduled": offset, "elapsed_seconds": round(time.monotonic() - started, 1)}), flush=True)
        results.extend(future.result() for future in pending)
    summary = summarize(results, seconds, tier["rps"], time.monotonic() - started,
                        expected_users=tier["users"] if seconds >= 900 else None)
    return summary, sequence_start + count, [row["proof"] for row in results if row.get("proof")]


def notification_metrics(started, finished):
    from apps.reports.models import NotificationEvent

    events = NotificationEvent.objects.filter(created_at__gte=started, created_at__lt=finished)
    deadline = time.monotonic() + 12
    while events.filter(delivered_at=None).exists() and time.monotonic() < deadline:
        time.sleep(0.2)
    rows = list(events.values_list("created_at", "delivered_at"))
    pending = sum(delivered is None for _, delivered in rows)
    latency = [(delivered - created).total_seconds() for created, delivered in rows if delivered is not None]
    p95 = percentile(latency)
    return {"events": len(rows), "pending": pending, "p95_seconds": p95,
            "target_met": bool(rows) and not pending and p95 is not None and p95 <= 10}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tier", choices=TIERS, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--warmup-seconds", type=int, default=300)
    parser.add_argument("--seconds", type=int, default=900)
    options = parser.parse_args()
    if not 0 <= options.warmup_seconds <= 300 or not 1 <= options.seconds <= 900:
        parser.error("Use warmup 0..300 seconds and measurement 1..900 seconds.")
    validate_environment(os.environ)
    output = options.output.resolve()
    if output.exists():
        raise ValueError("Refusing to overwrite an existing load result.")
    work = output.with_suffix("").with_name(output.stem + "-files")
    work.mkdir(parents=True, exist_ok=False)
    os.environ.update(DEBUG="False", ALLOWED_HOSTS="127.0.0.1", RECOVERY_STATE_FILE="", DB_CONN_MAX_AGE="60",
                      MEDIA_ROOT=str(work / "public"), PRIVATE_MEDIA_ROOT=str(work / "private"),
                      OPS_STATE_DIR=str(work / "ops"), REQUEST_LIMITS_KEY_PREFIX=f"campus-load-{uuid4().hex}")
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
    sys.path.insert(0, str(ROOT))
    import django
    django.setup()
    from itertools import count

    from django.conf import settings

    tier = TIERS[options.tier]
    data = seed(tier, work)
    print(json.dumps({"phase": "seeded", "tier": options.tier, **tier}), flush=True)
    processes = []
    report = {"tier": options.tier, "dataset": tier, "database": os.environ["DB_NAME"],
              "database_connection_max_age_seconds": settings.DATABASES["default"]["CONN_MAX_AGE"],
              "full_duration": options.warmup_seconds == 300 and options.seconds == 900,
              "media_capacity_verified": False, "media_note": "Three small shared PNG fixtures; transfer and 100/200/400 GiB storage tiers are not measured.",
              "deployment_acceptance": False, "request_limits": settings.REQUEST_LIMITS,
              "ip_request_limits": settings.REQUEST_LIMITS_IP,
              "ip_endpoint_multiplier": settings.REQUEST_LIMITS_IP_ENDPOINT_MULTIPLIER}
    try:
        with ExitStack() as stack:
            port = free_port()
            server = spawn(["scripts/serve.py", "--port", str(port)], stack.enter_context((work / "server.log").open("w")), os.environ.copy())
            processes.append(server)
            worker = spawn(["manage.py", "deliver_notifications", "--interval", "1"], stack.enter_context((work / "worker.log").open("w")), os.environ.copy())
            processes.append(worker)
            report["process_ids"] = {"server": server.pid, "worker": worker.pid}
            base = f"http://127.0.0.1:{port}"
            bootstrap(base, server, data["clients"])
            staff_sequence = count()
            sequence = 0
            proofs = []
            if options.warmup_seconds:
                report["warmup"], sequence, proofs = run_phase(base, data, tier, options.warmup_seconds, "warmup", sequence, staff_sequence)
            measurement_start = datetime.now(UTC)
            report["measurement"], _, measured_proofs = run_phase(base, data, tier, options.seconds, "measurement", sequence, staff_sequence)
            report["notifications"] = notification_metrics(measurement_start, datetime.now(UTC))
            report["writes"] = verify_writes(proofs + measured_proofs)
            report["processes_alive"] = server.poll() is None and worker.poll() is None
            report["observed_exit_codes"] = {"server": server.poll(), "worker": worker.poll()}
            report["local_targets_met"] = (report["measurement"]["targets_met"] and report["notifications"]["target_met"]
                                           and report["writes"]["ok"] and report["processes_alive"])
    except Exception as exc:
        report["error"] = type(exc).__name__
        report["local_targets_met"] = False
        raise
    finally:
        for process in reversed(processes):
            stop(process)
        report["owned_processes_stopped"] = all(process.poll() is not None for process in processes)
        output.write_text(json.dumps(report, indent=2) + "\n", encoding="ascii")
    print(json.dumps({"phase": "finished", "local_targets_met": report["local_targets_met"], "output": str(output)}), flush=True)
    return 0 if report["local_targets_met"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
