"""Attachment accounting and durable disk cleanup.

Reserve validated uploads inside the same atomic block as the business write.
After that block rolls back, cleanup_failed_uploads removes files or durably
charges and queues the remaining orphan. No filesystem scan runs per upload.
"""

import os
from pathlib import Path, PurePosixPath

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.files.storage import FileSystemStorage
from django.db import connection, transaction
from django.db.models import F
from django.utils import timezone

from .lifecycle_models import FileDeletion, StorageLedger
from .storage import PrivateEvidenceStorage

DEFAULT_CAPACITY = 100 * 1024 ** 3
DEFAULT_SCAN_LIMIT = 10000
BACKUP_DIRECTORIES = {"backup", "backups", ".git", "__pycache__"}


def capacity_bytes():
    return max(0, int(getattr(settings, "STORAGE_CAPACITY_BYTES", DEFAULT_CAPACITY)))


def storage_for(kind):
    if kind == "public":
        return FileSystemStorage(location=settings.MEDIA_ROOT)
    if kind == "private":
        return PrivateEvidenceStorage()
    raise ValueError("Unknown managed storage")


def storage_kind(storage):
    location = Path(storage.location).resolve()
    for kind in ("private", "public"):
        if location == Path(storage_for(kind).location).resolve():
            return kind
    raise ValueError("Storage is outside managed media roots")


def safe_path(kind, name):
    """Reject absolute paths, traversal, symlinks and backup directories."""
    normalized = str(name).replace("\\", "/")
    relative = PurePosixPath(normalized)
    if (not normalized or relative.is_absolute() or ":" in normalized
            or any(part in ("..", ".") for part in relative.parts)
            or any(part.lower() in BACKUP_DIRECTORIES for part in relative.parts)):
        raise ValueError("Unsafe managed file path")
    root = Path(storage_for(kind).location).resolve()
    path = root.joinpath(*relative.parts)
    current = root
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            raise ValueError("Managed file paths cannot contain symlinks")
    if not path.resolve().is_relative_to(root):
        raise ValueError("File is outside managed media root")
    return path


def scan_files(max_files=DEFAULT_SCAN_LIMIT):
    """Bounded maintenance scan, excluding backups and symlinks."""
    found = []
    for kind in ("public", "private"):
        root = Path(storage_for(kind).location).resolve()
        if not root.exists():
            continue
        for directory, directories, filenames in os.walk(root, followlinks=False):
            directories[:] = sorted(
                name for name in directories
                if name.lower() not in BACKUP_DIRECTORIES
                and not Path(directory, name).is_symlink()
            )
            for filename in sorted(filenames):
                path = Path(directory, filename)
                if path.is_symlink() or not path.is_file():
                    continue
                name = path.relative_to(root).as_posix()
                safe_path(kind, name)
                found.append((kind, name, path.stat().st_size))
                if len(found) > max_files:
                    raise ValidationError("Storage scan limit exceeded; rerun with a larger --max-files.")
    return found


def locked_ledger():
    if not connection.in_atomic_block:
        raise RuntimeError("Storage accounting requires an atomic transaction")
    StorageLedger.objects.get_or_create(pk=1)
    ledger = StorageLedger.objects.select_for_update().get(pk=1)
    if not ledger.initialized:
        ledger.used_bytes = sum(size for _, _, size in scan_files())
        ledger.initialized = True
        ledger.save(update_fields=["used_bytes", "initialized", "updated_at"])
    return ledger


def uploaded_bytes(upload):
    """Count real bytes independently of the client-supplied size."""
    position = upload.tell()
    try:
        upload.seek(0)
        actual = sum(len(chunk) for chunk in upload.chunks())
    finally:
        upload.seek(position)
    if actual != upload.size:
        raise ValidationError("Upload size does not match the received bytes.")
    return actual


def reserve_uploads(uploads):
    """Reserve before disk writes; transaction rollback releases the reservation."""
    amount = sum(uploaded_bytes(upload) for upload in uploads if upload)
    if not amount:
        return 0
    ledger = locked_ledger()
    capacity = capacity_bytes()
    if amount > capacity or ledger.used_bytes > capacity - amount:
        raise ValidationError("Attachment storage is full. Please contact service staff.")
    # The row lock serializes reservations; the predicate also prevents overshoot.
    changed = StorageLedger.objects.filter(pk=1, used_bytes__lte=capacity - amount).update(
        used_bytes=F("used_bytes") + amount,
    )
    if not changed:
        raise ValidationError("Attachment storage is full. Please contact service staff.")
    return amount


def storage_status():
    ledger = StorageLedger.objects.filter(pk=1).first()
    used = ledger.used_bytes if ledger else 0
    capacity = capacity_bytes()
    return {"used_bytes": used, "capacity_bytes": capacity,
            "warning": used * 100 >= capacity * 80, "initialized": bool(ledger and ledger.initialized)}


def queue_file_deletion(storage, name):
    """Call inside the transaction removing/replacing the corresponding DB reference."""
    if not name:
        return None
    kind = storage_kind(storage)
    path = safe_path(kind, name)
    size = path.stat().st_size if path.exists() else 0
    job, _ = FileDeletion.objects.get_or_create(
        storage=kind, name=name, defaults={"size_bytes": size},
    )
    if job.completed_at is not None:
        job.size_bytes = size
        job.completed_at = None
        job.last_error = ""
        job.save(update_fields=["size_bytes", "completed_at", "last_error"])
    return job


def cleanup_failed_uploads(files):
    """Run after business rollback, retaining a charge when disk deletion fails."""
    for field in files:
        kind = storage_kind(field.storage)
        path = safe_path(kind, field.name)
        size = path.stat().st_size if path.exists() else 0
        try:
            field.storage.delete(field.name)
            if path.exists():
                raise OSError("File remains after deletion")
        except Exception as exc:  # noqa: BLE001 - Cleanup failure must remain durable.
            if not path.exists():
                continue
            size = path.stat().st_size
            with transaction.atomic():
                existed = StorageLedger.objects.filter(pk=1, initialized=True).exists()
                locked_ledger()
                already_queued = FileDeletion.objects.filter(
                    storage=kind, name=field.name, completed_at=None,
                ).exists()
                job = queue_file_deletion(field.storage, field.name)
                # Initializing the ledger already counted this orphan on disk.
                if existed and not already_queued:
                    StorageLedger.objects.filter(pk=1).update(used_bytes=F("used_bytes") + size)
                job.attempts = F("attempts") + 1
                job.last_error = type(exc).__name__
                job.save(update_fields=["attempts", "last_error"])


def process_file_deletions(batch_size=100):
    """Delete under row/ledger locks; failed files remain charged and retryable."""
    from .models import ClaimAttachment, Report

    result = {"files_deleted": 0, "files_failed": 0, "files_referenced": 0}
    ids = list(FileDeletion.objects.filter(completed_at=None).order_by("attempts", "pk")
               .values_list("pk", flat=True)[:batch_size])
    for pk in ids:
        with transaction.atomic():
            ledger = locked_ledger()
            job = FileDeletion.objects.select_for_update().get(pk=pk)
            if job.completed_at is not None:
                continue
            if (Report.objects.filter(photo=job.name).exists()
                    or ClaimAttachment.objects.filter(image=job.name).exists()):
                job.attempts += 1
                job.last_error = "StillReferenced"
                job.save(update_fields=["attempts", "last_error"])
                result["files_referenced"] += 1
                continue
            job.attempts += 1
            try:
                path = safe_path(job.storage, job.name)
                storage_for(job.storage).delete(job.name)
                if path.exists():
                    raise OSError("File remains after deletion")
            except Exception as exc:  # noqa: BLE001 - Storage failures remain retryable.
                job.last_error = type(exc).__name__
                result["files_failed"] += 1
            else:
                ledger.used_bytes = max(0, ledger.used_bytes - job.size_bytes)
                ledger.save(update_fields=["used_bytes", "updated_at"])
                job.completed_at = timezone.now()
                job.last_error = ""
                # Evidence filenames must not survive successful deletion.
                job.name = f"removed/{job.pk}"
                result["files_deleted"] += 1
            job.save(update_fields=["attempts", "completed_at", "last_error", "name"])
    return result


def reconcile_storage(apply=False, max_files=DEFAULT_SCAN_LIMIT, batch_size=100):
    """Recount disk usage and queue unreferenced files without deleting them."""
    from .models import ClaimAttachment, Report

    def inspect():
        files = scan_files(max_files=max_files)
        references = {
            ("public", name) for name in Report.objects.exclude(photo="").exclude(photo=None)
            .values_list("photo", flat=True)
        }
        for name in ClaimAttachment.objects.values_list("image", flat=True):
            references.update({("private", name), ("public", name)})
        orphans = [(kind, name, size) for kind, name, size in files
                   if (kind, name) not in references]
        result = {"files": len(files), "used_bytes": sum(size for _, _, size in files),
                  "orphans": len(orphans), "queued": 0, "apply": apply}
        if apply:
            pending = list(FileDeletion.objects.select_for_update().filter(completed_at=None)
                           .order_by("pk")[:max_files + 1])
            if len(pending) > max_files:
                raise ValidationError("Pending cleanup scan limit exceeded; increase --max-files.")
            sizes = {(kind, name): size for kind, name, size in files}
            pending_paths = set()
            for job in pending:
                pending_paths.add((job.storage, job.name))
                job.size_bytes = sizes.get((job.storage, job.name), 0)
            FileDeletion.objects.bulk_update(pending, ["size_bytes"], batch_size=batch_size)
            StorageLedger.objects.filter(pk=1).update(
                used_bytes=result["used_bytes"], initialized=True, updated_at=timezone.now(),
            )
            unqueued = [(kind, name, size) for kind, name, size in orphans
                        if (kind, name) not in pending_paths]
            for kind, name, size in unqueued[:batch_size]:
                job = queue_file_deletion(storage_for(kind), name)
                job.size_bytes = size
                job.save(update_fields=["size_bytes"])
                result["queued"] += 1
        return result

    if not apply:
        return inspect()
    with transaction.atomic():
        StorageLedger.objects.get_or_create(pk=1)
        StorageLedger.objects.select_for_update().get(pk=1)
        return inspect()
