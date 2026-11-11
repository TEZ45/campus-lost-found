from django.contrib import admin
from django.urls import include, path

from apps.reports.views import report_photo

urlpatterns = [
    path("api/v1/", include("apps.reports.api_urls")),
    path("admin/", admin.site.urls),
    path("media/<path:name>", report_photo, name="report_photo"),
    path("", include("apps.reports.urls")),
]
