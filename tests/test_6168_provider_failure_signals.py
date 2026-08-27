"""Unit tests for the #6168 provider failure-signal detector.

failure_signals.py is stdlib-only, so it is imported directly by path here
and needs none of the pylon/arbiter stubs the other provider_worker tests use.
"""

import importlib.util
from pathlib import Path

import pytest

PLUGIN_ROOT = Path(__file__).parents[1]


def _load_failure_signals():
    spec = importlib.util.spec_from_file_location(
        "provider_worker_failure_signals_6168",
        PLUGIN_ROOT / "utils" / "failure_signals.py",
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def failure_signals():
    return _load_failure_signals()


def test_terminal_statuses_include_failed(failure_signals):
    assert failure_signals.TERMINAL_STATUSES == {"Completed", "Error", "Failed"}
    assert failure_signals.FAILURE_STATUSES == {"Error", "Failed"}


@pytest.mark.parametrize("status", ["Started", "InProgress", "Completed", None, "SomethingElse"])
def test_non_failure_statuses_return_none(failure_signals, status):
    assert failure_signals.detect_provider_failure(status, "timeout_error") is None


@pytest.mark.parametrize("status", ["Error", "Failed"])
@pytest.mark.parametrize(
    "category,expected_class",
    [
        ("timeout", "infrastructure"),
        ("timeout_error", "infrastructure"),
        ("service_busy", "infrastructure"),
        ("rate_limit", "infrastructure"),
        ("out_of_memory", "infrastructure"),
        ("killed", "infrastructure"),
        ("terminated", "infrastructure"),
        ("deadline_exceeded", "infrastructure"),
        ("backoff_limit_exceeded", "infrastructure"),
        ("scheduling_failed", "infrastructure"),
        ("platform_upload_failed", "infrastructure"),
        ("artifact_error", "infrastructure"),
        ("invalid_input", "input"),
        ("input_error", "input"),
        ("resource_not_found", "input"),
        ("branch_not_found", "input"),
        ("repository_not_found", "input"),
        ("empty_repository", "input"),
        ("runtime_error", "tool_internal"),
        ("training_failed", "tool_internal"),
        ("inference_failed", "tool_internal"),
        ("indexing_failed", "tool_internal"),
        ("authentication_error", "policy"),
    ],
)
def test_full_published_table(failure_signals, status, category, expected_class):
    result = failure_signals.detect_provider_failure(status, category)
    assert result == {"error_category": category, "would_be_error_class": expected_class}


@pytest.mark.parametrize("status", ["Error", "Failed"])
@pytest.mark.parametrize("category", ["unknown_error", "totally_unmapped_category"])
def test_unmapped_category_returns_none_class_but_preserves_raw_category(failure_signals, status, category):
    result = failure_signals.detect_provider_failure(status, category)
    assert result == {"error_category": category, "would_be_error_class": None}


def test_failure_without_category(failure_signals):
    result = failure_signals.detect_provider_failure("Error", None)
    assert result == {"error_category": None, "would_be_error_class": None}


def test_classify_category_matches_detect_provider_failure_table(failure_signals):
    """classify_category is status-independent so rung 2 (declared errors/warnings on a
    Completed response) can still classify a known category (2nd review)."""
    for category, expected_class in failure_signals.PROVIDER_CATEGORY_CLASSES.items():
        assert failure_signals.classify_category(category) == expected_class
    assert failure_signals.classify_category("unknown_error") is None
    assert failure_signals.classify_category(None) is None
