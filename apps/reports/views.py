from django.contrib import messages
from django.contrib.auth import login, logout
from django.contrib.auth.decorators import login_required
from django.contrib.auth.forms import AuthenticationForm, UserCreationForm
from django.core.exceptions import PermissionDenied, ValidationError
from django.http import FileResponse, Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_http_methods, require_POST, require_safe

from . import services
from .confirmations import FIELDS
from .forms import ClaimForm, ReportForm, SearchForm
from .models import Claim, Notice, Report
from .permissions import (
    can_manage_report,
    can_read_evidence,
    can_view_report,
    claim_actions,
    report_actions,
    staff_required,
)


def form_error(form, error):
    for message in error.messages:
        form.add_error(None, message)


def safe_path(value, fallback="report_list"):
    if value and value.startswith("/") and url_has_allowed_host_and_scheme(value, allowed_hosts=set()):
        return value
    return fallback


@require_http_methods(["GET", "POST"])
def register(request):
    form = UserCreationForm(request.POST if request.method == "POST" else None)
    if request.method == "POST" and form.is_valid():
        user = form.save()
        login(request, user)
        return redirect("report_list")
    return render(request, "register.html", {"form": form})


@require_http_methods(["GET", "POST"])
def login_view(request):
    if request.user.is_authenticated:
        return redirect("report_list")
    next_url = safe_path(request.POST.get("next") or request.GET.get("next"))
    form = AuthenticationForm(request, data=request.POST if request.method == "POST" else None)
    if request.method == "POST" and form.is_valid():
        login(request, form.get_user())
        return redirect(next_url)
    return render(request, "login.html", {"form": form, "next": next_url})


@login_required
@require_POST
def logout_view(request):
    logout(request)
    return redirect("report_list")


@require_safe
def report_list(request):
    form = SearchForm(request.GET)
    reports = (services.report_queryset(request.user, form.cleaned_data)
               if form.is_valid() else Report.objects.none())
    return render(request, "reports/list.html", {"reports": reports, "form": form})


@require_safe
def report_detail(request, pk):
    report = get_object_or_404(Report, pk=pk)
    if not can_view_report(request.user, report):
        raise Http404
    matches = services.public_matches().filter(
        **{"lost_report" if report.report_type == "lost" else "found_report": report},
    )
    return render(request, "reports/detail.html", {
        "report": report, "matches": matches,
        "available_actions": [item["key"] for item in report_actions(request.user, report)],
    })


@require_safe
def report_photo(request, name):
    report = get_object_or_404(Report, photo=name)
    if not can_view_report(request.user, report):
        raise Http404
    try:
        response = FileResponse(report.photo.open("rb"))
    except FileNotFoundError:
        raise Http404 from None
    response["Cache-Control"] = "private, no-store"
    return response


@login_required
@require_safe
def claim_attachment(request, pk):
    content = services.open_claim_attachment(request.user, pk)
    response = FileResponse(content, as_attachment=True, content_type="application/octet-stream")
    response["Cache-Control"] = "private, no-store"
    response["X-Content-Type-Options"] = "nosniff"
    return response


@login_required
@require_http_methods(["GET", "POST"])
def report_create(request):
    form = ReportForm(request.POST, request.FILES) if request.method == "POST" else ReportForm()
    if request.method == "POST" and form.is_valid():
        try:
            report = services.create_report(request.user, form.cleaned_data)
        except ValidationError as exc:
            form_error(form, exc)
        else:
            messages.success(request, "Report submitted for review.")
            return redirect("report_detail", pk=report.pk)
    return render(request, "reports/form.html", {"form": form, "title": "New Report"})


@login_required
@require_http_methods(["GET", "POST"])
def report_edit(request, pk):
    report = get_object_or_404(Report, pk=pk)
    if not can_manage_report(request.user, report):
        raise PermissionDenied
    if "edit" not in {item["key"] for item in report_actions(request.user, report)}:
        messages.error(request, "Cannot edit this report in its current state.")
        return redirect("report_detail", pk=pk)
    form = (ReportForm(request.POST, request.FILES, instance=report)
            if request.method == "POST" else ReportForm(instance=report))
    if request.method == "POST" and form.is_valid():
        try:
            services.update_report(request.user, pk, form.cleaned_data)
        except ValidationError as exc:
            form_error(form, exc)
        except ValueError as exc:
            messages.error(request, str(exc))
            return redirect("report_detail", pk=pk)
        else:
            messages.success(request, "Report updated.")
            return redirect("report_detail", pk=pk)
    return render(request, "reports/form.html", {"form": form, "title": "Edit Report"})


@login_required
@require_http_methods(["GET", "POST"])
def report_close_request(request, pk):
    report = get_object_or_404(Report, pk=pk)
    if not can_manage_report(request.user, report):
        raise PermissionDenied
    if request.method == "POST":
        try:
            services.request_report_closure(request.user, pk)
            messages.success(request, "Closure requested.")
        except ValueError as exc:
            messages.error(request, str(exc))
        return redirect("report_detail", pk=pk)
    return render(request, "reports/confirm_close.html", {"report": report})


@login_required
@require_http_methods(["GET", "POST"])
def claim_create(request, report_pk):
    report = get_object_or_404(Report, pk=report_pk, report_type="found")
    if "claim" not in {item["key"] for item in report_actions(request.user, report)}:
        messages.error(request, "Claims require an approved, open found report.")
        return redirect("report_detail", pk=report.pk)
    form = ClaimForm(request.POST, request.FILES) if request.method == "POST" else ClaimForm()
    if request.method == "POST" and form.is_valid():
        try:
            claim = services.create_claim(request.user, report_pk,
                                          {"evidence": form.cleaned_data["evidence"]},
                                          form.cleaned_data["images"])
        except ValidationError as exc:
            form_error(form, exc)
        except ValueError as exc:
            messages.error(request, str(exc))
            return redirect("report_detail", pk=report.pk)
        else:
            messages.success(request, f"Claim submitted. Reference: {claim.reference}.")
            return redirect("claim_detail", pk=claim.pk)
    return render(request, "reports/claim_form.html", {"form": form, "report": report})


@login_required
@require_safe
def claim_detail(request, pk):
    claim = get_object_or_404(Claim, pk=pk)
    if not can_read_evidence(request.user, claim):
        raise PermissionDenied
    actions = [item["key"] for item in claim_actions(request.user, claim)]
    return render(request, "reports/claim_detail.html", {
        "claim": claim, "resubmit_form": ClaimForm() if "resubmit" in actions else None,
        "available_actions": actions,
    })


@login_required
@require_http_methods(["GET", "POST"])
def claim_resubmit(request, pk):
    claim = get_object_or_404(Claim, pk=pk)
    if claim.claimant_id != request.user.pk:
        raise PermissionDenied
    form = (ClaimForm(request.POST, request.FILES, instance=claim)
            if request.method == "POST" else ClaimForm(instance=claim))
    if request.method == "POST" and form.is_valid():
        try:
            services.resubmit_claim(request.user, pk, form.cleaned_data["evidence"],
                                    form.cleaned_data["images"])
        except ValidationError as exc:
            form_error(form, exc)
        except ValueError as exc:
            messages.error(request, str(exc))
            return redirect("claim_detail", pk=pk)
        else:
            messages.success(request, "Evidence resubmitted.")
            return redirect("claim_detail", pk=pk)
    return render(request, "reports/claim_form.html", {
        "form": form, "report": claim.report, "claim": claim, "resubmit": True,
    })


@login_required
@require_POST
def claim_withdraw(request, pk):
    try:
        services.act_on_claim(request.user, pk, "withdraw")
        messages.success(request, "Claim withdrawn.")
    except ValueError as exc:
        messages.error(request, f"Cannot withdraw: {exc}")
    return redirect("claim_detail", pk=pk)


@login_required
@require_safe
def notices(request):
    return render(request, "notices.html", {"notices": Notice.objects.filter(user=request.user)})


@login_required
@require_POST
def notice_read(request, pk):
    notice = services.mark_notice_read(request.user, pk)
    return redirect(safe_path(notice.link, "notices"))


@staff_required
@require_safe
def staff_dashboard(request):
    return render(request, "staff/dashboard.html", {
        "pending_reports": Report.objects.filter(status="pending").count(),
        "close_requests": Report.objects.filter(status="close_requested").count(),
        "pending_claims": Claim.objects.filter(status="pending").count(),
        **{status: Claim.objects.filter(status=status).count() for status in (
            "awaiting_evidence", "awaiting_handover", "under_dispute", "handover_in_progress",
        )},
    })


@staff_required
@require_safe
def staff_report_queue(request):
    reports = Report.objects.filter(status__in=["pending", "close_requested"]).order_by("created_at")
    for report in reports:
        report.available_actions = [item for item in report_actions(request.user, report)
                                    if item["key"] in Report.ACTION_LABELS]
    return render(request, "staff/report_queue.html", {"reports": reports})


@staff_required
@require_POST
def staff_report_review(request, pk):
    try:
        report = services.review_report(request.user, pk, request.POST.get("action"),
                                        request.POST.get("reason", ""))
        messages.success(request, f"Report {report.status}.")
    except (ValueError, ValidationError) as exc:
        messages.error(request, f"Action failed: {exc}")
    return redirect("staff_report_queue")


def claim_queue(request, states, template):
    claims = (Claim.objects.filter(status__in=states).select_related("report")
              .prefetch_related("attachments").order_by("created_at"))
    for claim in claims:
        claim.available_actions = [item for item in claim_actions(request.user, claim)
                                   if item["key"] in Claim.ACTION_LABELS]
    return render(request, template, {"claims": claims})


@staff_required
@require_safe
def staff_claim_queue(request):
    return claim_queue(request, ["pending", "awaiting_evidence"], "staff/claim_queue.html")


@staff_required
@require_safe
def staff_handover_queue(request):
    return claim_queue(request, Claim.RESERVED_STATES, "staff/handover_queue.html")


def claim_action(request, pk, destination):
    action = request.POST.get("action")
    if action not in Claim.ACTION_LABELS:
        messages.error(request, "Unknown action.")
    else:
        try:
            confirmations = {
                key: request.POST[key] if key == "safe_outcome" else request.POST[key] in ("on", "true")
                for key in FIELDS if key in request.POST
            }
            services.act_on_claim(request.user, pk, action, request.POST.get("reason", ""),
                                  confirmations=confirmations)
            messages.success(request, "Action completed.")
        except (ValueError, ValidationError) as exc:
            messages.error(request, f"Action failed: {exc}")
    return redirect(destination)


@staff_required
@require_POST
def staff_claim_review(request, pk):
    return claim_action(request, pk, "staff_claim_queue")


@staff_required
@require_POST
def staff_handover_action(request, pk):
    return claim_action(request, pk, "staff_handover_queue")


@require_http_methods(["GET", "POST"])
def matches(request):
    if request.method == "POST":
        if not request.user.is_authenticated:
            return redirect("login")
        services.regenerate_matches(request.user)
        messages.success(request, "Match suggestions regenerated.")
    return render(request, "matches.html", {"matches": services.public_matches()[:100]})
