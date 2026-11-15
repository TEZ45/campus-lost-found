from pathlib import Path
from unittest.mock import patch

import pytest
from django.core.files.base import ContentFile

from apps.reports import services
from apps.reports.managed_storage import ManagedFileSystemStorage, capture_upload_files
from apps.reports.models import Claim, ClaimAttachment, FileDeletion, Report, StorageLedger
from apps.reports.storage import PrivateEvidenceStorage
from apps.reports.storage_quota import cleanup_failed_uploads, process_file_deletions

from .test_service_uploads import report_data
from .test_uploads import upload
from .test_workflow import make_report

pytestmark = pytest.mark.django_db


class InterruptedContent(ContentFile):
    def chunks(self, chunk_size=None):
        yield b"partial bytes"
        raise OSError("Content stream failed")


@pytest.mark.parametrize("private", [False, True])
def test_interrupted_stream_is_captured_before_field_commit(private):
    storage = PrivateEvidenceStorage() if private else ManagedFileSystemStorage()
    created = []
    with capture_upload_files(created), pytest.raises(OSError, match="stream failed"):
        storage.save("claims/proof.png", InterruptedContent(b"unused"))
    assert len(created) == 1
    stage = created[0]
    assert ".upload-" in stage.name
    assert Path(stage.storage.path(stage.name)).read_bytes() == b"partial bytes"
    cleanup_failed_uploads(created)
    assert not Path(stage.storage.path(stage.name)).exists()


def test_direct_failed_storage_call_removes_only_created_paths(settings):
    storage = ManagedFileSystemStorage()
    original = storage.save("reports/original.png", ContentFile(b"original"))
    with pytest.raises(OSError, match="stream failed"):
        storage.save("reports/original.png", InterruptedContent(b"unused"))
    assert Path(storage.path(original)).read_bytes() == b"original"
    assert list(Path(settings.MEDIA_ROOT).rglob("*.*")) == [Path(storage.path(original))]


def test_write_fault_keeps_real_partial_bytes_for_compensation():
    storage = ManagedFileSystemStorage()
    created = []
    original_fdopen = __import__("os").fdopen

    class FailingWriter:
        def __init__(self, descriptor, mode):
            self.stream = original_fdopen(descriptor, mode)

        def __enter__(self):
            return self

        def __exit__(self, *args):
            self.stream.close()

        def write(self, data):
            self.stream.write(data[:4])
            self.stream.flush()
            raise OSError("Disk write failed")

    with (capture_upload_files(created),
          patch("apps.reports.managed_storage.os.fdopen", FailingWriter),
          pytest.raises(OSError, match="Disk write")):
        storage.save("reports/proof.png", ContentFile(b"abcdefgh"))
    assert len(created) == 1
    assert Path(storage.path(created[0].name)).read_bytes() == b"abcd"
    cleanup_failed_uploads(created)
    assert not storage.exists(created[0].name)


def test_racing_collision_never_overwrites_or_tracks_the_other_writer(settings):
    import os

    storage = ManagedFileSystemStorage()
    original_link = os.link
    raced = False
    created = []

    def collision(stage, target):
        nonlocal raced
        if not raced:
            raced = True
            Path(target).write_bytes(b"other writer")
            raise FileExistsError("Another writer won")
        original_link(stage, target)

    with capture_upload_files(created), patch("apps.reports.managed_storage.os.link", collision):
        name = storage.save("reports/proof.png", ContentFile(b"new content"))
    assert name != "reports/proof.png"
    assert Path(storage.path("reports/proof.png")).read_bytes() == b"other writer"
    assert Path(storage.path(name)).read_bytes() == b"new content"
    assert [entry.name for entry in created] == [name]
    assert not list(Path(settings.MEDIA_ROOT).rglob(".upload-*"))


def test_promotion_failure_cleans_stage_without_touching_collision_winner(settings):
    storage = ManagedFileSystemStorage()
    calls = 0

    def collision_then_failure(stage, target):
        nonlocal calls
        calls += 1
        if calls == 1:
            Path(target).write_bytes(b"other writer")
            raise FileExistsError("Another writer won")
        raise OSError("Promotion failed")

    with (patch("apps.reports.managed_storage.os.link", collision_then_failure),
          pytest.raises(OSError, match="Promotion")):
        storage.save("reports/proof.png", ContentFile(b"new content"))
    assert Path(storage.path("reports/proof.png")).read_bytes() == b"other writer"
    assert not list(Path(settings.MEDIA_ROOT).rglob(".upload-*"))
    assert len([path for path in Path(settings.MEDIA_ROOT).rglob("*") if path.is_file()]) == 1


def test_service_cleans_content_failure_after_reservation(users, settings, monkeypatch):
    image = upload()
    original_chunks = image.chunks
    calls = 0

    def interrupted_chunks(chunk_size=None):
        nonlocal calls
        calls += 1
        if calls == 1:
            yield from original_chunks(chunk_size)
            return
        yield b"partial"
        raise OSError("Upload interrupted")

    monkeypatch.setattr(image, "chunks", interrupted_chunks)
    with pytest.raises(OSError, match="interrupted"):
        services.create_report(users["owner"], report_data(photo=image))
    assert calls == 2
    assert not Report.objects.exists()
    assert not StorageLedger.objects.exists()
    assert not FileDeletion.objects.exists()
    assert not [path for path in Path(settings.MEDIA_ROOT).rglob("*") if path.is_file()]


def test_failed_service_stage_cleanup_remains_charged_and_retryable(users, settings):
    StorageLedger.objects.create(pk=1, initialized=True)
    image = upload()
    with (patch("apps.reports.managed_storage.os.fsync", side_effect=OSError("Disk flush failed")),
          patch.object(ManagedFileSystemStorage, "delete", side_effect=OSError("File busy")),
          pytest.raises(OSError, match="Disk flush")):
        services.create_report(users["owner"], report_data(photo=image))
    assert not Report.objects.exists()
    job = FileDeletion.objects.get(completed_at=None)
    assert ".upload-" in job.name and job.size_bytes == image.size
    assert Path(settings.MEDIA_ROOT, job.name).read_bytes()
    assert StorageLedger.objects.get(pk=1).used_bytes == image.size
    process_file_deletions()
    assert not Path(settings.MEDIA_ROOT, job.name).exists()
    assert StorageLedger.objects.get(pk=1).used_bytes == 0


def test_post_promotion_failure_tracks_both_created_paths_for_rollback(users, settings):
    image = upload()
    with (patch("apps.reports.managed_storage.Path.unlink", side_effect=OSError("Stage unlink failed")),
          pytest.raises(OSError, match="Stage unlink")):
        services.create_report(users["owner"], report_data(photo=image))
    assert not Report.objects.exists()
    assert not StorageLedger.objects.exists()
    assert not [path for path in Path(settings.MEDIA_ROOT).rglob("*") if path.is_file()]


def test_successful_service_upload_preserves_name_and_accounts_once(users, settings):
    image = upload(name="proof.png")
    report = services.create_report(users["owner"], report_data(photo=image))
    assert report.photo.name == "reports/proof.png"
    assert report.photo._committed
    assert Path(report.photo.path).stat().st_size == image.size
    assert StorageLedger.objects.get(pk=1).used_bytes == image.size
    assert not list(Path(settings.MEDIA_ROOT).rglob(".upload-*"))


def test_private_partial_upload_rolls_back_claim_and_charges_uncleanable_stage(users, settings):
    report = make_report(users["owner"])
    StorageLedger.objects.create(pk=1, initialized=True)
    image = upload()
    with (patch("apps.reports.managed_storage.os.fsync", side_effect=OSError("Disk flush failed")),
          patch.object(ManagedFileSystemStorage, "delete", side_effect=OSError("File busy")),
          pytest.raises(OSError, match="Disk flush")):
        services.create_claim(users["claimant"], report.pk, {"evidence": "Proof"}, [image])
    assert not Claim.objects.exists() and not ClaimAttachment.objects.exists()
    assert Report.objects.filter(pk=report.pk).exists()
    job = FileDeletion.objects.get(completed_at=None)
    assert job.storage == "private"
    assert Path(settings.PRIVATE_MEDIA_ROOT, job.name).stat().st_size == image.size
    assert StorageLedger.objects.get(pk=1).used_bytes == image.size
    process_file_deletions()
    assert StorageLedger.objects.get(pk=1).used_bytes == 0
