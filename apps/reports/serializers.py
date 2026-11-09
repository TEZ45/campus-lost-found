from collections.abc import Mapping

from django.urls import reverse
from rest_framework import serializers

from .forms import validate_image
from .models import ActionHistory, Claim, Match, Notice, Report
from .permissions import can_manage_report, claim_actions, report_actions


class StrictSerializer(serializers.Serializer):
    def to_internal_value(self, data):
        if isinstance(data, Mapping):
            unknown = set(data) - set(self.fields)
            if unknown:
                raise serializers.ValidationError({key: ["Unknown field."] for key in sorted(unknown)})
        return super().to_internal_value(data)


class ReportInput(StrictSerializer, serializers.ModelSerializer):
    photo = serializers.FileField(required=False, allow_null=True)

    class Meta:
        model = Report
        fields = [
            "report_type", "item_name", "category", "description", "location", "event_date",
            "contact_name", "contact_email", "contact_phone", "photo",
        ]

    def validate_photo(self, value):
        return validate_image(value) if value is not None else False


class ClaimInput(StrictSerializer):
    evidence = serializers.CharField()
    images = serializers.ListField(child=serializers.FileField(), required=False, max_length=3)

    def validate_images(self, images):
        return [validate_image(image) for image in images]


class ActionInput(StrictSerializer):
    action = serializers.CharField(max_length=30)
    reason = serializers.CharField(required=False, allow_blank=True, default="")


class LoginInput(StrictSerializer):
    username = serializers.CharField(max_length=150)
    password = serializers.CharField(trim_whitespace=False, write_only=True)


class RegisterInput(StrictSerializer):
    username = serializers.CharField(max_length=150)
    password1 = serializers.CharField(trim_whitespace=False, write_only=True)
    password2 = serializers.CharField(trim_whitespace=False, write_only=True)


class ReportOutput(serializers.ModelSerializer):
    reference = serializers.CharField(read_only=True)
    photo_url = serializers.SerializerMethodField()
    allowed_actions = serializers.SerializerMethodField()

    class Meta:
        model = Report
        fields = [
            "id", "reference", "report_type", "item_name", "category", "description", "location",
            "event_date", "status", "photo_url", "created_at", "updated_at", "allowed_actions",
        ]

    def get_photo_url(self, report):
        return report.photo.url if report.photo else None

    def get_allowed_actions(self, report):
        return report_actions(self.context["request"].user, report)

    def to_representation(self, instance):
        result = super().to_representation(instance)
        if can_manage_report(self.context["request"].user, instance):
            result["contact"] = {
                "name": instance.contact_name,
                "email": instance.contact_email,
                "phone": instance.contact_phone,
            }
        return result


class HistoryOutput(serializers.ModelSerializer):
    class Meta:
        model = ActionHistory
        fields = [
            "id", "actor", "report", "claim", "action", "previous_state", "new_state",
            "reason", "result", "created_at",
        ]


class ClaimOutput(serializers.ModelSerializer):
    reference = serializers.CharField(read_only=True)
    report = ReportOutput(read_only=True)
    attachments = serializers.SerializerMethodField()
    history = HistoryOutput(many=True, read_only=True)
    allowed_actions = serializers.SerializerMethodField()

    class Meta:
        model = Claim
        fields = [
            "id", "reference", "report", "claimant_name", "claimant_email", "claimant_phone",
            "evidence", "status", "attachments", "history", "created_at", "updated_at",
            "allowed_actions",
        ]

    def get_attachments(self, claim):
        return [{"id": attachment.pk, "url": reverse("reports_api:attachment", args=[attachment.pk])}
                for attachment in claim.attachments.all()]

    def get_allowed_actions(self, claim):
        return claim_actions(self.context["request"].user, claim)


class NoticeOutput(serializers.ModelSerializer):
    class Meta:
        model = Notice
        fields = ["id", "title", "body", "link", "is_read", "created_at"]


class MatchOutput(serializers.ModelSerializer):
    lost_report = ReportOutput(read_only=True)
    found_report = ReportOutput(read_only=True)

    class Meta:
        model = Match
        fields = ["id", "lost_report", "found_report", "score", "reason", "status"]
