"""Access rules for reports, claims and staff queues."""
from functools import wraps

from django.shortcuts import redirect, render


def is_service_staff(user):
    """Service-staff authorisation; the admin flag alone does not grant it."""
    if not user or not user.is_authenticated or not user.is_active:
        return False
    profile = getattr(user, "profile", None)
    return bool(profile and profile.is_service_staff)


def staff_required(view):
    """Allow service staff only; anonymous users are sent to the login page."""
    @wraps(view)
    def wrapper(request, *args, **kwargs):
        if not request.user.is_authenticated:
            return redirect(f"/login/?next={request.path}")
        if not is_service_staff(request.user):
            return render(request, "403.html", status=403)
        return view(request, *args, **kwargs)
    return wrapper


def can_read_evidence(user, claim):
    """Only the claimant and service staff may read claim evidence."""
    return user.is_authenticated and user.is_active and (
        claim.claimant_id == user.id or is_service_staff(user)
    )


def can_view_report(user, report):
    """Approved reports are public; otherwise owner, staff or a claimant."""
    if report.is_public():
        return True
    if not user.is_authenticated or not user.is_active:
        return False
    if report.created_by_id == user.id or is_service_staff(user):
        return True
    return report.claims.filter(claimant=user).exists()


def can_manage_report(user, report):
    """Owner or service staff may edit a report or request its closure."""
    if not user.is_authenticated or not user.is_active:
        return False
    return report.created_by_id == user.id or is_service_staff(user)


def can_claim(user, report):
    """Signed-in users claim approved, open found reports."""
    return (
        user.is_authenticated
        and user.is_active
        and report.report_type == "found"
        and report.status == "approved"
    )


def can_withdraw(user, claim):
    """Claimant may withdraw only before the handover starts."""
    return (
        user.is_authenticated
        and user.is_active
        and claim.claimant_id == user.id
        and claim.status in claim.BEFORE_HANDOVER
    )


def can_resubmit(user, claim):
    """Claimant may resubmit only while evidence is awaited."""
    return (
        user.is_authenticated
        and user.is_active
        and claim.claimant_id == user.id
        and claim.status == "awaiting_evidence"
    )


def report_actions(user, report):
    actions = []
    in_progress = report.claims.filter(status="handover_in_progress").exists()
    if is_service_staff(user):
        actions.extend(action for action in report.staff_actions
                       if action["key"] != "close" or not in_progress)
    if can_manage_report(user, report):
        reserved = report.claims.filter(status__in=[
            "awaiting_handover", "under_dispute", "handover_in_progress",
        ]).exists()
        if report.status not in ("closed", "close_requested") and not reserved:
            actions.append({"key": "edit", "label": "Edit", "reason": False})
        if report.status not in ("closed", "close_requested") and not in_progress:
            actions.append({"key": "request_close", "label": "Request closure", "reason": False})
    if can_claim(user, report):
        actions.append({"key": "claim", "label": "Submit claim", "reason": False})
    return actions


def claim_actions(user, claim):
    actions = []
    if is_service_staff(user):
        for action in claim.staff_actions:
            if action["key"] in ("approve", "start_handover") and (
                claim.report.status != "approved" or claim.report.report_type != "found"
            ):
                continue
            if action["key"] == "approve" and claim.reservation_conflict_exists():
                continue
            actions.append(action)
    if can_resubmit(user, claim):
        actions.append({"key": "resubmit", "label": "Resubmit evidence", "reason": False})
    if can_withdraw(user, claim):
        actions.append({"key": "withdraw", "label": "Withdraw claim", "reason": False})
    return actions
