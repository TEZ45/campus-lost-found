import json
from http.cookiejar import CookieJar
from urllib.error import HTTPError
from urllib.request import HTTPCookieProcessor, ProxyHandler, Request, build_opener

import pytest


@pytest.mark.django_db(transaction=True)
def test_session_and_report_flow_over_local_http(live_server, users):
    users["owner"].set_password("Local-http-test-password-57")
    users["owner"].save()
    browser = build_opener(ProxyHandler({}), HTTPCookieProcessor(CookieJar()))
    anonymous = build_opener(ProxyHandler({}), HTTPCookieProcessor(CookieJar()))

    def request(client, path, data=None, csrf=None):
        headers = {"Content-Type": "application/json"}
        if csrf:
            headers["X-CSRFToken"] = csrf
        body = json.dumps(data).encode() if data is not None else None
        message = Request(f"{live_server.url}/api/v1/{path}", data=body, headers=headers)
        try:
            response = client.open(message, timeout=10)
        except HTTPError as exc:
            response = exc
        with response:
            raw = response.read()
            return response.status, json.loads(raw) if raw else None

    credentials = {"username": "owner", "password": "Local-http-test-password-57"}
    assert request(browser, "auth/login/", credentials)[0] == 403
    status, body = request(browser, "auth/csrf/")
    assert status == 200
    status, body = request(browser, "auth/login/", credentials, body["csrf_token"])
    assert status == 200
    csrf = body["csrf_token"]
    status, report = request(browser, "reports/", {
        "report_type": "found", "item_name": "HTTP wallet", "location": "Library",
        "event_date": "2026-10-01", "contact_name": "Owner",
    }, csrf)
    assert status == 201
    assert report["status"] == "pending"
    path = f"reports/{report['id']}/"
    assert request(browser, path)[0] == 200
    assert request(anonymous, path)[0] == 404
    assert request(browser, "auth/logout/", {}, csrf)[0] == 204
    assert request(browser, "auth/me/")[0] == 401
