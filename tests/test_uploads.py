from io import BytesIO

import pytest
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from PIL import Image

from apps.reports.forms import (
    MAX_IMAGE_SIZE,
    ClaimForm,
    ReportForm,
    SingleImageField,
    validate_image,
)
from apps.reports.models import ClaimAttachment

from .test_workflow import make_claim, make_report


def upload(image_format="PNG", name=None, content_type=None):
    data = BytesIO()
    Image.new("RGB", (16, 16), (20, 60, 90)).save(data, image_format)
    extension = "jpg" if image_format == "JPEG" else image_format.lower()
    return SimpleUploadedFile(
        name or f"proof.{extension}", data.getvalue(),
        content_type=content_type or f"image/{'jpeg' if image_format == 'JPEG' else extension}",
    )


@pytest.mark.parametrize("image_format", ["PNG", "JPEG"])
def test_validator_decodes_valid_images_and_rewinds(image_format):
    image = upload(image_format)
    expected = image.read()
    assert validate_image(image) is image
    assert image.tell() == 0
    assert image.read() == expected


@pytest.mark.parametrize("image", [
    SimpleUploadedFile("proof.png", b"not an image", content_type="image/png"),
    upload("GIF", name="proof.png", content_type="image/png"),
    upload("PNG", content_type="image/gif"),
    upload("PNG", content_type="image/jpeg"),
    upload("PNG", name="proof.html"),
])
def test_validator_rejects_forged_content_and_metadata(image):
    with pytest.raises(ValidationError):
        validate_image(image)


@pytest.mark.parametrize("image_format", ["PNG", "JPEG"])
def test_validator_rejects_truncated_images(image_format):
    image = upload(image_format)
    broken = SimpleUploadedFile(image.name, image.read()[:-20], content_type=image.content_type)
    with pytest.raises(ValidationError):
        validate_image(broken)


def test_validator_rejects_oversize_before_decoding():
    image = SimpleUploadedFile("large.png", b"x" * (MAX_IMAGE_SIZE + 1), content_type="image/png")
    with pytest.raises(ValidationError, match="5 MB"):
        validate_image(image)


def test_validator_rejects_decompression_bombs(monkeypatch):
    image = upload()
    monkeypatch.setattr(Image, "MAX_IMAGE_PIXELS", 100)
    with pytest.raises(ValidationError):
        validate_image(image)


def test_optional_image_preserves_empty_and_clear_values():
    field = SingleImageField(required=False)
    assert field.clean(None) is None
    assert field.clean(False) is False


def report_data(report):
    return {name: getattr(report, name) for name in ReportForm.Meta.fields if name != "photo"}


@pytest.mark.django_db
def test_report_form_keeps_existing_image_without_new_upload(users):
    report = make_report(users["owner"])
    report.photo.save("proof.png", upload())
    original = report.photo.name
    form = ReportForm(report_data(report), instance=report)
    assert form.is_valid(), form.errors
    assert form.save().photo.name == original


@pytest.mark.django_db
def test_report_form_clears_existing_image(users):
    report = make_report(users["owner"])
    report.photo.save("proof.png", upload())
    data = report_data(report)
    data["photo-clear"] = "on"
    form = ReportForm(data, instance=report)
    assert form.is_valid(), form.errors
    assert not form.save().photo


@pytest.mark.django_db
def test_report_form_rejects_invalid_replacement_and_preserves_saved_image(users):
    report = make_report(users["owner"])
    report.photo.save("proof.png", upload())
    original = report.photo.name
    form = ReportForm(report_data(report), {
        "photo": upload("GIF", name="proof.png", content_type="image/png"),
    }, instance=report)
    assert not form.is_valid()
    assert "photo" in form.errors
    report.refresh_from_db()
    assert report.photo.name == original


@pytest.mark.parametrize("count,valid", [(3, True), (4, False)])
def test_claim_form_checks_each_submission_limit(count, valid):
    form = ClaimForm({"evidence": "Serial number 123"}, {
        "images": [upload() for _ in range(count)],
    })
    assert form.is_valid() is valid


def test_claim_form_rejects_one_invalid_image_in_batch():
    form = ClaimForm({"evidence": "Serial number 123"}, {
        "images": [upload(), SimpleUploadedFile("fake.png", b"fake", content_type="image/png")],
    })
    assert not form.is_valid()
    assert "images" in form.errors


@pytest.mark.django_db
@pytest.mark.parametrize("existing,valid", [(2, True), (3, False)])
def test_claim_form_checks_total_attachment_limit(users, existing, valid):
    claim = make_claim(make_report(users["owner"]), users["claimant"])
    for index in range(existing):
        ClaimAttachment.objects.create(claim=claim, image=f"claims/{index}.png")
    form = ClaimForm({"evidence": "More proof"}, {
        "images": [upload() for _ in range(3)],
    }, instance=claim)
    assert form.is_valid() is valid
