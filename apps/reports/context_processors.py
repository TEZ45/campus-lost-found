def unread_notices(request):
    """Provide unread notice count and service-staff flag for the navigation bar."""
    if not request.user.is_authenticated:
        return {"unread_count": 0, "is_service_staff": False}
    from .models import Notice
    from .views import _is_service_staff
    return {
        "unread_count": Notice.objects.filter(user=request.user, is_read=False).count(),
        "is_service_staff": _is_service_staff(request.user),
    }
