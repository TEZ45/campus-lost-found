from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest
from django.contrib.auth.models import User
from django.db import close_old_connections, connections

from apps.reports.models import Claim, Report

from .test_workflow import CONFIRMATIONS, make_claim, make_report

pytestmark = pytest.mark.django_db(transaction=True)


def race(*operations):
    barrier = Barrier(len(operations))

    def run(operation):
        model, pk, method, actor_id, args = operation
        close_old_connections()
        try:
            obj = model.objects.get(pk=pk)
            actor = User.objects.get(pk=actor_id)
            barrier.wait(timeout=10)
            try:
                kwargs = {"confirmations": CONFIRMATIONS[method]} if method in CONFIRMATIONS else {}
                getattr(obj, method)(actor, *args, **kwargs)
                return True
            except ValueError:
                return False
        finally:
            connections.close_all()

    with ThreadPoolExecutor(max_workers=len(operations)) as executor:
        return list(executor.map(run, operations))


def test_concurrent_approvals_reserve_one_claim(users):
    report = make_report(users["owner"])
    first = make_claim(report, users["claimant"])
    second = make_claim(report, users["other"])
    results = race(
        (Claim, first.pk, "approve", users["service"].pk, ()),
        (Claim, second.pk, "approve", users["service"].pk, ()),
    )
    assert sorted(results) == [False, True]
    assert report.claims.filter(status__in=Claim.RESERVED_STATES).count() == 1
    assert report.history.filter(action="approved").count() == 1


def test_concurrent_handover_and_withdrawal_have_one_winner(users):
    claim = make_claim(make_report(users["owner"]), users["claimant"])
    claim.approve(users["service"], confirmations={'evidence_reviewed': True, 'custody_confirmed': True})
    results = race(
        (Claim, claim.pk, "start_handover", users["service"].pk, ()),
        (Claim, claim.pk, "withdraw", users["claimant"].pk, ()),
    )
    assert sorted(results) == [False, True]
    claim.refresh_from_db()
    assert claim.status in ("handover_in_progress", "withdrawn")
    assert claim.history.count() == 2


def test_concurrent_handover_and_report_close_are_consistent(users):
    report = make_report(users["owner"])
    claim = make_claim(report, users["claimant"])
    claim.approve(users["service"], confirmations={'evidence_reviewed': True, 'custody_confirmed': True})
    results = race(
        (Claim, claim.pk, "start_handover", users["service"].pk, ()),
        (Report, report.pk, "review", users["service"].pk, ("close",)),
    )
    assert sorted(results) == [False, True]
    claim.refresh_from_db()
    report.refresh_from_db()
    assert (report.status, claim.status) in (
        ("approved", "handover_in_progress"), ("closed", "invalidated"),
    )


def test_concurrent_completion_records_one_handover(users):
    report = make_report(users["owner"])
    claim = make_claim(report, users["claimant"])
    other = make_claim(report, users["other"])
    claim.approve(users["service"], confirmations={'evidence_reviewed': True, 'custody_confirmed': True})
    claim.start_handover(users["service"], confirmations={'identity_verified': True, 'custody_confirmed': True})
    operation = (Claim, claim.pk, "complete_handover", users["service"].pk, ())
    assert sorted(race(operation, operation)) == [False, True]
    report.refresh_from_db()
    claim.refresh_from_db()
    other.refresh_from_db()
    assert (report.status, claim.status, other.status) == ("closed", "completed", "invalidated")
    assert claim.history.filter(action="handover_completed").count() == 1
    assert other.history.filter(action="invalidated").count() == 1


def test_concurrent_report_reviews_make_one_decision(users):
    report = make_report(users["owner"], status="pending")
    results = race(
        (Report, report.pk, "review", users["service"].pk, ("approve",)),
        (Report, report.pk, "review", users["service"].pk, ("reject", "Invalid report")),
    )
    assert sorted(results) == [False, True]
    assert report.history.count() == 1
