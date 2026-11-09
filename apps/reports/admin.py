from django.contrib import admin
from django.contrib.auth.admin import UserAdmin
from django.contrib.auth.models import User

from .models import ActionHistory, Claim, Match, Notice, Report, UserProfile
from .permissions import is_service_staff


class UserProfileInline(admin.StackedInline):
    model = UserProfile
    can_delete = False
    verbose_name_plural = "Service authorisation"

    def has_add_permission(self, request, obj=None):
        return request.user.is_superuser

    def has_change_permission(self, request, obj=None):
        return request.user.is_superuser


class UserAdminWithProfile(UserAdmin):
    inlines = [UserProfileInline]

    def has_delete_permission(self, request, obj=None):
        # Deactivate accounts instead of cascading deletion into claims and history.
        return False


admin.site.unregister(User)
admin.site.register(User, UserAdminWithProfile)


class ServiceReadOnlyAdmin(admin.ModelAdmin):
    actions = None

    def has_module_permission(self, request):
        return is_service_staff(request.user) and super().has_module_permission(request)

    def has_view_permission(self, request, obj=None):
        return is_service_staff(request.user) and super().has_view_permission(request, obj)

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(Report)
class ReportAdmin(ServiceReadOnlyAdmin):
    list_display = ("item_name", "report_type", "category", "status", "location", "event_date", "created_at")
    list_filter = ("report_type", "status", "event_date")
    search_fields = ("item_name", "description", "location", "category")


@admin.register(Claim)
class ClaimAdmin(ServiceReadOnlyAdmin):
    list_display = ("claimant_name", "report", "status", "created_at")
    list_filter = ("status", "created_at")
    search_fields = ("claimant_name", "evidence")


@admin.register(Match)
class MatchAdmin(ServiceReadOnlyAdmin):
    list_display = ("lost_report", "found_report", "score", "status")
    list_filter = ("status",)


@admin.register(Notice)
class NoticeAdmin(ServiceReadOnlyAdmin):
    list_display = ("user", "title", "is_read", "created_at")
    list_filter = ("is_read",)


@admin.register(ActionHistory)
class ActionHistoryAdmin(ServiceReadOnlyAdmin):
    list_display = ("created_at", "actor", "action", "previous_state", "new_state")
    list_filter = ("action",)
    readonly_fields = [f.name for f in ActionHistory._meta.fields]
