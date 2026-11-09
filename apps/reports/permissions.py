"""Access rules for reports, claims and staff queues."""
from functools import wraps

from django.shortcuts import redirect, render


def is_service_staff(user):
    """Service-staff authorisation; the admin flag alone does not grant it."""
    if not user.is_authenticated or not user.is_active:
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
        and report.report_type == "found"
        and report.status == "approved"
    )


def can_withdraw(user, claim):
    """Claimant may withdraw only before the handover starts."""
    return (
        user.is_authenticated
        and claim.claimant_id == user.id
        and claim.status in claim.BEFORE_HANDOVER
    )


def can_resubmit(user, claim):
    """Claimant may resubmit only while evidence is awaited."""
    return (
        user.is_authenticated
        and claim.claimant_id == user.id
        and claim.status == "awaiting_evidence"
    )
