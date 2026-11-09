from django.urls import path

from . import api

app_name = "reports_api"

urlpatterns = [
    path("auth/csrf/", api.CsrfToken.as_view(), name="csrf"),
    path("auth/me/", api.CurrentUser.as_view(), name="me"),
    path("auth/register/", api.Register.as_view(), name="register"),
    path("auth/login/", api.Login.as_view(), name="login"),
    path("auth/logout/", api.Logout.as_view(), name="logout"),
    path("reports/", api.Reports.as_view(), name="reports"),
    path("reports/mine/", api.OwnReports.as_view(), name="own-reports"),
    path("reports/<int:pk>/", api.ReportDetail.as_view(), name="report"),
    path("reports/<int:pk>/close/", api.ReportClosure.as_view(), name="close"),
    path("reports/<int:pk>/claims/", api.ReportClaims.as_view(), name="create-claim"),
    path("claims/", api.OwnClaims.as_view(), name="claims"),
    path("claims/<int:pk>/", api.ClaimDetail.as_view(), name="claim"),
    path("claims/<int:pk>/resubmit/", api.ClaimResubmission.as_view(), name="resubmit"),
    path("claims/<int:pk>/withdraw/", api.ClaimWithdrawal.as_view(), name="withdraw"),
    path("attachments/<int:pk>/", api.Attachment.as_view(), name="attachment"),
    path("notices/", api.Notices.as_view(), name="notices"),
    path("notices/<int:pk>/read/", api.NoticeRead.as_view(), name="notice-read"),
    path("matches/", api.Matches.as_view(), name="matches"),
    path("matches/regenerate/", api.MatchRegeneration.as_view(), name="regenerate"),
    path("staff/dashboard/", api.StaffDashboard.as_view(), name="dashboard"),
    path("staff/reports/", api.StaffReports.as_view(), name="staff-reports"),
    path("staff/reports/<int:pk>/review/", api.StaffReportReview.as_view(), name="review-report"),
    path("staff/claims/", api.StaffClaims.as_view(), name="staff-claims"),
    path("staff/claims/<int:pk>/actions/", api.StaffClaimAction.as_view(), name="claim-action"),
    path("staff/handovers/", api.StaffHandovers.as_view(), name="handovers"),
    path("staff/history/", api.StaffHistory.as_view(), name="history"),
]
