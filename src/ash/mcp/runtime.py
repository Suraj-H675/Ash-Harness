"""MCP tool discovery and Ash tool adapters."""

from __future__ import annotations

import asyncio
import base64
from collections.abc import Awaitable, Callable
from copy import deepcopy
import json
import re
import sys
from typing import Any

from jsonschema import Draft202012Validator  # type: ignore[import-untyped]
from jsonschema.exceptions import SchemaError  # type: ignore[import-untyped]
from jsonschema.validators import validator_for  # type: ignore[import-untyped]
from pydantic import BaseModel, Field

from ash.mcp.client import MCPClient, MCPProtocolError
from ash.mcp.server import MCPServerConfig
from ash.safety.environment import build_scrubbed_environment
from ash.safety.guard import SafetyGuard
from ash.sandbox.process_utils import (
    ProcessOutputLimitExceeded,
    communicate_process,
    process_group_options,
    terminate_process_tree,
)
from ash.tools.base import BaseTool, ToolResult, count_output_tokens


CURRENT_SCHEMA_DIALECT = "https://json-schema.org/draft/2020-12/schema"
LEGACY_SCHEMA_DIALECT = "http://json-schema.org/draft-07/schema#"
SCHEMA_VALIDATION_TIMEOUT_SECONDS = 1.5
MAX_SCHEMA_BYTES = 256 * 1024
MAX_SCHEMA_NODES = 4096
MAX_SCHEMA_DEPTH = 64
MAX_PATTERN_CHARACTERS = 1024
MAX_VALIDATION_PAYLOAD_BYTES = 2 * 1024 * 1024
MAX_WORKER_OUTPUT_BYTES = 64 * 1024
MAX_CONSECUTIVE_TOOL_REFRESHES = 3
TOOL_REFRESH_DEBOUNCE_SECONDS = 0.05


def _default_schema_dialect(protocol_version: str) -> str:
    return (
        CURRENT_SCHEMA_DIALECT
        if protocol_version >= "2025-11-25"
        else LEGACY_SCHEMA_DIALECT
    )


def _schema_validator(
    schema: dict[str, Any],
    *,
    label: str,
    protocol_version: str,
) -> str:
    """Check a bounded MCP JSON Schema and return its effective dialect."""

    if schema.get("type") != "object":
        raise ValueError(f"{label} root type must be object")
    try:
        encoded = _json_dump(schema).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} is not JSON-serializable: {exc}") from exc
    if len(encoded) > MAX_SCHEMA_BYTES:
        raise ValueError(f"{label} exceeds {MAX_SCHEMA_BYTES} bytes")
    nodes = 0
    pending: list[tuple[Any, int]] = [(schema, 0)]
    while pending:
        value, depth = pending.pop()
        nodes += 1
        if nodes > MAX_SCHEMA_NODES:
            raise ValueError(f"{label} exceeds {MAX_SCHEMA_NODES} nodes")
        if depth > MAX_SCHEMA_DEPTH:
            raise ValueError(f"{label} exceeds depth {MAX_SCHEMA_DEPTH}")
        if isinstance(value, dict):
            for key, child in value.items():
                if key in {"$ref", "$dynamicRef", "$recursiveRef"}:
                    if isinstance(child, str) and not child.startswith("#"):
                        raise ValueError(
                            f"{label} contains a non-local reference {child!r}"
                        )
                if key == "pattern" and isinstance(child, str):
                    if len(child) > MAX_PATTERN_CHARACTERS:
                        raise ValueError(
                            f"{label} pattern exceeds {MAX_PATTERN_CHARACTERS} characters"
                        )
                    try:
                        re.compile(child)
                    except re.error as exc:
                        raise ValueError(f"{label} contains an invalid pattern: {exc}") from exc
                if (
                    key == "patternProperties"
                    and isinstance(child, dict)
                    and all(
                        isinstance(subschema, (dict, bool))
                        for subschema in child.values()
                    )
                ):
                    for pattern in child:
                        if len(pattern) > MAX_PATTERN_CHARACTERS:
                            raise ValueError(
                                f"{label} pattern exceeds "
                                f"{MAX_PATTERN_CHARACTERS} characters"
                            )
                        try:
                            re.compile(pattern)
                        except re.error as exc:
                            raise ValueError(
                                f"{label} contains an invalid pattern: {exc}"
                            ) from exc
                pending.append((child, depth + 1))
        elif isinstance(value, list):
            pending.extend((child, depth + 1) for child in value)

    dialect = schema.get("$schema")
    if dialect is None:
        dialect = _default_schema_dialect(protocol_version)
        validator_class = (
            Draft202012Validator
            if dialect == CURRENT_SCHEMA_DIALECT
            else validator_for({"$schema": dialect}, default=None)
        )
    else:
        if not isinstance(dialect, str) or not dialect:
            raise ValueError(f"{label} has an invalid $schema dialect")
        validator_class = validator_for(schema, default=None)
        if validator_class is None:
            raise ValueError(f"{label} uses unsupported JSON Schema dialect {dialect!r}")
    try:
        validator_class.check_schema(schema)
    except SchemaError as exc:
        raise ValueError(f"{label} is not valid JSON Schema: {exc.message}") from exc
    return dialect


def _validation_error(label: str, message: str, path_parts: list[str]) -> str:
    path = ".".join(path_parts)
    location = f" at {path}" if path else ""
    return f"{label}{location}: {message}"


def _json_dump(value: Any) -> str:
    return json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=False,
        sort_keys=True,
    )


def _render_tool_result(result: dict[str, Any], *, is_error: bool) -> str:
    content = result["content"]
    assert isinstance(content, list)  # validated by the caller
    text_blocks: list[str] = []
    text_only = True
    for item in content:
        assert isinstance(item, dict)  # validated by the caller
        if (
            item.get("type") != "text"
            or not isinstance(item.get("text"), str)
            or set(item) != {"type", "text"}
        ):
            text_only = False
            continue
        text_blocks.append(item["text"])

    extension_keys = set(result) - {"content", "isError"}
    if not is_error and not extension_keys and text_only:
        return "\n".join(text_blocks)

    envelope = deepcopy(result)
    envelope["isError"] = is_error
    return _json_dump(envelope)


def _tool_error_text(content: list[dict[str, Any]]) -> str:
    messages = [
        item["text"].strip()
        for item in content
        if item.get("type") == "text"
        and isinstance(item.get("text"), str)
        and item["text"].strip()
    ]
    return "\n".join(messages) or "MCP tool returned an error"


def _validate_annotations(value: Any) -> str | None:
    if not isinstance(value, dict):
        return "annotations must be an object"
    audience = value.get("audience")
    if audience is not None and (
        not isinstance(audience, list)
        or not all(
            isinstance(item, str) and item in {"user", "assistant"}
            for item in audience
        )
    ):
        return "annotations.audience must contain only user or assistant"
    priority = value.get("priority")
    if priority is not None and (
        isinstance(priority, bool)
        or not isinstance(priority, (int, float))
        or not 0 <= priority <= 1
    ):
        return "annotations.priority must be a number from 0 to 1"
    last_modified = value.get("lastModified")
    if last_modified is not None and not isinstance(last_modified, str):
        return "annotations.lastModified must be a string"
    return None


def _valid_base64(value: str) -> bool:
    try:
        base64.b64decode(value, validate=True)
    except (ValueError, TypeError):
        return False
    return True


def _validate_content_block(item: dict[str, Any], protocol_version: str) -> str | None:
    block_type = item.get("type")
    allowed = {"text", "image", "resource"}
    if protocol_version >= "2025-03-26":
        allowed.add("audio")
    if protocol_version >= "2025-06-18":
        allowed.add("resource_link")
    if block_type not in allowed:
        return f"unsupported content type {block_type!r}"
    if "_meta" in item and not isinstance(item["_meta"], dict):
        return "content _meta must be an object"
    if "annotations" in item:
        error = _validate_annotations(item["annotations"])
        if error is not None:
            return error
    if block_type == "text":
        return None if isinstance(item.get("text"), str) else "text must be a string"
    if block_type in {"image", "audio"}:
        data = item.get("data")
        if not isinstance(data, str) or not _valid_base64(data):
            return f"{block_type} data must be valid base64"
        if not isinstance(item.get("mimeType"), str) or not item["mimeType"]:
            return f"{block_type} mimeType must be a non-empty string"
        return None
    if block_type == "resource_link":
        if not isinstance(item.get("uri"), str) or not item["uri"]:
            return "resource link uri must be a non-empty string"
        if not isinstance(item.get("name"), str) or not item["name"]:
            return "resource link name must be a non-empty string"
        size = item.get("size")
        if size is not None and (
            isinstance(size, bool) or not isinstance(size, int) or size < 0
        ):
            return "resource link size must be a non-negative integer"
        for field in ("title", "description", "mimeType"):
            if field in item and not isinstance(item[field], str):
                return f"resource link {field} must be a string"
        if "icons" in item and (
            not isinstance(item["icons"], list)
            or not all(isinstance(icon, dict) for icon in item["icons"])
        ):
            return "resource link icons must be an array of objects"
        return None
    resource = item.get("resource")
    if not isinstance(resource, dict):
        return "embedded resource must be an object"
    if not isinstance(resource.get("uri"), str) or not resource["uri"]:
        return "embedded resource uri must be a non-empty string"
    if "_meta" in resource and not isinstance(resource["_meta"], dict):
        return "embedded resource _meta must be an object"
    if "mimeType" in resource and not isinstance(resource["mimeType"], str):
        return "embedded resource mimeType must be a string"
    has_text = "text" in resource
    has_blob = "blob" in resource
    if has_text == has_blob:
        return "embedded resource must contain exactly one of text or blob"
    if has_text and not isinstance(resource["text"], str):
        return "embedded resource text must be a string"
    if has_blob and (
        not isinstance(resource["blob"], str) or not _valid_base64(resource["blob"])
    ):
        return "embedded resource blob must be valid base64"
    return None


async def _validate_schema_instance(
    schema: dict[str, Any],
    instance: Any,
    *,
    default_dialect: str,
) -> dict[str, Any]:
    request = _json_dump(
        {
            "schema": schema,
            "instance": instance,
            "defaultDialect": default_dialect,
        }
    ).encode("utf-8")
    if len(request) > MAX_VALIDATION_PAYLOAD_BYTES:
        return {
            "valid": False,
            "internal": True,
            "message": f"validation payload exceeds {MAX_VALIDATION_PAYLOAD_BYTES} bytes",
        }
    try:
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "ash.mcp.schema_worker",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=build_scrubbed_environment(),
            **process_group_options(),
        )
    except OSError as exc:
        return {
            "valid": False,
            "internal": True,
            "message": f"could not start isolated schema validator: {exc}",
        }
    try:
        stdout, stderr = await asyncio.wait_for(
            communicate_process(
                process,
                input_data=request,
                max_output_bytes=MAX_WORKER_OUTPUT_BYTES,
            ),
            timeout=SCHEMA_VALIDATION_TIMEOUT_SECONDS,
        )
    except asyncio.TimeoutError:
        await terminate_process_tree(process, grace_seconds=0.1)
        return {
            "valid": False,
            "internal": True,
            "message": "schema validation exceeded its isolated deadline",
        }
    except ProcessOutputLimitExceeded:
        await terminate_process_tree(process, grace_seconds=0.1)
        return {
            "valid": False,
            "internal": True,
            "message": "schema validator exceeded its output limit",
        }
    except asyncio.CancelledError:
        await terminate_process_tree(process, grace_seconds=0.1)
        raise
    except Exception as exc:  # noqa: BLE001 - contain validator infrastructure
        await terminate_process_tree(process, grace_seconds=0.1)
        return {
            "valid": False,
            "internal": True,
            "message": str(exc).strip() or type(exc).__name__,
        }
    if process.returncode != 0:
        message = stderr.decode("utf-8", errors="replace").strip()
        if process.returncode is not None and process.returncode < 0:
            message = "schema validation process exceeded its resource limit"
        return {
            "valid": False,
            "internal": True,
            "message": message or "isolated schema validator failed",
        }
    try:
        response = json.loads(stdout)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return {
            "valid": False,
            "internal": True,
            "message": "isolated schema validator returned invalid JSON",
        }
    if not isinstance(response, dict) or not isinstance(response.get("valid"), bool):
        return {
            "valid": False,
            "internal": True,
            "message": "isolated schema validator returned an invalid response",
        }
    return response


class MCPTool(BaseTool):
    args_schema = None

    def __init__(
        self,
        safety_guard: SafetyGuard,
        *,
        client: MCPClient,
        server_name: str,
        definition: dict[str, Any],
        protocol_version: str = "2025-11-25",
    ) -> None:
        super().__init__(safety_guard)
        self.client = client
        self.protocol_version = protocol_version
        self._contract_fingerprint = (
            f"{protocol_version}\0{_json_dump(deepcopy(definition))}"
        )
        remote_name = definition.get("name")
        if not isinstance(remote_name, str) or not remote_name:
            raise ValueError("MCP tool name must be a non-empty string")
        self.remote_name = remote_name
        self.name = f"mcp__{server_name}__{self.remote_name}"
        description = definition.get("description", "MCP tool")
        self.description = description if isinstance(description, str) else "MCP tool"
        if protocol_version >= "2025-11-25" and "execution" in definition:
            execution = definition["execution"]
            if not isinstance(execution, dict):
                raise ValueError("MCP tool execution metadata must be an object")
            task_support = execution.get("taskSupport", "forbidden")
            if task_support not in {"forbidden", "optional", "required"}:
                raise ValueError("MCP tool taskSupport is invalid")
            if task_support == "required":
                raise ValueError(
                    "MCP tool requires experimental task execution, which Ash "
                    "does not support"
                )
        input_schema = definition.get("inputSchema")
        if not isinstance(input_schema, dict):
            raise ValueError("MCP tool inputSchema must be an object")
        self._input_schema = deepcopy(input_schema)
        self._input_dialect = _schema_validator(
            self._input_schema,
            label=f"MCP tool {self.remote_name!r} inputSchema",
            protocol_version=protocol_version,
        )
        output_schema = (
            definition.get("outputSchema")
            if protocol_version >= "2025-06-18"
            else None
        )
        if output_schema is not None and not isinstance(output_schema, dict):
            raise ValueError("MCP tool outputSchema must be an object")
        self._output_schema = (
            deepcopy(output_schema) if isinstance(output_schema, dict) else None
        )
        self._output_dialect = (
            _schema_validator(
                self._output_schema,
                label=f"MCP tool {self.remote_name!r} outputSchema",
                protocol_version=protocol_version,
            )
            if self._output_schema is not None
            else None
        )

    def json_schema(self) -> dict[str, Any]:
        """Return the server-declared schema without lossy reconstruction."""

        return deepcopy(self._input_schema)

    def contract_fingerprint(self) -> str:
        """Return a canonical fingerprint of the complete server declaration."""

        return self._contract_fingerprint

    async def run(self, **kwargs: Any) -> ToolResult:
        try:
            input_validation = await _validate_schema_instance(
                self._input_schema,
                kwargs,
                default_dialect=self._input_dialect,
            )
        except (TypeError, ValueError) as exc:
            return ToolResult(
                success=False,
                output="",
                error=f"invalid MCP tool arguments: not JSON-serializable: {exc}",
            )
        if not input_validation["valid"]:
            message = str(input_validation.get("message", "validation failed"))
            path = input_validation.get("path", [])
            if input_validation.get("internal"):
                error = f"MCP input schema could not validate arguments: {message}"
            else:
                error = _validation_error(
                    "invalid MCP tool arguments",
                    message,
                    path if isinstance(path, list) else [],
                )
            return ToolResult(
                success=False,
                output="",
                error=error,
            )

        try:
            validator = getattr(self.client, "tool_contract_validator", None)
            generation = int(getattr(self.client, "session_generation", 0))
            if callable(validator) and not validator(
                self.remote_name, self._contract_fingerprint, generation
            ):
                raise MCPProtocolError(
                    f"MCP tool {self.remote_name!r} no longer matches "
                    "the active verified server contract"
                )
            result = await self.client.call_tool(
                self.remote_name,
                dict(kwargs),
                expected_contract=self._contract_fingerprint,
            )
        except MCPProtocolError as exc:
            error_payload: dict[str, Any] = {
                "type": "mcp_protocol_error",
                "message": str(exc),
            }
            if exc.code is not None:
                error_payload["code"] = exc.code
            if exc.has_data:
                error_payload["data"] = exc.data
            output = _json_dump({"error": error_payload})
            return ToolResult(
                success=False,
                output=output,
                error=str(exc),
                token_count=count_output_tokens(output),
            )
        except Exception as exc:  # noqa: BLE001 - prevent unsafe automatic replay
            message = str(exc).strip() or type(exc).__name__
            output = _json_dump(
                {
                    "error": {
                        "type": "mcp_transport_error",
                        "message": message,
                    }
                }
            )
            return ToolResult(
                success=False,
                output=output,
                error=message,
                token_count=count_output_tokens(output),
            )
        if not isinstance(result, dict):
            try:
                raw_result = _json_dump(result)
            except (TypeError, ValueError):
                raw_result = ""
            return ToolResult(
                success=False,
                output=raw_result,
                error="invalid MCP tool result: result must be an object",
                token_count=count_output_tokens(raw_result),
            )
        try:
            raw_result = _json_dump(result)
        except (TypeError, ValueError) as exc:
            return ToolResult(
                success=False,
                output="",
                error=f"invalid MCP tool result: not JSON-serializable: {exc}",
            )
        if "content" not in result:
            return ToolResult(
                success=False,
                output=raw_result,
                error="invalid MCP tool result: content is required",
                token_count=count_output_tokens(raw_result),
            )
        raw_content = result["content"]
        if not isinstance(raw_content, list) or not all(
            isinstance(item, dict) for item in raw_content
        ):
            return ToolResult(
                success=False,
                output=raw_result,
                error="invalid MCP tool result: content must be an array of objects",
                token_count=count_output_tokens(raw_result),
            )
        content = [dict(item) for item in raw_content]
        for index, item in enumerate(content):
            content_error = _validate_content_block(item, self.protocol_version)
            if content_error is not None:
                return ToolResult(
                    success=False,
                    output=raw_result,
                    error=(
                        f"invalid MCP tool result: content[{index}] {content_error}"
                    ),
                    token_count=count_output_tokens(raw_result),
                )
        if "structuredContent" in result and not isinstance(
            result["structuredContent"], dict
        ):
            return ToolResult(
                success=False,
                output=raw_result,
                error="invalid MCP tool result: structuredContent must be an object",
                token_count=count_output_tokens(raw_result),
            )
        structured_content = result.get("structuredContent")
        if "_meta" in result and not isinstance(result["_meta"], dict):
            return ToolResult(
                success=False,
                output=raw_result,
                error="invalid MCP tool result: _meta must be an object",
                token_count=count_output_tokens(raw_result),
            )
        raw_is_error = result.get("isError", False)
        if not isinstance(raw_is_error, bool):
            return ToolResult(
                success=False,
                output=raw_result,
                error="invalid MCP tool result: isError must be a boolean",
                token_count=count_output_tokens(raw_result),
            )
        is_error = raw_is_error
        normalized_result = deepcopy(result)
        normalized_result["content"] = content
        normalized_result["isError"] = is_error
        output = _render_tool_result(normalized_result, is_error=is_error)

        if self._output_schema is not None:
            if structured_content is None:
                return ToolResult(
                    success=False,
                    output=output,
                    error=(
                        "invalid MCP tool result: outputSchema requires "
                        "structuredContent"
                    ),
                    token_count=count_output_tokens(output),
                )
            assert self._output_dialect is not None
            try:
                output_validation = await _validate_schema_instance(
                    self._output_schema,
                    structured_content,
                    default_dialect=self._output_dialect,
                )
            except (TypeError, ValueError) as exc:
                return ToolResult(
                    success=False,
                    output=output,
                    error=f"invalid MCP structured result: {exc}",
                    token_count=count_output_tokens(output),
                )
            if not output_validation["valid"]:
                message = str(output_validation.get("message", "validation failed"))
                path = output_validation.get("path", [])
                error = (
                    f"MCP output schema could not validate result: {message}"
                    if output_validation.get("internal")
                    else _validation_error(
                        "invalid MCP structured result",
                        message,
                        path if isinstance(path, list) else [],
                    )
                )
                return ToolResult(
                    success=False,
                    output=output,
                    error=error,
                    token_count=count_output_tokens(output),
                )

        return ToolResult(
            success=not is_error,
            output=output,
            error=_tool_error_text(content) if is_error else None,
            token_count=count_output_tokens(output),
        )


class MCPReadResourceArgs(BaseModel):
    server: str
    uri: str


class MCPReadResourceTool(BaseTool):
    name = "mcp_read_resource"
    description = "Read one resource from a connected MCP server."
    args_schema = MCPReadResourceArgs

    def __init__(self, safety_guard: SafetyGuard, runtime: "MCPRuntime") -> None:
        super().__init__(safety_guard)
        self.runtime = runtime

    async def run(self, **kwargs: Any) -> ToolResult:
        args = MCPReadResourceArgs(**kwargs)
        client = self.runtime.clients.get(args.server)
        if client is None:
            return ToolResult(success=False, output="", error="Unknown MCP server")
        result = await client.read_resource(args.uri)
        output = json.dumps(result, ensure_ascii=False)
        return ToolResult(
            success=True, output=output, token_count=count_output_tokens(output)
        )


class MCPGetPromptArgs(BaseModel):
    server: str
    name: str
    arguments: dict[str, str] = Field(default_factory=dict)


class MCPGetPromptTool(BaseTool):
    name = "mcp_get_prompt"
    description = "Render one prompt from a connected MCP server."
    args_schema = MCPGetPromptArgs

    def __init__(self, safety_guard: SafetyGuard, runtime: "MCPRuntime") -> None:
        super().__init__(safety_guard)
        self.runtime = runtime

    async def run(self, **kwargs: Any) -> ToolResult:
        args = MCPGetPromptArgs(**kwargs)
        client = self.runtime.clients.get(args.server)
        if client is None:
            return ToolResult(success=False, output="", error="Unknown MCP server")
        result = await client.get_prompt(args.name, args.arguments)
        output = json.dumps(result, ensure_ascii=False)
        return ToolResult(
            success=True, output=output, token_count=count_output_tokens(output)
        )


class MCPListCapabilitiesArgs(BaseModel):
    server: str = ""


class _MCPListCapabilityTool(BaseTool):
    capability_method = ""

    def __init__(self, safety_guard: SafetyGuard, runtime: "MCPRuntime") -> None:
        super().__init__(safety_guard)
        self.runtime = runtime

    async def run(self, **kwargs: Any) -> ToolResult:
        args = MCPListCapabilitiesArgs(**kwargs)
        try:
            items = await self.runtime.list_capability(
                self.capability_method,
                server=args.server or None,
            )
        except ValueError as exc:
            return ToolResult(success=False, output="", error=str(exc))
        output = json.dumps(items, ensure_ascii=False)
        return ToolResult(
            success=True,
            output=output,
            token_count=count_output_tokens(output),
        )


class MCPListResourcesTool(_MCPListCapabilityTool):
    name = "mcp_list_resources"
    description = "List resources exposed by connected MCP servers."
    args_schema = MCPListCapabilitiesArgs
    capability_method = "list_resources"


class MCPListResourceTemplatesTool(_MCPListCapabilityTool):
    name = "mcp_list_resource_templates"
    description = "List parameterized resource templates from MCP servers."
    args_schema = MCPListCapabilitiesArgs
    capability_method = "list_resource_templates"


class MCPListPromptsTool(_MCPListCapabilityTool):
    name = "mcp_list_prompts"
    description = "List prompt templates exposed by connected MCP servers."
    args_schema = MCPListCapabilitiesArgs
    capability_method = "list_prompts"


class MCPRuntime:
    def __init__(
        self,
        configs: dict[str, MCPServerConfig],
        safety_guard: SafetyGuard,
        *,
        tool_change_handler: Callable[
            [str, dict[str, BaseTool], dict[str, BaseTool]], Awaitable[None]
        ]
        | None = None,
        event_sink: Callable[[dict[str, Any]], None] | None = None,
        defer_notifications: bool = False,
    ) -> None:
        self.configs = configs
        self.safety_guard = safety_guard
        self.clients: dict[str, MCPClient] = {}
        self.errors: dict[str, str] = {}
        self._server_tools: dict[str, dict[str, BaseTool]] = {}
        self._tool_change_handler = tool_change_handler
        self._event_sink = event_sink
        self._defer_notifications = defer_notifications
        self._refresh_locks: dict[str, asyncio.Lock] = {}
        self._recovery_reconcile_locks: dict[str, asyncio.Lock] = {}
        self._refresh_owners: dict[str, asyncio.Task[Any]] = {}
        self._recovery_results: dict[tuple[str, int], bool] = {}
        self._validated_generations: dict[str, int] = {}
        self._tool_catalog_epochs: dict[str, int] = {}
        self._validated_tool_catalog_epochs: dict[str, int] = {}
        self._refresh_tasks: dict[str, asyncio.Task[None]] = {}
        self._refresh_requested: set[str] = set()
        self._startup_notifications: set[str] = set()
        self._catalog_revisions: dict[str, int] = {}
        self._started = False
        self._notifications_active = False
        self._closed = False

    async def start(self) -> dict[str, BaseTool]:
        if self._started:
            raise RuntimeError("MCP runtime is already started")
        self._closed = False
        tools: dict[str, BaseTool] = {}
        for name, config in self.configs.items():
            client = MCPClient(
                config,
                roots=(self.safety_guard.project_root,),
            )
            client.notification_handler = (
                lambda method, params, server=name, source=client: (
                    self._handle_notification(server, source, method, params)
                )
            )
            client.session_reinitialized_handler = (
                lambda generation, method, params, server=name, source=client: (
                    self._handle_session_reinitialized(
                        server, source, generation, method, params
                    )
                )
            )
            client.tool_contract_validator = (
                lambda remote_name, fingerprint, generation, server=name: (
                    self._validate_tool_contract(
                        server, remote_name, fingerprint, generation
                    )
                )
            )
            self.clients[name] = client
            self._refresh_locks[name] = asyncio.Lock()
            self._recovery_reconcile_locks[name] = asyncio.Lock()
            try:
                await client.connect()
                async with self._refresh_locks[name]:
                    task = asyncio.current_task()
                    catalog_epoch = self._tool_catalog_epochs.get(name, 0)
                    if task is not None:
                        self._refresh_owners[name] = task
                    try:
                        definitions = (
                            await client.list_tools()
                            if client.supports_server_capability("tools")
                            else []
                        )
                        server_tools = self._build_server_tools(
                            name, client, definitions, strict=False
                        )
                        collisions = tools.keys() & server_tools.keys()
                        if collisions:
                            raise MCPProtocolError(
                                "MCP tool name collision: "
                                + ", ".join(sorted(collisions))
                            )
                    finally:
                        if self._refresh_owners.get(name) is task:
                            self._refresh_owners.pop(name, None)
            except asyncio.CancelledError:
                await asyncio.shield(self.close())
                raise
            except Exception as exc:  # noqa: BLE001
                self.errors[name] = str(exc)
                self.clients.pop(name, None)
                self._refresh_locks.pop(name, None)
                self._recovery_reconcile_locks.pop(name, None)
                await client.disconnect()
                continue
            self._server_tools[name] = server_tools
            self._validated_generations[name] = int(
                getattr(client, "session_generation", 0)
            )
            self._validated_tool_catalog_epochs[name] = catalog_epoch
            tools.update(server_tools)
        if self.clients:
            resource_tool = MCPReadResourceTool(self.safety_guard, self)
            prompt_tool = MCPGetPromptTool(self.safety_guard, self)
            capability_tools: list[BaseTool] = [
                resource_tool,
                prompt_tool,
                MCPListResourcesTool(self.safety_guard, self),
                MCPListResourceTemplatesTool(self.safety_guard, self),
                MCPListPromptsTool(self.safety_guard, self),
            ]
            tools.update({tool.name: tool for tool in capability_tools})
        self._started = True
        if not self._defer_notifications:
            self.activate_notifications()
        return tools

    def activate_notifications(self) -> None:
        if not self._started or self._closed:
            raise RuntimeError("MCP runtime is not active")
        self._notifications_active = True
        for name in tuple(self._startup_notifications):
            self._schedule_tool_refresh(name)
        self._startup_notifications.clear()

    def server_tools_snapshot(self) -> dict[str, dict[str, BaseTool]]:
        return {name: dict(tools) for name, tools in self._server_tools.items()}

    def _build_server_tools(
        self,
        server_name: str,
        client: MCPClient,
        definitions: list[dict[str, Any]],
        *,
        strict: bool,
    ) -> dict[str, BaseTool]:
        prefix = f"{server_name}:tool:"
        for key in tuple(self.errors):
            if key.startswith(prefix):
                self.errors.pop(key, None)
        tools: dict[str, BaseTool] = {}
        failures: list[str] = []
        remote_names: set[str] = set()
        for definition in definitions:
            remote_name = definition.get("name", "<unknown>")
            label = str(remote_name)
            try:
                if not isinstance(remote_name, str) or not remote_name:
                    raise ValueError("MCP tool name must be a non-empty string")
                if remote_name in remote_names:
                    raise ValueError(f"duplicate MCP tool name {remote_name!r}")
                remote_names.add(remote_name)
                tool = MCPTool(
                    self.safety_guard,
                    client=client,
                    server_name=server_name,
                    definition=definition,
                    protocol_version=getattr(
                        client, "protocol_version", "2025-11-25"
                    ),
                )
                if tool.name in tools:
                    raise ValueError(f"duplicate generated MCP tool name {tool.name!r}")
                tools[tool.name] = tool
            except Exception as exc:  # noqa: BLE001 - untrusted catalog entry
                self.errors[f"{prefix}{label}"] = str(exc)
                failures.append(f"{label}: {exc}")
        if strict and failures:
            raise MCPProtocolError(
                f"MCP server {server_name!r} returned an invalid tool catalog: "
                + "; ".join(failures)
            )
        return tools

    async def _handle_notification(
        self,
        server_name: str,
        client: MCPClient,
        method: str,
        params: dict[str, Any],
    ) -> None:
        del params
        if self._closed or self.clients.get(server_name) is not client:
            return
        capability_by_notification = {
            "notifications/tools/list_changed": "tools",
            "notifications/resources/list_changed": "resources",
            "notifications/prompts/list_changed": "prompts",
        }
        capability = capability_by_notification.get(method)
        if capability is None:
            return
        advertised = client.server_capabilities.get(capability)
        if not isinstance(advertised, dict) or advertised.get("listChanged") is not True:
            self.errors[f"{server_name}:notification:{method}"] = (
                "server sent list_changed without declaring listChanged"
            )
            return
        if capability == "tools":
            self._tool_catalog_epochs[server_name] = (
                self._tool_catalog_epochs.get(server_name, 0) + 1
            )
            if not self._notifications_active:
                self._startup_notifications.add(server_name)
            else:
                self._schedule_tool_refresh(server_name)
            return
        revision = self._catalog_revisions.get(server_name, 0) + 1
        self._catalog_revisions[server_name] = revision
        self._emit_event(
            {
                "type": "mcp.catalog.changed",
                "server": server_name,
                "capability": capability,
                "revision": revision,
            }
        )

    async def _handle_session_reinitialized(
        self,
        server_name: str,
        client: MCPClient,
        generation: int,
        method: str,
        params: dict[str, Any],
    ) -> bool:
        del params
        if self._closed or self.clients.get(server_name) is not client:
            return False
        current_task = asyncio.current_task()
        if self._refresh_owners.get(server_name) is current_task:
            return True
        recovery_lock = self._recovery_reconcile_locks.get(server_name)
        if recovery_lock is None:
            return False
        key = (server_name, generation)
        async with recovery_lock:
            cached = self._recovery_results.get(key)
            if cached is not None:
                return cached if method == "tools/call" else True
            previous = self._server_tools.get(server_name, {})
            previous_contract = {
                name: getattr(tool, "contract_fingerprint")()
                for name, tool in previous.items()
            }
            try:
                await self._refresh_server_tools(server_name)
            except Exception as exc:  # noqa: BLE001 - keep last good catalog
                self.errors[f"{server_name}:tools/recovery"] = str(exc)
                retry_allowed = False
            else:
                replacement = self._server_tools.get(server_name, {})
                replacement_contract = {
                    name: getattr(tool, "contract_fingerprint")()
                    for name, tool in replacement.items()
                }
                retry_allowed = previous_contract == replacement_contract
                self.errors.pop(f"{server_name}:tools/recovery", None)
            self._recovery_results[key] = retry_allowed
            if len(self._recovery_results) > 16:
                oldest = next(iter(self._recovery_results))
                self._recovery_results.pop(oldest, None)
            self._emit_event(
                {
                    "type": "mcp.session.reinitialized",
                    "server": server_name,
                    "generation": generation,
                    "contract_changed": not retry_allowed,
                }
            )
            return retry_allowed if method == "tools/call" else True

    def _validate_tool_contract(
        self,
        server_name: str,
        remote_name: str,
        fingerprint: str,
        generation: int,
    ) -> bool:
        client = self.clients.get(server_name)
        if (
            self._closed
            or client is None
            or int(getattr(client, "session_generation", 0)) != generation
            or self._validated_generations.get(server_name) != generation
            or self._validated_tool_catalog_epochs.get(server_name, 0)
            != self._tool_catalog_epochs.get(server_name, 0)
        ):
            return False
        tool = self._server_tools.get(server_name, {}).get(
            f"mcp__{server_name}__{remote_name}"
        )
        return bool(
            tool is not None
            and getattr(tool, "contract_fingerprint")() == fingerprint
        )

    def _schedule_tool_refresh(self, server_name: str) -> None:
        if self._closed or server_name not in self.clients:
            return
        self._refresh_requested.add(server_name)
        task = self._refresh_tasks.get(server_name)
        if task is not None and not task.done():
            return
        task = asyncio.create_task(self._run_tool_refreshes(server_name))
        self._refresh_tasks[server_name] = task

    async def _run_tool_refreshes(self, server_name: str) -> None:
        consecutive = 0
        try:
            while (
                server_name in self._refresh_requested
                and not self._closed
                and consecutive < MAX_CONSECUTIVE_TOOL_REFRESHES
            ):
                self._refresh_requested.discard(server_name)
                try:
                    await self._refresh_server_tools(server_name)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001 - preserve last good catalog
                    self.errors[f"{server_name}:tools/refresh"] = str(exc)
                consecutive += 1
                await asyncio.sleep(TOOL_REFRESH_DEBOUNCE_SECONDS)
            if server_name in self._refresh_requested:
                self._refresh_requested.discard(server_name)
                self.errors[f"{server_name}:tools/refresh"] = (
                    "suppressed a self-sustaining tools/list_changed refresh storm"
                )
                self._emit_event(
                    {
                        "type": "mcp.catalog.refresh_suppressed",
                        "server": server_name,
                        "capability": "tools",
                    }
                )
        finally:
            current = self._refresh_tasks.get(server_name)
            if current is asyncio.current_task():
                self._refresh_tasks.pop(server_name, None)

    async def _refresh_server_tools(self, server_name: str) -> None:
        client = self.clients.get(server_name)
        lock = self._refresh_locks.get(server_name)
        if client is None or lock is None:
            return
        async with lock:
            task = asyncio.current_task()
            catalog_epoch = self._tool_catalog_epochs.get(server_name, 0)
            if task is not None:
                self._refresh_owners[server_name] = task
            try:
                if self._closed or self.clients.get(server_name) is not client:
                    return
                definitions = (
                    await client.list_tools()
                    if client.supports_server_capability("tools")
                    else []
                )
                if self._closed or self.clients.get(server_name) is not client:
                    return
                replacement = self._build_server_tools(
                    server_name, client, definitions, strict=True
                )
                previous = self._server_tools.get(server_name, {})
                if self._tool_change_handler is not None:
                    await self._tool_change_handler(
                        server_name, previous, replacement
                    )
                if self._closed or self.clients.get(server_name) is not client:
                    return
                self._server_tools[server_name] = replacement
                self._validated_generations[server_name] = int(
                    getattr(client, "session_generation", 0)
                )
                self._validated_tool_catalog_epochs[server_name] = catalog_epoch
                self.errors.pop(f"{server_name}:tools/refresh", None)
                revision = self._catalog_revisions.get(server_name, 0) + 1
                self._catalog_revisions[server_name] = revision
                previous_names = set(previous)
                replacement_names = set(replacement)
                changed = {
                    name
                    for name in previous_names & replacement_names
                    if getattr(previous[name], "contract_fingerprint")()
                    != getattr(replacement[name], "contract_fingerprint")()
                }
                self._emit_event(
                    {
                        "type": "mcp.catalog.changed",
                        "server": server_name,
                        "capability": "tools",
                        "revision": revision,
                        "added": sorted(replacement_names - previous_names),
                        "removed": sorted(previous_names - replacement_names),
                        "changed": sorted(changed),
                    }
                )
            finally:
                if self._refresh_owners.get(server_name) is task:
                    self._refresh_owners.pop(server_name, None)

    def _emit_event(self, event: dict[str, Any]) -> None:
        if self._event_sink is not None:
            self._event_sink(event)

    async def wait_for_refreshes(self) -> None:
        idle_ticks = 0
        while idle_ticks < 2:
            await asyncio.sleep(0)
            tasks = tuple(self._refresh_tasks.values())
            if not tasks:
                idle_ticks += 1
                continue
            idle_ticks = 0
            await asyncio.gather(*tasks, return_exceptions=True)

    async def close(self) -> None:
        self._closed = True
        self._refresh_requested.clear()
        tasks = tuple(self._refresh_tasks.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self._refresh_tasks.clear()
        await asyncio.gather(
            *(client.disconnect() for client in self.clients.values()),
            return_exceptions=True,
        )
        self.clients.clear()
        self._server_tools.clear()
        self._refresh_locks.clear()
        self._recovery_reconcile_locks.clear()
        self._refresh_owners.clear()
        self._recovery_results.clear()
        self._validated_generations.clear()
        self._tool_catalog_epochs.clear()
        self._validated_tool_catalog_epochs.clear()
        self._started = False
        self._notifications_active = False

    async def list_resources(self) -> list[dict[str, Any]]:
        return await self._list_capability("list_resources")

    async def list_prompts(self) -> list[dict[str, Any]]:
        return await self._list_capability("list_prompts")

    async def list_resource_templates(self) -> list[dict[str, Any]]:
        return await self._list_capability("list_resource_templates")

    async def _list_capability(self, method: str) -> list[dict[str, Any]]:
        return await self.list_capability(method)

    async def list_capability(
        self,
        method: str,
        *,
        server: str | None = None,
    ) -> list[dict[str, Any]]:
        capability_by_method = {
            "list_resources": "resources",
            "list_resource_templates": "resources",
            "list_prompts": "prompts",
        }
        if method not in capability_by_method:
            raise ValueError(f"unsupported MCP list capability: {method}")
        if server is not None and server not in self.clients:
            raise ValueError(f"unknown MCP server: {server}")
        output: list[dict[str, Any]] = []
        clients = (
            [(server, self.clients[server])]
            if server is not None
            else list(self.clients.items())
        )
        for server_name, client in clients:
            if not client.supports_server_capability(capability_by_method[method]):
                continue
            try:
                items = await getattr(client, method)()
            except Exception as exc:  # noqa: BLE001
                self.errors[f"{server_name}:{method}"] = str(exc)
                continue
            output.extend({"server": server_name, **item} for item in items)
        return output
