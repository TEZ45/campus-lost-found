"""Exercise normal business writes using the disposable runtime role."""

import os
import sys
from datetime import date, time, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

import django

django.setup()

from django.conf import settings
from django.contrib.auth.models import User
from django.db import connection, transaction
from django.utils import timezone

from apps.reports.lifecycle import cleanup_retention
from apps.reports.models import ActionHistory, Claim, Notice, NotificationEvent, Report, UserProfile
from apps.reports.notifications import deliver_pending
from apps.reports.services import create_claim, create_report


def main():
    if settings.APP_ENV != "test" or not connection.settings_dict["NAME"].startswith("test_"):
        raise RuntimeError("This smoke test requires a disposable test_ database.")
    with transaction.atomic():
        owner = User.objects.create_user("role_test_owner")
        claimant = User.objects.create_user("role_test_claimant")
        staff = User.objects.create_user("role_test_staff")
        UserProfile.objects.create(user=staff, is_service_staff=True)
        report = create_report(owner, {
            "report_type": "found", "item_name": "Test umbrella", "category": "Other",
            "description": "Disposable role test", "location": "Test desk",
            "event_date": date.today(), "event_time": time(12), "contact_name": "Test owner",
            "contact_email": "test@example.invalid", "contact_phone": "",
        })
        report.review(staff, "approve")
        claim = create_claim(claimant, report.pk, {"evidence": "Private test evidence"})
        claim.approve(staff, confirmations={"evidence_reviewed": True, "custody_confirmed": True})
        claim.start_handover(staff, confirmations={"identity_verified": True, "custody_confirmed": True})
        claim.complete_handover(staff, confirmations={"transfer_confirmed": True})
    claim.refresh_from_db()
    report.refresh_from_db()
    assert claim.status == "completed" and report.status == "closed"
    delivery = deliver_pending()
    assert delivery["delivered"] > 0 and delivery["failed"] == delivery["pending"] == 0
    assert Notice.objects.filter(user=claimant).exists()
    with transaction.atomic():
        Claim.objects.filter(pk=claim.pk).update(finalized_at=timezone.now() - timedelta(days=16))
        Report.objects.filter(pk=report.pk).update(closed_at=timezone.now() - timedelta(days=16))
    from django.core.management import call_command

    call_command("cleanup_retention", apply=True, batch_size=100, verbosity=0)
    claim.refresh_from_db()
    assert claim.evidence == ""
    cleanup_retention(apply=True, now=timezone.now() + timedelta(days=50))
    for model in (Claim, Report, ActionHistory, NotificationEvent, Notice):
        assert not model.objects.exists()
    print("Runtime workflow, notice delivery and complete retention cleanup passed.")


if __name__ == "__main__":
    main()
