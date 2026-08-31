"""Issue #6403 - datamodel-code-generator usage contract for provider OpenAPI toolkits.

Exercises Toolkit._prepare_api_info() + OpenAPIClient against a self-contained
snapshot of the real epam_ai_run.spi.json / epam_ai_run.spi.schema.json fixtures
(elitea_core/data/), merged the same way _prepare_api_info consumes them: a single
JSON string fed straight into OpenAPIParser with no multi-file resolution. This
runs the REAL OpenAPIParser -> compile() -> exec() -> jsonref.replace_refs pipeline,
so a datamodel-code-generator version bump that changes generated-code shape shows
up here before it reaches production.

The two source files can't be read from disk here: they live under pylon_main's
plugin tree, which is not mounted into the pylon_indexer container this test runs
in. The embedded dicts below are a snapshot of their content.
"""

import copy
import importlib.util
import json
from pathlib import Path
import sys
import types
from unittest.mock import Mock

import pydantic
import pytest

pytest.importorskip("datamodel_code_generator")

PLUGIN_ROOT = Path(__file__).parents[1]

SPI_SCHEMA_DEFS = {
    "HealthResponse": {
        "type": "object",
        "properties": {
            "status": {"type": "string"},
            "providerVersion": {"type": "string"},
            "uptime": {"type": "integer", "description": "Service uptime in seconds."},
            "timestamp": {"type": "string", "format": "date-time", "description": "Timestamp of the health check."},
            "extra_info": {
                "type": "object",
                "description": "Additional information about a provider state.",
                "propertyNames": {"type": "string"},
                "additionalProperties": {},
            },
        },
    },
    "ToolkitConfiguration": {
        "type": "object",
        "properties": {
            "configuration_type": {
                "type": "string",
                "description": "Type of the configuration. The configuration type is defined in the Service Provider Descriptor.",
            },
            "parameters": {
                "type": "object",
                "description": "Toolkit configuration parameters.",
                "propertyNames": {"type": "string"},
                "additionalProperties": {},
            },
        },
    },
    "ToolInvocationRequest": {
        "type": "object",
        "properties": {
            "user_id": {"type": "string", "description": "User ID of the data source owner."},
            "project_id": {"type": "string", "description": "Project ID of the data source."},
            "configuration": {"$ref": "#/$defs/ToolkitConfiguration"},
            "parameters": {
                "type": "object",
                "description": "Tool-specific configuration or input",
                "propertyNames": {"type": "string"},
                "additionalProperties": {},
            },
            "async": {
                "type": "boolean",
                "description": "If true, the tool invocation is asynchronous. The response will contain the invocation_id.",
                "default": False,
            },
            "callback_url": {
                "type": "string",
                "description": "URL to which the tool invocation response will be sent. Required if async is true.",
            },
        },
        "required": ["parameters"],
    },
    "ToolInvocationResponse": {
        "type": "object",
        "properties": {
            "invocation_id": {"type": "string"},
            "status": {
                "type": "string",
                "enum": ["Started", "InProgress", "Completed", "Error"],
            },
            "result": {"description": "Immediate result if the operation is synchronous"},
            "result_type": {
                "type": "string",
                "description": "Type of the result returned by the tool.",
                "enum": ["Integer", "Float", "String", "Bytes", "Bool", "Json", "Yaml"],
            },
            "warnings": {"type": "string", "description": "Warnings if any"},
            "errors": {"type": "string", "description": "Errors if any"},
            "error_category": {"type": "string", "description": "Machine-readable failure category when status is Error."},
            "error_type": {"type": "string", "description": "Machine-readable failure sub-type when status is Error."},
            "custom_events": {
                "type": "array",
                "description": "Custom events (if any)",
                "items": {"type": "object", "description": "Custom event", "propertyNames": {"type": "string"}},
            },
        },
    },
    "ErrorResponse": {
        "type": "object",
        "properties": {
            "errorCode": {"type": "string"},
            "message": {"type": "string"},
            "details": {"type": "array", "items": {"type": "string"}},
        },
    },
}

SPI_JSON = {
    "openapi": "3.0.4",
    "info": {"title": "Service Provider Interface (SPI) OpenAPI", "version": "1.0.0"},
    "servers": [],
    "tags": [
        {"name": "Service Provider Health", "description": "Endpoints for health checks and capability discovery."},
        {"name": "Tool Invocation and Management", "description": "Endpoints for listing, invoking, and managing external tool operations."},
    ],
    "paths": {
        "/health": {
            "get": {
                "tags": ["Service Provider Metadata & Health"],
                "summary": "Health Check",
                "operationId": "healthCheck",
                "parameters": [{"$ref": "#/components/parameters/CorrelationId"}],
                "responses": {
                    "200": {
                        "description": "Service is healthy",
                        "content": {"application/json": {"schema": {"$ref": "#/components/schemas/HealthResponse"}}},
                    },
                    "500": {"$ref": "#/components/responses/InternalServerError"},
                },
            }
        },
        "/tools/{toolkit_name}/{tool_name}/invoke": {
            "post": {
                "tags": ["Tool Invocation & Management"],
                "summary": "Invoke Tool",
                "operationId": "invokeTool",
                "parameters": [
                    {"$ref": "#/components/parameters/CorrelationId"},
                    {"$ref": "#/components/parameters/ToolkitName"},
                    {"$ref": "#/components/parameters/ToolName"},
                ],
                "requestBody": {
                    "required": True,
                    "content": {"application/json": {"schema": {"$ref": "#/components/schemas/ToolInvocationRequest"}}},
                },
                "responses": {
                    "200": {
                        "description": "Tool invocation completed or started",
                        "content": {"application/json": {"schema": {"$ref": "#/components/schemas/ToolInvocationResponse"}}},
                    },
                    "400": {"$ref": "#/components/responses/BadRequest"},
                    "404": {"$ref": "#/components/responses/NotFound"},
                    "500": {"$ref": "#/components/responses/InternalServerError"},
                },
            }
        },
        "/tools/{toolkit_name}/{tool_name}/invocations/{invocation_id}": {
            "get": {
                "tags": ["Tool Invocation & Management"],
                "summary": "Get Tool Invocation Status",
                "operationId": "getToolInvocationStatus",
                "parameters": [
                    {"$ref": "#/components/parameters/CorrelationId"},
                    {"$ref": "#/components/parameters/ToolkitName"},
                    {"$ref": "#/components/parameters/ToolName"},
                    {"$ref": "#/components/parameters/InvocationId"},
                ],
                "responses": {
                    "200": {
                        "description": "Invocation status retrieved",
                        "content": {"application/json": {"schema": {"$ref": "#/components/schemas/ToolInvocationResponse"}}},
                    },
                    "404": {"$ref": "#/components/responses/NotFound"},
                    "500": {"$ref": "#/components/responses/InternalServerError"},
                },
            },
            "delete": {
                "tags": ["Tool Invocation & Management"],
                "summary": "Cancel Tool Invocation",
                "operationId": "cancelToolInvocation",
                "parameters": [
                    {"$ref": "#/components/parameters/CorrelationId"},
                    {"$ref": "#/components/parameters/ToolkitName"},
                    {"$ref": "#/components/parameters/ToolName"},
                    {"$ref": "#/components/parameters/InvocationId"},
                ],
                "responses": {
                    "204": {"description": "Tool invocation cancelled successfully."},
                    "409": {"$ref": "#/components/responses/Conflict"},
                    "404": {"$ref": "#/components/responses/NotFound"},
                    "500": {"$ref": "#/components/responses/InternalServerError"},
                },
            },
        },
    },
    "components": {
        "parameters": {
            "CorrelationId": {"name": "X-Correlation-Id", "in": "header", "required": False, "schema": {"type": "string"}},
            "ToolName": {"name": "tool_name", "in": "path", "required": True, "schema": {"type": "string"}},
            "ToolkitName": {"name": "toolkit_name", "in": "path", "required": True, "schema": {"type": "string"}},
            "InvocationId": {"name": "invocation_id", "in": "path", "required": True, "schema": {"type": "string"}},
        },
        "securitySchemes": {"bearerAuth": {"type": "http", "scheme": "bearer", "bearerFormat": "JWT"}},
        "schemas": {
            "ErrorResponse": {"$ref": "epam_ai_run.spi.schema.json#/$defs/ErrorResponse"},
            "HealthResponse": {"$ref": "epam_ai_run.spi.schema.json#/$defs/HealthResponse"},
            "ToolInvocationRequest": {"$ref": "epam_ai_run.spi.schema.json#/$defs/ToolInvocationRequest"},
            "ToolInvocationResponse": {"$ref": "epam_ai_run.spi.schema.json#/$defs/ToolInvocationResponse"},
        },
        "responses": {
            "BadRequest": {"description": "Bad Request", "content": {"application/json": {"schema": {"$ref": "#/components/schemas/ErrorResponse"}}}},
            "Conflict": {"description": "Conflict", "content": {"application/json": {"schema": {"$ref": "#/components/schemas/ErrorResponse"}}}},
            "NotFound": {"description": "Not Found", "content": {"application/json": {"schema": {"$ref": "#/components/schemas/ErrorResponse"}}}},
            "InternalServerError": {"description": "Internal Server Error", "content": {"application/json": {"schema": {"$ref": "#/components/schemas/ErrorResponse"}}}},
        },
    },
    "security": [{"bearerAuth": []}],
}


def _build_merged_api_schema_json():
    """Merge spi.json + spi.schema.json into one self-contained doc, as production does."""
    merged = copy.deepcopy(SPI_JSON)
    merged["components"]["schemas"].update(copy.deepcopy(SPI_SCHEMA_DEFS))
    serialized = json.dumps(merged)
    return serialized.replace("#/$defs/", "#/components/schemas/")


def _find_refs(node):
    if isinstance(node, dict):
        if "$ref" in node:
            yield node["$ref"]
        for value in node.values():
            yield from _find_refs(value)
    elif isinstance(node, list):
        for item in node:
            yield from _find_refs(item)


def _load_provider_tools(monkeypatch):
    pylon = types.ModuleType("pylon")
    pylon_core = types.ModuleType("pylon.core")
    pylon_tools = types.ModuleType("pylon.core.tools")
    pylon_tools.log = types.SimpleNamespace(
        debug=Mock(), error=Mock(), exception=Mock(), info=Mock(), warning=Mock(),
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
        "provider_worker_tools_6403",
        PLUGIN_ROOT / "utils" / "tools.py",
    )
    loaded = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(loaded)
    return loaded


@pytest.fixture
def provider_tools_module(monkeypatch):
    return _load_provider_tools(monkeypatch)


@pytest.fixture
def toolkit(provider_tools_module):
    instance = provider_tools_module.Toolkit.__new__(provider_tools_module.Toolkit)
    instance.provider_name = "example_provider"
    instance.toolkit_name = "search"
    instance.api_info = {"api_schema_json": _build_merged_api_schema_json()}
    instance._prepare_api_info()
    return instance


class TestOpenApiToolkitCodegen:
    def test_generated_models_have_expected_defaults_and_requireds(self, toolkit):
        request = toolkit.api_models.ToolInvocationRequest.model_validate({"parameters": {}})
        dumped = request.model_dump(by_alias=True)
        assert dumped["async"] is False

        with pytest.raises(pydantic.ValidationError):
            toolkit.api_models.ToolInvocationRequest.model_validate({})

    def test_open_parameters_dict_round_trips_nested_extras(self, toolkit):
        request = toolkit.api_models.ToolInvocationRequest.model_validate({
            "parameters": {"query": "elitea", "filters": {"tags": ["a", "b"]}},
        })
        dumped = request.model_dump(by_alias=True)
        assert dumped["parameters"] == {"query": "elitea", "filters": {"tags": ["a", "b"]}}

    def test_response_status_enum_rejects_invalid_value(self, toolkit):
        valid = toolkit.api_models.ToolInvocationResponse.model_validate({"status": "Completed"})
        assert valid.model_dump(by_alias=True, mode="json")["status"] == "Completed"

        with pytest.raises(pydantic.ValidationError):
            toolkit.api_models.ToolInvocationResponse.model_validate({"status": "NotARealStatus"})

    def test_error_response_details_list(self, toolkit):
        error = toolkit.api_models.ErrorResponse.model_validate({
            "errorCode": "E1", "message": "boom", "details": ["a", "b"],
        })
        assert error.model_dump(by_alias=True)["details"] == ["a", "b"]

    def test_api_schema_has_no_unresolved_refs(self, toolkit):
        assert list(_find_refs(toolkit.api_schema)) == []

    def test_openapi_client_exposes_callables_for_all_operation_ids(self, toolkit, provider_tools_module):
        openapi_client = provider_tools_module.OpenAPIClient(
            base_url="https://example.com",
            api_schema=toolkit.api_schema,
            api_models=toolkit.api_models,
        )
        for operation_id in ("healthCheck", "invokeTool", "getToolInvocationStatus", "cancelToolInvocation"):
            assert callable(getattr(openapi_client, operation_id))
