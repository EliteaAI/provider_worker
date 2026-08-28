"""Translate provider budget failures into the SDK's terminal exception."""

import json
from typing import Any, Optional

from elitea_sdk.runtime.exceptions import (
    BUDGET_ERROR_TYPE,
    DEFAULT_BUDGET_SCOPE,
    BUDGET_SCOPES,
    BudgetExceededError,
)


def _budget_detail(value: Any) -> Optional[dict]:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (TypeError, ValueError):
            return None

    if isinstance(value, list):
        for item in value:
            detail = _budget_detail(item)
            if detail is not None:
                return detail
        return None

    if not isinstance(value, dict):
        return None

    detail = value.get("error") if isinstance(value.get("error"), dict) else value
    if detail.get("type") == BUDGET_ERROR_TYPE:
        return detail
    if detail.get("error_category") == BUDGET_ERROR_TYPE:
        return {
            "type": BUDGET_ERROR_TYPE,
            "code": detail.get("budget_error_code"),
            "message": detail.get("data") or detail.get("message"),
        }
    return None


def budget_error_from_provider_response(response: Any) -> Optional[BudgetExceededError]:
    """Return the SDK exception encoded in a provider's standard ``errors`` field."""
    detail = _budget_detail(getattr(response, "errors", None))
    if detail is None:
        detail = _budget_detail(getattr(response, "result", None))
    if detail is None:
        return None

    scope = detail.get("code")
    if scope not in BUDGET_SCOPES:
        scope = DEFAULT_BUDGET_SCOPE
    return BudgetExceededError(detail.get("message") or "Budget exceeded", scope)
