"""Physical handover confirmations must be explicit, authorized and auditable."""
from html.parser import HTMLParser

import pytest
from django.core.exceptions import PermissionDenied, ValidationError
from django.urls import reverse
from django.utils import timezone
from rest_framework.test import APIClient

from apps.reports import services
from apps.reports.models import ActionHistory, Claim, Notice, NotificationEvent, Report

from .test_workflow import make_claim, make_report

pytestmark = pytest.mark.django_db

CASES = {
    "approve": (
        "pending", "awaiting_handover", "approved",
        {"evidence_reviewed": True, "custody_confirmed": True},
    ),
    "start_dispute": (
        "awaiting_handover", "under_dispute", "dispute_started",
        {"custody_confirmed": True},
    ),
    "resolve_keep": (
        "under_dispute", "awaiting_handover", "dispute_resolved",
        {"custody_confirmed": True},
    ),
    "resolve_reject": (
        "under_dispute", "rejected", "dispute_rejected",
        {"custody_confirmed": True},
    ),
    "start_handover": (
        "awaiting_handover", "handover_in_progress", "handover_started",
        {"identity_verified": True, "custody_confirmed": True},
    ),
    "complete_handover": (
        "handover_in_progress", "completed", "handover_completed",
        {"transfer_confirmed": True},
    ),
    "interrupt_handover": (
        "handover_in_progress", "awaiting_handover", "handover_interrupted",
        {"safe_outcome": "not_transferred"},
    ),
}
OMITTED = object()
REASON = "Staff verified the physical item and recorded the outcome."


def invoke(claim, actor, action, confirmations=OMITTED):
    kwargs = {} if confirmations is OMITTED else {"confirmations": confirmations}
    if action in ("resolve_keep", "resolve_reject"):
        return claim.resolve_dispute(actor, action == "resolve_keep", REASON, **kwargs)
    if action in ("start_handover", "complete_handover"):
        return getattr(claim, action)(actor, **kwargs)
    return getattr(claim, action)(actor, REASON, **kwargs)


def ready_claim(users, action):
    report = make_report(users["owner"])
    claim = make_claim(report, users["claimant"])
    state = CASES[action][0]
    if state != "pending":
        invoke(claim, users["service"], "approve", CASES["approve"][3])
    if state == "under_dispute":
        invoke(claim, users["service"], "start_dispute", CASES["start_dispute"][3])
    if state == "handover_in_progress":
        invoke(claim, users["service"], "start_handover", CASES["start_handover"][3])
    # Completion must not invalidate another claimant before confirmation succeeds.
    make_claim(report, users["other"])
    return claim


def database_snapshot():
    """Compare full rows, including timestamps and pre-existing audit/outbox rows."""
    return {
        model.__name__: list(model.objects.order_by("pk").values())
        for model in (Report, Claim, ActionHistory, NotificationEvent, Notice)
    }


def bad_confirmations(action, variant):
    values = dict(CASES[action][3])
    first = next(iter(values))
    if variant == "absent":
        return OMITTED
    if variant == "null":
        return None
    if variant == "missing":
        values.pop(first)
    elif variant == "false":
        values[first] = False
    elif variant == "string":
        values[first] = "true"
    elif variant == "number":
        values[first] = 1
    elif variant == "list":
        return list(values)
    elif variant == "actor":
        values["actor"] = 123
    elif variant == "time":
        values["created_at"] = "2000-01-01T00:00:00Z"
    else:
        raise AssertionError(variant)
    return values


@pytest.mark.parametrize("action", CASES)
@pytest.mark.parametrize(
    "variant", ["absent", "null", "missing", "false", "string", "number", "list", "actor", "time"],
)
def test_model_rejects_invalid_confirmations_without_any_writes(users, action, variant):
    claim = ready_claim(users, action)
    before = database_snapshot()
    with pytest.raises(ValidationError) as error:
        invoke(claim, users["service"], action, bad_confirmations(action, variant))
    assert "confirmations" in error.value.message_dict
    assert database_snapshot() == before


@pytest.mark.parametrize("action", CASES)
def test_model_records_valid_confirmation_actor_time_and_event(users, action):
    claim = ready_claim(users, action)
    previous, expected, event, confirmations = CASES[action]
    started = timezone.now()
    invoke(claim, users["service"], action, dict(confirmations))
    finished = timezone.now()
    claim.refresh_from_db()
    history = ActionHistory.objects.get(claim=claim, action=event)
    assert claim.status == expected
    assert history.previous_state == previous
    assert history.new_state == expected
    assert history.confirmations == confirmations
    assert history.actor == users["service"]
    assert started <= history.created_at <= finished
    assert claim.reviewed_by == users["service"]
    assert started <= claim.reviewed_at <= finished
    notification = NotificationEvent.objects.get(history=history)
    assert notification.user == users["claimant"]
    if action in ("start_handover", "complete_handover"):
        prefix = "handover_started" if action == "start_handover" else "handover_completed"
        assert getattr(claim, f"{prefix}_by") == users["service"]
        assert started <= getattr(claim, f"{prefix}_at") <= finished


@pytest.mark.parametrize("outcome", ["not_transferred", "recovered"])
def test_interruption_preserves_reservation_for_each_safe_outcome(users, outcome):
    claim = ready_claim(users, "interrupt_handover")
    invoke(claim, users["service"], "interrupt_handover", {"safe_outcome": outcome})
    assert claim.status == "awaiting_handover"
    assert claim.holds_reservation
    assert Report.objects.get(pk=claim.report_id).status == "approved"
    assert claim.history.get(action="handover_interrupted").confirmations == {
        "safe_outcome": outcome,
    }


@pytest.mark.parametrize("action", CASES)
@pytest.mark.parametrize("role", ["claimant", "admin"])
@pytest.mark.parametrize("boundary", ["model", "service"])
def test_permission_precedes_confirmation_validation(users, action, role, boundary):
    claim = ready_claim(users, action)
    before = database_snapshot()
    with pytest.raises(PermissionDenied):
        if boundary == "model":
            invoke(claim, users[role], action, {"actor": users["service"].pk})
        else:
            services.act_on_claim(users[role], claim.pk, action, REASON, confirmations=[])
    assert database_snapshot() == before


@pytest.fixture
def api_client():
    return APIClient()


def action_url(claim):
    return reverse("reports_api:claim-action", args=[claim.pk])


@pytest.mark.parametrize("action", CASES)
@pytest.mark.parametrize(
    "variant", ["absent", "null", "missing", "false", "string", "number", "list", "actor", "time"],
)
def test_api_rejects_malformed_or_forged_confirmations(api_client, users, action, variant):
    claim = ready_claim(users, action)
    api_client.force_login(users["service"])
    payload = {"action": action, "reason": REASON}
    confirmations = bad_confirmations(action, variant)
    if confirmations is not OMITTED:
        payload["confirmations"] = confirmations
    before = database_snapshot()
    response = api_client.post(action_url(claim), payload, format="json")
    assert response.status_code == 400
    assert "confirmations" in response.json()["error"]["details"]
    assert database_snapshot() == before
    current = api_client.get(reverse("reports_api:claim", args=[claim.pk]))
    assert current.status_code == 200
    assert current.json()["status"] == CASES[action][0]
    assert database_snapshot() == before


@pytest.mark.parametrize(
    "field,value", [
        ("actor", 123), ("created_at", "2000-01-01T00:00:00Z"),
        ("handover_completed_by", 123), ("handover_completed_at", "2000-01-01T00:00:00Z"),
        ("status", "completed"), ("transfer_confirmed", True),
    ],
)
def test_api_rejects_top_level_fields_even_with_valid_confirmations(
    api_client, users, field, value,
):
    claim = ready_claim(users, "complete_handover")
    api_client.force_login(users["service"])
    before = database_snapshot()
    response = api_client.post(action_url(claim), {
        "action": "complete_handover", "confirmations": {"transfer_confirmed": True},
        field: value,
    }, format="json")
    assert response.status_code == 400
    assert field in response.json()["error"]["details"]
    assert database_snapshot() == before


@pytest.mark.parametrize("action", CASES)
def test_api_valid_confirmation_is_audited(api_client, users, action):
    claim = ready_claim(users, action)
    api_client.force_login(users["service"])
    started = timezone.now()
    response = api_client.post(action_url(claim), {
        "action": action, "reason": REASON, "confirmations": CASES[action][3],
    }, format="json")
    assert response.status_code == 200
    assert response.json()["status"] == CASES[action][1]
    entries = [row for row in response.json()["history"] if row["action"] == CASES[action][2]]
    assert len(entries) == 1
    assert entries[0]["confirmations"] == CASES[action][3]
    assert entries[0]["actor"] == users["service"].pk
    history = ActionHistory.objects.get(pk=entries[0]["id"])
    assert started <= history.created_at <= timezone.now()
    assert NotificationEvent.objects.filter(history=history, user=users["claimant"]).count() == 1


@pytest.mark.parametrize("role", ["claimant", "admin"])
def test_api_permission_precedes_payload_and_current_state_checks(api_client, users, role):
    claim = ready_claim(users, "complete_handover")
    invoke(claim, users["service"], "complete_handover", {"transfer_confirmed": True})
    api_client.force_login(users[role])
    before = database_snapshot()
    response = api_client.post(action_url(claim), {
        "action": "complete_handover", "confirmations": [], "actor": users["service"].pk,
    }, format="json")
    assert response.status_code == 403
    assert database_snapshot() == before


class ActionFormParser(HTMLParser):
    """Keep controls grouped by form so one action cannot satisfy another's inputs."""

    def __init__(self):
        super().__init__()
        self.forms = []
        self.current = None

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        if tag == "form":
            self.current = {"attributes": attributes, "controls": []}
            self.forms.append(self.current)
        elif self.current is not None:
            self.current["controls"].append((tag, attributes))

    def handle_endtag(self, tag):
        if tag == "form":
            self.current = None

    def action_form(self, action, submit):
        matches = [
            form for form in self.forms
            if form["attributes"].get("action") == submit
            and any(attrs.get("name") == "action" and attrs.get("value") == action
                   for _, attrs in form["controls"])
        ]
        assert len(matches) == 1
        return matches[0]


def html_urls(claim, action):
    queue, submit = (
        ("staff_claim_queue", "staff_claim_review") if action == "approve"
        else ("staff_handover_queue", "staff_handover_action")
    )
    return reverse(queue), reverse(submit, args=[claim.pk])


@pytest.mark.parametrize("action", CASES)
def test_html_requires_explicit_unchecked_confirmations(client, users, action):
    claim = ready_claim(users, action)
    client.force_login(users["service"])
    queue, submit = html_urls(claim, action)
    before = database_snapshot()
    response = client.get(queue)
    assert response.status_code == 200
    parser = ActionFormParser()
    parser.feed(response.content.decode())
    form = parser.action_form(action, submit)
    assert form["attributes"]["method"].lower() == "post"
    assert form["attributes"]["action"] == submit
    for key in CASES[action][3]:
        controls = [(tag, attrs) for tag, attrs in form["controls"] if attrs.get("name") == key]
        assert len(controls) == 1
        tag, attrs = controls[0]
        assert "required" in attrs
        if key == "safe_outcome":
            assert tag == "select"
            options = [attrs for tag, attrs in form["controls"] if tag == "option"]
            assert [option.get("value") for option in options] == [
                "", "not_transferred", "recovered",
            ]
            assert all("selected" not in option for option in options[1:])
        else:
            assert tag == "input"
            assert attrs["type"] == "checkbox"
            assert "checked" not in attrs
    assert database_snapshot() == before


@pytest.mark.parametrize("action", CASES)
@pytest.mark.parametrize("variant", ["missing", "false", "extra"])
def test_html_invalid_post_preserves_state_and_audit(client, users, action, variant):
    claim = ready_claim(users, action)
    client.force_login(users["service"])
    _, submit = html_urls(claim, action)
    payload = {
        key: "on" if value is True else value for key, value in CASES[action][3].items()
    }
    if variant == "missing":
        payload.pop(next(iter(payload)))
    elif variant == "false":
        payload[next(iter(payload))] = "false"
    else:
        extra = "evidence_reviewed" if action == "complete_handover" else "transfer_confirmed"
        payload[extra] = "on"
    payload.update(action=action, reason=REASON)
    before = database_snapshot()
    response = client.post(submit, payload, follow=True)
    assert response.status_code == 200
    assert b"Action failed" in response.content
    assert database_snapshot() == before


@pytest.mark.parametrize("action", CASES)
@pytest.mark.parametrize("checkbox,outcome", [("on", "not_transferred"), ("true", "recovered")])
def test_html_valid_post_records_confirmations(client, users, action, checkbox, outcome):
    claim = ready_claim(users, action)
    client.force_login(users["service"])
    _, submit = html_urls(claim, action)
    confirmations = dict(CASES[action][3])
    if action == "interrupt_handover":
        confirmations["safe_outcome"] = outcome
    payload = {key: checkbox if value is True else value for key, value in confirmations.items()}
    response = client.post(submit, {"action": action, "reason": REASON, **payload})
    assert response.status_code == 302
    claim.refresh_from_db()
    assert claim.status == CASES[action][1]
    history = claim.history.get(action=CASES[action][2])
    assert history.confirmations == confirmations
    assert history.actor == users["service"]


@pytest.mark.parametrize("boundary", ["model", "api", "html"])
def test_repeated_completion_and_status_gets_do_not_repeat_effects(
    api_client, client, users, boundary,
):
    claim = ready_claim(users, "complete_handover")
    invoke(claim, users["service"], "complete_handover", {"transfer_confirmed": True})
    before = database_snapshot()
    api_client.force_login(users["service"])
    client.force_login(users["service"])
    if boundary == "model":
        with pytest.raises(ValueError):
            invoke(claim, users["service"], "complete_handover", {"transfer_confirmed": True})
    elif boundary == "api":
        response = api_client.post(action_url(claim), {
            "action": "complete_handover", "confirmations": {"transfer_confirmed": True},
        }, format="json")
        assert response.status_code == 409
    else:
        response = client.post(reverse("staff_handover_action", args=[claim.pk]), {
            "action": "complete_handover", "transfer_confirmed": "on",
        }, follow=True)
        assert response.status_code == 200
        assert b"Action failed" in response.content
    for _ in range(2):
        response = api_client.get(reverse("reports_api:claim", args=[claim.pk]))
        assert response.status_code == 200
        assert response.json()["status"] == "completed"
        assert client.get(reverse("claim_detail", args=[claim.pk])).status_code == 200
    assert api_client.get(action_url(claim)).status_code == 405
    assert client.get(reverse("staff_handover_action", args=[claim.pk])).status_code == 405
    assert database_snapshot() == before
    assert claim.history.filter(action="handover_completed").count() == 1


@pytest.mark.parametrize("action,key", [
    ("approve", "custody_confirmed"), ("start_handover", "custody_confirmed"),
])
@pytest.mark.parametrize("variant", ["missing", "false"])
def test_custody_confirmation_cannot_be_omitted_after_other_checks_pass(users, action, key, variant):
    claim = ready_claim(users, action)
    confirmations = dict(CASES[action][3])
    if variant == "missing":
        confirmations.pop(key)
    else:
        confirmations[key] = False
    before = database_snapshot()
    with pytest.raises(ValidationError):
        invoke(claim, users["service"], action, confirmations)
    assert database_snapshot() == before


@pytest.mark.parametrize("outcome", ["", "transferred", "unknown", "RECOVERED"])
def test_unsafe_or_unknown_interruption_outcome_is_rejected(api_client, users, outcome):
    claim = ready_claim(users, "interrupt_handover")
    api_client.force_login(users["service"])
    before = database_snapshot()
    response = api_client.post(action_url(claim), {
        "action": "interrupt_handover", "reason": REASON,
        "confirmations": {"safe_outcome": outcome},
    }, format="json")
    assert response.status_code == 400
    assert database_snapshot() == before


@pytest.mark.parametrize("action", CASES)
def test_get_cannot_perform_an_available_action(api_client, client, users, action):
    claim = ready_claim(users, action)
    api_client.force_login(users["service"])
    client.force_login(users["service"])
    _, submit = html_urls(claim, action)
    payload = {"action": action, "reason": REASON}
    payload.update({key: "on" if value is True else value for key, value in CASES[action][3].items()})
    before = database_snapshot()
    assert client.get(submit, payload).status_code == 405
    assert api_client.get(action_url(claim), {"action": action}).status_code == 405
    assert database_snapshot() == before
