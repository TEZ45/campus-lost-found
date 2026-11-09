from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("reports", "0004_secure_claim_reservations"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AlterModelOptions(
            name="actionhistory",
            options={
                "ordering": ["-created_at"],
                "base_manager_name": "objects",
                "default_manager_name": "objects",
            },
        ),
        migrations.AlterField(
            model_name="actionhistory", name="actor",
            field=models.ForeignKey(
                settings.AUTH_USER_MODEL, on_delete=models.PROTECT, null=True, blank=True,
            ),
        ),
        migrations.AlterField(
            model_name="actionhistory", name="claim",
            field=models.ForeignKey(
                "reports.claim", on_delete=models.PROTECT, null=True, blank=True,
                related_name="history",
            ),
        ),
        migrations.AlterField(
            model_name="actionhistory", name="report",
            field=models.ForeignKey(
                "reports.report", on_delete=models.PROTECT, null=True, blank=True,
                related_name="history",
            ),
        ),
    ]
