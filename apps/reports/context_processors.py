from .models import Notice
from .permissions import is_service_staff


def unread_notices(request):
    """Provide unread notice count and service-staff flag for the navigation bar."""
    if not request.user.is_authenticated:
        return {"unread_count": 0, "is_service_staff": False}
    return {
        "unread_count": Notice.objects.filter(user=request.user, is_read=False).count(),
        "is_service_staff": is_service_staff(request.user),
    }
