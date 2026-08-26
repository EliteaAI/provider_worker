import json
from types import SimpleNamespace

from utils.budget_errors import budget_error_from_provider_response


def test_provider_budget_marker_becomes_typed_sdk_exception():
    response = SimpleNamespace(errors=json.dumps({
        "type": "budget_exceeded",
        "code": "member_budget_exceeded",
        "message": "Your project budget is exhausted.",
    }))

    error = budget_error_from_provider_response(response)

    assert error.scope == "member_budget_exceeded"
    assert "budget is exhausted" in str(error)


def test_non_budget_provider_error_is_ignored():
    response = SimpleNamespace(errors=json.dumps({
        "type": "rate_limit",
        "message": "Try later",
    }))

    assert budget_error_from_provider_response(response) is None


def test_budget_marker_in_result_survives_older_response_models():
    response = SimpleNamespace(
        errors=None,
        result=json.dumps([{
            "object_type": "message",
            "data": "Friendly budget message",
            "error_category": "budget_exceeded",
            "budget_error_code": "project_budget_exceeded",
        }]),
    )

    error = budget_error_from_provider_response(response)

    assert error.scope == "project_budget_exceeded"
    assert str(error) == "Friendly budget message"
