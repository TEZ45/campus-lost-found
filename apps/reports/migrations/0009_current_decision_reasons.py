from datetime import timedelta

from django.db import migrations, models
from django.utils import timezone


def backfill_current_reasons(apps, schema_editor):
    alias = schema_editor.connection.alias
    Claim = apps.get_model("reports", "Claim")
    Report = apps.get_model("reports", "Report")
    History = apps.get_model("reports", "ActionHistory")
    cutoff = timezone.now() - timedelta(days=15)
    final_states = {"completed", "rejected", "withdrawn", "invalidated"}
    claim_actions = {
        "approved", "rejected", "evidence_requested", "dispute_started", "dispute_resolved",
        "dispute_rejected", "handover_started", "handover_completed", "handover_interrupted",
        "withdrawn", "invalidated",
    }
    for claim in Claim.objects.using(alias).all().iterator():
        if claim.status in final_states and (
            claim.finalized_at is None or claim.finalized_at <= cutoff
            or claim.evidence_purged_at is not None
        ):
            continue
        decision = History.objects.using(alias).filter(
            claim_id=claim.pk, new_state=claim.status, action__in=claim_actions,
        ).order_by("-created_at", "-pk").first()
        if decision and decision.reason.strip():
            Claim.objects.using(alias).filter(pk=claim.pk).update(current_reason=decision.reason)
    for report in Report.objects.using(alias).all().iterator():
        if report.status == "closed" and (
            report.closed_at is None or report.closed_at <= cutoff
        ):
            continue
        decision = History.objects.using(alias).filter(
            report_id=report.pk, claim_id=None, new_state=report.status,
            action__in={"report_approved", "report_rejected", "report_closed"},
        ).order_by("-created_at", "-pk").first()
        if decision and decision.reason.strip():
            Report.objects.using(alias).filter(pk=report.pk).update(review_reason=decision.reason)


class Migration(migrations.Migration):
    dependencies = [("reports", "0008_storage_ledger_and_file_deletion")]

    operations = [
        migrations.AddField(
            model_name="claim", name="current_reason",
            field=models.TextField(blank=True, default="", max_length=2000),
        ),
        migrations.AddField(
            model_name="report", name="review_reason",
            field=models.TextField(blank=True, default="", max_length=2000),
        ),
        migrations.RunPython(backfill_current_reasons, migrations.RunPython.noop),
    ]
