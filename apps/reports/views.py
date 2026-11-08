from functools import wraps
from urllib.parse import urlparse

from django.contrib import messages
from django.contrib.auth import login, logout
from django.contrib.auth.decorators import login_required
from django.contrib.auth.forms import AuthenticationForm, UserCreationForm
from django.db.models import Q
from django.shortcuts import get_object_or_404, redirect, render

from .forms import ClaimForm, ReportForm, SearchForm
from .models import ActionHistory, Claim, Match, Notice, Report, UserProfile

# --------------------------------------------------------------------- #
# Access control                                                        #
# --------------------------------------------------------------------- #

def _is_service_staff(user):
    """URS REQ-1.1 / SEC-1.3: business authorisation for lost-property work.
    Administrator status alone does NOT authorise this role."""
    if not user.is_authenticated:
        return False
    profile = getattr(user, "profile", None)
    if profile is None:
        profile, _ = UserProfile.objects.get_or_create(user=user)
    return profile.is_service_staff


def staff_required(view):
    """URS REQ-1.1 / SEC-1.3: service-staff authorisation only."""
    @wraps(view)
    def wrapper(request, *args, **kwargs):
        if not request.user.is_authenticated:
            return redirect(f"/login/?next={request.path}")
        if not _is_service_staff(request.user):
            return render(request, "403.html", status=403)
        return view(request, *args, **kwargs)
    return wrapper


def can_read_evidence(user, claim):
    """SEC-1.2: only the claimant and authorised staff may read claim evidence.
    Administrator status alone does not authorise evidence access (SEC-1.3)."""
    if claim.claimant is not None and user == claim.claimant:
        return True
    return _is_service_staff(user)


def log_history(action, actor, report=None, claim=None, prev="", new="", reason="", result=""):
    ActionHistory.objects.create(
        actor=actor, action=action, report=report, claim=claim,
        previous_state=prev, new_state=new, reason=reason or "", result=result or "",
    )


def generate_matches(report):
    """REQ-1.6 (Could): suggest candidate lost<->found pairs with a reason.
    Never approves a claim automatically (DOM-1)."""
    if report.report_type == "lost":
        candidates = Report.objects.filter(report_type="found", status="approved").exclude(pk=report.pk)
    else:
        candidates = Report.objects.filter(report_type="lost", status="approved").exclude(pk=report.pk)
    created = 0
    for cand in candidates:
        score, reason = match_score(report, cand)
        if score <= 0:
            continue
        kwargs = {"lost_report": report if report.report_type == "lost" else cand,
                  "found_report": report if report.report_type == "found" else cand}
        _, was_created = Match.objects.get_or_create(**kwargs, defaults={"score": score, "reason": reason})
        created += int(was_created)
    return created


def match_score(a, b):
    """Simple heuristic: same category or shared keywords."""
    s = 0.0
    reasons = []
    if a.category and a.category.lower() == (b.category or "").lower():
        s += 0.5
        reasons.append("same category")
    a_words = {w.lower() for w in a.item_name.split() if len(w) > 2}
    b_words = {w.lower() for w in b.item_name.split() if len(w) > 2}
    shared = a_words & b_words
    if shared:
        s += 0.3 * min(len(shared), 3)
        reasons.append(f"keywords: {', '.join(sorted(shared))}")
    return round(s, 2), "; ".join(reasons)


# --------------------------------------------------------------------- #
# Accounts (REQ-1.1)                                                    #
# --------------------------------------------------------------------- #

def register(request):
    if request.method == "POST":
        form = UserCreationForm(request.POST)
        if form.is_valid():
            user = form.save()
            login(request, user)
            messages.success(request, "Account created. Welcome!")
            return redirect("report_list")
    else:
        form = UserCreationForm()
    return render(request, "register.html", {"form": form})


from urllib.parse import urlparse


def _safe_next(value):
    """Allow only local paths for the post-login redirect (prevent open redirects)."""
    if not value:
        return "report_list"
    if value.startswith("//") or urlparse(value).netloc:
        return "report_list"
    return value


def login_view(request):
    if request.user.is_authenticated:
        return redirect("report_list")
    next_url = _safe_next(request.POST.get("next") or request.GET.get("next"))
    if request.method == "POST":
        form = AuthenticationForm(request, data=request.POST)
        if form.is_valid():
            login(request, form.get_user())
            return redirect(next_url)
    else:
        form = AuthenticationForm(request)
    return render(request, "login.html", {"form": form, "next": next_url})


def logout_view(request):
    logout(request)
    messages.info(request, "You have been signed out.")
    return redirect("report_list")


# --------------------------------------------------------------------- #
# Reports                                                               #
# --------------------------------------------------------------------- #

def report_list(request):
    """REQ-1.5 Search: keyword + type / category / location / date / status."""
    form = SearchForm(request.GET or None)
    reports = Report.objects.all()
    if not _is_service_staff(request.user):
        reports = reports.filter(status="approved")
    if form.is_valid():
        cd = form.cleaned_data
        q = cd.get("q")
        if q:
            reports = reports.filter(
                Q(item_name__icontains=q) | Q(description__icontains=q) | Q(category__icontains=q)
            )
        if cd.get("report_type"):
            reports = reports.filter(report_type=cd["report_type"])
        if cd.get("category"):
            reports = reports.filter(category__icontains=cd["category"])
        if cd.get("location"):
            reports = reports.filter(location__icontains=cd["location"])
        if cd.get("date_from"):
            reports = reports.filter(event_date__gte=cd["date_from"])
        if cd.get("date_to"):
            reports = reports.filter(event_date__lte=cd["date_to"])
        if cd.get("status"):
            reports = reports.filter(status=cd["status"])
    return render(request, "reports/list.html", {"reports": reports, "form": form})


def report_detail(request, pk):
    report = get_object_or_404(Report, pk=pk)
    matches = report.lost_matches.all() if report.report_type == "lost" else report.found_matches.all()
    return render(request, "reports/detail.html", {"report": report, "matches": matches})


@login_required
def report_create(request):
    if request.method == "POST":
        form = ReportForm(request.POST, request.FILES)
        if form.is_valid():
            report = form.save(commit=False)
            report.created_by = request.user
            report.status = "pending"
            report.save()
            log_history("report_submitted", request.user, report=report, new="pending")
            messages.success(request, "Report submitted! It will be reviewed soon.")
            return redirect("report_detail", pk=report.pk)
    else:
        form = ReportForm()
    return render(request, "reports/form.html", {"form": form, "title": "New Report"})


@login_required
def report_edit(request, pk):
    """REQ-1.4: owners may edit; changed public content returns to pending review."""
    report = get_object_or_404(Report, pk=pk)
    if report.created_by != request.user and not request.user.is_staff:
        return render(request, "403.html", status=403)
    was_public = report.is_public()
    if request.method == "POST":
        form = ReportForm(request.POST, request.FILES, instance=report)
        if form.is_valid():
            report = form.save(commit=False)
            if was_public:
                report.status = "pending"  # changed public content requires another review
            report.save()
            log_history("report_edited", request.user, report=report,
                        prev="approved" if was_public else report.status,
                        new=report.status, result="Resubmitted for review" if was_public else "Saved")
            messages.success(request, "Report updated.")
            return redirect("report_detail", pk=report.pk)
    else:
        form = ReportForm(instance=report)
    return render(request, "reports/form.html", {"form": form, "title": "Edit Report"})


@login_required
def report_close_request(request, pk):
    """REQ-1.4 / 1.12: owner requests closure; staff approve (close request queue)."""
    report = get_object_or_404(Report, pk=pk)
    if report.created_by != request.user and not request.user.is_staff:
        return render(request, "403.html", status=403)
    if report.status == "closed":
        messages.warning(request, "This report is already closed.")
        return redirect("report_detail", pk=pk)
    if request.method == "POST":
        report.status = "close_requested"
        report.save(update_fields=["status", "updated_at"])
        log_history("close_requested", request.user, report=report, prev="", new="close_requested")
        messages.success(request, "Closure requested. Staff will review it.")
        return redirect("report_detail", pk=pk)
    return render(request, "reports/confirm_close.html", {"report": report})


# --------------------------------------------------------------------- #
# Claims                                                                #
# --------------------------------------------------------------------- #

@login_required
def claim_create(request, report_pk):
    """REQ-1.7 / DOM-1: signed-in users claim approved, open found reports."""
    report = get_object_or_404(Report, pk=report_pk, report_type="found")
    if report.status != "approved":
        messages.error(request, "Claims are only accepted for approved, open found-item reports.")
        return redirect("report_detail", pk=report.pk)
    if request.method == "POST":
        form = ClaimForm(request.POST, request.FILES)
        if form.is_valid():
            claim = form.save(commit=False)
            claim.report = report
            claim.claimant = request.user
            claim.claimant_name = request.user.get_full_name() or request.user.username
            claim.claimant_email = request.user.email or ""
            claim.status = "pending"
            claim.save()
            form.save_attachments(claim)
            log_history("claim_submitted", request.user, report=report, claim=claim, new="pending")
            messages.success(request, f"Claim submitted. Reference: {claim.reference}.")
            return redirect("claim_detail", pk=claim.pk)
    else:
        form = ClaimForm()
    return render(request, "reports/claim_form.html", {"form": form, "report": report})


@login_required
def claim_detail(request, pk):
    claim = get_object_or_404(Claim, pk=pk)
    if not can_read_evidence(request.user, claim):
        return render(request, "403.html", status=403)
    resubmit_form = None
    if claim.status == "awaiting_evidence" and claim.claimant == request.user:
        resubmit_form = ClaimForm()
    return render(request, "reports/claim_detail.html",
                  {"claim": claim, "resubmit_form": resubmit_form,
                   "can_read_evidence": True})


@login_required
def claim_resubmit(request, pk):
    """REQ-1.8: awaiting_evidence -> pending with new evidence and attachments."""
    claim = get_object_or_404(Claim, pk=pk)
    if claim.claimant != request.user:
        return render(request, "403.html", status=403)
    if request.method == "POST":
        form = ClaimForm(request.POST, request.FILES)
        if form.is_valid():
            claim.evidence = form.cleaned_data["evidence"]
            claim.resubmit_evidence()
            form.save_attachments(claim)
            messages.success(request, "Evidence resubmitted. Back to pending review.")
            return redirect("claim_detail", pk=claim.pk)
    else:
        form = ClaimForm(instance=claim)
    return render(request, "reports/claim_form.html", {"form": form, "report": claim.report,
                                                       "claim": claim, "resubmit": True})


@login_required
def claim_withdraw(request, pk):
    """REQ-1.12: claimant may withdraw only before handover."""
    claim = get_object_or_404(Claim, pk=pk)
    if claim.claimant != request.user:
        return render(request, "403.html", status=403)
    try:
        claim.withdraw(request.user)
        messages.success(request, "Claim withdrawn.")
    except ValueError as exc:
        messages.error(request, f"Cannot withdraw: {exc}")
    return redirect("claim_detail", pk=claim.pk)


def notices(request):
    return render(request, "notices.html", {"notices": Notice.objects.filter(user=request.user)})


@login_required
def notice_read(request, pk):
    notice = get_object_or_404(Notice, pk=pk, user=request.user)
    notice.is_read = True
    notice.save(update_fields=["is_read"])
    if notice.link:
        return redirect(notice.link)
    return redirect("notices")


# --------------------------------------------------------------------- #
# Staff: moderation and queues (REQ-1.3/1.8/1.10/1.11/1.14)             #
# --------------------------------------------------------------------- #

@staff_required
def staff_dashboard(request):
    context = {
        "pending_reports": Report.objects.filter(status="pending").count(),
        "close_requests": Report.objects.filter(status="close_requested").count(),
        "pending_claims": Claim.objects.filter(status="pending").count(),
        "awaiting_evidence": Claim.objects.filter(status="awaiting_evidence").count(),
        "awaiting_handover": Claim.objects.filter(status="awaiting_handover").count(),
        "under_dispute": Claim.objects.filter(status="under_dispute").count(),
        "handover_in_progress": Claim.objects.filter(status="handover_in_progress").count(),
    }
    return render(request, "staff/dashboard.html", context)


@staff_required
def staff_report_queue(request):
    reports = Report.objects.filter(status__in=["pending", "close_requested"]).order_by("created_at")
    return render(request, "staff/report_queue.html", {"reports": reports})


@staff_required
def staff_report_review(request, pk):
    report = get_object_or_404(Report, pk=pk)
    action = request.POST.get("action")
    reason = request.POST.get("reason", "").strip()
    if action == "approve":
        report.status = "approved"
        report.save(update_fields=["status", "updated_at"])
        log_history("report_approved", request.user, report=report,
                    prev="pending", new="approved", reason=reason)
        if report.created_by:
            Notice.objects.create(user=report.created_by,
                                  title=f"Report {report.reference} approved",
                                  body=reason or "Your report is now public.",
                                  link=f"/report/{report.pk}/")
        messages.success(request, "Report approved and published.")
    elif action == "reject":
        if not reason:
            messages.error(request, "A rejection reason is required.")
            return redirect("staff_report_queue")
        report.status = "rejected"
        report.save(update_fields=["status", "updated_at"])
        log_history("report_rejected", request.user, report=report,
                    prev="pending", new="rejected", reason=reason)
        if report.created_by:
            Notice.objects.create(user=report.created_by,
                                  title=f"Report {report.reference} rejected",
                                  body=f"Reason: {reason}", link=f"/report/{report.pk}/")
        messages.success(request, "Report rejected.")
    elif action == "close":
        # REQ-1.12: verified report cancellation invalidates before-handover claims.
        # Handover in Progress cannot be ended through report cancellation (REQ-1.11).
        if report.claims.filter(status="handover_in_progress").exists():
            messages.error(request, "Cannot close: a handover is in progress for this report.")
            return redirect("staff_report_queue")
        for claim in report.claims.filter(status__in=Claim.BEFORE_HANDOVER):
            claim.invalidate(request.user, "Report closed by staff")
        report.status = "closed"
        report.save(update_fields=["status", "updated_at"])
        log_history("report_closed", request.user, report=report, prev="", new="closed", reason=reason)
        messages.success(request, "Report closed.")
    else:
        messages.error(request, "Unknown action.")
    return redirect("staff_report_queue")


@staff_required
def staff_claim_queue(request):
    claims = Claim.objects.filter(status__in=["pending", "awaiting_evidence"]).order_by("created_at")
    return render(request, "staff/claim_queue.html", {"claims": claims})


@staff_required
def staff_claim_review(request, pk):
    claim = get_object_or_404(Claim, pk=pk)
    action = request.POST.get("action")
    reason = request.POST.get("reason", "").strip()
    try:
        if action == "approve":
            claim.approve(request.user, reason)
            messages.success(request, f"Claim {claim.reference} approved.")
        elif action == "reject":
            claim.reject(request.user, reason)
            messages.success(request, f"Claim {claim.reference} rejected.")
        elif action == "request_evidence":
            claim.request_evidence(request.user, reason)
            messages.success(request, f"Evidence requested for {claim.reference}.")
        else:
            messages.error(request, "Unknown action.")
    except ValueError as exc:
        messages.error(request, f"Action failed: {exc}")
    return redirect("staff_claim_queue")


@staff_required
def staff_handover_queue(request):
    claims = Claim.objects.filter(
        status__in=["awaiting_handover", "under_dispute", "handover_in_progress"]
    ).order_by("created_at")
    return render(request, "staff/handover_queue.html", {"claims": claims})


@staff_required
def staff_handover_action(request, pk):
    claim = get_object_or_404(Claim, pk=pk)
    action = request.POST.get("action")
    reason = request.POST.get("reason", "").strip()
    try:
        if action == "start_dispute":
            claim.start_dispute(request.user, reason)
            messages.success(request, f"Claim {claim.reference} moved to dispute review.")
        elif action == "resolve_keep":
            claim.resolve_dispute(request.user, approve=True, reason=reason)
            messages.success(request, f"Dispute resolved; {claim.reference} returns to awaiting handover.")
        elif action == "resolve_reject":
            claim.resolve_dispute(request.user, approve=False, reason=reason)
            messages.success(request, f"Dispute resolved; {claim.reference} rejected.")
        elif action == "start_handover":
            claim.start_handover(request.user)
            messages.success(request, f"Handover started for {claim.reference}.")
        elif action == "complete_handover":
            claim.complete_handover(request.user)
            messages.success(request, f"Handover completed for {claim.reference}; report closed.")
        elif action == "interrupt_handover":
            claim.interrupt_handover(request.user, reason)
            messages.success(request, f"Handover interrupted safely for {claim.reference}.")
        else:
            messages.error(request, "Unknown action.")
    except ValueError as exc:
        messages.error(request, f"Action failed: {exc}")
    return redirect("staff_handover_queue")


# --------------------------------------------------------------------- #
# Matches (REQ-1.6, Could)                                              #
# --------------------------------------------------------------------- #

def matches(request):
    """Suggest candidate matches and list existing suggestions."""
    if request.method == "POST":
        for report in Report.objects.filter(status="approved"):
            generate_matches(report)
        messages.success(request, "Match suggestions regenerated.")
    matches_qs = Match.objects.select_related("lost_report", "found_report").order_by("-score")[:100]
    return render(request, "matches.html", {"matches": matches_qs})
