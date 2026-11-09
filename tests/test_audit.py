import pytest
from django.db import IntegrityError, connection, transaction
from django.db.migrations.executor import MigrationExecutor
from django.db.models.deletion import ProtectedError

from apps.reports.models import ActionHistory

from .test_workflow import make_claim, make_report


@pytest.fixture
def history(users):
    report = make_report(users["owner"])
    claim = make_claim(report, users["claimant"])
    return ActionHistory.objects.create(
        actor=users["service"], report=report, claim=claim,
        action="claim_submitted", previous_state="", new_state="pending",
        reason="Original reason", result="Original result",
    )


@pytest.mark.django_db
@pytest.mark.parametrize("operation", ["save", "delete", "update", "query_delete", "bulk_update",
                                       "upsert", "update_or_create", "base_manager_update"])
def test_history_rejects_existing_record_changes(history, operation):
    history.reason = "Changed"
    with pytest.raises(ValueError, match="append-only"):
        if operation == "save":
            history.save()
        elif operation == "delete":
            history.delete()
        elif operation == "update":
            ActionHistory.objects.filter(pk=history.pk).update(reason="Changed")
        elif operation == "query_delete":
            ActionHistory.objects.filter(pk=history.pk).delete()
        elif operation == "bulk_update":
            ActionHistory.objects.bulk_update([history], ["reason"])
        elif operation == "upsert":
            ActionHistory.objects.bulk_create(
                [ActionHistory(pk=history.pk, action="Changed")], update_conflicts=True,
                update_fields=["action"], unique_fields=["pk"],
            )
        elif operation == "update_or_create":
            ActionHistory.objects.update_or_create(pk=history.pk, defaults={"reason": "Changed"})
        else:
            ActionHistory._base_manager.filter(pk=history.pk).update(reason="Changed")
    history.refresh_from_db()
    assert (history.action, history.reason, history.result) == (
        "claim_submitted", "Original reason", "Original result",
    )


@pytest.mark.django_db
def test_new_instance_cannot_overwrite_history(history):
    replacement = ActionHistory(pk=history.pk, action="Changed")
    with pytest.raises(IntegrityError), transaction.atomic():
        replacement.save()
    history.refresh_from_db()
    assert history.action == "claim_submitted"


@pytest.mark.django_db
def test_history_allows_appending_new_records(history):
    created = ActionHistory.objects.create(action="new_event", actor=history.actor)
    ActionHistory.objects.bulk_create([ActionHistory(action="batch_event")])
    assert created.pk != history.pk
    assert set(ActionHistory.objects.values_list("action", flat=True)) == {
        "claim_submitted", "new_event", "batch_event",
    }


@pytest.mark.django_db
@pytest.mark.parametrize("relation", ["actor", "report", "claim", "claimant"])
@pytest.mark.parametrize("bulk", [False, True])
def test_history_prevents_related_record_deletion(history, relation, bulk):
    obj = history.claim.claimant if relation == "claimant" else getattr(history, relation)
    with pytest.raises(ProtectedError):
        if bulk:
            type(obj).objects.filter(pk=obj.pk).delete()
        else:
            obj.delete()
    assert type(obj).objects.filter(pk=obj.pk).exists()
    assert ActionHistory.objects.filter(pk=history.pk).exists()


@pytest.mark.django_db(transaction=True)
def test_audit_migration_preserves_existing_records():
    previous = [("reports", "0004_secure_claim_reservations")]
    current = [("reports", "0005_protect_action_history")]
    executor = MigrationExecutor(connection)
    executor.migrate(previous)
    apps = executor.loader.project_state(previous).apps
    History = apps.get_model("reports", "ActionHistory")
    entry = History.objects.create(action="existing_event", reason="Keep this record")
    try:
        MigrationExecutor(connection).migrate(current)
        preserved = ActionHistory.objects.get(pk=entry.pk)
        assert (preserved.action, preserved.reason) == ("existing_event", "Keep this record")
    finally:
        MigrationExecutor(connection).migrate(current)
