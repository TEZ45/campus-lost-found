from django.shortcuts import render, get_object_or_404, redirect
from django.contrib import messages
from .models import Report, Claim, Match
from .forms import ReportForm, ClaimForm


def report_list(request):
    """List approved reports."""
    reports = Report.objects.filter(status="approved").order_by("-created_at")
    report_type = request.GET.get("type", "")
    if report_type in ("lost", "found"):
        reports = reports.filter(report_type=report_type)
    return render(request, "reports/list.html", {"reports": reports, "report_type": report_type})


def report_detail(request, pk):
    """View a single report."""
    report = get_object_or_404(Report, pk=pk)
    claims = report.claims.all() if report.report_type == "found" else []
    return render(request, "reports/detail.html", {"report": report, "claims": claims})


def report_create(request):
    """Submit a new report."""
    if request.method == "POST":
        form = ReportForm(request.POST, request.FILES)
        if form.is_valid():
            report = form.save(commit=False)
            if request.user.is_authenticated:
                report.created_by = request.user
            report.save()
            messages.success(request, "Report submitted! It will be reviewed soon.")
            return redirect("report_detail", pk=report.pk)
    else:
        form = ReportForm()
    return render(request, "reports/form.html", {"form": form, "title": "New Report"})


def claim_create(request, report_pk):
    """Submit a claim for a found item."""
    report = get_object_or_404(Report, pk=report_pk, report_type="found")
    if request.method == "POST":
        form = ClaimForm(request.POST)
        if form.is_valid():
            claim = form.save(commit=False)
            claim.report = report
            claim.save()
            messages.success(request, "Claim submitted! Staff will review it soon.")
            return redirect("report_detail", pk=report.pk)
    else:
        form = ClaimForm()
    return render(request, "reports/claim_form.html", {"form": form, "report": report})
