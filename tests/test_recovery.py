import io
import json
import os
from pathlib import Path
from unittest.mock import patch

import pytest
from django.core.exceptions import PermissionDenied
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from rest_framework.test import APIClient

from apps.reports import services
from apps.reports.models import Claim
from apps.reports.recovery import (
    RecoveryPaused,
    _operator_lock,
    assert_collection_open,
    change_recovery_state,
    read_recovery_state,
    recovery_status,
)

from .test_confirmations import CASES, database_snapshot, invoke, ready_claim
from .test_workflow import make_claim, make_report

pytestmark = pytest.mark.django_db


def change(paused, **overrides):
    return change_recovery_state(
        **{
            "paused": paused, "operator": "staff-operations", "reference": "custody-check-2026-11-24",
            "quiesced": True, "custody_reconciled": not paused, **overrides,
        }
    )


@pytest.fixture
def recovery_file(settings, tmp_path):
    directory = tmp_path / "control"
    directory.mkdir()
    path = directory / "recovery.json"
    settings.RECOVERY_STATE_FILE = str(path)
    change(True)
    change(False)
    return path


@pytest.mark.parametrize("action", CASES)
@pytest.mark.parametrize("entry", ["model", "api", "html"])
def test_recovery_pause_blocks_claim_transitions_without_mutation(
    users, recovery_file, action, entry,
):
    claim = ready_claim(users, action)
    change(True)
    before = database_snapshot()
    if entry == "model":
        with pytest.raises(RecoveryPaused):
            invoke(claim, users["service"], action, CASES[action][3])
    else:
        client = APIClient()
        client.force_login(users["service"])
        if entry == "api":
            response = client.post(reverse("reports_api:claim-action", args=[claim.pk]), {
                "action": action, "reason": "Recovery check", "confirmations": CASES[action][3],
            }, format="json")
            assert response.status_code == 409
            assert response.json()["error"]["code"] == "state_conflict"
        else:
            payload = {"action": action, "reason": "Recovery check"}
            payload.update({key: "on" if value is True else value for key, value in CASES[action][3].items()})
            response = client.post(reverse("staff_claim_review", args=[claim.pk]), payload, follow=True)
            assert response.status_code == 200
            assert b"Collection is paused" in response.content
    assert database_snapshot() == before


@pytest.mark.parametrize("action,state,role", [
    ("withdraw", "awaiting_handover", "claimant"),
    ("invalidate", "awaiting_handover", "service"),
    ("reject", "pending", "service"),
    ("request_evidence", "pending", "service"),
    ("resubmit_evidence", "under_dispute", "claimant"),
])
def test_other_claim_transitions_remain_paused(users, recovery_file, action, state, role):
    claim = make_claim(make_report(users["owner"]), users["claimant"], status=state)
    change(True)
    before = database_snapshot()
    with pytest.raises(RecoveryPaused):
        if action == "resubmit_evidence":
            claim.resubmit_evidence("Additional evidence", actor=users[role])
        else:
            getattr(claim, action)(users[role], "Recovery check")
    assert database_snapshot() == before


@pytest.mark.parametrize("action,state", [("approve", "pending"), ("reject", "pending"), ("close", "approved")])
def test_report_reviews_cannot_change_state_or_release_reservations(users, recovery_file, action, state):
    report = make_report(users["owner"], status=state)
    make_claim(report, users["claimant"], status="awaiting_handover")
    change(True)
    before = database_snapshot()
    with pytest.raises(RecoveryPaused):
        services.review_report(users["service"], report.pk, action, "Recovery check")
    assert database_snapshot() == before


def test_pause_survives_restored_claim_state_and_requires_explicit_custody_resume(users, recovery_file):
    claim = ready_claim(users, "start_handover")
    change(True)
    Claim.objects.filter(pk=claim.pk).update(status="awaiting_handover")
    restored = Claim.objects.get(pk=claim.pk)
    with pytest.raises(RecoveryPaused):
        restored.start_handover(users["service"], confirmations=CASES["start_handover"][3])
    with pytest.raises(ValueError, match="custody-reconciled"):
        change(False, custody_reconciled=False)
    change(False)
    restored.start_handover(users["service"], confirmations=CASES["start_handover"][3])
    assert restored.status == "handover_in_progress"


def test_reads_remain_available_and_actor_authorization_still_runs_first(users, recovery_file):
    claim = ready_claim(users, "approve")
    change(True)
    with pytest.raises(PermissionDenied):
        claim.approve(users["admin"], confirmations=CASES["approve"][3])
    with pytest.raises(PermissionDenied):
        claim.report.review(users["other"], "close", "No authority")
    client = APIClient()
    client.force_login(users["claimant"])
    assert client.get(reverse("reports_api:claim", args=[claim.pk])).status_code == 200
    assert client.get(reverse("reports_api:claim-history", args=[claim.pk])).status_code == 200


@pytest.mark.parametrize("data", [b"", b"invalid", b"[]", b"x" * 4097, b'{"paused":false}',
                                  b'{"version":true,"paused":false,"event_id":"bad"}'])
def test_invalid_external_state_fails_closed(recovery_file, data):
    recovery_file.write_bytes(data)
    assert recovery_status()["valid"] is False
    with pytest.raises(RecoveryPaused):
        assert_collection_open()


def test_missing_or_unreadable_state_fails_closed(recovery_file):
    recovery_file.unlink()
    assert recovery_status()["paused"] is True
    with patch.object(Path, "open", side_effect=PermissionError):
        assert recovery_status()["paused"] is True


@pytest.mark.parametrize("mutation", ["missing", "mismatch", "integer_boolean", "naive_timestamp"])
def test_open_state_requires_valid_matching_immutable_event(recovery_file, mutation):
    state = json.loads(recovery_file.read_text())
    event_path = recovery_file.with_name(recovery_file.name + ".events") / f"{state['event_id']}.json"
    if mutation == "missing":
        event_path.unlink()
    else:
        event = json.loads(event_path.read_text())
        if mutation == "mismatch":
            event["paused"] = True
        elif mutation == "integer_boolean":
            event["paused"] = 0
        else:
            event["recorded_at"] = "2026-11-24T00:00:00"
        event_path.write_text(json.dumps(event))
    assert recovery_status()["valid"] is False
    assert recovery_status()["paused"] is True


def test_recovery_control_is_independent_of_database_and_preserves_events(recovery_file):
    events = recovery_file.with_name(recovery_file.name + ".events")
    before = {path.name: path.read_bytes() for path in events.iterdir()}
    with CaptureQueriesContext(connection) as queries:
        paused = change(True)
        status = read_recovery_state(recovery_file)
    assert not queries
    assert status["paused"] is True
    assert status["event_id"] == paused["event_id"]
    assert all((events / name).read_bytes() == value for name, value in before.items())
    assert len(list(events.iterdir())) == len(before) + 1
    assert set(paused) == {"configured", "paused", "valid", "event_id"}
    if os.name != "nt":
        assert recovery_file.stat().st_mode & 0o777 == 0o640
        assert events.stat().st_mode & 0o2777 == 0o2750
        assert all(path.stat().st_mode & 0o777 == 0o640 for path in events.iterdir())


def test_failed_state_replace_keeps_existing_pause(recovery_file):
    change(True)
    before = recovery_file.read_bytes()
    with patch("apps.reports.recovery.os.replace", side_effect=OSError("disk unavailable")), pytest.raises(OSError):
        change(False)
    assert recovery_file.read_bytes() == before
    assert recovery_status()["paused"] is True
    assert not list(recovery_file.parent.glob("*.tmp"))


@pytest.mark.parametrize("options", [
    {"quiesced": False}, {"operator": ""}, {"reference": ""},
    {"reference": "line\nbreak"}, {"operator": "a" * 201},
])
def test_control_requires_quiescence_operator_and_reference(recovery_file, options):
    before = recovery_file.read_bytes()
    with pytest.raises(ValueError):
        change(True, **options)
    assert recovery_file.read_bytes() == before


def test_concurrent_operator_change_is_rejected(recovery_file):
    before = recovery_file.read_bytes()
    with _operator_lock(recovery_file), pytest.raises(OSError):
        change(True)
    assert recovery_file.read_bytes() == before


def test_command_exposes_safe_status_and_requires_manual_resume(recovery_file):
    output = io.StringIO()
    call_command("recovery_control", "pause", quiesced=True, operator="operator-1", reference="restore-1", stdout=output)
    assert json.loads(output.getvalue())["paused"] is True
    with pytest.raises(CommandError):
        call_command("recovery_control", "resume", quiesced=True, operator="operator-1", reference="review-1")
    output = io.StringIO()
    call_command("recovery_control", "resume", quiesced=True, custody_reconciled=True,
                 operator="operator-1", reference="review-1", stdout=output)
    assert json.loads(output.getvalue())["paused"] is False


@pytest.mark.parametrize("profile,paused", [("development", False), ("test", False), ("production", True)])
def test_unconfigured_control_fails_closed_only_in_production(settings, profile, paused):
    settings.APP_ENV = profile
    settings.RECOVERY_STATE_FILE = ""
    assert recovery_status()["paused"] is paused


@pytest.mark.parametrize("location", ["relative", "application", "public", "private", "database"])
def test_control_cannot_overwrite_managed_data(settings, tmp_path, location, monkeypatch):
    path = {
        "relative": "recovery.json", "application": Path(settings.BASE_DIR) / "recovery.json",
        "public": Path(settings.MEDIA_ROOT) / "recovery.json",
        "private": Path(settings.PRIVATE_MEDIA_ROOT) / "recovery.json",
        "database": tmp_path / "database.sqlite3",
    }[location]
    if location == "database":
        monkeypatch.setitem(settings.DATABASES, "default", {
            "ENGINE": "django.db.backends.sqlite3", "NAME": str(path),
        })
    settings.RECOVERY_STATE_FILE = str(path)
    with pytest.raises(ValueError):
        change(True)
