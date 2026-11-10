import pytest
from django.db import connection
from django.db.migrations.executor import MigrationExecutor


@pytest.mark.django_db(transaction=True)
def test_reservation_migration_preserves_existing_conflicts():
    previous = [("reports", "0003_userprofile")]
    current = [("reports", "0004_secure_claim_reservations")]
    executor = MigrationExecutor(connection)
    executor.migrate(previous)
    apps = executor.loader.project_state(previous).apps
    Report = apps.get_model("reports", "Report")
    Claim = apps.get_model("reports", "Claim")
    report = Report.objects.create(
        report_type="found", item_name="Wallet", location="Library",
        event_date="2026-10-01", contact_name="Staff", status="approved",
    )
    first = Claim.objects.create(
        report=report, claimant_name="One", claimant_email="one@example.com",
        evidence="Proof", status="awaiting_handover",
    )
    second = Claim.objects.create(
        report=report, claimant_name="Two", claimant_email="two@example.com",
        evidence="Proof", status="under_dispute",
    )
    try:
        with pytest.raises(RuntimeError, match="Resolve duplicate reservations"):
            MigrationExecutor(connection).migrate(current)
        first.refresh_from_db()
        second.refresh_from_db()
        assert (first.status, second.status) == ("awaiting_handover", "under_dispute")
    finally:
        Claim.objects.filter(pk=second.pk).update(status="withdrawn")
        MigrationExecutor(connection).migrate([("reports", "0006_notification_outbox_and_confirmations")])
