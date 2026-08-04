"""Provider/Pydantic/LangChain compatibility contract for issue #5854."""

import importlib.util
from pathlib import Path
import sys
import types
from unittest.mock import Mock

from langchain_core.tools import StructuredTool

PLUGIN_ROOT = Path(__file__).parents[1]


def _load_provider_tools(monkeypatch):
    pylon = types.ModuleType("pylon")
    pylon_core = types.ModuleType("pylon.core")
    pylon_tools = types.ModuleType("pylon.core.tools")
    pylon_tools.log = types.SimpleNamespace(
        debug=Mock(),
        error=Mock(),
        exception=Mock(),
        info=Mock(),
        warning=Mock(),
    )
    monkeypatch.setitem(sys.modules, "pylon", pylon)
    monkeypatch.setitem(sys.modules, "pylon.core", pylon_core)
    monkeypatch.setitem(sys.modules, "pylon.core.tools", pylon_tools)
    monkeypatch.setitem(sys.modules, "arbiter", types.ModuleType("arbiter"))

    worker_tools = types.ModuleType("tools")
    worker_tools.context = types.SimpleNamespace(id="test")
    worker_tools.this = types.SimpleNamespace()
    worker_tools.worker_core = types.SimpleNamespace()
    monkeypatch.setitem(sys.modules, "tools", worker_tools)

    spec = importlib.util.spec_from_file_location(
        "provider_worker_tools_5854",
        PLUGIN_ROOT / "utils" / "tools.py",
    )
    loaded = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(loaded)
    return loaded


def test_generated_pydantic_schema_is_accepted_by_structured_tool(monkeypatch):
    """The provider's generated Pydantic model must remain a valid tool schema."""

    provider_tools = _load_provider_tools(monkeypatch)
    toolkit = provider_tools.Toolkit.__new__(provider_tools.Toolkit)
    toolkit.provider_name = "example_provider"
    toolkit.toolkit_name = "search"

    args_model = toolkit._compile_args_schema(
        "find",
        {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "limit": {"type": "integer", "default": 1},
            },
            "required": ["query"],
        },
    )
    tool = StructuredTool(
        name="example_provider___find",
        description="Find matching records",
        args_schema=args_model,
        func=lambda query, limit=1: {"query": query, "limit": limit},
    )

    assert tool.invoke({"query": "elitea", "limit": 2}) == {
        "query": "elitea",
        "limit": 2,
    }
    schema = tool.get_input_schema().model_json_schema()
    assert schema["required"] == ["query"]
    assert schema["properties"]["limit"]["default"] == 1
