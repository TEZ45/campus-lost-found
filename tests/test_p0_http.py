import json
from http.cookiejar import CookieJar
from io import StringIO
from urllib.error import HTTPError
from urllib.request import HTTPCookieProcessor, ProxyHandler, Request, build_opener

import pytest
from django.core.management import call_command

from apps.reports.models import Notice, NotificationEvent

from .test_workflow import make_claim, make_report


@pytest.mark.django_db(transaction=True)
def test_handover_and_notification_failure_over_local_http(live_server, users, monkeypatch):
    staff = users["service"]
    staff.set_password("Local-handover-test-password-83")
    staff.save()
    report = make_report(users["owner"])
    claim = make_claim(report, users["claimant"])
    other = make_claim(report, users["other"])
    browser = build_opener(ProxyHandler({}), HTTPCookieProcessor(CookieJar()))

    def request(path, data=None, csrf=None):
        headers = {"Content-Type": "application/json"}
        if csrf:
            headers["X-CSRFToken"] = csrf
        body = json.dumps(data).encode() if data is not None else None
        message = Request(f"{live_server.url}/api/v1/{path}", data=body, headers=headers)
        try:
            response = browser.open(message, timeout=10)
        except HTTPError as exc:
            response = exc
        with response:
            return response.status, json.load(response)

    status, token = request("auth/csrf/")
    assert status == 200
    status, session = request("auth/login/", {
        "username": staff.username, "password": "Local-handover-test-password-83",
    }, token["csrf_token"])
    assert status == 200
    csrf = session["csrf_token"]
    path = f"staff/claims/{claim.pk}/actions/"
    assert request(path, {"action": "approve"}, csrf)[0] == 400
    assert request(path, {
        "action": "approve", "confirmations": {"evidence_reviewed": True, "custody_confirmed": True},
    }, csrf)[0] == 200
    assert request(path, {
        "action": "start_handover", "confirmations": {"identity_verified": True, "custody_confirmed": True},
    }, csrf)[0] == 200
    assert request(path, {
        "action": "complete_handover", "confirmations": {"transfer_confirmed": False},
    }, csrf)[0] == 400
    assert request(f"claims/{claim.pk}/")[1]["status"] == "handover_in_progress"

    complete = {"action": "complete_handover", "confirmations": {"transfer_confirmed": True}}
    assert request(path, complete, csrf)[1]["status"] == "completed"
    assert request(f"claims/{claim.pk}/")[1]["status"] == "completed"
    assert request(path, complete, csrf)[0] == 409
    assert claim.history.filter(action="handover_completed").count() == 1

    def fail_delivery(**kwargs):
        raise RuntimeError("Notice storage temporarily unavailable")

    with monkeypatch.context() as patch:
        patch.setattr(Notice.objects, "create", fail_delivery)
        output = StringIO()
        call_command("deliver_notifications", once=True, stdout=output)
        assert json.loads(output.getvalue())["failed"] > 0
    assert request(f"claims/{claim.pk}/")[1]["status"] == "completed"
    report.refresh_from_db()
    other.refresh_from_db()
    assert (report.status, other.status) == ("closed", "invalidated")
    assert not Notice.objects.exists()

    call_command("deliver_notifications", once=True, stdout=StringIO())
    expected = NotificationEvent.objects.count()
    assert Notice.objects.count() == expected
    assert not NotificationEvent.objects.filter(delivered_at__isnull=True).exists()
    call_command("deliver_notifications", once=True, stdout=StringIO())
    assert Notice.objects.count() == expected
