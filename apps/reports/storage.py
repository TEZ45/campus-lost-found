from pathlib import Path

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.core.files.storage import FileSystemStorage


class PrivateEvidenceStorage(FileSystemStorage):
    @property
    def location(self):
        private = Path(settings.PRIVATE_MEDIA_ROOT).resolve()
        public = Path(settings.MEDIA_ROOT).resolve()
        if private.is_relative_to(public) or public.is_relative_to(private):
            raise ImproperlyConfigured("Public and private media directories must be separate.")
        return str(private)

    def url(self, name):
        raise ValueError("Evidence requires an authenticated download.")
