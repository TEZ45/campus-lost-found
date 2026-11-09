import filecmp
import shutil
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from apps.reports.storage import PrivateEvidenceStorage


class Command(BaseCommand):
    help = "Move legacy claim files out of public media without changing database paths."

    def handle(self, *args, **options):
        public = Path(settings.MEDIA_ROOT).resolve()
        private = Path(PrivateEvidenceStorage().location)
        source = public / "claims"
        moved = 0
        if source.is_symlink():
            raise CommandError("The legacy claims directory must not be a symbolic link.")
        for entry in source.rglob("*"):
            if entry.is_symlink() or not entry.resolve().is_relative_to(source):
                raise CommandError("Legacy evidence must stay inside the claims directory.")
            if not entry.is_file():
                continue
            target = (private / entry.relative_to(public)).resolve()
            if not target.is_relative_to(private):
                raise CommandError("The destination must stay inside private media.")
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.exists():
                if not filecmp.cmp(entry, target, shallow=False):
                    raise CommandError("Conflicting evidence files; no file was overwritten.")
                entry.unlink()
            else:
                shutil.move(str(entry), str(target))
            moved += 1
        self.stdout.write(f"Moved {moved} legacy evidence files.")
