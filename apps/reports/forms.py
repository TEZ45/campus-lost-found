import warnings
from pathlib import Path

from django import forms
from django.core.exceptions import ValidationError
from PIL import Image

from .models import Claim, Report

MAX_IMAGE_SIZE = 5 * 1024 * 1024  # CAP-3.1: 5 MB per image
ALLOWED_IMAGE_TYPES = ["image/jpeg", "image/png"]
MAX_IMAGES_PER_SUBMISSION = 3  # CAP-3.1
MAX_IMAGES_PER_CLAIM = 5  # CAP-3.1


def validate_image(image):
    if image.size > MAX_IMAGE_SIZE:
        raise ValidationError(f"Image exceeds the 5 MB limit ({image.size // 1024} KB).")
    content_type = getattr(image, "content_type", None)
    if content_type not in ALLOWED_IMAGE_TYPES:
        raise ValidationError("Only JPG and PNG images are allowed.")
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            image.seek(0)
            with Image.open(image) as decoded:
                expected = {"JPEG": ("image/jpeg", {".jpg", ".jpeg"}),
                            "PNG": ("image/png", {".png"})}.get(decoded.format)
                if (
                    not expected or content_type != expected[0]
                    or Path(image.name).suffix.lower() not in expected[1]
                ):
                    raise ValidationError("The file type must match a JPG or PNG image.")
                decoded.verify()
            image.seek(0)
            with Image.open(image) as decoded:
                decoded.load()
    except (OSError, ValueError, SyntaxError, Image.DecompressionBombError,
            Image.DecompressionBombWarning) as exc:
        raise ValidationError("Upload a complete, valid JPG or PNG image.") from exc
    finally:
        image.seek(0)
    return image


class SingleImageField(forms.ImageField):
    def to_python(self, data):
        image = forms.FileField.to_python(self, data)
        return validate_image(image) if image is not None else None


class MultipleFileInput(forms.ClearableFileInput):
    allow_multiple_selected = True


class MultipleImageField(forms.FileField):
    """Accepts multiple image uploads; validates CAP-3.1 constraints per submission."""

    def __init__(self, *args, **kwargs):
        kwargs.setdefault("widget", MultipleFileInput(attrs={"multiple": True}))
        super().__init__(*args, **kwargs)

    def clean(self, data, initial=None):
        if isinstance(data, (list, tuple)):
            files = data
        elif data is None or data == "" or data is False:
            return []
        else:
            files = [data]
        files = [f for f in files if f]
        if len(files) > MAX_IMAGES_PER_SUBMISSION:
            raise ValidationError(f"At most {MAX_IMAGES_PER_SUBMISSION} images per submission.")
        field = SingleImageField(required=self.required)
        return [field.clean(file) for file in files]


class ReportForm(forms.ModelForm):
    photo = SingleImageField(required=False)

    class Meta:
        model = Report
        fields = ["report_type", "item_name", "category", "description", "location",
                  "event_date", "contact_name", "contact_email", "contact_phone", "photo"]
        widgets = {
            "event_date": forms.DateInput(attrs={"type": "date"}),
            "description": forms.Textarea(attrs={"rows": 4}),
        }


class ClaimForm(forms.ModelForm):
    images = MultipleImageField(required=False, label="Evidence images (optional, up to 3)")

    class Meta:
        model = Claim
        fields = ["evidence"]
        widgets = {
            "evidence": forms.Textarea(attrs={"rows": 4,
                                              "help_text": "Describe unique features of the item to prove ownership"}),
        }

    def clean(self):
        cleaned = super().clean()
        images = cleaned.get("images") or []
        claim = getattr(self, "instance", None)
        if claim and claim.pk:
            existing = claim.attachments.count()
            if existing + len(images) > MAX_IMAGES_PER_CLAIM:
                raise ValidationError(
                    f"This claim already has {existing} image(s); at most "
                    f"{MAX_IMAGES_PER_CLAIM} in total."
                )
        return cleaned

class SearchForm(forms.Form):
    q = forms.CharField(required=False, label="Keyword")
    report_type = forms.ChoiceField(
        required=False, choices=[("", "Any type"), ("lost", "Lost Item"), ("found", "Found Item")]
    )
    category = forms.CharField(required=False, label="Category")
    location = forms.CharField(required=False, label="Location")
    date_from = forms.DateField(required=False, widget=forms.DateInput(attrs={"type": "date"}))
    date_to = forms.DateField(required=False, widget=forms.DateInput(attrs={"type": "date"}))
    status = forms.ChoiceField(
        required=False,
        choices=[("", "Any status"), ("approved", "Approved"), ("pending", "Pending"),
                 ("rejected", "Rejected"), ("closed", "Closed")],
    )
