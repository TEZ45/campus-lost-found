from django.contrib import admin
from django.contrib.auth.admin import UserAdmin
from django.contrib.auth.models import User

from .models import ActionHistory, Claim, Match, Notice, Report, UserProfile


class UserProfileInline(admin.StackedInline):
    model = UserProfile
    can_delete = False
    verbose_name_plural = "Service authorisation"


class UserAdminWithProfile(UserAdmin):
    inlines = [UserProfileInline]


admin.site.unregister(User)
admin.site.register(User, UserAdminWithProfile)


@admin.register(Report)
class ReportAdmin(admin.ModelAdmin):
    list_display = ("item_name", "report_type", "category", "status", "location", "event_date", "created_at")
    list_filter = ("report_type", "status", "event_date")
    search_fields = ("item_name", "description", "location", "category")
    actions = ["approve_reports", "reject_reports", "close_reports"]

    def approve_reports(self, request, queryset):
        queryset.update(status="approved")
    approve_reports.short_description = "Approve selected reports"

    def reject_reports(self, request, queryset):
        queryset.update(status="rejected")
    reject_reports.short_description = "Reject selected reports"

    def close_reports(self, request, queryset):
        queryset.update(status="closed")
    close_reports.short_description = "Close selected reports"


@admin.register(Claim)
class ClaimAdmin(admin.ModelAdmin):
    list_display = ("claimant_name", "report", "status", "created_at")
    list_filter = ("status", "created_at")
    search_fields = ("claimant_name", "evidence")


@admin.register(Match)
class MatchAdmin(admin.ModelAdmin):
    list_display = ("lost_report", "found_report", "score", "status")
    list_filter = ("status",)


@admin.register(Notice)
class NoticeAdmin(admin.ModelAdmin):
    list_display = ("user", "title", "is_read", "created_at")
    list_filter = ("is_read",)


@admin.register(ActionHistory)
class ActionHistoryAdmin(admin.ModelAdmin):
    list_display = ("created_at", "actor", "action", "previous_state", "new_state")
    list_filter = ("action",)
    readonly_fields = [f.name for f in ActionHistory._meta.fields]
