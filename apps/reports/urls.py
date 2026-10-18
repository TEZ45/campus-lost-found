from django.urls import path

from . import views

urlpatterns = [
    # Public / user pages
    path("", views.report_list, name="report_list"),
    path("register/", views.register, name="register"),
    path("login/", views.login_view, name="login"),
    path("logout/", views.logout_view, name="logout"),
    path("report/new/", views.report_create, name="report_create"),
    path("report/<int:pk>/", views.report_detail, name="report_detail"),
    path("report/<int:pk>/edit/", views.report_edit, name="report_edit"),
    path("report/<int:pk>/close/", views.report_close_request, name="report_close_request"),
    # Claims
    path("report/<int:report_pk>/claim/", views.claim_create, name="claim_create"),
    path("claim/<int:pk>/", views.claim_detail, name="claim_detail"),
    path("claim/<int:pk>/resubmit/", views.claim_resubmit, name="claim_resubmit"),
    path("claim/<int:pk>/withdraw/", views.claim_withdraw, name="claim_withdraw"),
    # Notices
    path("notices/", views.notices, name="notices"),
    path("notice/<int:pk>/read/", views.notice_read, name="notice_read"),
    # Matches (Could)
    path("matches/", views.matches, name="matches"),
    # Staff
    path("staff/", views.staff_dashboard, name="staff_dashboard"),
    path("staff/reports/", views.staff_report_queue, name="staff_report_queue"),
    path("staff/reports/<int:pk>/review/", views.staff_report_review, name="staff_report_review"),
    path("staff/claims/", views.staff_claim_queue, name="staff_claim_queue"),
    path("staff/claims/<int:pk>/review/", views.staff_claim_review, name="staff_claim_review"),
    path("staff/handovers/", views.staff_handover_queue, name="staff_handover_queue"),
    path("staff/handovers/<int:pk>/action/", views.staff_handover_action, name="staff_handover_action"),
]
