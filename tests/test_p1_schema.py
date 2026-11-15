from datetime import timedelta

import pytest
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.utils import timezone

PREVIOUS = [("reports", "0006_notification_outbox_and_confirmations")]
CURRENT = [("reports", "0007_p1_schema_and_lifecycle")]


def legacy_state():
    executor = MigrationExecutor(connection)
    executor.migrate(PREVIOUS)
    return executor.loader.project_state(PREVIOUS).apps


def restore_latest():
    executor = MigrationExecutor(connection)
    executor.migrate(executor.loader.graph.leaf_nodes())


def legacy_report(apps, user, **changes):
    return apps.get_model("reports", "Report").objects.create(
        report_type="found", item_name="Legacy item", category="Historical category",
        location="Library", event_date="2026-10-01", contact_name="Owner",
        created_by_id=user.pk, **changes,
    )


@pytest.mark.django_db(transaction=True)
def test_p1_migration_fails_clearly_without_changing_unidentified_legacy_claim():
    apps = legacy_state()
    user = apps.get_model("auth", "User").objects.create(username="schema-legacy-owner")
    report = legacy_report(apps, user)
    Claim = apps.get_model("reports", "Claim")
    claim = Claim.objects.create(
        report_id=report.pk, claimant=None, claimant_name="Unknown account",
        claimant_email="original@example.com", evidence="Original evidence", status="pending",
    )
    try:
        with pytest.raises(RuntimeError, match="identified claimant") as error:
            MigrationExecutor(connection).migrate(CURRENT)
        assert str(claim.pk) in str(error.value)
        retained = Claim.objects.get(pk=claim.pk)
        assert retained.claimant_id is None
        assert retained.claimant_email == "original@example.com"
        assert retained.evidence == "Original evidence"
        assert "reports_category" not in connection.introspection.table_names()
        assert not MigrationExecutor(connection).recorder.migration_qs.filter(
            app="reports", name=CURRENT[0][1],
        ).exists()
    finally:
        # Explicit repair supplies the known account; the migration never guesses one.
        Claim.objects.filter(pk=claim.pk).update(claimant_id=user.pk)
        restore_latest()


@pytest.mark.django_db(transaction=True)
def test_p1_migration_preserves_legacy_unknowns_and_uses_only_reliable_final_dates():
    apps = legacy_state()
    user = apps.get_model("auth", "User").objects.create(username="schema-history-owner")
    unknown = legacy_report(apps, user, status="closed")
    known = legacy_report(apps, user, status="closed")
    blank = legacy_report(apps, user, status="approved")
    apps.get_model("reports", "Report").objects.filter(pk=blank.pk).update(category="")
    Claim = apps.get_model("reports", "Claim")
    claims = [Claim.objects.create(
        report_id=report.pk, claimant_id=user.pk, claimant_name="Known user",
        claimant_email="owner@example.com", evidence="Private original evidence", status="rejected",
    ) for report in (unknown, known)]
    History = apps.get_model("reports", "ActionHistory")
    rejected = History.objects.create(
        actor_id=user.pk, action="rejected", report_id=known.pk, claim_id=claims[1].pk,
        previous_state="pending", new_state="rejected", reason="Ownership did not match",
    )
    closed = History.objects.create(
        actor_id=user.pk, action="report_closed", report_id=known.pk,
        previous_state="approved", new_state="closed", reason="Search ended",
    )
    reliable_time = timezone.now() - timedelta(days=10)
    History.objects.filter(pk__in=[rejected.pk, closed.pk]).update(created_at=reliable_time)
    try:
        MigrationExecutor(connection).migrate(CURRENT)
        current = MigrationExecutor(connection).loader.project_state(CURRENT).apps
        Report = current.get_model("reports", "Report")
        CurrentClaim = current.get_model("reports", "Claim")
        CurrentHistory = current.get_model("reports", "ActionHistory")
        assert Report.objects.get(pk=unknown.pk).closed_at is None
        assert CurrentClaim.objects.get(pk=claims[0].pk).finalized_at is None
        assert Report.objects.get(pk=known.pk).closed_at == reliable_time
        assert CurrentClaim.objects.get(pk=claims[1].pk).finalized_at == reliable_time
        assert Report.objects.filter(event_time__isnull=False).count() == 0
        assert Report.objects.get(pk=blank.pk).category == ""
        retained = CurrentClaim.objects.get(pk=claims[1].pk)
        assert retained.claimant_id == user.pk and retained.evidence == "Private original evidence"
        assert retained.evidence_purged_at is None
        history = CurrentHistory.objects.get(pk=rejected.pk)
        assert history.reason == "Ownership did not match"
        assert history.report_reference == f"RPT-{known.pk:06d}"
        assert history.claim_reference == f"CLM-{claims[1].pk:06d}"
        categories = set(current.get_model("reports", "Category").objects.values_list("name", flat=True))
        assert {"Electronics", "Accessories", "Other", "Historical category"} <= categories
    finally:
        restore_latest()
