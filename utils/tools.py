#!/usr/bin/python3
# coding=utf-8
# pylint: disable=E1101

#   Copyright 2025 EPAM Systems
#
#   Licensed under the Apache License, Version 2.0 (the "License");
#   you may not use this file except in compliance with the License.
#   You may obtain a copy of the License at
#
#       http://www.apache.org/licenses/LICENSE-2.0
#
#   Unless required by applicable law or agreed to in writing, software
#   distributed under the License is distributed on an "AS IS" BASIS,
#   WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#   See the License for the specific language governing permissions and
#   limitations under the License.

""" Tools """

import sys
import json
import time
import uuid
import types
import queue
import base64
import functools
import importlib

from datamodel_code_generator import DataModelType, PythonVersion, OpenAPIScope  # pylint: disable=E0401
from datamodel_code_generator.model import get_data_model_types  # pylint: disable=E0401
from datamodel_code_generator.parser.openapi import OpenAPIParser  # pylint: disable=E0401
from datamodel_code_generator.parser.jsonschema import JsonSchemaParser  # pylint: disable=E0401

import requests  # pylint: disable=E0401
import jsonref  # pylint: disable=E0401

from langchain_core.tools import StructuredTool, ToolException  # pylint: disable=E0401

try:
    from langchain_core.callbacks import dispatch_custom_event  # pylint: disable=E0401
except ImportError:
    def dispatch_custom_event(*_args, **_kwargs):  # pylint: disable=C
        pass

import arbiter  # pylint: disable=E0401

from pylon.core.tools import log  # pylint: disable=E0611,E0401,W0611

from tools import context, this, worker_core  # pylint: disable=E0611,E0401

try:
    from .failure_signals import TERMINAL_STATUSES, detect_provider_failure
except ImportError:  # loaded standalone via spec_from_file_location, e.g. in tests
    sys.path.insert(0, __file__.rsplit("/", 1)[0])
    from failure_signals import TERMINAL_STATUSES, detect_provider_failure  # pylint: disable=E0401


# Media types that have artifacts pre-created by provider plugins
# These objects contain filepath (/{bucket}/{filename}) instead of raw data
MEDIA_RESULT_TYPES = {"image", "audio", "video"}


# Pattern matches characters that are NOT alphanumeric, underscores, dots, or hyphens
import re
_CLEAN_STRING_PATTERN = re.compile(r'[^a-zA-Z0-9_.-]')


def clean_string(s: str) -> str:
    """Sanitize string for use in tool names (OpenAI function naming requirements)."""
    return re.sub(_CLEAN_STRING_PATTERN, '', str(s)).replace('.', '_')


class Toolkit:  # pylint: disable=R0902,R0903
    """ Toolkit """

    def __init__(self, *, elitea, provider, toolkit, name, toolkit_configuration, **kwargs):  # pylint: disable=R0913
        self.elitea = elitea
        self.provider_name = provider
        self.llm = kwargs["llm"] if "llm" in kwargs else None
        #
        self.api_info = self._get_provider_api_info(provider)
        if self.api_info is None:
            raise RuntimeError("Failed to get provider info")
        #
        #
        self.api_models = None
        self.api_schema = None
        self._prepare_api_info()
        #
        if toolkit not in self.api_info["toolkits"]:
            raise ValueError("Unknown toolkit")
        #
        self.toolkit_name = toolkit
        self.toolkit_info = self.api_info["toolkits"][toolkit]
        #
        # Get toolkit-level metadata (includes required_context)
        self.toolkit_metadata = self.api_info.get("toolkits_metadata", {}).get(toolkit, {})
        #
        # Build ToolkitConfiguration: first extend incoming payload with `llm_settings`
        llm_settings = self._extract_llm_settings()
        if llm_settings:
            try:
                if isinstance(toolkit_configuration, dict):
                    toolkit_configuration["llm_settings"] = llm_settings
                else:
                    setattr(toolkit_configuration, "llm_settings", llm_settings)
            except Exception:  # pylint: disable=W0703
                log.warning("Failed to attach 'llm_settings' to incoming toolkit_configuration; proceeding without it")
        self.toolkit_configuration = self.api_models.ToolkitConfiguration(
            parameters=toolkit_configuration,
        )
        #
        self.toolkit_settings = {}
        known_settings = ["selected_tools"]
        for item in known_settings:
            if item in kwargs:
                self.toolkit_settings[item] = kwargs.pop(item)
        #
        self.tool_info = {}
        for tool_obj in self.toolkit_info:
            self.tool_info[tool_obj["name"]] = {
                "tool_metadata": tool_obj["tool_metadata"].copy(),
                "sync_invocation_supported": tool_obj["sync_invocation_supported"],
                "async_invocation_supported": tool_obj["async_invocation_supported"],
            }
        #
        # Sanitize toolkit name for use in tool names (preserving original for events)
        self.event_toolkit_name = clean_string(name)
        self.original_toolkit_name = name
        #
        self.async_wait_interval = this.descriptor.config.get("async_wait_interval", 3)
        #
        self.api_client_kwargs = {}
        #
        known_kwargs = [
            "headers",
            "timeout",
            "verify",
        ]
        #
        for kwarg in known_kwargs:
            if kwarg in kwargs:
                self.api_client_kwargs[kwarg] = kwargs.pop(kwarg)
        #
        self.api_client = OpenAPIClient(
            base_url=self.api_info["service_location_url"],
            api_schema=self.api_schema,
            api_models=self.api_models,
            **self.api_client_kwargs
        )

    @classmethod
    def get_toolkit(cls, **kwargs):
        """ Get toolkit """
        required = [
            "provider",
            "toolkit",
            "name",
        ]
        #
        for item in required:
            if item not in kwargs:
                raise ValueError("Invalid settings")
        #
        if "toolkit_configuration" not in kwargs:
            toolkit_configuration = {}
            #
            for item in list(kwargs):
                tag = "toolkit_configuration_"
                #
                if item.startswith(tag):
                    toolkit_configuration[item[len(tag):]] = kwargs.pop(item)
            #
            kwargs["toolkit_configuration"] = toolkit_configuration
        #
        import tasknode_task  # pylint: disable=E0401,C0415
        #
        if tasknode_task.multiprocessing_context == "fork":
            local_event_node = worker_core.event_node.clone()
            local_event_node.start()
            #
            local_rpc_node = arbiter.RpcNode(
                local_event_node,
                id_prefix="indexer_",
                proxy_timeout=5,
            )
            local_rpc_node.start()
            #
            worker_core.event_node = local_event_node
            worker_core.rpc_node = local_rpc_node
        #
        return cls(**kwargs)  # Yes, not a real BaseToolkit

    def get_tools(self):
        """ Get tools """
        tools = []
        #
        for tool_obj in self.toolkit_info:
            # name
            # description
            # args_schema
            # sync_invocation_supported
            # async_invocation_supported
            #
            tool_name = tool_obj["name"]
            structured_tool_name = f"{self.event_toolkit_name}___{tool_name}"
            #
            selected_tools = self.toolkit_settings.get("selected_tools", [])
            if selected_tools and tool_name not in selected_tools:
                continue
            #
            tools.append(
                StructuredTool(
                    name=structured_tool_name,
                    description=tool_obj["description"],
                    args_schema=self._compile_args_schema(tool_name, tool_obj["args_schema"]),
                    func=functools.partial(self._run_tool, tool_name),
                    metadata={
                        "toolkit_name": self.original_toolkit_name,
                    },
                )
            )
        #
        return tools

    def _unwrap_secret(self, value):  # pylint: disable=R
        """Return plain value for SecretStr-like objects without logging it."""
        try:
            if hasattr(value, "get_secret_value") and callable(value.get_secret_value):
                return value.get_secret_value()
        except Exception:  # pylint: disable=W0703
            return value
        return value

    def _json_safe(self, value):  # pylint: disable=R
        """Best-effort conversion to JSON-serializable primitives."""
        try:
            # Fast path: primitives are fine
            if value is None or isinstance(value, (bool, int, float, str)):
                return value
            if isinstance(value, bytes):
                return value.decode("utf-8", errors="ignore")
            if isinstance(value, (list, tuple)):
                return [self._json_safe(v) for v in value]
            if isinstance(value, dict):
                return {str(k): self._json_safe(v) for k, v in value.items()}
            # Try JSON dump to detect serializability
            json.dumps(value)
            return value
        except Exception:  # pylint: disable=W0703
            try:
                return str(value)
            except Exception:  # pylint: disable=W0703
                return None

    def _extract_llm_field(self, llm, attr_name, secret_fields, dict_fields):
        """Extract a single field from the LLM with proper handling.

        Args:
            llm: The LLM instance
            attr_name: The attribute name to extract
            secret_fields: Set of field names that contain secrets
            dict_fields: Set of field names that should be treated as dicts

        Returns:
            The processed value or None if not available.
        """
        try:
            value = getattr(llm, attr_name, None)
        except Exception:  # pylint: disable=W0703
            return None
        if value is None:
            return None
        if attr_name in secret_fields:
            return self._unwrap_secret(value)
        if attr_name in dict_fields:
            return self._json_safe(value) or {}
        return self._json_safe(value)

    def _extract_llm_settings(self):  # pylint: disable=R
        """Extract a serializable snapshot of the LLM settings for plugins.

        Supports both ChatOpenAI and ChatAnthropic models with normalized field names.

        Returns:
            dict | None: Minimal JSON-serializable configuration or None if not available.
        """
        llm = self.llm
        if llm is None:
            return None

        # Detect provider type based on class name
        llm_class_name = type(llm).__name__
        is_anthropic = llm_class_name == "ChatAnthropic" or "anthropic" in llm_class_name.lower()

        data = {}
        data["provider"] = "anthropic" if is_anthropic else "openai"

        if is_anthropic:
            # ChatAnthropic: maps attribute names to output names
            field_mapping = {
                "model": "model_name",
                "anthropic_api_key": "api_key",
                "anthropic_api_url": "api_base",
                "max_tokens": "max_tokens",
                "temperature": "temperature",
                "max_retries": "max_retries",
                "streaming": "streaming",
                "default_headers": "default_headers",
            }
            secret_fields = {"anthropic_api_key"}
            dict_fields = {"default_headers"}
        else:
            # ChatOpenAI: maps attribute names to output names
            field_mapping = {
                "model_name": "model_name",
                "temperature": "temperature",
                "max_retries": "max_retries",
                "max_tokens": "max_tokens",
                "streaming": "streaming",
                "openai_api_key": "api_key",
                "openai_api_base": "api_base",
                "openai_organization": "organization",
                "model_kwargs": "model_kwargs",
            }
            secret_fields = {"openai_api_key"}
            dict_fields = {"model_kwargs"}

        # Extract fields using unified logic
        for attr_name, output_name in field_mapping.items():
            value = self._extract_llm_field(llm, attr_name, secret_fields, dict_fields)
            if value is None:
                continue
            # Skip empty dicts for model_kwargs
            if attr_name in dict_fields and not value:
                continue
            data[output_name] = value

        # Extract organization from default_headers if present (Anthropic only)
        if is_anthropic and "default_headers" in data:
            org = data["default_headers"].get("openai-organization")
            if org:
                data["organization"] = org

        # Add project_id from elitea client (needed for artifact storage in provider plugins)
        if self.elitea is not None:
            try:
                data["project_id"] = self.elitea.project_id
            except Exception:  # pylint: disable=W0703
                pass
            # Add model_image_generation from elitea client
            try:
                if hasattr(self.elitea, "model_image_generation") and self.elitea.model_image_generation:
                    data["model_image_generation"] = self.elitea.model_image_generation
            except Exception:  # pylint: disable=W0703
                pass

        return data or None

    def _get_required_context(self):
        """Get context fields to pass based on toolkit's required_context declaration.

        Returns dict with user_id and project_id as strings (or None if not required).
        """
        required_context = self.toolkit_metadata.get("required_context", [])
        #
        _uid = getattr(self, "_user_id", None)
        _pid = getattr(self, "_project_id", None)
        #
        # If no required_context specified, don't pass any context (backward compatible)
        # Providers must explicitly declare what context they need
        if not required_context:
            return {"user_id": None, "project_id": None}
        #
        return {
            "user_id": str(_uid) if _uid is not None and "user_id" in required_context else None,
            "project_id": str(_pid) if _pid is not None and "project_id" in required_context else None,
        }

    def _run_tool(self, tool_name, **parameters):  # pylint: disable=R0912,R0915
        #
        # Invoke
        #
        invocation_id = None  # only ever set on the async path; kept defined for the shadow-detector log below
        if self.tool_info[tool_name]["async_invocation_supported"]:
            log.info("Invoking in async mode")
            #
            # Get context fields based on toolkit's required_context declaration
            ctx = self._get_required_context()
            async_response = self.api_client.invoke_tool(
                toolkit_name=self.toolkit_name,
                tool_name=tool_name,
                request_body=self.api_models.ToolInvocationRequest(
                    user_id=ctx["user_id"],
                    project_id=ctx["project_id"],
                    configuration=self.toolkit_configuration,
                    parameters=parameters,
                    async_=True,
                    callback_url="http://127.0.0.1",
                ),
            )
            #
            log.info("Initial async response: %s", async_response)
            #
            invocation_id = None
            #
            try:
                invocation_id = async_response.invocation_id
                if invocation_id is None:
                    raise RuntimeError("No invocation ID in async response")
            except:  # pylint: disable=W0702
                response = async_response
            else:
                import tasknode_task  # pylint: disable=E0401,C0415
                #
                # Safely get task_id - may not be available in all thread contexts
                try:
                    task_id = tasknode_task.id
                except AttributeError:
                    task_id = None
                #
                invocation_event = {
                    "provider_name": self.provider_name,
                    "toolkit_name": self.toolkit_name,
                    "tool_name": tool_name,
                    "invocation_id": invocation_id,
                    "service_location_url": self.api_info["service_location_url"],
                    "api_client_kwargs": self.api_client_kwargs,
                    "pylon_id": context.id,
                    "task_id": task_id,
                }
                #
                worker_core.event_node.emit("provider_invocation_started", invocation_event)
                #
                # Value-based, not an enum-member list: provider_worker and pylon_main ship
                # independently, so a schema-version mismatch must never AttributeError here.
                while getattr(async_response.status, "value", async_response.status) not in TERMINAL_STATUSES:
                    time.sleep(self.async_wait_interval)
                    #
                    async_response = self.api_client.get_tool_invocation_status(
                        toolkit_name=self.toolkit_name,
                        tool_name=tool_name,
                        invocation_id=invocation_id,
                    )
                    #
                    log.info("Status async response: %s", async_response)
                    #
                    try:
                        custom_events = async_response.custom_events
                        if custom_events:
                            log.info("Got custom events: %s", custom_events)
                            #
                            for custom_event in custom_events:
                                event_name = custom_event.get("name", "thinking_step_update")
                                event_data = custom_event.get("data", None)
                                #
                                if isinstance(event_data, dict):
                                    event_data["tool_name"] = tool_name
                                    event_data["toolkit"] = self.event_toolkit_name
                                    event_data["markdown"] = True
                                #
                                dispatch_custom_event(name=event_name, data=event_data)
                    except:  # pylint: disable=W0702
                        log.exception("Custom events exception (ignored)")
                #
                worker_core.event_node.emit("provider_invocation_ended", invocation_event)
                #
                response = async_response
        elif self.tool_info[tool_name]["sync_invocation_supported"]:
            log.info("Invoking in sync mode")
            #
            # Get context fields based on toolkit's required_context declaration
            ctx = self._get_required_context()
            response = self.api_client.invoke_tool(
                toolkit_name=self.toolkit_name,
                tool_name=tool_name,
                request_body=self.api_models.ToolInvocationRequest(
                    user_id=ctx["user_id"],
                    project_id=ctx["project_id"],
                    configuration=self.toolkit_configuration,
                    parameters=parameters,
                ),
            )
        else:
            raise ToolException("No invocation types supported")
        #
        # Process response
        #
        log.info("Final response: %s", response)
        #
        # Shadow-mode only: read-only, never returns/raises/mutates response (see #6168).
        response_status = getattr(getattr(response, "status", None), "value", getattr(response, "status", None))
        shadow_failure = detect_provider_failure(response_status, getattr(response, "error_category", None))
        if shadow_failure is not None:
            log.warning(
                "TOOL_FAILURE_SHADOW %s",
                json.dumps({
                    "detected_by": "provider_status",
                    "would_be_error_class": shadow_failure["would_be_error_class"],
                    "provider_name": self.provider_name,
                    "toolkit_name": self.original_toolkit_name,
                    "toolkit_type": None,
                    "toolkit_id": None,
                    "tool_name": tool_name,
                    "error_category": shadow_failure["error_category"],
                    "error_type": getattr(response, "error_type", None),
                    "invocation_id": invocation_id,
                    "project_id": getattr(self, "_project_id", None),
                    "user_id": getattr(self, "_user_id", None),
                    "result_len": len(str(getattr(response, "result", ""))),
                    "delivered_as_success": True,
                }),
            )
        #
        try:
            final_result = str(response.result)
            #
            tool_metadata = self.tool_info[tool_name]["tool_metadata"]
            result_composition = tool_metadata.get("result_composition", None)
            #
            if result_composition == "json_object":
                result_object = json.loads(final_result)
            elif result_composition == "list_of_objects":
                return self._process_list_of_objects_result(final_result, tool_metadata, tool_name)
            else:
                result_object = {
                    "message": "",
                    "result": final_result,
                }
            #
            final_result = result_object.get("result", "")
            #
            # Use helper method to process the result
            processed_result = self._process_result_object(
                object_data=final_result,
                result_target=tool_metadata.get("result_target", "response"),
                result_encoding=tool_metadata.get("result_encoding", "plain"),
                result_extension=tool_metadata.get("result_extension", "txt"),
                artifact_bucket=tool_metadata.get("result_bucket", "provider-results")
            )
            #
            if processed_result and processed_result["type"] == "artifact":
                final_result = {
                    "message": result_object.get("message", ""),
                    **processed_result["data"]
                }
                final_result = json.dumps(final_result)
            elif processed_result and processed_result["type"] == "message":
                final_result = processed_result["data"]
            else:
                # Fallback to original behavior if processing fails
                pass
            #
            return final_result
        except Exception as exc:  # pylint: disable=W0702
            try:
                error_details = f"{str(response.message)}: {str(response.details)}"
            except:  # pylint: disable=W0702
                error_details = str(response.message)
            #
            raise ToolException(error_details) from exc

    def _process_list_of_objects_result(self, final_result, tool_metadata, tool_name):
        """ Process list_of_objects result composition type """
        try:
            # Parse the result as JSON containing a list of objects
            result_data = json.loads(final_result)

            if not isinstance(result_data, list):
                raise ValueError("Expected list of objects for list_of_objects result composition")

            # Get the object type definitions from metadata
            result_objects_config = tool_metadata.get("result_objects", [])
            if not result_objects_config:
                raise ValueError("No result_objects configuration found for list_of_objects composition")

            # Create lookup for object type configurations
            object_configs = {config["object_type"]: config for config in result_objects_config}

            # Separate text messages and artifacts
            text_messages = []
            artifacts_info = []

            # Process each object in the result
            for result_obj in result_data:
                if not isinstance(result_obj, dict):
                    continue

                object_type = result_obj.get("object_type", "")

                if object_type not in object_configs:
                    log.warning("Unknown object type: %s", object_type)
                    continue

                config = object_configs[object_type]

                # Check if this is a media type with pre-created artifact (has filepath)
                if object_type in MEDIA_RESULT_TYPES and result_obj.get("filepath"):
                    # Artifact MUST be already created by provider plugin - just emit event and collect info
                    artifact_info = {
                        "filepath": result_obj.get("filepath"),
                        "meta": result_obj.get("meta", {}),
                    }
                    #
                    # Emit file_modified event for UI updates
                    self._emit_media_file_modified(artifact_info, object_type, tool_name)
                    #
                    artifacts_info.append({
                        "object_type": object_type,
                        **artifact_info
                    })
                    continue

                # Standard processing for objects with data field
                object_data = result_obj.get("data", "")
                object_bucket = result_obj.get("result_bucket")
                object_name = result_obj.get("name", None)

                # Use helper method to process the result object
                processed_result = self._process_result_object(
                    object_data=object_data,
                    result_target=config.get("result_target", "response"),
                    result_encoding=config.get("result_encoding", "plain"),
                    result_extension=config.get("result_extension", "txt"),
                    artifact_bucket=object_bucket if object_bucket else config.get("result_bucket", "provider-results"),
                    object_type=object_type,
                    object_name=object_name
                )

                if processed_result is None:
                    continue

                if processed_result["type"] == "message":
                    text_messages.append(processed_result["data"])
                elif processed_result["type"] == "artifact":
                    artifacts_info.append(processed_result["data"])

            # Combine results
            final_response = {}

            # Combine all text messages into one
            if text_messages:
                final_response["message"] = "\n\n".join(text_messages)

            # Add artifact information
            if artifacts_info:
                final_response["artifacts"] = artifacts_info

            return json.dumps(final_response)

        except Exception as exc:
            log.error("Failed to process list_of_objects result: %s", exc)
            # Fallback to treating as simple text
            return final_result

    def _process_result_object(self, object_data, result_target, result_encoding, result_extension="txt", artifact_bucket="provider-results", object_type=None, object_name=None):
        """ Process a single result object (text or artifact) """
        # Decode if needed
        processed_data = object_data
        if result_encoding == "base64":
            try:
                processed_data = base64.b64decode(object_data)
            except Exception as exc:
                log.error("Failed to decode base64 data for object type %s: %s", object_type or "unknown", exc)
                return None

        if result_target == "response":
            # Return as text message
            if isinstance(processed_data, bytes):
                processed_data = processed_data.decode('utf-8', errors='ignore')
            return {
                "type": "message",
                "data": str(processed_data)
            }

        elif result_target == "artifact":
            # Process as artifact
            if object_name:
                artifact_object = object_name
            else:
                artifact_object = f"{uuid.uuid4()}.{result_extension}"

            try:
                self.elitea.create_artifact(
                    artifact_bucket,
                    artifact_object,
                    processed_data
                )

                artifact_size = len(processed_data) if isinstance(processed_data, (str, bytes)) else 0

                return {
                    "type": "artifact",
                    "data": {
                        "object_type": object_type,
                        "artifact_bucket": artifact_bucket,
                        "artifact_object": artifact_object,
                        "artifact_size": artifact_size,
                    }
                }

            except Exception as exc:
                log.error("Failed to create artifact for object type %s: %s", object_type or "unknown", exc)
                return None

        return None

    def _emit_media_file_modified(self, artifact_info, object_type, tool_name):
        """
        Emit file_modified event for media artifacts created by provider plugins.
        
        This is used for media types (image, audio, video) where the artifact
        is already created by the provider plugin. We just emit the event
        so the callback can capture it.
        
        Args:
            artifact_info: dict with filepath and meta
            object_type: 'image', 'audio', or 'video'
            tool_name: Name of the tool that generated the artifact
        """
        filepath = artifact_info.get("filepath", "")

        event_data = {
            "message": f"{object_type.capitalize()} created at {filepath}",
            "filepath": filepath,
            "tool_name": tool_name,
            "toolkit": self.event_toolkit_name,
            "operation_type": "create",
            "media_type": object_type,
            "meta": {
                "media_type": object_type,
                **artifact_info.get("meta", {})
            }
        }
        dispatch_custom_event(name="file_modified", data=event_data)

    def _compile_args_schema(self, tool_name, args_schema):
        data_model_types = get_data_model_types(
            DataModelType.PydanticV2BaseModel,
            target_python_version=PythonVersion.PY_312,
        )
        #
        parser = JsonSchemaParser(
           json.dumps(args_schema),
           #
           data_model_type=data_model_types.data_model,
           data_model_root_type=data_model_types.root_model,
           data_model_field_type=data_model_types.field_model,
           data_type_manager_type=data_model_types.data_type_manager,
           dump_resolve_reference_action=data_model_types.dump_resolve_reference_action,
           #
           target_python_version=PythonVersion.PY_312,
           #
           remove_special_field_name_prefix=True,
           allow_population_by_field_name=True,
        )
        #
        parsed_schema = parser.parse()
        #
        code = compile(
            parsed_schema, "<generated>:<schema>",
            mode="exec", dont_inherit=True,
        )
        #
        module_name = "_".join([self.provider_name, self.toolkit_name, tool_name])
        #
        sys.modules[module_name] = types.ModuleType(module_name)
        sys.modules[module_name].__path__ = []
        #
        exec(code, sys.modules[module_name].__dict__)  # pylint: disable=W0122
        #
        module_obj = importlib.import_module(module_name)
        return module_obj.Model

    def _prepare_api_info(self):
        data_model_types = get_data_model_types(
            DataModelType.PydanticV2BaseModel,
            target_python_version=PythonVersion.PY_312,
        )
        #
        parser = OpenAPIParser(
            self.api_info["api_schema_json"],
            #
            data_model_type=data_model_types.data_model,
            data_model_root_type=data_model_types.root_model,
            data_model_field_type=data_model_types.field_model,
            data_type_manager_type=data_model_types.data_type_manager,
            dump_resolve_reference_action=data_model_types.dump_resolve_reference_action,
            #
            target_python_version=PythonVersion.PY_312,
            #
            openapi_scopes=[
                OpenAPIScope.Schemas,
                OpenAPIScope.Paths,
                OpenAPIScope.Tags,
                OpenAPIScope.Parameters,
            ],
            #
            remove_special_field_name_prefix=True,
            allow_population_by_field_name=True,
        )
        #
        parsed_schema = parser.parse()
        #
        schema_code = compile(
            parsed_schema, "<generated>:<api_schema_json>",
            mode="exec", dont_inherit=True,
        )
        #
        module_name = "generated_api_models"
        #
        sys.modules[module_name] = types.ModuleType(module_name)
        sys.modules[module_name].__path__ = []
        #
        exec(schema_code, sys.modules[module_name].__dict__)  # pylint: disable=W0122
        #
        import generated_api_models  # pylint: disable=C0415,E0401
        self.api_models = generated_api_models
        #
        openapi_schema = jsonref.replace_refs(json.loads(self.api_info["api_schema_json"]))
        self.api_schema = openapi_schema

    def _get_provider_api_info(self, provider):
        try:
            import tasknode_task  # pylint: disable=C0415,E0401
            user_context_meta = tasknode_task.meta.get("user_context", {})
            #
            user_id = user_context_meta["user_id"]
            project_id = user_context_meta["project_id"]
            #
            # Store user/project context for tool invocations
            self._user_id = user_id
            self._project_id = project_id
        except:  # pylint: disable=W0702
            log.exception("Failed to get current user_id/project_id")
            return None
        #
        while True:
            try:
                log.info("Pinging main")
                ping_ok = worker_core.rpc_node.timeout(5).restricted_ping()
                if ping_ok is True:
                    break
            except queue.Empty:
                continue
        #
        return worker_core.rpc_node.timeout(30).get_provider_api_info(user_id, project_id, provider)


class OpenAPIClient:  # pylint: disable=R0903
    """ Client """

    def __init__(  # pylint: disable=R0913
            self, *, base_url,
            api_schema=None,
            api_models=None,
            headers=None,
            timeout=None,
            verify=None,
    ):
        self.base_url = base_url.rstrip("/")
        self.session = requests.Session()
        #
        if headers is not None:
            self.session.headers.update(headers)
        #
        self.timeout = timeout
        self.verify = verify
        #
        self._populate_methods(api_schema, api_models)

    def _populate_methods(self, api_schema, api_models):
        if api_schema is None:
            return
        #
        api_models = api_models.__dict__ if api_models is not None else {}
        #
        for path_url, path in api_schema.get("paths", {}).items():
            for method, operation in path.items():
                operation_id = operation.get("operationId", "").strip()
                if not operation_id:
                    continue
                #
                parameters = operation.get("parameters", [])
                request_body = operation.get("requestBody", {})
                responses = operation.get("responses", {})
                #
                target_method = functools.partial(
                    self._make_method(
                        path_url, method, parameters, request_body, responses, api_models
                    ),
                    self
                )
                #
                for method_name in [operation_id, self._to_snake_case(operation_id)]:
                    setattr(self, method_name, target_method)

    def _to_snake_case(self, camel_case_name):
        result = "".join(
            item if item.islower() else f"_{item.lower()}" for item in camel_case_name
        )
        #
        result = result.replace("-", "_")
        while "__" in result:
            result = result.replace("__", "_")
        #
        return result.strip("_")

    def _request(self, method, path_url, *args, **kwargs):
        target_url = "/".join([
            self.base_url,
            path_url.lstrip("/"),
        ])
        #
        return self.session.request(method, target_url, *args, **kwargs)

    def _make_schema_model(self, schema_obj, models):
        try:
            ref = schema_obj["schema"].__reference__
            model_name = ref["$ref"].rsplit("/", 1)[1]
            #
            if model_name in models:
                return models[model_name]
        except:  # pylint: disable=W0702
            pass
        #
        param_schema = schema_obj.get("schema", {})
        if not param_schema:
            return None
        #
        data_model_types = get_data_model_types(
            DataModelType.PydanticV2BaseModel,
            target_python_version=PythonVersion.PY_312,
        )
        #
        parser = JsonSchemaParser(
           json.dumps(param_schema),
           #
           data_model_type=data_model_types.data_model,
           data_model_root_type=data_model_types.root_model,
           data_model_field_type=data_model_types.field_model,
           data_type_manager_type=data_model_types.data_type_manager,
           dump_resolve_reference_action=data_model_types.dump_resolve_reference_action,
           #
           target_python_version=PythonVersion.PY_312,
           #
           remove_special_field_name_prefix=True,
           allow_population_by_field_name=True,
        )
        #
        parsed_schema = parser.parse()
        #
        code = compile(
            parsed_schema, "<generated>:<schema>",
            mode="exec", dont_inherit=True,
        )
        scope = {}
        exec(code, scope)  # pylint: disable=W0122
        #
        if "Model" in scope:
            return scope["Model"]
        #
        return None

    def _make_param_models(self, parameters, api_models):
        param_models = {}
        #
        for parameter in parameters:
            param_name = parameter.get("name", "").strip()
            if not param_name:
                continue
            #
            model = self._make_schema_model(parameter, api_models)
            if model is not None:
                param_models[param_name] = model
        #
        return param_models

    def _make_request_models(self, request_body, api_models):
        request_models = {}
        #
        if request_body and "content" in request_body:
            for content_type, schema_obj in request_body["content"].items():
                model = self._make_schema_model(schema_obj, api_models)
                if model is not None:
                    request_models[content_type] = model
        #
        return request_models

    def _make_response_models(self, responses, api_models):
        response_models = {}
        #
        for response_code, response_data in responses.items():
            if response_code not in response_models:
                response_models[response_code] = {}
            #
            if "content" not in response_data:
                continue
            #
            for content_type, schema_obj in response_data["content"].items():
                model = self._make_schema_model(schema_obj, api_models)
                if model is not None:
                    response_models[response_code][content_type] = model
        #
        return response_models

    def _make_method(self, path_url, http_method, parameters, request_body, responses, api_models):  # pylint: disable=R
        _path_url = path_url
        _http_method = http_method
        _parameters = parameters
        _request_body = request_body
        _responses = responses
        _api_models = api_models
        #
        param_models = self._make_param_models(_parameters, _api_models)
        request_models = self._make_request_models(_request_body, _api_models)
        response_models = self._make_response_models(_responses, _api_models)
        #
        def _method(self, **kwargs):  # pylint: disable=R
            url = _path_url
            params = {}
            data = None
            json_data = None
            headers = {}
            cookies = {}
            #
            # Prepare data
            #
            for parameter in _parameters:
                param_name = parameter.get("name", "").strip()
                if not param_name:
                    continue
                #
                param_aliases = [param_name, self._to_snake_case(param_name)]
                param_data = None
                param_found = False
                #
                for alias in param_aliases:
                    if alias in kwargs:
                        param_data = kwargs.pop(alias)
                        param_found = True
                        break
                #
                if parameter.get("required", False) and not param_found:
                    raise ValueError(f"Required parameter not set: {param_name}")
                #
                if not param_found:
                    continue
                #
                if param_name in param_models:
                    param_value = param_models[param_name].model_validate(param_data).model_dump(
                        by_alias=True,
                    )
                else:
                    param_value = param_data
                #
                param_loc = parameter.get("in", "").strip()
                if not param_loc:
                    continue
                #
                if param_loc == "header":
                    headers[param_name] = param_value
                elif param_loc == "path":
                    path_var = f"{{{param_name}}}"
                    url = url.replace(path_var, param_value)
                elif param_loc == "query":
                    params[param_name] = param_value
                elif param_loc == "cookie":
                    cookies[param_name] = param_value
            #
            if _request_body.get("required", False) and "request_body" not in kwargs:
                raise ValueError("Request body not set")
            #
            if "request_body" in kwargs:
                body_data = kwargs.pop("request_body")
                body_type = None
                #
                if not request_models:
                    if isinstance(body_data, dict):
                        json_data = body_data
                    else:
                        data = body_data
                else:
                    for content_type, body_model in request_models.items():
                        try:
                            body_obj = body_model.model_validate(body_data).model_dump(
                                by_alias=True,
                            )
                            body_type = content_type
                            break
                        except:  # pylint: disable=W0702
                            pass
                    #
                    if body_type is None:
                        raise ValueError("Invalid request body")
                    #
                    if body_type == "application/json":
                        json_data = body_obj
                    else:
                        headers["Content-Type"] = body_type
                        data = body_obj
            #
            # Make request
            #
            request_kwargs = {}
            request_kwargs.update(kwargs)
            #
            if params:
                request_kwargs["params"] = params
            #
            if data is not None:
                request_kwargs["data"] = data
            #
            if json_data is not None:
                request_kwargs["json"] = json_data
            #
            if headers:
                request_kwargs["headers"] = headers
            #
            if cookies:
                request_kwargs["cookies"] = cookies
            #
            if "timeout" not in request_kwargs and self.timeout is not None:
                request_kwargs["timeout"] = self.timeout
            #
            if "verify" not in request_kwargs and self.verify is not None:
                request_kwargs["verify"] = self.verify
            #
            response = self._request(_http_method, url, **request_kwargs)
            #
            # Validate response
            #
            if not response_models:
                return response
            #
            response_code = str(response.status_code)
            response_code_models = None
            #
            for code_alias in [response_code, f"{response_code[0]}XX"]:
                if code_alias in response_models:
                    response_code_models = response_models[code_alias]
                    break
            #
            if response_code_models is None:
                raise ValueError("Invalid response code")
            #
            response_type = response.headers.get("content-type", "")
            #
            if response_type not in response_code_models:
                raise ValueError("Invalid response type")
            #
            response_model = response_code_models[response_type]
            #
            try:
                if response_type == "application/json":
                    response_value = response_model.model_validate_json(response.content)
                else:
                    response_value = response_model.model_validate(response.content)
                #
                return response_value
            except Exception as exc:
                # Diagnostic only -- type/message below are unchanged so callers keep behaving
                # exactly as before (e.g. an out-of-enum status still surfaces as this ValueError).
                log.error(
                    "Invalid response for model %s, http_status=%s: %s",
                    getattr(response_model, "__name__", response_model),
                    response.status_code,
                    repr(exc)[:500],
                )
                raise ValueError("Invalid response") from exc
            #
            return None
        #
        return _method
