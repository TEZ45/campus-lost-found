"""Stop aggregate file data before image decoding or file validation."""

from django.conf import settings
from django.core.exceptions import RequestDataTooBig
from django.core.files.uploadhandler import FileUploadHandler


def close_upload_files(request):
    for handler in getattr(request, "_upload_handlers", ()):
        uploaded = getattr(handler, "file", None)
        if uploaded is not None:
            uploaded.close()


class AggregateUploadLimitHandler(FileUploadHandler):
    def __init__(self, request=None):
        super().__init__(request)
        self.total_bytes = 0

    def receive_data_chunk(self, raw_data, start):
        self.total_bytes += len(raw_data)
        if self.total_bytes > settings.REQUEST_MAX_UPLOAD_BYTES:
            self.request._request_limits_upload_rejected = True
            close_upload_files(self.request)
            raise RequestDataTooBig("Uploaded files exceed the aggregate byte limit.")
        return raw_data

    def file_complete(self, file_size):
        return None
