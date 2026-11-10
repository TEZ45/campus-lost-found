from django.core.exceptions import ValidationError

FIELDS = {
    "evidence_reviewed": {"key": "evidence_reviewed", "label": "I reviewed the ownership evidence."},
    "custody_confirmed": {"key": "custody_confirmed", "label": "The item is in service-point custody."},
    "identity_verified": {"key": "identity_verified", "label": "I verified the claimant's identity."},
    "transfer_confirmed": {"key": "transfer_confirmed", "label": "I confirmed the item was handed over."},
    "safe_outcome": {
        "key": "safe_outcome", "label": "Verified outcome",
        "choices": [
            {"value": "not_transferred", "label": "The item was not handed over"},
            {"value": "recovered", "label": "The item was recovered into service-point custody"},
        ],
    },
}

REQUIRED = {
    "approve": ("evidence_reviewed", "custody_confirmed"),
    "start_dispute": ("custody_confirmed",),
    "resolve_keep": ("custody_confirmed",),
    "resolve_reject": ("custody_confirmed",),
    "start_handover": ("identity_verified", "custody_confirmed"),
    "complete_handover": ("transfer_confirmed",),
    "interrupt_handover": ("safe_outcome",),
}

EVENT_ACTIONS = {
    "approved": "approve", "dispute_started": "start_dispute",
    "dispute_resolved": "resolve_keep", "dispute_rejected": "resolve_reject",
    "handover_started": "start_handover", "handover_completed": "complete_handover",
    "handover_interrupted": "interrupt_handover",
}


def confirmation_fields(action):
    return [FIELDS[key] for key in REQUIRED.get(action, ())]


def validate_confirmations(event, values):
    values = {} if values is None else values
    required = REQUIRED.get(EVENT_ACTIONS.get(event), ())
    if not isinstance(values, dict) or set(values) != set(required):
        raise ValidationError({"confirmations": "Supply exactly the confirmations for this action."})
    for key in required:
        if key == "safe_outcome":
            valid = values[key] in ("not_transferred", "recovered")
        else:
            valid = values[key] is True
        if not valid:
            raise ValidationError({"confirmations": f"A valid {key} confirmation is required."})
    return dict(values)
