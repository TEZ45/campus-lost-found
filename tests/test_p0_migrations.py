import pytest
from django.db import connection
from django.db.migrations.executor import MigrationExecutor

from apps.reports.models import ActionHistory, Notice, NotificationEvent


@pytest.mark.django_db(transaction=True)
def test_outbox_migration_preserves_history_and_existing_notices():
    previous = [("reports", "0005_protect_action_history")]
    current = [("reports", "0006_notification_outbox_and_confirmations")]
    executor = MigrationExecutor(connection)
    executor.migrate(previous)
    apps = executor.loader.project_state(previous).apps
    user = apps.get_model("auth", "User").objects.create(username="legacy-notice-user")
    history = apps.get_model("reports", "ActionHistory").objects.create(
        actor_id=user.pk, action="existing_event", result="Existing result",
    )
    notice = apps.get_model("reports", "Notice").objects.create(
        user_id=user.pk, title="Existing notice", is_read=True,
    )
    try:
        MigrationExecutor(connection).migrate(current)
        retained_history = ActionHistory.objects.get(pk=history.pk)
        retained_notice = Notice.objects.get(pk=notice.pk)
        assert retained_history.confirmations == {}
        assert retained_history.result == "Existing result"
        assert retained_notice.title == "Existing notice" and retained_notice.is_read
        assert retained_notice.event_id is None
        assert not NotificationEvent.objects.exists()
    finally:
        MigrationExecutor(connection).migrate(current)
