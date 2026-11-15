"""Track created paths before writing so failed uploads can be compensated."""

import os
import tempfile
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path

from django.core.exceptions import SuspiciousFileOperation
from django.core.files.storage import FileSystemStorage

_upload_files = ContextVar("managed_upload_files", default=None)
_maximum_name_length = ContextVar("managed_upload_name_length", default=None)


@dataclass(frozen=True)
class CreatedUpload:
    storage: FileSystemStorage
    name: str


@contextmanager
def capture_upload_files(files):
    """Use around service writes; compensate this list after DB rollback."""
    token = _upload_files.set(files)
    try:
        yield files
    finally:
        _upload_files.reset(token)


class ManagedFileSystemStorage(FileSystemStorage):
    """Write to a tracked stage, then publish with an exclusive local hard link.

    Both paths stay on the same filesystem. A hard-link failure fails the save
    safely; no fallback can overwrite a previous file. Service captures retain
    failed paths until their database transaction has rolled back. Direct
    storage calls best-effort remove only files created by the failed call.
    """

    def save(self, name, content, max_length=None):
        token = _maximum_name_length.set(max_length)
        try:
            return super().save(name, content, max_length=max_length)
        finally:
            _maximum_name_length.reset(token)

    def _safe_path(self, name):
        root = Path(self.location).resolve()
        path = Path(self.path(name))
        if not path.resolve().is_relative_to(root):
            raise SuspiciousFileOperation("Upload path is outside its media root.")
        current = root
        relative = path.relative_to(Path(self.location))
        for part in relative.parts:
            current = current / part
            if current.is_symlink():
                raise SuspiciousFileOperation("Upload directories cannot contain symlinks.")
        return root.joinpath(*relative.parts)

    def _save(self, name, content):
        root = Path(self.location).resolve()
        target = self._safe_path(name)
        target.parent.mkdir(
            parents=True, exist_ok=True,
            mode=self.directory_permissions_mode if self.directory_permissions_mode is not None else 0o777,
        )
        self._safe_path(name)
        created = []
        captured = _upload_files.get()

        def track(path):
            entry = CreatedUpload(self, path.relative_to(root).as_posix())
            created.append((path, entry))
            if captured is not None:
                captured.append(entry)
            return entry

        try:
            descriptor, stage_name = tempfile.mkstemp(prefix=".upload-", suffix=".tmp", dir=target.parent)
            stage = Path(stage_name)
            stage_entry = track(stage)
            try:
                stream = os.fdopen(descriptor, "wb")
            except BaseException:
                os.close(descriptor)
                raise
            with stream:
                for chunk in content.chunks():
                    stream.write(chunk)
                stream.flush()
                os.fsync(stream.fileno())
            if self.file_permissions_mode is not None:
                os.chmod(stage, self.file_permissions_mode)
            for _ in range(100):
                try:
                    os.link(stage, target)
                except FileExistsError:
                    directory, filename = os.path.split(name)
                    file_root, file_ext = os.path.splitext(filename)
                    candidate = os.path.join(directory, self.get_alternative_name(file_root, file_ext))
                    name = self.get_available_name(candidate, max_length=_maximum_name_length.get())
                    target = self._safe_path(name)
                else:
                    track(target)
                    break
            else:
                raise OSError("Unable to allocate a unique upload filename.")
            stage.unlink()
            if captured is not None:
                captured.remove(stage_entry)
            return target.relative_to(root).as_posix()
        except BaseException:
            if captured is None:
                for path, _ in reversed(created):
                    try:
                        path.unlink(missing_ok=True)
                    except OSError:
                        pass
            raise
