import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


def require_identified_claimants(apps, schema_editor):
    Claim = apps.get_model("reports", "Claim")
    missing = Claim.objects.using(schema_editor.connection.alias).filter(claimant_id__isnull=True)
    ids = list(missing.order_by("pk").values_list("pk", flat=True)[:20])
    if ids:
        raise RuntimeError(
            "Cannot migrate claims without an identified claimant. Resolve the real account "
            f"for each legacy claim before retrying; no account was assigned. Claim IDs: {ids}"
        )


def seed_catalog_and_reliable_dates(apps, schema_editor):
    alias = schema_editor.connection.alias
    Category = apps.get_model("reports", "Category")
    Report = apps.get_model("reports", "Report")
    Claim = apps.get_model("reports", "Claim")
    History = apps.get_model("reports", "ActionHistory")
    names = {"Electronics", "Accessories", "Other"}
    names.update(
        value.strip() for value in Report.objects.using(alias).values_list("category", flat=True)
        if value and value.strip()
    )
    for name in sorted(names):
        Category.objects.using(alias).get_or_create(name=name)

    # Preserve report category text and unknown event times exactly as stored.
    # Audit timestamps and actual transfer timestamps are the only date evidence.
    claim_dates = {}
    report_dates = {}
    final_actions = {
        "completed": {"handover_completed"},
        "rejected": {"rejected", "dispute_rejected"},
        "withdrawn": {"withdrawn"},
        "invalidated": {"invalidated"},
    }
    claim_reports = dict(Claim.objects.using(alias).values_list("pk", "report_id"))
    for history in History.objects.using(alias).all().iterator():
        changes = {}
        report_id = history.report_id or claim_reports.get(history.claim_id)
        if report_id:
            changes["report_reference"] = f"RPT-{report_id:06d}"
        if history.claim_id:
            changes["claim_reference"] = f"CLM-{history.claim_id:06d}"
        if changes:
            History.objects.using(alias).filter(pk=history.pk).update(**changes)
        if history.claim_id and history.action in final_actions.get(history.new_state, set()):
            key = (history.claim_id, history.new_state)
            claim_dates[key] = max(claim_dates.get(key, history.created_at), history.created_at)
        closes_report = (
            history.action == "report_closed" and history.new_state == "closed"
        ) or (
            history.action == "handover_completed" and history.new_state == "completed"
        )
        if report_id and closes_report:
            report_dates[report_id] = max(
                report_dates.get(report_id, history.created_at), history.created_at,
            )
    for claim in Claim.objects.using(alias).filter(status__in=list(final_actions)).iterator():
        finalized_at = claim_dates.get((claim.pk, claim.status))
        if claim.status == "completed" and claim.handover_completed_at is not None:
            finalized_at = claim.handover_completed_at
            report_dates[claim.report_id] = max(
                report_dates.get(claim.report_id, finalized_at), finalized_at,
            )
        if finalized_at is not None:
            Claim.objects.using(alias).filter(pk=claim.pk).update(finalized_at=finalized_at)
    for report_id, closed_at in report_dates.items():
        Report.objects.using(alias).filter(pk=report_id, status="closed").update(closed_at=closed_at)


class Migration(migrations.Migration):
    dependencies = [
        ("reports", "0006_notification_outbox_and_confirmations"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.RunPython(require_identified_claimants, migrations.RunPython.noop),
        migrations.CreateModel(
            name="Category",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("name", models.CharField(max_length=100, unique=True)),
                ("is_active", models.BooleanField(default=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
            ],
            options={"ordering": ["name"], "verbose_name_plural": "categories"},
        ),
        migrations.CreateModel(
            name="ResourceLease",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("key", models.CharField(max_length=100, unique=True)),
                ("token", models.CharField(max_length=64)),
                ("expires_at", models.DateTimeField(db_index=True)),
            ],
        ),
        migrations.AlterField(
            model_name="claim", name="claimant",
            field=models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="claims", to=settings.AUTH_USER_MODEL),
        ),
        migrations.AlterField(model_name="report", name="category", field=models.CharField(max_length=100)),
        migrations.AlterField(model_name="report", name="description", field=models.TextField(blank=True, max_length=10000)),
        migrations.AlterField(model_name="claim", name="evidence", field=models.TextField(help_text="Description proving ownership", max_length=10000)),
        migrations.AlterField(model_name="actionhistory", name="reason", field=models.TextField(blank=True, default="", max_length=2000)),
        migrations.AddField(model_name="report", name="event_time", field=models.TimeField(blank=True, help_text="Time the item was lost or found", null=True)),
        migrations.AddField(model_name="report", name="closed_at", field=models.DateTimeField(blank=True, db_index=True, null=True)),
        migrations.AddField(model_name="claim", name="finalized_at", field=models.DateTimeField(blank=True, db_index=True, null=True)),
        migrations.AddField(model_name="claim", name="evidence_purged_at", field=models.DateTimeField(blank=True, db_index=True, null=True)),
        migrations.AddField(model_name="actionhistory", name="report_reference", field=models.CharField(blank=True, default="", max_length=100)),
        migrations.AddField(model_name="actionhistory", name="claim_reference", field=models.CharField(blank=True, default="", max_length=100)),
        migrations.RunPython(seed_catalog_and_reliable_dates, migrations.RunPython.noop),
    ]
