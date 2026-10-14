from django import forms
from .models import Report, Claim


class ReportForm(forms.ModelForm):
    class Meta:
        model = Report
        fields = ["report_type", "item_name", "description", "location", "event_date", "contact_name", "contact_email", "contact_phone", "photo"]
        widgets = {
            "event_date": forms.DateInput(attrs={"type": "date"}),
            "description": forms.Textarea(attrs={"rows": 4}),
        }


class ClaimForm(forms.ModelForm):
    class Meta:
        model = Claim
        fields = ["claimant_name", "claimant_email", "claimant_phone", "evidence", "evidence_photo"]
        widgets = {
            "evidence": forms.Textarea(attrs={"rows": 4, "help_text": "Describe unique features of the item to prove ownership"}),
        }
