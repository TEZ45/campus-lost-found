from django.urls import path
from . import views

app_name = "reports"

urlpatterns = [
    path("", views.report_list, name="report_list"),
    path("report/new/", views.report_create, name="report_create"),
    path("report/<int:pk>/", views.report_detail, name="report_detail"),
    path("report/<int:report_pk>/claim/", views.claim_create, name="claim_create"),
]
