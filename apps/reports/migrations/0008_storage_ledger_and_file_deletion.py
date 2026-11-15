from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("reports", "0007_p1_schema_and_lifecycle")]

    operations = [
        migrations.CreateModel(
            name="StorageLedger",
            fields=[
                ("id", models.PositiveSmallIntegerField(default=1, editable=False, primary_key=True, serialize=False)),
                ("used_bytes", models.PositiveBigIntegerField(default=0)),
                ("initialized", models.BooleanField(default=False)),
                ("updated_at", models.DateTimeField(auto_now=True)),
            ],
            options={"constraints": [models.CheckConstraint(condition=models.Q(id=1), name="one_storage_ledger")]},
        ),
        migrations.CreateModel(
            name="FileDeletion",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("storage", models.CharField(choices=[("public", "Public"), ("private", "Private")], max_length=10)),
                ("name", models.CharField(max_length=500)),
                ("size_bytes", models.PositiveBigIntegerField(default=0)),
                ("attempts", models.PositiveIntegerField(default=0)),
                ("last_error", models.CharField(blank=True, max_length=100)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("completed_at", models.DateTimeField(blank=True, db_index=True, null=True)),
            ],
            options={
                "ordering": ["created_at", "pk"],
                "constraints": [models.UniqueConstraint(fields=("storage", "name"), name="one_file_deletion_per_path")],
            },
        ),
    ]
