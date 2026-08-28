"""Pure, stdlib-only helpers for detecting provider tool-invocation failures.

No pylon/langchain/datamodel imports so this module can be unit-tested outside
the plugin venv (datamodel_code_generator only exists inside the container).
"""
from typing import Optional

TERMINAL_STATUSES = {"Completed", "Error", "Failed"}
FAILURE_STATUSES = {"Error", "Failed"}

# Values are the string values of elitea_sdk's ToolErrorClass ("infrastructure" |
# "input" | "tool_internal" | "policy"). Kept as plain strings, not the enum,
# since elitea_sdk is not a provider_worker dependency; ToolErrorClass(value)
# reconstructs losslessly on the SDK side.
PROVIDER_CATEGORY_CLASSES = {
    "timeout": "infrastructure",
    "timeout_error": "infrastructure",
    "service_busy": "infrastructure",
    "rate_limit": "infrastructure",
    "out_of_memory": "infrastructure",
    "killed": "infrastructure",
    "terminated": "infrastructure",
    "deadline_exceeded": "infrastructure",
    "backoff_limit_exceeded": "infrastructure",
    "scheduling_failed": "infrastructure",
    "platform_upload_failed": "infrastructure",
    "artifact_error": "infrastructure",
    "invalid_input": "input",
    "input_error": "input",
    "resource_not_found": "input",
    "branch_not_found": "input",
    "repository_not_found": "input",
    "empty_repository": "input",
    "runtime_error": "tool_internal",
    "training_failed": "tool_internal",
    "inference_failed": "tool_internal",
    "indexing_failed": "tool_internal",
    "authentication_error": "policy",
}


def classify_category(error_category: Optional[str]) -> Optional[str]:
    """Map an error_category to its would-be ToolErrorClass, or None if unmapped.

    Independent of invocation status: also used by the declared-errors/warnings
    rung (a Completed response can still carry a known error_category).
    """
    return PROVIDER_CATEGORY_CLASSES.get(error_category)


def detect_provider_failure(status: Optional[str], error_category: Optional[str]) -> Optional[dict]:
    """Return shadow payload fields for a failed provider invocation, or None.

    `status` is the raw invocation status (may already be an enum member's
    `.value`); only "Error"/"Failed" are treated as failures. Unmapped or
    absent categories return `would_be_error_class: None` deliberately —
    that is shadow-mode data, not a bug.
    """
    if status not in FAILURE_STATUSES:
        return None
    return {
        "error_category": error_category,
        "would_be_error_class": classify_category(error_category),
    }
