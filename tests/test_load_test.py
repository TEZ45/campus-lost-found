import itertools
import random
from io import BytesIO
from unittest.mock import MagicMock, Mock

import pytest
from PIL import Image

from scripts import load_test


def environment(**overrides):
    return {"APP_ENV": "test", "DB_ENGINE": "postgresql", "DB_NAME": "test_campus_load_smoke",
            "DB_HOST": "127.0.0.1", "REQUEST_LIMITS_REDIS_URL": "redis://127.0.0.1:6379/0", **overrides}


@pytest.mark.parametrize("overrides", [
    {"APP_ENV": "production"}, {"DB_ENGINE": "sqlite"}, {"DB_NAME": "campus"},
    {"DB_NAME": "test_campus_load_bad-name"}, {"DB_HOST": "remote.example.com"},
    {"REQUEST_LIMITS_REDIS_URL": "redis://remote.example.com/0"}, {"REQUEST_LIMITS_REDIS_URL": ""},
])
def test_load_harness_refuses_nonisolated_services(overrides):
    with pytest.raises(ValueError):
        load_test.validate_environment(environment(**overrides))


def test_load_harness_accepts_isolated_services():
    load_test.validate_environment(environment())


@pytest.mark.django_db
def test_load_harness_refuses_populated_database(users):
    with pytest.raises(ValueError, match="populated"):
        load_test.assert_empty_database()


def test_request_mix_and_staff_actions_are_deterministic_and_unique():
    data = {"clients": [{"id": index, "claims": [index + 100]} for index in range(50)],
            "staff": [0, 10, 20, 30, 40], "reports": [1, 2], "found": [1], "claims": list(range(100, 1000))}
    sequence = itertools.count()
    rng = random.Random(7)
    requests = [load_test.plan_request(index, data, rng, sequence) for index in range(300)]
    assert [sum(item["group"] == group for item in requests) for group in ("reads", "writes", "progress")] == [210, 60, 30]
    staff = [item for item in requests if item["path"].startswith("/api/v1/staff/")]
    assert len({item["path"] for item in staff}) == len(staff)
    assert all(item["client"]["id"] in data["staff"] for item in staff)


def result(group="reads", **overrides):
    return {"group": group, "status": 200, "error": None, "user": 1,
            "seconds": 0.1, "http_seconds": 0.05, "queue_seconds": 0.05, **overrides}


def test_rate_limits_and_backpressure_are_failures_not_successes():
    rows = [result("reads"), result("writes", status=429), result("progress", status=0, error="ClientBackpressure")]
    summary = load_test.summarize(rows, 1, 3)
    assert summary["failure_rate"] == 2 / 3
    assert summary["achieved_rps"] == 1
    assert not summary["targets_met"]
    assert summary["status_counts"]["429"] == 1
    assert summary["error_counts"]["ClientBackpressure"] == 1


def test_latency_includes_generator_queue_delay_and_requires_every_group():
    rows = [result("reads", seconds=3, queue_seconds=2.95), result("writes"), result("progress")]
    assert not load_test.summarize(rows, 1, 3)["targets_met"]
    assert not load_test.summarize([result()], 1, 1)["targets_met"]


def test_completed_requests_must_meet_rate_after_queue_drain():
    rows = [result("reads"), result("writes"), result("progress")]
    summary = load_test.summarize(rows, 1, 3, elapsed=3)
    assert summary["achieved_rps"] == 1
    assert not summary["rate_target_met"]
    assert not summary["targets_met"]


def test_full_duration_requires_all_configured_users():
    rows = [result("reads"), result("writes"), result("progress")]
    summary = load_test.summarize(rows, 1, 3, expected_users=2)
    assert summary["active_users"] == 1
    assert not summary["user_target_met"]
    assert not summary["targets_met"]


def test_generator_bounds_pending_work_and_counts_backpressure(monkeypatch):
    executor = MagicMock()
    executor.__enter__.return_value = executor
    future = Mock()
    future.done.return_value = False
    future.result.return_value = result()
    executor.submit.return_value = future
    monkeypatch.setattr(load_test, "ThreadPoolExecutor", Mock(return_value=executor))
    monkeypatch.setattr(load_test.time, "monotonic", lambda: 0)
    monkeypatch.setattr(load_test.time, "sleep", lambda seconds: None)
    data = {"clients": [{"id": 1, "claims": [1]}], "staff": [0],
            "reports": [1], "found": [1], "claims": [1]}
    summary, sequence, proofs = load_test.run_phase(
        "http://127.0.0.1:8123", data, {"users": 1, "rps": 10}, 1, "measurement", 0, itertools.count(),
    )
    assert executor.submit.call_count == 1
    assert summary["scheduled_requests"] == sequence == 10
    assert summary["error_counts"] == {"ClientBackpressure": 9}
    assert summary["failure_rate"] == 0.9
    assert not summary["targets_met"]
    assert proofs == []


@pytest.mark.django_db
def test_successful_http_writes_are_verified_against_saved_rows_and_audits(users):
    from rest_framework.test import APIClient

    from apps.reports.models import Report

    from .test_api import report_data

    client = APIClient()
    client.force_login(users["owner"])
    planned = {"path": "/api/v1/reports/", "payload": report_data(), "client": {"id": users["owner"].pk}}
    response = client.post(planned["path"], planned["payload"], format="json")
    proof = load_test.validate_response(planned, response.json(), response.status_code)
    assert load_test.verify_writes([proof]) == {"acknowledged": 1, "mismatches": 0, "ok": True}
    assert not load_test.verify_writes([proof, proof])["ok"]
    Report.objects.filter(pk=proof["id"]).update(item_name="Unexpected saved value")
    assert load_test.verify_writes([proof]) == {"acknowledged": 1, "mismatches": 1, "ok": False}


@pytest.mark.parametrize("response,status", [
    ({"id": 1, "status": "approved"}, 201),
    ({"id": 1, "status": "pending"}, 200),
    ({"id": True, "status": "pending"}, 201),
])
def test_unexpected_success_response_is_not_accepted_as_a_write(response, status):
    planned = {"path": "/api/v1/reports/", "payload": {"item_name": "Synthetic"}, "client": {"id": 1}}
    with pytest.raises(ValueError):
        load_test.validate_response(planned, response, status)


def test_malformed_success_body_is_counted_as_a_failed_request(monkeypatch):
    response = Mock(status=200)
    response.getheader.return_value = "application/json"
    response.read.return_value = b"[]"
    connection = Mock()
    connection.getresponse.return_value = response
    monkeypatch.setattr(load_test, "HTTPConnection", Mock(return_value=connection))
    planned = {"group": "reads", "path": "/api/v1/reports/", "payload": None,
               "client": {"id": 1, "cookie": "sessionid=test", "csrf": "test"}}
    row = load_test.request_once("http://127.0.0.1:8123", planned, load_test.time.monotonic())
    assert row["status"] == 200
    assert row["error"] == "TypeError"
    assert row["proof"] is None
    connection.close.assert_called_once()


@pytest.mark.parametrize("base", ["https://127.0.0.1:8123", "http://example.com", "http://user@127.0.0.1"])
def test_http_requests_cannot_leave_the_owned_loopback_server(monkeypatch, base):
    connection = Mock()
    monkeypatch.setattr(load_test, "HTTPConnection", connection)
    planned = {"payload": None, "client": {"cookie": "sessionid=test", "csrf": "test"}}
    with pytest.raises(ValueError):
        load_test.request_once(base, planned, load_test.time.monotonic())
    connection.assert_not_called()


def test_percentile_uses_nearest_rank_and_handles_empty_samples():
    assert load_test.percentile(list(range(1, 101))) == 95
    assert load_test.percentile([]) is None


def test_load_media_fixture_is_a_valid_png():
    image = Image.open(BytesIO(load_test.PNG))
    assert image.format == "PNG"
    image.verify()
