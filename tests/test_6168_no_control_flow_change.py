"""#6168 guard: detection must not change control flow on the rungs it does not enforce.

Since #6401 a failure *status* does raise, so the Error/Failed composition cases live in
test_6401_provider_failure_raises.py. What stays here: Completed responses return
unchanged, the declared-errors/warnings rungs never raise, and the shadow-log payload.

Reuses the `_load_provider_tools` harness from test_5854_dependency_contract.py
(stubs pylon.core.tools / arbiter / tools, loads utils/tools.py by spec).
"""

import importlib.util
import json
import sys
import types
from pathlib import Path
from unittest.mock import Mock

import pytest
from langchain_core.tools import ToolException

PLUGIN_ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(Path(__file__).parent))
from test_5854_dependency_contract import _load_provider_tools  # noqa: E402


class FakeResponse:
    def __init__(
        self, status, result, error_category=None, error_type=None, message="", details="",
        errors=None, warnings=None,
    ):
        self.status = status
        self.result = result
        self.error_category = error_category
        self.error_type = error_type
        self.message = message
        self.details = details
        self.errors = errors
        self.warnings = warnings


@pytest.fixture
def provider_tools(monkeypatch):
    return _load_provider_tools(monkeypatch)


def _build_toolkit(provider_tools, tool_name, tool_metadata, response, toolkit_id=None, toolkit_type=None):
    toolkit = provider_tools.Toolkit.__new__(provider_tools.Toolkit)
    toolkit.provider_name = "example_provider"
    toolkit.toolkit_name = "search"
    toolkit.original_toolkit_name = "search"
    toolkit.event_toolkit_name = "search"
    toolkit.toolkit_metadata = {}
    toolkit.toolkit_id = toolkit_id
    toolkit.toolkit_type = toolkit_type
    toolkit._user_id = "1"
    toolkit._project_id = "2"
    toolkit.elitea = None
    toolkit.toolkit_configuration = None
    toolkit.api_models = types.SimpleNamespace(ToolInvocationRequest=lambda **kw: kw)
    toolkit.api_client = types.SimpleNamespace(invoke_tool=lambda **kw: response)
    toolkit.tool_info = {
        tool_name: {
            "async_invocation_supported": False,
            "sync_invocation_supported": True,
            "tool_metadata": tool_metadata,
        }
    }
    return toolkit


def test_json_object_composition_unchanged(provider_tools):
    response = FakeResponse("Completed", json.dumps({"result": "hello", "message": ""}), error_category="timeout")
    toolkit = _build_toolkit(provider_tools, "t", {"result_composition": "json_object"}, response)
    assert toolkit._run_tool("t") == "hello"


def test_default_composition_unchanged(provider_tools):
    response = FakeResponse("Completed", "plain text result", error_category="runtime_error")
    toolkit = _build_toolkit(provider_tools, "t", {}, response)
    assert toolkit._run_tool("t") == "plain text result"


def test_list_of_objects_file_modified_ordering_unchanged(provider_tools, monkeypatch):
    """Two image objects must dispatch file_modified, in order. Failing statuses do the
    same before raising - see test_6401_provider_failure_raises.py."""
    dispatched = Mock()
    monkeypatch.setattr(provider_tools, "dispatch_custom_event", dispatched)

    result_data = [
        {"object_type": "image", "filepath": "/bucket/first.png", "meta": {}},
        {"object_type": "image", "filepath": "/bucket/second.png", "meta": {}},
    ]
    response = FakeResponse("Completed", json.dumps(result_data), error_category="artifact_error")
    tool_metadata = {
        "result_composition": "list_of_objects",
        "result_objects": [{"object_type": "image"}],
    }
    toolkit = _build_toolkit(provider_tools, "t", tool_metadata, response)

    result = toolkit._run_tool("t")

    assert json.loads(result) == {"artifacts": result_data}
    assert dispatched.call_count == 2
    first_call, second_call = dispatched.call_args_list
    assert first_call.kwargs["data"]["filepath"] == "/bucket/first.png"
    assert second_call.kwargs["data"]["filepath"] == "/bucket/second.png"


def test_shadow_log_fires_only_for_failure_statuses(provider_tools):
    response = FakeResponse("Completed", "plain text", error_category="timeout")
    toolkit = _build_toolkit(provider_tools, "t", {}, response)
    toolkit._run_tool("t")
    assert not any(
        "TOOL_FAILURE_SHADOW" in str(call.args[0])
        for call in provider_tools.log.warning.call_args_list
    )


@pytest.mark.parametrize("status", ["Error", "Failed"])
def test_shadow_log_fires_for_failure_statuses(provider_tools, status):
    response = FakeResponse(status, "plain text", error_category="timeout")
    toolkit = _build_toolkit(provider_tools, "t", {}, response)

    with pytest.raises(ToolException):
        toolkit._run_tool("t")

    shadow_calls = [
        call for call in provider_tools.log.warning.call_args_list
        if "TOOL_FAILURE_SHADOW" in str(call.args[0])
    ]
    assert len(shadow_calls) == 1
    payload = json.loads(shadow_calls[0].args[1])
    assert payload["would_be_error_class"] == "infrastructure"
    assert payload["error_category"] == "timeout"
    # This rung is enforced by a raise since #6401.
    assert payload["delivered_as_success"] is False


@pytest.mark.parametrize("status", ["Error", "Failed"])
def test_shadow_log_carries_toolkit_attribution_when_present(provider_tools, status):
    """toolkit_id/toolkit_type come from self, not a hardcoded None (see #6168 review)."""
    response = FakeResponse(status, "plain text", error_category="timeout")
    toolkit = _build_toolkit(
        provider_tools, "t", {}, response, toolkit_id=42, toolkit_type="search_provider"
    )

    with pytest.raises(ToolException):
        toolkit._run_tool("t")

    payload = json.loads(
        next(
            call for call in provider_tools.log.warning.call_args_list
            if "TOOL_FAILURE_SHADOW" in str(call.args[0])
        ).args[1]
    )
    assert payload["toolkit_id"] == 42
    assert payload["toolkit_type"] == "search_provider"


@pytest.mark.parametrize("status", ["Error", "Failed"])
def test_shadow_log_toolkit_attribution_defaults_to_none_when_absent(provider_tools, status):
    """Older callers that never set toolkit_id/toolkit_type must not crash _run_tool."""
    response = FakeResponse(status, "plain text", error_category="timeout")
    toolkit = _build_toolkit(provider_tools, "t", {}, response)
    del toolkit.toolkit_id
    del toolkit.toolkit_type

    with pytest.raises(ToolException):
        toolkit._run_tool("t")

    payload = json.loads(
        next(
            call for call in provider_tools.log.warning.call_args_list
            if "TOOL_FAILURE_SHADOW" in str(call.args[0])
        ).args[1]
    )
    assert payload["toolkit_id"] is None
    assert payload["toolkit_type"] is None


def test_shadow_log_fires_on_declared_errors_despite_completed_status(provider_tools):
    """Rung 2 of the signal ladder: a Completed status with a non-empty `errors` list
    is a hidden-failure signal per #6168 and must still be detected."""
    response = FakeResponse("Completed", "plain text", errors=["upstream returned malformed data"])
    toolkit = _build_toolkit(provider_tools, "t", {}, response)

    result = toolkit._run_tool("t")

    assert result == "plain text"
    payload = json.loads(
        next(
            call for call in provider_tools.log.warning.call_args_list
            if "TOOL_FAILURE_SHADOW" in str(call.args[0])
        ).args[1]
    )
    assert payload["detected_by"] == "provider_declared_errors"
    assert payload["would_be_error_class"] is None


def test_shadow_log_declared_errors_classifies_known_category(provider_tools):
    """2nd review: Completed + errors + a known error_category must not lose the
    classification the table already has for it (previously hardcoded to None)."""
    response = FakeResponse(
        "Completed", "plain text", error_category="timeout", errors=["upstream timed out"]
    )
    toolkit = _build_toolkit(provider_tools, "t", {}, response)

    toolkit._run_tool("t")

    payload = json.loads(
        next(
            call for call in provider_tools.log.warning.call_args_list
            if "TOOL_FAILURE_SHADOW" in str(call.args[0])
        ).args[1]
    )
    assert payload["detected_by"] == "provider_declared_errors"
    assert payload["error_category"] == "timeout"
    assert payload["would_be_error_class"] == "infrastructure"


def test_shadow_log_fires_on_declared_warnings_when_no_errors(provider_tools):
    """Rung 2, warnings variant: only checked when errors is empty/absent."""
    response = FakeResponse("Completed", "plain text", warnings=["result may be truncated"])
    toolkit = _build_toolkit(provider_tools, "t", {}, response)

    toolkit._run_tool("t")

    payload = json.loads(
        next(
            call for call in provider_tools.log.warning.call_args_list
            if "TOOL_FAILURE_SHADOW" in str(call.args[0])
        ).args[1]
    )
    assert payload["detected_by"] == "provider_declared_warnings"


def test_shadow_log_status_rung_takes_priority_over_errors_rung(provider_tools):
    """Cheapest-first ladder: when status already detects a failure, rung 2 must not
    also fire (avoids a duplicate line with a different detected_by for one failure)."""
    response = FakeResponse("Error", "plain text", error_category="timeout", errors=["timed out"])
    toolkit = _build_toolkit(provider_tools, "t", {}, response)

    with pytest.raises(ToolException):
        toolkit._run_tool("t")

    shadow_calls = [
        call for call in provider_tools.log.warning.call_args_list
        if "TOOL_FAILURE_SHADOW" in str(call.args[0])
    ]
    assert len(shadow_calls) == 1
    assert json.loads(shadow_calls[0].args[1])["detected_by"] == "provider_status"
