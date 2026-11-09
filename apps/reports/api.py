from django.contrib.auth import authenticate, login, logout
from django.contrib.auth.forms import UserCreationForm
from django.core.exceptions import ObjectDoesNotExist, PermissionDenied
from django.core.exceptions import ValidationError as ModelValidationError
from django.db.models import Count
from django.http import FileResponse, Http404
from django.middleware.csrf import get_token
from django.shortcuts import get_object_or_404
from rest_framework.authentication import SessionAuthentication
from rest_framework.exceptions import APIException, NotAuthenticated, ValidationError
from rest_framework.pagination import PageNumberPagination
from rest_framework.permissions import AllowAny
from rest_framework.renderers import JSONRenderer
from rest_framework.response import Response
from rest_framework.views import APIView

from . import services
from .forms import SearchForm
from .models import ActionHistory, Claim, Notice, Report
from .permissions import can_read_evidence, can_view_report, is_service_staff
from .serializers import (
    ActionInput,
    ClaimInput,
    ClaimOutput,
    HistoryOutput,
    LoginInput,
    MatchOutput,
    NoticeOutput,
    RegisterInput,
    ReportInput,
    ReportOutput,
    StrictSerializer,
)


class SessionWithCsrf(SessionAuthentication):
    def authenticate(self, request):
        self.enforce_csrf(request)
        return super().authenticate(request)

    def authenticate_header(self, request):
        return "Session"


class Conflict(APIException):
    status_code = 409
    default_code = "state_conflict"


class Pagination(PageNumberPagination):
    page_size = 20
    page_size_query_param = "page_size"
    max_page_size = 100


class BaseApi(APIView):
    authentication_classes = [SessionWithCsrf]
    permission_classes = [AllowAny]
    renderer_classes = [JSONRenderer]

    def finalize_response(self, request, response, *args, **kwargs):
        response = super().finalize_response(request, response, *args, **kwargs)
        response["Cache-Control"] = "private, no-store"
        return response

    def handle_exception(self, exc):
        if isinstance(exc, ModelValidationError):
            exc = ValidationError(getattr(exc, "message_dict", None) or exc.messages)
        elif isinstance(exc, ObjectDoesNotExist):
            exc = Http404()
        elif isinstance(exc, ValueError):
            exc = Conflict(str(exc))
        response = super().handle_exception(exc)
        code = getattr(exc, "default_code", None) or {
            403: "permission_denied", 404: "not_found",
        }.get(response.status_code, "request_failed")
        response.data = {"error": {"code": code, "details": response.data}}
        return response

    def require_user(self, request):
        if not request.user.is_authenticated:
            raise NotAuthenticated()

    def require_staff(self, request):
        self.require_user(request)
        if not is_service_staff(request.user):
            raise PermissionDenied

    def input(self, serializer, request, **kwargs):
        instance = serializer(data=request.data, **kwargs)
        instance.is_valid(raise_exception=True)
        return instance.validated_data

    def output(self, serializer, value, request, status=200):
        return Response(serializer(value, context={"request": request}).data, status=status)

    def page(self, serializer, queryset, request):
        pager = Pagination()
        page = pager.paginate_queryset(queryset, request, view=self)
        return pager.get_paginated_response(serializer(page, many=True, context={"request": request}).data)


def user_data(user):
    return {"id": user.pk, "username": user.username, "is_service_staff": is_service_staff(user)}


class CsrfToken(BaseApi):
    def get(self, request):
        return Response({"csrf_token": get_token(request._request)})


class CurrentUser(BaseApi):
    def get(self, request):
        self.require_user(request)
        return Response(user_data(request.user))


class Register(BaseApi):
    def post(self, request):
        data = self.input(RegisterInput, request)
        form = UserCreationForm(data)
        if not form.is_valid():
            raise ValidationError(dict(form.errors))
        user = form.save()
        login(request._request, user)
        return Response({**user_data(user), "csrf_token": get_token(request._request)}, status=201)


class Login(BaseApi):
    def post(self, request):
        data = self.input(LoginInput, request)
        user = authenticate(request._request, **data)
        if user is None:
            raise NotAuthenticated("Invalid username or password.")
        login(request._request, user)
        return Response({**user_data(user), "csrf_token": get_token(request._request)})


class Logout(BaseApi):
    def post(self, request):
        self.require_user(request)
        self.input(StrictSerializer, request)
        logout(request._request)
        return Response(status=204)


class Reports(BaseApi):
    def get(self, request):
        unknown = set(request.query_params) - set(SearchForm.base_fields) - {"page", "page_size"}
        if unknown:
            raise ValidationError({key: ["Unknown filter."] for key in sorted(unknown)})
        form = SearchForm(request.query_params)
        if not form.is_valid():
            raise ValidationError(dict(form.errors))
        return self.page(ReportOutput, services.report_queryset(request.user, form.cleaned_data), request)

    def post(self, request):
        self.require_user(request)
        data = self.input(ReportInput, request)
        report = services.create_report(request.user, data)
        return self.output(ReportOutput, report, request, status=201)


class OwnReports(BaseApi):
    def get(self, request):
        self.require_user(request)
        return self.page(ReportOutput, Report.objects.filter(created_by=request.user), request)


class ReportDetail(BaseApi):
    def get(self, request, pk):
        report = get_object_or_404(Report, pk=pk)
        if not can_view_report(request.user, report):
            raise Http404
        return self.output(ReportOutput, report, request)

    def put(self, request, pk):
        return self.update(request, pk, partial=False)

    def patch(self, request, pk):
        return self.update(request, pk, partial=True)

    def update(self, request, pk, partial):
        self.require_user(request)
        data = self.input(ReportInput, request, partial=partial)
        report = services.update_report(request.user, pk, data)
        return self.output(ReportOutput, report, request)


class ReportClosure(BaseApi):
    def post(self, request, pk):
        self.require_user(request)
        self.input(StrictSerializer, request)
        report = services.request_report_closure(request.user, pk)
        return self.output(ReportOutput, report, request)


class ReportClaims(BaseApi):
    def post(self, request, pk):
        self.require_user(request)
        data = self.input(ClaimInput, request)
        images = data.pop("images", [])
        claim = services.create_claim(request.user, pk, data, images=images)
        return self.output(ClaimOutput, claim, request, status=201)


class OwnClaims(BaseApi):
    def get(self, request):
        self.require_user(request)
        return self.page(ClaimOutput, claim_queryset().filter(claimant=request.user), request)


def claim_queryset():
    return Claim.objects.select_related("report").prefetch_related("attachments", "history")


class ClaimDetail(BaseApi):
    def get(self, request, pk):
        self.require_user(request)
        claim = get_object_or_404(claim_queryset(), pk=pk)
        if not can_read_evidence(request.user, claim):
            raise PermissionDenied
        return self.output(ClaimOutput, claim, request)


class ClaimResubmission(BaseApi):
    def post(self, request, pk):
        self.require_user(request)
        data = self.input(ClaimInput, request)
        claim = services.resubmit_claim(request.user, pk, data["evidence"], data.get("images", []))
        return self.output(ClaimOutput, claim, request)


class ClaimWithdrawal(BaseApi):
    def post(self, request, pk):
        self.require_user(request)
        self.input(StrictSerializer, request)
        claim = services.act_on_claim(request.user, pk, "withdraw")
        return self.output(ClaimOutput, claim, request)


class Attachment(BaseApi):
    def get(self, request, pk):
        self.require_user(request)
        content = services.open_claim_attachment(request.user, pk)
        response = FileResponse(content, as_attachment=True, content_type="application/octet-stream")
        response["Cache-Control"] = "private, no-store"
        response["X-Content-Type-Options"] = "nosniff"
        return response


class Notices(BaseApi):
    def get(self, request):
        self.require_user(request)
        return self.page(NoticeOutput, Notice.objects.filter(user=request.user), request)


class NoticeRead(BaseApi):
    def post(self, request, pk):
        self.require_user(request)
        self.input(StrictSerializer, request)
        return self.output(NoticeOutput, services.mark_notice_read(request.user, pk), request)


class Matches(BaseApi):
    def get(self, request):
        return self.page(MatchOutput, services.public_matches(), request)


class MatchRegeneration(BaseApi):
    def post(self, request):
        self.require_staff(request)
        self.input(StrictSerializer, request)
        return Response({"count": services.regenerate_matches(request.user)})


class StaffDashboard(BaseApi):
    def get(self, request):
        self.require_staff(request)
        return Response({
            "reports": dict(Report.objects.values_list("status").annotate(count=Count("pk"))),
            "claims": dict(Claim.objects.values_list("status").annotate(count=Count("pk"))),
        })


class StaffReports(BaseApi):
    def get(self, request):
        self.require_staff(request)
        reports = Report.objects.filter(status__in=["pending", "close_requested"]).order_by("created_at")
        return self.page(ReportOutput, reports, request)


class StaffReportReview(BaseApi):
    def post(self, request, pk):
        self.require_staff(request)
        data = self.input(ActionInput, request)
        if data["action"] not in {"approve", "reject", "close"}:
            raise ValidationError({"action": ["Unknown report action."]})
        report = services.review_report(request.user, pk, **data)
        return self.output(ReportOutput, report, request)


class StaffClaims(BaseApi):
    states = ["pending", "awaiting_evidence"]

    def get(self, request):
        self.require_staff(request)
        claims = claim_queryset().filter(status__in=self.states).order_by("created_at")
        return self.page(ClaimOutput, claims, request)


class StaffHandovers(StaffClaims):
    states = Claim.RESERVED_STATES


class StaffClaimAction(BaseApi):
    def post(self, request, pk):
        self.require_staff(request)
        data = self.input(ActionInput, request)
        if data["action"] not in Claim.ACTION_LABELS:
            raise ValidationError({"action": ["Unknown staff claim action."]})
        claim = services.act_on_claim(request.user, pk, **data)
        return self.output(ClaimOutput, claim, request)


class StaffHistory(BaseApi):
    def get(self, request):
        self.require_staff(request)
        return self.page(HistoryOutput, ActionHistory.objects.all(), request)
