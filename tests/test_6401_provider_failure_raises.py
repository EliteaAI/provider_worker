"""#6401: a provider failure status becomes a real tool failure.

Same harness as test_6168_no_control_flow_change.py. The invariant that matters most
here is ordering: composition (and therefore the media file_modified side channel) has
to run before the raise, or a partially successful imagegen run silently loses its
images.
"""

import json
import sys
import types
from pathlib import Path
from unittest.mock import Mock

import pytest
from langchain_core.tools import ToolException

PLUGIN_ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(Path(__file__).parent))
from test_6168_no_control_flow_change import FakeResponse, _build_toolkit  # noqa: E402
from test_5854_dependency_contract import _load_provider_tools  # noqa: E402


@pytest.fixture
def provider_tools(monkeypatch):
    return _load_provider_tools(monkeypatch)


@pytest.mark.parametrize("status", ["Error", "Failed"])
def test_failure_status_raises_with_provider_category_attached(provider_tools, status):
    response = FakeResponse(
        status, "plain text", error_category="timeout", error_type="UpstreamTimeout",
        message="tool failed", details="upstream did not answer",
    )
    toolkit = _build_toolkit(provider_tools, "t", {}, response)

    with pytest.raises(ToolException) as excinfo:
        toolkit._run_tool("t")

    # The SDK classifier reads these off the instance; no ToolErrorClass in this repo.
    assert excinfo.value.provider_error_category == "timeout"
    assert excinfo.value.provider_error_type == "UpstreamTimeout"
    assert "tool failed" in str(excinfo.value)
    assert "upstream did not answer" in str(excinfo.value)


def test_failure_without_category_still_raises(provider_tools):
    """An unmapped/absent category must not suppress the failure, only the classification."""
    response = FakeResponse("Error", "plain text", message="boom")
    toolkit = _build_toolkit(provider_tools, "t", {}, response)

    with pytest.raises(ToolException) as excinfo:
        toolkit._run_tool("t")

    assert excinfo.value.provider_error_category is None


def test_file_modified_dispatches_before_the_raise(provider_tools, monkeypatch):
    """The regression that matters: a failing-but-partial imagegen run keeps its images."""
    dispatched = Mock()
    monkeypatch.setattr(provider_tools, "dispatch_custom_event", dispatched)

    result_data = [
        {"object_type": "image", "filepath": "/bucket/first.png", "meta": {}},
        {"object_type": "image", "filepath": "/bucket/second.png", "meta": {}},
    ]
    response = FakeResponse("Error", json.dumps(result_data), error_category="artifact_error")
    tool_metadata = {
        "result_composition": "list_of_objects",
        "result_objects": [{"object_type": "image"}],
    }
    toolkit = _build_toolkit(provider_tools, "t", tool_metadata, response)

    with pytest.raises(ToolException):
        toolkit._run_tool("t")

    assert dispatched.call_count == 2
    first_call, second_call = dispatched.call_args_list
    assert first_call.kwargs["data"]["filepath"] == "/bucket/first.png"
    assert second_call.kwargs["data"]["filepath"] == "/bucket/second.png"


def test_unparseable_result_still_raises_the_category_bearing_exception(provider_tools):
    """A failing response often carries a result the composer cannot parse; the
    category-bearing exception must win over the composer's prose-only one."""
    response = FakeResponse(
        "Error", "not json at all", error_category="runtime_error", message="broke",
    )
    toolkit = _build_toolkit(provider_tools, "t", {"result_composition": "json_object"}, response)

    with pytest.raises(ToolException) as excinfo:
        toolkit._run_tool("t")

    assert excinfo.value.provider_error_category == "runtime_error"


def test_completed_with_declared_errors_does_not_raise(provider_tools):
    response = FakeResponse("Completed", "plain text", errors=["upstream returned odd data"])
    toolkit = _build_toolkit(provider_tools, "t", {}, response)
    assert toolkit._run_tool("t") == "plain text"


def test_completed_with_declared_warnings_does_not_raise(provider_tools):
    response = FakeResponse("Completed", "plain text", warnings=["may be truncated"])
    toolkit = _build_toolkit(provider_tools, "t", {}, response)
    assert toolkit._run_tool("t") == "plain text"


def test_completed_with_a_known_error_category_does_not_raise(provider_tools):
    """Only the status rung enforces: a category on a Completed response is not a failure."""
    response = FakeResponse("Completed", "plain text", error_category="timeout")
    toolkit = _build_toolkit(provider_tools, "t", {}, response)
    assert toolkit._run_tool("t") == "plain text"


def test_budget_rejection_keeps_its_own_exception_type(provider_tools):
    """The budget raise happens before this one, and must not be downgraded to ToolException."""
    from utils.budget_errors import budget_error_from_provider_response

    response = FakeResponse(
        "Error", "plain text", error_category="timeout",
        errors=json.dumps({
            "type": "budget_exceeded",
            "code": "member_budget_exceeded",
            "message": "Your project budget is exhausted.",
        }),
    )
    expected = budget_error_from_provider_response(response)
    assert expected is not None, "harness assumption: this response is a budget rejection"

    toolkit = _build_toolkit(provider_tools, "t", {}, response)
    with pytest.raises(type(expected)):
        toolkit._run_tool("t")


def test_reason_from_result_when_message_and_details_are_empty(provider_tools):
    """imagegen reports the reason in `result`, not in message/details — the model still
    needs it, so the raise falls back to the composed result text."""
    response = FakeResponse(
        "Error", "Error: Failed to download source image", error_category="artifact_error",
    )
    toolkit = _build_toolkit(provider_tools, "t", {}, response)

    with pytest.raises(ToolException) as excinfo:
        toolkit._run_tool("t")

    assert "Failed to download source image" in str(excinfo.value)
    assert excinfo.value.provider_error_category == "artifact_error"
