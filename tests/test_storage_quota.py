import json
from concurrent.futures import ThreadPoolExecutor
from io import StringIO
from threading import Barrier
from unittest.mock import patch

import pytest
from django.core.exceptions import ValidationError
from django.core.files.base import ContentFile
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command
from django.db import close_old_connections, connections, transaction

from apps.reports.models import FileDeletion, StorageLedger
from apps.reports.storage_quota import (
    cleanup_failed_uploads,
    process_file_deletions,
    queue_file_deletion,
    reconcile_storage,
    reserve_uploads,
    storage_for,
    storage_status,
)

pytestmark = pytest.mark.django_db


def test_capacity_counts_existing_files_and_actual_uploaded_bytes(settings):
    settings.STORAGE_CAPACITY_BYTES = 10
    storage_for("private").save("claims/existing", ContentFile(b"123456"))
    upload = SimpleUploadedFile("image.png", b"12345")
    with pytest.raises(ValidationError, match="full"), transaction.atomic():
        reserve_uploads([upload])
    assert not StorageLedger.objects.exists()
    upload.size = 1
    with pytest.raises(ValidationError, match="does not match"), transaction.atomic():
        reserve_uploads([upload])


def test_reservation_rolls_back_and_preserves_cursor(settings):
    settings.STORAGE_CAPACITY_BYTES = 10
    upload = SimpleUploadedFile("image.png", b"12345678")
    upload.seek(3)
    with pytest.raises(RuntimeError), transaction.atomic():
        assert reserve_uploads([upload]) == 8
        assert upload.tell() == 3
        assert storage_status()["warning"] is True
        raise RuntimeError("DB write failed")
    assert not StorageLedger.objects.exists()


def test_failed_rollback_cleanup_remains_charged_then_retries(settings):
    from types import SimpleNamespace

    settings.STORAGE_CAPACITY_BYTES = 20
    StorageLedger.objects.create(pk=1, initialized=True, used_bytes=0)
    storage = storage_for("public")
    name = storage.save("reports/orphan.png", ContentFile(b"123456"))
    field = SimpleNamespace(storage=storage, name=name)
    with patch.object(storage, "delete", side_effect=OSError("busy")):
        cleanup_failed_uploads([field])
    assert StorageLedger.objects.get(pk=1).used_bytes == 6
    assert FileDeletion.objects.get().attempts == 1
    assert storage.exists(name)
    process_file_deletions()
    assert not storage.exists(name)
    assert StorageLedger.objects.get(pk=1).used_bytes == 0
    job = FileDeletion.objects.get()
    assert job.completed_at is not None and job.name == f"removed/{job.pk}"


def test_reconciliation_is_read_only_by_default_and_excludes_backups(settings):
    storage = storage_for("public")
    storage.save("reports/orphan.png", ContentFile(b"1234"))
    storage.save("backups/private-copy.png", ContentFile(b"1234567890"))
    output = StringIO()
    call_command("reconcile_storage", stdout=output)
    assert json.loads(output.getvalue())["used_bytes"] == 4
    assert not StorageLedger.objects.exists() and not FileDeletion.objects.exists()
    result = reconcile_storage(apply=True)
    assert result["queued"] == 1 and result["used_bytes"] == 4
    assert storage.exists("reports/orphan.png")
    process_file_deletions()
    assert storage.exists("backups/private-copy.png")


def test_outbox_never_deletes_a_live_reference(users):
    from .test_workflow import make_report

    storage = storage_for("public")
    name = storage.save("reports/used.png", ContentFile(b"1234"))
    report = make_report(users["owner"])
    report.photo = name
    report.save()
    with transaction.atomic():
        queue_file_deletion(storage, name)
    assert process_file_deletions()["files_referenced"] == 1
    assert storage.exists(name)
    assert FileDeletion.objects.get().completed_at is None


def test_reconciliation_scan_is_bounded_and_never_follows_symlinks():
    storage = storage_for("public")
    storage.save("one", ContentFile(b"1"))
    storage.save("two", ContentFile(b"2"))
    with pytest.raises(ValidationError, match="scan limit"):
        reconcile_storage(apply=True, max_files=1)
    assert not StorageLedger.objects.exists()
    with pytest.raises(ValueError, match="Unsafe"):
        queue_file_deletion(storage, "../outside")


def test_reconciliation_updates_pending_sizes_and_makes_progress_across_batches():
    storage = storage_for("public")
    storage.save("first", ContentFile(b"1234"))
    storage.save("second", ContentFile(b"123456"))
    assert reconcile_storage(apply=True, batch_size=1)["queued"] == 1
    assert reconcile_storage(apply=True, batch_size=1)["queued"] == 1
    assert FileDeletion.objects.filter(completed_at=None).count() == 2
    storage.delete("first")
    reconcile_storage(apply=True, batch_size=1)
    assert FileDeletion.objects.get(name="first").size_bytes == 0
    assert StorageLedger.objects.get(pk=1).used_bytes == 6
    process_file_deletions()
    assert StorageLedger.objects.get(pk=1).used_bytes == 0


def test_failed_cleanup_after_reconciliation_does_not_double_charge():
    from types import SimpleNamespace

    storage = storage_for("public")
    name = storage.save("orphan", ContentFile(b"1234"))
    reconcile_storage(apply=True)
    with patch.object(storage, "delete", side_effect=OSError("busy")):
        cleanup_failed_uploads([SimpleNamespace(storage=storage, name=name)])
    assert StorageLedger.objects.get(pk=1).used_bytes == 4


@pytest.mark.django_db(transaction=True)
def test_concurrent_reservations_cannot_exceed_capacity(settings):
    settings.STORAGE_CAPACITY_BYTES = 10
    StorageLedger.objects.create(pk=1, initialized=True)
    barrier = Barrier(2)

    def reserve(_):
        close_old_connections()
        try:
            barrier.wait(timeout=10)
            try:
                with transaction.atomic():
                    reserve_uploads([SimpleUploadedFile("image.png", b"123456")])
                return True
            except ValidationError:
                return False
        finally:
            connections.close_all()

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(reserve, range(2)))
    assert sorted(results) == [False, True]
    assert StorageLedger.objects.get(pk=1).used_bytes == 6
