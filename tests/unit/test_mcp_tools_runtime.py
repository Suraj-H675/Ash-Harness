# tests/unit/test_mcp_tools_runtime.py
import asyncio
import io
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, Mock

import pytest

from ash.core.loop import AshLoop
from ash.core.secret_middleware import SecretRedactionMiddleware
from ash.core.session import SessionStore, ToolCallRecord
from ash.mcp import client as mcp_client_module
from ash.mcp.client import (
    MCPClient,
    MCPProtocolError,
    MCPTaskTimeout,
)
from ash.mcp.runtime import (
    CURRENT_SCHEMA_DIALECT,
    MCPListResourcesTool,
    MCPRuntime,
    MCPTool,
    PROVIDER_TOOL_NAME,
    _validate_schema_instance,
    mcp_tool_name,
)
from ash.mcp.server import (
    MCPServerConfig,
    mcp_server_fingerprint,
)
from ash.logging import current_log_context, replace_log_context
from ash.providers.base import ProviderABC, StreamChunk
from ash.safety.guard import SafetyGuard
from ash.tools.base import ToolExecutionOutcome
from ash.ui.headless import HeadlessUI

from .mcp_test_fixtures import DYNAMIC_MCP_SERVER, FAKE_MCP_SERVER


class IdleProvider(ProviderABC):
    model_name = "idle"

    async def stream_chat(self, messages, temperature=0.0, tools=None):
        yield StreamChunk(content="idle", is_done=True)

    def count_tokens(self, text: str) -> int:
        return len(text.split())




COMPLEX_MCP_INPUT_SCHEMA = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$defs": {
        "identifier": {"type": "string", "pattern": "^[a-z]+$"},
    },
    "type": "object",
    "properties": {
        "mode": {"type": "string", "enum": ["safe", "fast"]},
        "target": {
            "oneOf": [
                {
                    "type": "object",
                    "properties": {
                        "kind": {"const": "path"},
                        "path": {"$ref": "#/$defs/identifier"},
                    },
                    "required": ["kind", "path"],
                    "additionalProperties": False,
                },
                {
                    "type": "object",
                    "properties": {
                        "kind": {"const": "id"},
                        "id": {"type": "integer", "minimum": 1},
                    },
                    "required": ["kind", "id"],
                    "additionalProperties": False,
                },
            ]
        },
        "options": {
            "anyOf": [
                {"type": "null"},
                {
                    "type": "object",
                    "properties": {"enabled": {"type": "boolean"}},
                    "required": ["enabled"],
                    "additionalProperties": False,
                },
            ]
        },
        "tags": {
            "type": "array",
            "items": {"type": "string", "minLength": 1},
            "uniqueItems": True,
        },
        "limit": {"type": "integer", "minimum": 1},
    },
    "required": ["mode", "target", "options", "tags", "limit"],
    "additionalProperties": False,
}


class StubMCPClient:
    def __init__(self, result: dict) -> None:
        self.result = result
        self.calls: list[tuple[str, dict]] = []
        self.config = MCPServerConfig(name="stub", command="fake", args=[], env={})
        self.server_info: dict[str, Any] = {}

    async def call_tool(
        self,
        name: str,
        arguments: dict,
        *,
        expected_contract: str | None = None,
        as_task: bool = False,
        header_annotations: list | None = None,
    ) -> dict:
        del expected_contract, as_task, header_annotations
        self.calls.append((name, arguments))
        return self.result


def _mcp_tool(
    tmp_path: Path,
    client: StubMCPClient,
    *,
    input_schema: dict | None = None,
    output_schema: dict | None = None,
    protocol_version: str = "2025-11-25",
) -> MCPTool:
    definition = {
        "name": "complex",
        "description": "Exercise the complete MCP schema boundary.",
        "inputSchema": input_schema or COMPLEX_MCP_INPUT_SCHEMA,
    }
    if output_schema is not None:
        definition["outputSchema"] = output_schema
    return MCPTool(
        SafetyGuard(tmp_path),
        client=client,  # type: ignore[arg-type]
        server_name="test",
        definition=definition,
        protocol_version=protocol_version,
    )


@pytest.mark.asyncio
async def test_modern_mcp_tool_binds_task_state_to_ash_call_identity(
    tmp_path: Path,
) -> None:
    persisted: list[dict[str, Any]] = []
    client = StubMCPClient({"content": [{"type": "text", "text": "done"}]})

    async def call_tool(
        name: str,
        arguments: dict,
        **kwargs: Any,
    ) -> dict:
        assert name == "durable"
        assert arguments == {}
        callback = kwargs.get("modern_task_state_callback")
        assert callback is not None
        await callback(
            {
                "taskId": "task-1",
                "status": "working",
                "createdAt": "2026-09-20T00:00:00Z",
                "lastUpdatedAt": "2026-09-20T00:00:01Z",
                "ttlMs": None,
            },
            {"approve": "fingerprint"},
        )
        return {"content": [{"type": "text", "text": "done"}]}

    client.call_tool = call_tool  # type: ignore[method-assign]

    async def persist(payload: dict[str, Any]) -> None:
        persisted.append(payload)

    tool = MCPTool(
        SafetyGuard(tmp_path),
        client=client,  # type: ignore[arg-type]
        server_name="durable-server",
        definition={
            "name": "durable",
            "description": "durable task",
            "inputSchema": {"type": "object"},
        },
        protocol_version="2026-07-28",
        task_state_handler=persist,
    )

    with tool.event_context({"call_id": "call-123"}):
        result = await tool.run()

    assert result.success is True
    assert len(persisted) == 1
    payload = persisted[0]
    assert payload["call_id"] == "call-123"
    assert payload["server_name"] == "durable-server"
    assert payload["remote_tool_name"] == "durable"
    assert payload["protocol_version"] == "2026-07-28"
    assert isinstance(payload["contract_fingerprint"], str)
    assert payload["task"]["taskId"] == "task-1"
    assert payload["answered_inputs"] == {"approve": "fingerprint"}


@pytest.mark.asyncio
async def test_mcp_tool_preserves_and_enforces_complete_input_schema(
    tmp_path: Path,
) -> None:
    client = StubMCPClient({"content": [{"type": "text", "text": "ok"}]})
    tool = _mcp_tool(tmp_path, client)
    exposed = tool.json_schema()

    assert exposed == COMPLEX_MCP_INPUT_SCHEMA
    exposed["properties"]["mode"]["enum"].append("mutated")
    assert tool.json_schema() == COMPLEX_MCP_INPUT_SCHEMA

    arguments = {
        "mode": "safe",
        "target": {"kind": "path", "path": "alpha"},
        "options": None,
        "tags": ["one", "two"],
        "limit": 2,
    }
    result = await tool.run(**arguments)

    assert result.success is True
    assert result.output == "ok"
    assert client.calls == [("complex", arguments)]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "overrides",
    [
        {"mode": "unknown"},
        {"target": {"kind": "path", "path": "INVALID"}},
        {"tags": ["same", "same"]},
        {"limit": "2"},
        {"extra": True},
    ],
)
async def test_mcp_tool_rejects_invalid_arguments_without_remote_call(
    tmp_path: Path,
    overrides: dict,
) -> None:
    client = StubMCPClient({"content": []})
    tool = _mcp_tool(tmp_path, client)
    arguments = {
        "mode": "safe",
        "target": {"kind": "id", "id": 4},
        "options": {"enabled": True},
        "tags": ["one"],
        "limit": 2,
        **overrides,
    }

    result = await tool.run(**arguments)

    assert result.success is False
    assert result.error is not None and "invalid MCP tool arguments" in result.error
    assert client.calls == []


def test_mcp_tool_rejects_invalid_and_unknown_schema_dialects(tmp_path: Path) -> None:
    client = StubMCPClient({"content": []})
    with pytest.raises(ValueError, match="not valid JSON Schema"):
        _mcp_tool(
            tmp_path,
            client,
            input_schema={"type": "object", "required": "not-an-array"},
        )
    with pytest.raises(ValueError, match="unsupported JSON Schema dialect"):
        _mcp_tool(
            tmp_path,
            client,
            input_schema={
                "$schema": "https://example.invalid/unknown-dialect",
                "type": "object",
            },
        )

    draft7 = _mcp_tool(
        tmp_path,
        client,
        input_schema={
            "$schema": "http://json-schema.org/draft-07/schema#",
            "type": "object",
            "properties": {"value": {"type": "string"}},
        },
    )
    assert draft7.json_schema()["$schema"].endswith("draft-07/schema#")


def test_mcp_tool_rejects_non_object_roots_and_remote_references(
    tmp_path: Path,
) -> None:
    client = StubMCPClient({"content": []})
    for schema in ({}, {"type": "string"}):
        with pytest.raises(ValueError, match="root type must be object"):
            MCPTool(
                SafetyGuard(tmp_path),
                client=client,  # type: ignore[arg-type]
                server_name="test",
                definition={"name": "invalid", "inputSchema": schema},
            )
    with pytest.raises(ValueError, match="non-local reference"):
        _mcp_tool(
            tmp_path,
            client,
            input_schema={
                "type": "object",
                "properties": {
                    "value": {"$ref": "http://169.254.169.254/latest/meta-data/"}
                },
            },
        )
    with pytest.raises(ValueError, match="root type must be object"):
        _mcp_tool(
            tmp_path,
            client,
            output_schema={"type": "array"},
        )


def test_mcp_tool_preserves_task_execution_support(tmp_path: Path) -> None:
    client = StubMCPClient({"content": []})
    required = MCPTool(
        SafetyGuard(tmp_path),
        client=client,  # type: ignore[arg-type]
        server_name="test",
        definition={
            "name": "task-required",
            "inputSchema": {"type": "object"},
            "execution": {"taskSupport": "required"},
        },
    )

    assert required.name == "mcp__test__task-required"
    assert required._task_support == "required"
    optional = MCPTool(
        SafetyGuard(tmp_path),
        client=client,  # type: ignore[arg-type]
        server_name="test",
        definition={
            "name": "ordinary-or-task",
            "inputSchema": {"type": "object"},
            "execution": {"taskSupport": "optional"},
        },
    )
    assert optional.name == "mcp__test__ordinary-or-task"
    default_forbidden = MCPTool(
        SafetyGuard(tmp_path),
        client=client,  # type: ignore[arg-type]
        server_name="test",
        definition={
            "name": "ordinary",
            "inputSchema": {"type": "object"},
            "execution": {},
        },
    )
    assert default_forbidden.name == "mcp__test__ordinary"


def test_mcp_tool_name_aliases_nonportable_remote_identity_stably(tmp_path: Path) -> None:
    remote_name = "folder/read file/" + "x" * 80
    alias = mcp_tool_name("example.server", remote_name)

    assert len(alias) <= 64
    assert PROVIDER_TOOL_NAME.fullmatch(alias)
    assert alias == mcp_tool_name("example.server", remote_name)
    assert alias != mcp_tool_name("example.server", remote_name + "2")
    assert mcp_tool_name("test", "ordinary") == "mcp__test__ordinary"

    client = StubMCPClient({"content": []})
    tool = MCPTool(
        SafetyGuard(tmp_path),
        client=client,  # type: ignore[arg-type]
        server_name="example.server",
        definition={"name": remote_name, "inputSchema": {"type": "object"}},
    )

    assert tool.name == alias
    assert tool.remote_name == remote_name


def test_provider_history_rewrites_unique_legacy_mcp_tool_name(tmp_path: Path) -> None:
    remote_name = "folder/read file"
    client = StubMCPClient({"content": []})
    tool = MCPTool(
        SafetyGuard(tmp_path),
        client=client,  # type: ignore[arg-type]
        server_name="example.server",
        definition={"name": remote_name, "inputSchema": {"type": "object"}},
    )
    loop = AshLoop(
        SessionStore(tmp_path / "sessions.db"),
        IdleProvider(),
        SafetyGuard(tmp_path),
        HeadlessUI(output_format="text", stream=io.StringIO()),
        tmp_path,
        tools={tool.name: tool},
    )
    legacy = f"mcp__example.server__{remote_name}"
    original = [
        {"call_id": "call-1", "name": legacy, "arguments": {"path": "README.md"}}
    ]

    rewritten = loop._provider_history_tool_calls(original)

    assert rewritten[0]["name"] == tool.name
    assert original[0]["name"] == legacy


@pytest.mark.asyncio
async def test_mcp_schema_regex_cannot_block_runtime_or_reach_server(
    tmp_path: Path,
) -> None:
    client = StubMCPClient({"content": []})
    tool = _mcp_tool(
        tmp_path,
        client,
        input_schema={
            "type": "object",
            "properties": {"value": {"type": "string", "pattern": "^(a+)+$"}},
            "required": ["value"],
        },
    )

    result = await asyncio.wait_for(tool.run(value="a" * 32 + "!"), timeout=3)

    assert result.success is False
    assert result.error is not None
    assert "deadline" in result.error or "resource limit" in result.error
    assert client.calls == []


@pytest.mark.asyncio
async def test_mcp_schema_worker_ignores_workspace_shadow_package(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    package = workspace / "ash" / "mcp"
    package.mkdir(parents=True)
    (workspace / "ash" / "__init__.py").write_text("", encoding="utf-8")
    (package / "__init__.py").write_text("", encoding="utf-8")
    marker = workspace / "shadow-executed.txt"
    (package / "schema_worker.py").write_text(
        "from pathlib import Path\n"
        f"Path({str(marker)!r}).write_text('executed', encoding='utf-8')\n"
        "print('{\"valid\": true}')\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(workspace)

    result = await _validate_schema_instance(
        {"type": "object", "properties": {"name": {"type": "string"}}},
        {"name": "ash"},
        default_dialect=CURRENT_SCHEMA_DIALECT,
    )

    assert result["valid"] is True
    assert not marker.exists()


@pytest.mark.asyncio
async def test_mcp_schema_worker_rejects_duplicate_response_fields(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Process:
        returncode = 0

    async def fake_spawn(*args, **kwargs):
        del args, kwargs
        return Process()

    async def fake_communicate(*args, **kwargs):
        del args, kwargs
        return b'{"valid":true,"valid":false}', b""

    monkeypatch.setattr(
        "ash.mcp.runtime.asyncio.create_subprocess_exec",
        fake_spawn,
    )
    monkeypatch.setattr("ash.mcp.runtime.communicate_process", fake_communicate)

    result = await _validate_schema_instance(
        {"type": "string"},
        "value",
        default_dialect=CURRENT_SCHEMA_DIALECT,
    )

    assert result == {
        "valid": False,
        "internal": True,
        "message": "isolated schema validator returned invalid JSON",
    }


def test_mcp_schema_worker_rejects_duplicate_request_fields(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from ash.mcp import schema_worker

    monkeypatch.setattr(schema_worker, "_apply_resource_limits", lambda: None)
    monkeypatch.setattr(
        sys,
        "stdin",
        io.TextIOWrapper(
            io.BytesIO(b'{"schema":{},"schema":{},"instance":null}'),
            encoding="utf-8",
        ),
    )

    assert schema_worker.main() == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["valid"] is False
    assert payload["internal"] is True
    assert "duplicate JSON object key" in payload["message"]


@pytest.mark.asyncio
async def test_mcp_legacy_protocol_uses_draft7_for_implicit_schema(
    tmp_path: Path,
) -> None:
    client = StubMCPClient({"content": [{"type": "text", "text": "ok"}]})
    tool = _mcp_tool(
        tmp_path,
        client,
        protocol_version="2025-03-26",
        input_schema={
            "type": "object",
            "properties": {
                "pair": {
                    "type": "array",
                    "items": [{"type": "string"}, {"type": "integer"}],
                    "additionalItems": False,
                }
            },
            "required": ["pair"],
        },
        output_schema={
            "type": "object",
            "required": ["ignored-for-legacy-protocol"],
        },
    )

    result = await tool.run(pair=["one", 2])

    assert result.success is True
    assert result.output == "ok"


@pytest.mark.asyncio
async def test_mcp_schema_allows_properties_named_like_schema_keywords(
    tmp_path: Path,
) -> None:
    client = StubMCPClient({"content": [{"type": "text", "text": "ok"}]})
    tool = _mcp_tool(
        tmp_path,
        client,
        input_schema={
            "type": "object",
            "properties": {
                "$ref": {"type": "string"},
                "patternProperties": {"type": "string"},
            },
            "required": ["$ref", "patternProperties"],
            "additionalProperties": False,
        },
    )

    result = await tool.run(**{"$ref": "literal", "patternProperties": "literal"})

    assert result.success is True


@pytest.mark.asyncio
async def test_mcp_tool_preserves_rich_result_and_validates_output_schema(
    tmp_path: Path,
) -> None:
    output_schema = {
        "type": "object",
        "properties": {"count": {"type": "integer", "minimum": 1}},
        "required": ["count"],
        "additionalProperties": False,
    }
    remote_result = {
        "content": [
            {"type": "text", "text": "two results"},
            {"type": "image", "mimeType": "image/png", "data": "AAAA"},
        ],
        "structuredContent": {"count": 2},
        "isError": False,
        "_meta": {"cacheKey": "stable"},
        "vendorExtension": {"trace": "abc"},
    }
    client = StubMCPClient(remote_result)
    tool = _mcp_tool(
        tmp_path,
        client,
        input_schema={"type": "object", "additionalProperties": False},
        output_schema=output_schema,
    )

    result = await tool.run()

    assert result.success is True
    assert json.loads(result.output) == remote_result


@pytest.mark.asyncio
async def test_mcp_tool_validates_raw_structured_content_before_output_redaction(
    tmp_path: Path,
) -> None:
    secret = "tiny-k"
    remote_result = {
        "content": [{"type": "text", "text": f"credential {secret}"}],
        "structuredContent": {"credential": secret},
    }

    class SanitizingClient(StubMCPClient):
        def redact_remote_output(self, value: Any) -> Any:
            if isinstance(value, str):
                return value.replace(secret, "[REDACTED]")
            if isinstance(value, dict):
                return {
                    key: self.redact_remote_output(item)
                    for key, item in value.items()
                }
            if isinstance(value, list):
                return [self.redact_remote_output(item) for item in value]
            return value

    tool = _mcp_tool(
        tmp_path,
        SanitizingClient(remote_result),
        input_schema={"type": "object"},
        output_schema={
            "type": "object",
            "properties": {"credential": {"const": secret}},
            "required": ["credential"],
        },
    )

    result = await tool.run()

    assert result.success is True
    assert secret not in result.output
    assert json.loads(result.output)["structuredContent"]["credential"] == "[REDACTED]"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("output_schema", "structured_content"),
    [
        ({"type": "array", "items": {"type": "integer"}}, [1, 2]),
        ({"type": "string"}, "value"),
        ({"type": "number"}, 1.5),
        ({"type": "boolean"}, True),
        ({"type": "null"}, None),
    ],
)
async def test_modern_mcp_tool_accepts_arbitrary_json_structured_content(
    tmp_path: Path,
    output_schema: dict,
    structured_content: object,
) -> None:
    remote_result = {
        "content": [{"type": "text", "text": "modern structured result"}],
        "structuredContent": structured_content,
    }
    tool = _mcp_tool(
        tmp_path,
        StubMCPClient(remote_result),
        input_schema={"type": "object"},
        output_schema=output_schema,
        protocol_version="2026-07-28",
    )

    result = await tool.run()

    assert result.success is True
    assert json.loads(result.output)["structuredContent"] == structured_content


@pytest.mark.asyncio
async def test_modern_mcp_tool_validates_non_object_structured_content_schema(
    tmp_path: Path,
) -> None:
    remote_result = {
        "content": [{"type": "text", "text": "invalid array"}],
        "structuredContent": [1, "two"],
    }
    tool = _mcp_tool(
        tmp_path,
        StubMCPClient(remote_result),
        input_schema={"type": "object"},
        output_schema={"type": "array", "items": {"type": "integer"}},
        protocol_version="2026-07-28",
    )

    result = await tool.run()

    assert result.success is False
    assert result.outcome is ToolExecutionOutcome.UNKNOWN
    assert result.error is not None and "invalid MCP structured result" in result.error
    assert json.loads(result.output)["structuredContent"] == [1, "two"]


def test_legacy_mcp_tool_still_requires_object_output_schema(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="outputSchema root type must be object"):
        _mcp_tool(
            tmp_path,
            StubMCPClient({"content": []}),
            input_schema={"type": "object"},
            output_schema={"type": "array", "items": {"type": "integer"}},
            protocol_version="2025-11-25",
        )


@pytest.mark.asyncio
async def test_mcp_tool_preserves_annotated_text_block_envelope(tmp_path: Path) -> None:
    remote_result = {
        "content": [
            {
                "type": "text",
                "text": "annotated",
                "annotations": {"audience": ["assistant"], "priority": 0.8},
                "_meta": {"trace": "one"},
                "vendor": "retained",
            }
        ]
    }
    tool = _mcp_tool(
        tmp_path,
        StubMCPClient(remote_result),
        input_schema={"type": "object"},
    )

    result = await tool.run()

    assert result.success is True
    assert json.loads(result.output)["content"] == remote_result["content"]


@pytest.mark.asyncio
async def test_mcp_tool_keeps_structured_only_and_application_error_envelopes(
    tmp_path: Path,
) -> None:
    structured_client = StubMCPClient(
        {"content": [], "structuredContent": {"items": [1, 2]}}
    )
    structured_tool = _mcp_tool(
        tmp_path,
        structured_client,
        input_schema={"type": "object"},
    )
    structured = await structured_tool.run()
    assert structured.outcome is ToolExecutionOutcome.COMPLETED
    assert json.loads(structured.output) == {
        "content": [],
        "structuredContent": {"items": [1, 2]},
        "isError": False,
    }

    error_client = StubMCPClient(
        {
            "content": [{"type": "text", "text": "retry with another date"}],
            "structuredContent": {"code": "invalid_date"},
            "isError": True,
            "_meta": {"request": "one"},
        }
    )
    error_tool = _mcp_tool(
        tmp_path,
        error_client,
        input_schema={"type": "object"},
    )
    failed = await error_tool.run()
    assert failed.success is False
    assert failed.outcome is ToolExecutionOutcome.COMPLETED
    assert failed.error == "retry with another date"
    assert json.loads(failed.output) == {
        "content": [{"type": "text", "text": "retry with another date"}],
        "structuredContent": {"code": "invalid_date"},
        "isError": True,
        "_meta": {"request": "one"},
    }


@pytest.mark.asyncio
async def test_mcp_application_error_output_is_redacted_and_bounded(
    tmp_path: Path,
) -> None:
    marker = "synthetic application error marker"
    remote_result = {
        "content": [
            {
                "type": "text",
                "text": f'password="{marker}" ' + "x" * 5000,
            }
        ],
        "isError": True,
    }
    tool = _mcp_tool(
        tmp_path,
        StubMCPClient(remote_result),
        input_schema={"type": "object"},
    )

    result = await tool.run()

    assert result.success is False
    assert marker not in result.output
    assert marker not in (result.error or "")
    assert len(result.output) <= 512
    assert len(result.error or "") <= 512


@pytest.mark.asyncio
async def test_mcp_capability_tool_redacts_and_bounds_direct_errors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    marker = "synthetic capability error marker"
    runtime = MCPRuntime({}, SafetyGuard(tmp_path))

    async def fail_list_capability(method: str, *, server: str | None = None):
        del method, server
        raise ValueError(f'password="{marker}" ' + "x" * 5000)

    monkeypatch.setattr(runtime, "list_capability", fail_list_capability)
    tool = MCPListResourcesTool(SafetyGuard(tmp_path), runtime)

    result = await tool.run()

    assert result.success is False
    assert marker not in (result.error or "")
    assert len(result.error or "") <= 512


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("remote_result", "message"),
    [
        ({"structuredContent": {"value": 1}}, "content is required"),
        ({"content": None}, "content must be an array"),
        ({"content": [], "structuredContent": None}, "must be an object"),
        ({"content": [], "isError": None}, "isError must be a boolean"),
        ({"content": [], "_meta": None}, "_meta must be an object"),
    ],
)
async def test_mcp_tool_rejects_malformed_results_without_losing_wire_payload(
    tmp_path: Path,
    remote_result: dict,
    message: str,
) -> None:
    tool = _mcp_tool(
        tmp_path,
        StubMCPClient(remote_result),
        input_schema={"type": "object"},
    )

    result = await tool.run()

    assert result.success is False
    assert result.outcome is ToolExecutionOutcome.UNKNOWN
    assert result.error is not None and message in result.error
    assert json.loads(result.output) == remote_result


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "content",
    [
        {"type": "text", "text": 7},
        {"type": "image", "data": "not base64", "mimeType": "image/png"},
        {"type": "resource", "resource": {"text": "missing uri"}},
        {"type": "unknown", "value": "extension"},
    ],
)
async def test_mcp_tool_rejects_malformed_content_blocks(
    tmp_path: Path,
    content: dict,
) -> None:
    remote_result = {"content": [content]}
    tool = _mcp_tool(
        tmp_path,
        StubMCPClient(remote_result),
        input_schema={"type": "object"},
    )

    result = await tool.run()

    assert result.success is False
    assert result.outcome is ToolExecutionOutcome.UNKNOWN
    assert result.error is not None and "content[0]" in result.error
    assert json.loads(result.output) == remote_result


@pytest.mark.asyncio
async def test_mcp_tool_preserves_invalid_structured_output_for_recovery(
    tmp_path: Path,
) -> None:
    remote_result = {
        "content": [{"type": "text", "text": "server summary"}],
        "structuredContent": {"count": "two"},
    }
    tool = _mcp_tool(
        tmp_path,
        StubMCPClient(remote_result),
        input_schema={"type": "object"},
        output_schema={
            "type": "object",
            "properties": {"count": {"type": "integer"}},
            "required": ["count"],
        },
    )

    result = await tool.run()

    assert result.success is False
    assert result.outcome is ToolExecutionOutcome.UNKNOWN
    assert result.error is not None and "invalid MCP structured result" in result.error
    assert json.loads(result.output)["structuredContent"] == {"count": "two"}


@pytest.mark.asyncio
async def test_mcp_tool_requires_structured_content_for_declared_output_schema(
    tmp_path: Path,
) -> None:
    tool = _mcp_tool(
        tmp_path,
        StubMCPClient({"content": [{"type": "text", "text": "summary"}]}),
        input_schema={"type": "object"},
        output_schema={"type": "object"},
    )

    result = await tool.run()

    assert result.success is False
    assert result.outcome is ToolExecutionOutcome.UNKNOWN
    assert result.output == "summary"
    assert result.error is not None and "requires structuredContent" in result.error


@pytest.mark.asyncio
async def test_mcp_tool_rejects_non_json_wire_values(tmp_path: Path) -> None:
    tool = _mcp_tool(
        tmp_path,
        StubMCPClient({"content": [], "structuredContent": {"value": float("nan")}}),
        input_schema={"type": "object"},
    )

    result = await tool.run()

    assert result.success is False
    assert result.outcome is ToolExecutionOutcome.UNKNOWN
    assert result.output == ""
    assert result.error is not None and "not JSON-serializable" in result.error


@pytest.mark.asyncio
async def test_mcp_tool_preserves_protocol_error_data_without_replay(
    tmp_path: Path,
) -> None:
    class ErrorClient:
        def __init__(self) -> None:
            self.calls = 0

        async def call_tool(
            self,
            name: str,
            arguments: dict,
            *,
            expected_contract: str | None = None,
            as_task: bool = False,
            header_annotations: list | None = None,
        ) -> dict:
            del expected_contract
            del as_task
            self.calls += 1
            raise MCPProtocolError(
                "tools/call failed (-32602): invalid mode",
                code=-32602,
                data={"field": "mode", "expected": ["safe", "fast"]},
            )

    client = ErrorClient()
    tool = MCPTool(
        SafetyGuard(tmp_path),
        client=client,  # type: ignore[arg-type]
        server_name="test",
        definition={"name": "fails", "inputSchema": {"type": "object"}},
    )

    result = await tool.run()

    assert result.success is False
    assert client.calls == 1
    assert result.outcome == "unknown"
    assert json.loads(result.output) == {
        "error": {
            "type": "mcp_protocol_error",
            "message": "tools/call failed (-32602): invalid mode",
            "code": -32602,
            "data": {"field": "mode", "expected": ["safe", "fast"]},
        }
    }


@pytest.mark.asyncio
async def test_mcp_tool_redacts_secret_values_in_protocol_errors(
    tmp_path: Path,
) -> None:
    marker = "synthetic protocol marker"

    class ErrorClient:
        async def call_tool(
            self,
            name: str,
            arguments: dict,
            *,
            expected_contract: str | None = None,
            as_task: bool = False,
            header_annotations: list | None = None,
        ) -> dict:
            del name, arguments, expected_contract, as_task, header_annotations
            raise MCPProtocolError(
                f'upstream payload={{\\"password\\": \\"{marker}\\"}} '
                f'password="line one\n{marker}"',
                code=-32602,
                data={"password": marker, "detail": "not secret"},
            )

    tool = MCPTool(
        SafetyGuard(tmp_path),
        client=ErrorClient(),  # type: ignore[arg-type]
        server_name="test",
        definition={"name": "fails", "inputSchema": {"type": "object"}},
    )

    result = await tool.run()
    await SecretRedactionMiddleware().after_tool("mcp__test__fails", {}, result)

    assert marker not in result.output
    assert marker not in (result.error or "")
    payload = json.loads(result.output)
    assert payload["error"]["data"] == {
        "password": "[REDACTED]",
        "detail": "not secret",
    }


@pytest.mark.asyncio
async def test_mcp_tool_preserves_explicit_null_protocol_error_data(
    tmp_path: Path,
) -> None:
    class ErrorClient:
        async def call_tool(
            self,
            name: str,
            arguments: dict,
            *,
            expected_contract: str | None = None,
            as_task: bool = False,
            header_annotations: list | None = None,
        ) -> dict:
            del expected_contract
            del as_task
            raise MCPProtocolError("explicit null", code=-32000, data=None)

    tool = MCPTool(
        SafetyGuard(tmp_path),
        client=ErrorClient(),  # type: ignore[arg-type]
        server_name="test",
        definition={"name": "fails", "inputSchema": {"type": "object"}},
    )

    result = await tool.run()

    assert json.loads(result.output)["error"]["data"] is None


@pytest.mark.asyncio
async def test_mcp_output_schema_applies_to_application_errors(tmp_path: Path) -> None:
    remote_result = {
        "content": [{"type": "text", "text": "failed"}],
        "structuredContent": {"code": 7},
        "isError": True,
    }
    tool = _mcp_tool(
        tmp_path,
        StubMCPClient(remote_result),
        input_schema={"type": "object"},
        output_schema={
            "type": "object",
            "properties": {"code": {"type": "string"}},
            "required": ["code"],
        },
    )

    result = await tool.run()

    assert result.success is False
    assert result.error is not None and "invalid MCP structured result" in result.error
    assert json.loads(result.output) == remote_result


@pytest.mark.asyncio
async def test_mcp_client_retains_jsonrpc_error_code_and_data() -> None:
    client = MCPClient(MCPServerConfig(name="fake", command="fake", args=[], env={}))
    client._request_stdio = AsyncMock(
        return_value={
            "jsonrpc": "2.0",
            "id": 1,
            "error": {
                "code": -32602,
                "message": "invalid arguments",
                "data": {"field": "query"},
            },
        }
    )

    with pytest.raises(MCPProtocolError) as caught:
        await client.request("tools/call", {"name": "search", "arguments": {}})

    assert caught.value.code == -32602
    assert caught.value.data == {"field": "query"}


@pytest.mark.asyncio
async def test_mcp_client_rejects_boolean_error_code_and_distinguishes_data() -> None:
    client = MCPClient(MCPServerConfig(name="fake", command="fake", args=[], env={}))
    client._request_stdio = AsyncMock(
        return_value={
            "jsonrpc": "2.0",
            "id": 1,
            "error": {"code": True, "message": "invalid", "data": None},
        }
    )

    with pytest.raises(MCPProtocolError) as caught:
        await client.request("tools/call")

    assert caught.value.code is None
    assert "invalid error code" in str(caught.value)
    assert caught.value.has_data is True
    assert caught.value.data is None


def _task_client(
    responses: list[dict | Exception],
    *,
    timeout: float = 30.0,
) -> tuple[MCPClient, AsyncMock]:
    client = MCPClient(
        MCPServerConfig(name="fake", command="fake", args=[], env={}),
        timeout=timeout,
    )
    client.protocol_version = "2025-11-25"
    client.server_capabilities = {
        "tasks": {
            "cancel": {},
            "list": {},
            "requests": {"tools": {"call": {}}},
        }
    }
    request = AsyncMock(side_effect=responses)
    client.request = request  # type: ignore[method-assign]
    return client, request


@pytest.mark.asyncio
async def test_mcp_required_task_tool_polls_and_fetches_result() -> None:
    client, request = _task_client(
        [
            {
                "task": {
                    "taskId": "one",
                    "status": "working",
                    "ttl": None,
                    "pollInterval": 0,
                }
            },
            {"task": {"taskId": "one", "status": "completed", "ttl": None}},
            {"content": [{"type": "text", "text": "done"}]},
        ]
    )
    result = await client.call_tool("long", {}, as_task=True)

    assert result["content"][0]["text"] == "done"
    assert [call.args[0] for call in request.await_args_list] == [
        "tools/call",
        "tasks/get",
        "tasks/result",
    ]
    assert request.await_args_list[0].args[1]["task"] == {}
    assert request.await_args_list[1].args[1] == {"taskId": "one"}


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["failed", "cancelled"])
async def test_mcp_task_terminal_failure_is_not_fetched(status: str) -> None:
    client, request = _task_client(
        [
            {
                "task": {
                    "taskId": "bad",
                    "status": status,
                    "ttl": None,
                    "statusMessage": "no",
                }
            },
        ]
    )

    with pytest.raises(MCPProtocolError, match=f"MCP tool task {status}: no"):
        await client.call_tool("long", {}, as_task=True)

    assert request.await_count == 1


@pytest.mark.asyncio
async def test_mcp_task_status_notification_wakes_without_polling() -> None:
    client, request = _task_client(
        [
            {
                "task": {
                    "taskId": "fast",
                    "status": "working",
                    "ttl": None,
                    "pollInterval": 100000,
                }
            },
            {"content": [{"type": "text", "text": "notified"}]},
        ]
    )

    call = asyncio.create_task(client.call_tool("long", {}, as_task=True))
    await asyncio.sleep(0.01)
    await client._handle_incoming(
        {
            "jsonrpc": "2.0",
            "method": "notifications/tasks/status",
            "params": {
                "taskId": "fast",
                "status": "completed",
                "createdAt": "2025-11-25T10:30:00Z",
                "lastUpdatedAt": "2025-11-25T10:31:00Z",
                "ttl": None,
            },
        }
    )

    result = await asyncio.wait_for(call, 1)

    assert result["content"][0]["text"] == "notified"
    assert [sent.args[0] for sent in request.await_args_list] == [
        "tools/call",
        "tasks/result",
    ]


@pytest.mark.asyncio
async def test_mcp_task_invalid_notifications_are_ignored_and_fallback_polls() -> None:
    client, request = _task_client(
        [
            {
                "task": {
                    "taskId": "safe",
                    "status": "working",
                    "ttl": None,
                    "pollInterval": 10,
                }
            },
            {
                "task": {
                    "taskId": "safe",
                    "status": "completed",
                    "ttl": None,
                }
            },
            {"content": [{"type": "text", "text": "polled"}]},
        ]
    )

    call = asyncio.create_task(client.call_tool("long", {}, as_task=True))
    await asyncio.sleep(0.01)
    await client._handle_incoming(
        {
            "jsonrpc": "2.0",
            "method": "notifications/tasks/status",
            "params": {"taskId": "other", "status": "completed", "ttl": None},
        }
    )
    await client._handle_incoming(
        {
            "jsonrpc": "2.0",
            "method": "notifications/tasks/status",
            "params": {"taskId": "safe", "status": "exploded", "ttl": None},
        }
    )

    result = await asyncio.wait_for(call, 1)

    assert result["content"][0]["text"] == "polled"
    assert [sent.args[0] for sent in request.await_args_list] == [
        "tools/call",
        "tasks/get",
        "tasks/result",
    ]


@pytest.mark.asyncio
async def test_mcp_task_status_notification_can_update_before_terminal() -> None:
    client, request = _task_client(
        [
            {
                "task": {
                    "taskId": "ordered",
                    "status": "input_required",
                    "ttl": None,
                    "pollInterval": 100000,
                    "statusMessage": "need input",
                }
            },
            {
                "task": {
                    "taskId": "ordered",
                    "status": "completed",
                    "ttl": None,
                }
            },
            {"content": [{"type": "text", "text": "resumed"}]},
        ]
    )

    call = asyncio.create_task(client.call_tool("long", {}, as_task=True))
    await asyncio.sleep(0.01)
    await client._handle_incoming(
        {
            "jsonrpc": "2.0",
            "method": "notifications/tasks/status",
            "params": {
                "taskId": "ordered",
                "status": "working",
                "createdAt": "2025-11-25T10:30:00Z",
                "lastUpdatedAt": "2025-11-25T10:31:00Z",
                "ttl": None,
                "pollInterval": 10,
            },
        }
    )

    result = await asyncio.wait_for(call, 1)

    assert result["content"][0]["text"] == "resumed"
    assert [sent.args[0] for sent in request.await_args_list] == [
        "tools/call",
        "tasks/get",
        "tasks/result",
    ]

    assert request.await_args_list[1].args[1] == {"taskId": "ordered"}


@pytest.mark.asyncio
async def test_mcp_input_required_opens_result_then_resumes_polling() -> None:
    client, request = _task_client([])
    states = iter(["working", "input_required", "working", "completed"])
    result_calls = 0

    def task_state():
        status = next(states)
        return {
            "taskId": "input",
            "status": status,
            "createdAt": "2025-11-25T10:00:00Z",
            "lastUpdatedAt": "2025-11-25T10:01:00Z",
            "ttl": None,
            "pollInterval": 0,
        }

    async def respond(method, params, **_):
        nonlocal result_calls
        del params
        if method == "tools/call":
            return {"task": task_state()}
        if method == "tasks/get":
            return {"task": task_state()}
        if method == "tasks/result":
            result_calls += 1
            if result_calls < 2:
                return {"value": {}}
            return {"content": [{"type": "text", "text": "answered"}]}
        raise AssertionError(f"unexpected MCP method {method}")

    request.side_effect = respond

    result = await client.call_tool("long", {}, as_task=True)

    assert result["content"][0]["text"] == "answered"
    assert [sent.args[0] for sent in request.await_args_list] == [
        "tools/call",
        "tasks/get",
        "tasks/result",
        "tasks/get",
        "tasks/get",
        "tasks/result",
    ]
    assert request.await_args_list[3].args[1] == {"taskId": "input"}


@pytest.mark.asyncio
async def test_mcp_task_timeout_cancels_remote_task() -> None:
    client, request = _task_client([], timeout=0.01)
    responses = iter(
        [
            {
                "task": {
                    "taskId": "slow",
                    "status": "working",
                    "ttl": None,
                    "pollInterval": 0,
                }
            },
        ]
    )

    async def request_side_effect(method, params, **_):
        del params
        if method == "tools/call":
            return next(responses)
        return {"task": {"taskId": "slow", "status": "working", "ttl": None}}

    request.side_effect = request_side_effect
    client._task_poll_delay = lambda task: 1.0  # type: ignore[method-assign]

    with pytest.raises(MCPTaskTimeout):
        await client.call_tool("slow", {}, as_task=True)

    assert request.await_args_list[-1].args[:2] == ("tasks/cancel", {"taskId": "slow"})


@pytest.mark.asyncio
async def test_mcp_list_tasks_paginates_and_validates_states() -> None:
    client, request = _task_client([])
    responses = [
        {
            "tasks": [
                {
                    "taskId": "working",
                    "status": "working",
                    "createdAt": "2025-11-25T10:00:00Z",
                    "lastUpdatedAt": "2025-11-25T10:01:00Z",
                    "ttl": None,
                }
            ],
            "nextCursor": "page-two",
        },
        {"tasks": []},
    ]

    async def request_side_effect(method, params, **_):
        del params
        return responses.pop(0)

    request.side_effect = request_side_effect
    tasks = await client.list_mcp_tasks()

    assert [task["taskId"] for task in tasks] == ["working"]
    assert [sent.args[0] for sent in request.await_args_list] == [
        "tasks/list",
        "tasks/list",
    ]
    assert request.await_args_list[1].args[1] == {"cursor": "page-two"}


@pytest.mark.asyncio
async def test_mcp_list_tasks_rejects_oversized_aggregate_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, request = _task_client([])
    task = {
        "taskId": "working",
        "status": "working",
        "createdAt": "2025-11-25T10:00:00Z",
        "lastUpdatedAt": "2025-11-25T10:01:00Z",
        "ttl": None,
    }
    request.side_effect = [
        {"tasks": [task], "nextCursor": "page-two"},
        {"tasks": [{**task, "taskId": "second"}]},
    ]
    monkeypatch.setattr(mcp_client_module, "MAX_PAGINATION_RESULT_BYTES", 200)

    with pytest.raises(MCPProtocolError, match="aggregate result exceeds"):
        await client.list_mcp_tasks()


@pytest.mark.asyncio
async def test_mcp_list_tasks_requires_capability_and_fails_closed() -> None:
    client, request = _task_client([])
    request.side_effect = AssertionError("must not call unadvertised method")
    del client.server_capabilities["tasks"]["list"]

    with pytest.raises(MCPProtocolError, match="tasks/list"):
        await client.list_mcp_tasks()

    request.assert_not_awaited()


@pytest.mark.asyncio
async def test_mcp_cancel_task_validates_response_and_capability() -> None:
    client, request = _task_client(
        [
            {"task": {}},
            {"task": {}},
        ]
    )
    cancelled = {
        "taskId": "stop",
        "status": "cancelled",
        "createdAt": "2025-11-25T10:00:00Z",
        "lastUpdatedAt": "2025-11-25T10:01:00Z",
        "ttl": None,
        "statusMessage": "stopped by user",
    }
    request.side_effect = [
        {"task": cancelled},
        {"task": {**cancelled, "taskId": "different"}},
    ]
    task = await client.cancel_mcp_task("stop")

    assert task == cancelled
    assert request.await_args.args == ("tasks/cancel", {"taskId": "stop"})

    with pytest.raises(MCPProtocolError, match="another taskId"):
        await client.cancel_mcp_task("stop")

    del client.server_capabilities["tasks"]["cancel"]
    request.reset_mock()
    with pytest.raises(MCPProtocolError, match="tasks/cancel"):
        await client.cancel_mcp_task("stop")
    request.assert_not_awaited()


@pytest.mark.asyncio
async def test_runtime_cancel_task_binds_server_and_records_errors(
    tmp_path: Path,
) -> None:
    class RuntimeClient:
        def __init__(self) -> None:
            self.calls = []

        async def cancel_mcp_task(self, task_id):
            self.calls.append(task_id)
            return {"taskId": task_id, "status": "cancelled"}

    config = MCPServerConfig(name="unused", command="unused", args=[], env={})
    runtime = MCPRuntime({"fake": config}, SafetyGuard(tmp_path))
    client = RuntimeClient()
    runtime.clients["fake"] = client

    task = await runtime.cancel_task("fake", "stop")

    assert task == {"server": "fake", "taskId": "stop", "status": "cancelled"}
    assert client.calls == ["stop"]
    assert not runtime.errors

    with pytest.raises(ValueError, match="unknown MCP server"):
        await runtime.cancel_task("missing", "stop")


@pytest.mark.asyncio
async def test_mcp_task_cancellation_sends_tasks_cancel() -> None:
    client, request = _task_client(
        [
            {
                "task": {
                    "taskId": "stop",
                    "status": "working",
                    "ttl": None,
                    "pollInterval": 100000,
                }
            },
        ]
    )

    async def cancel_side_effect(method, params, **_):
        if method != "tools/call":
            return {"task": {"taskId": "stop", "status": "cancelled", "ttl": None}}
        return {
            "task": {
                "taskId": "stop",
                "status": "working",
                "ttl": None,
                "pollInterval": 100000,
            }
        }

    request.side_effect = cancel_side_effect
    call = asyncio.create_task(client.call_tool("stop", {}, as_task=True))
    await asyncio.sleep(0)
    call.cancel()
    with pytest.raises(asyncio.CancelledError):
        await call

    await asyncio.sleep(0)
    assert request.await_args_list[-1].args[:2] == ("tasks/cancel", {"taskId": "stop"})


@pytest.mark.asyncio
async def test_mcp_request_cancellation_owns_one_notification_until_settled() -> None:
    client = MCPClient(MCPServerConfig(name="fake", command="fake", args=[], env={}))
    request_started = asyncio.Event()
    cancel_started = asyncio.Event()
    cancel_release = asyncio.Event()
    cancel_finished = asyncio.Event()
    cancel_calls = 0

    async def blocked_request(*args: object, **kwargs: object) -> dict[str, object]:
        del args, kwargs
        request_started.set()
        await asyncio.Event().wait()
        return {}

    async def blocked_cancel(request_id: int, reason: str) -> None:
        nonlocal cancel_calls
        assert request_id == 1
        assert reason == "tools/call was cancelled"
        cancel_calls += 1
        cancel_started.set()
        await cancel_release.wait()
        cancel_finished.set()

    client._request_stdio = blocked_request  # type: ignore[method-assign]
    client._cancel_request = blocked_cancel  # type: ignore[method-assign]
    request_task = asyncio.create_task(client.request("tools/call"))
    await request_started.wait()

    request_task.cancel()
    await cancel_started.wait()
    request_task.cancel()
    await asyncio.sleep(0)
    assert not request_task.done()
    assert cancel_calls == 1
    assert not cancel_finished.is_set()

    cancel_release.set()
    with pytest.raises(asyncio.CancelledError):
        await request_task
    assert cancel_finished.is_set()
    assert not any(
        task.get_name().startswith("ash-mcp-cancel-request-")
        and not task.done()
        for task in asyncio.all_tasks()
    )


@pytest.mark.asyncio
async def test_mcp_task_cancellation_owns_one_cancel_request_until_settled() -> None:
    client, request = _task_client(
        [
            {
                "task": {
                    "taskId": "stop-once",
                    "status": "working",
                    "ttl": None,
                    "pollInterval": 100000,
                }
            }
        ]
    )
    cancel_started = asyncio.Event()
    cancel_release = asyncio.Event()
    cancel_finished = asyncio.Event()
    cancel_calls = 0

    async def blocked_cancel(task_id: str) -> None:
        nonlocal cancel_calls
        assert task_id == "stop-once"
        cancel_calls += 1
        cancel_started.set()
        await cancel_release.wait()
        cancel_finished.set()

    client._cancel_mcp_task = blocked_cancel  # type: ignore[method-assign]
    call = asyncio.create_task(client.call_tool("slow", {}, as_task=True))
    await asyncio.sleep(0)
    call.cancel()
    await cancel_started.wait()
    call.cancel()
    await asyncio.sleep(0)
    assert not call.done()
    assert cancel_calls == 1
    assert not cancel_finished.is_set()

    cancel_release.set()
    with pytest.raises(asyncio.CancelledError):
        await call
    assert cancel_finished.is_set()
    assert request.await_count == 1


@pytest.mark.asyncio
async def test_mcp_runtime_isolates_invalid_tool_schema(
    tmp_path: Path, monkeypatch
) -> None:
    class CatalogClient:
        server_capabilities = {"tools": {}}

        def __init__(self, config, *, roots=()) -> None:
            self.config = config

        async def connect(self) -> None:
            return None

        def supports_server_capability(self, name: str) -> bool:
            return name == "tools"

        async def list_tools(self) -> list[dict]:
            return [
                {
                    "name": "broken",
                    "inputSchema": {"type": "object", "required": "invalid"},
                },
                {
                    "name": "task-only",
                    "inputSchema": {"type": "object"},
                    "execution": {"taskSupport": "required"},
                },
                {"name": "healthy", "inputSchema": {"type": "object"}},
            ]

        async def disconnect(self) -> None:
            return None

    monkeypatch.setattr("ash.mcp.runtime.MCPClient", CatalogClient)
    config = MCPServerConfig(name="catalog", command="unused", args=[], env={})
    runtime = MCPRuntime({"catalog": config}, SafetyGuard(tmp_path))

    tools = await runtime.start()
    try:
        assert "mcp__catalog__healthy" in tools
        assert "mcp__catalog__broken" not in tools
        assert "mcp__catalog__task-only" in tools
        assert "not valid JSON Schema" in runtime.errors["catalog:tool:broken"]
        assert "catalog:tool:task-only" not in runtime.errors
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_async_client_initializes_lists_and_calls_tools() -> None:
    config = MCPServerConfig(
        name="fake",
        command=sys.executable,
        args=["-u", "-c", FAKE_MCP_SERVER],
        env={},
    )
    client = MCPClient(config)
    await asyncio.wait_for(client.connect(), timeout=1)
    try:
        assert client.protocol_version == "2025-06-18"
        assert client.server_info == {"name": "fake", "version": "1"}
        assert client.supports_server_capability("tools") is True
        tools = await client.list_tools()
        assert tools[0]["name"] == "echo"
        result = await client.call_tool("echo", {"text": "hello"})
        assert result["content"][0]["text"] == "hello"
    finally:
        await client.disconnect()


@pytest.mark.skipif(os.name == "nt", reason="POSIX cwd identity regression")
@pytest.mark.asyncio
async def test_stdio_client_refuses_cwd_replaced_after_config_creation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    saved = tmp_path / "workspace-saved"
    workspace.mkdir()
    config = MCPServerConfig(
        name="config-cwd-race",
        command=sys.executable,
        args=["-c", "pass"],
        env={},
        cwd=str(workspace),
    )
    workspace.rename(saved)
    workspace.mkdir()
    create = AsyncMock(side_effect=AssertionError("MCP stdio must not launch"))
    monkeypatch.setattr("ash.mcp.client.asyncio.create_subprocess_exec", create)
    client = MCPClient(config)

    with pytest.raises(MCPProtocolError, match="working directory identity changed"):
        await client.connect()

    create.assert_not_awaited()


@pytest.mark.skipif(os.name == "nt", reason="POSIX cwd race regression")
@pytest.mark.asyncio
async def test_stdio_client_cwd_swap_cannot_escape_workspace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ash.sandbox import process_utils as process_utils_module

    workspace = tmp_path / "workspace"
    outside = tmp_path / "outside"
    saved = tmp_path / "workspace-saved"
    workspace.mkdir()
    outside.mkdir()
    cwd_log = tmp_path / "client-cwd.txt"
    server = (
        "import os\n"
        "from pathlib import Path\n"
        f"Path({str(cwd_log)!r}).write_text(os.getcwd(), encoding='utf-8')\n"
        + FAKE_MCP_SERVER
    )
    config = MCPServerConfig(
        name="cwd-race",
        command=sys.executable,
        args=["-u", "-c", server],
        env={},
        cwd=str(workspace),
    )
    real_prepare = process_utils_module.prepare_process_tree
    swapped = False

    def prepare_then_swap(*args, **kwargs):
        nonlocal swapped
        plan = real_prepare(*args, **kwargs)
        if not swapped:
            swapped = True
            workspace.rename(saved)
            try:
                workspace.symlink_to(outside, target_is_directory=True)
            except OSError as exc:
                pytest.skip(f"symlink creation is unavailable: {exc}")
        return plan

    monkeypatch.setattr("ash.mcp.client.prepare_process_tree", prepare_then_swap)
    client = MCPClient(config)
    try:
        await asyncio.wait_for(client.connect(), timeout=1)
    finally:
        await client.disconnect()

    assert swapped is True
    assert Path(cwd_log.read_text(encoding="utf-8")).resolve() == saved.resolve()


@pytest.mark.asyncio
async def test_stdio_client_fails_closed_when_stable_cwd_is_unavailable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ash.sandbox.process_utils import ProcessTreeUnavailable

    config = MCPServerConfig(
        name="stable-cwd",
        command=sys.executable,
        args=["-c", "pass"],
        env={},
        cwd=str(tmp_path),
    )

    def unavailable(*args, **kwargs):
        raise ProcessTreeUnavailable("stable cwd unavailable")

    create = AsyncMock(side_effect=AssertionError("MCP stdio must not launch"))
    monkeypatch.setattr("ash.mcp.client.prepare_scoped_process_launch", unavailable)
    monkeypatch.setattr("ash.mcp.client.asyncio.create_subprocess_exec", create)
    client = MCPClient(config)

    with pytest.raises(MCPProtocolError, match="stable cwd unavailable"):
        await client.connect()

    create.assert_not_awaited()


@pytest.mark.asyncio
async def test_runtime_list_tasks_aggregates_and_records_errors(
    tmp_path: Path,
) -> None:
    class RuntimeClient:
        def __init__(self, *, fail: bool = False) -> None:
            self.fail = fail

        async def list_mcp_tasks(self) -> list[dict[str, str]]:
            if self.fail:
                raise RuntimeError("unavailable")
            return [
                {
                    "taskId": "one",
                    "status": "working",
                    "statusMessage": "running",
                },
                {"taskId": "two", "status": "completed"},
            ]

    config = MCPServerConfig(name="unused", command="unused", args=[], env={})
    runtime = MCPRuntime(
        {"healthy": config, "broken": config},
        SafetyGuard(tmp_path),
    )
    runtime.clients = {
        "healthy": RuntimeClient(),
        "broken": RuntimeClient(fail=True),
    }

    tasks = await runtime.list_tasks()

    assert [(task["server"], task["taskId"]) for task in tasks] == [
        ("healthy", "one"),
        ("healthy", "two"),
    ]
    assert runtime.errors["broken:list_mcp_tasks"] == "unavailable"


@pytest.mark.asyncio
async def test_stdio_client_accepts_bounded_rich_results_above_64_kib() -> None:
    config = MCPServerConfig(
        name="fake",
        command=sys.executable,
        args=["-u", "-c", FAKE_MCP_SERVER],
        env={},
    )
    client = MCPClient(config)
    await asyncio.wait_for(client.connect(), timeout=1)
    try:
        text = "x" * 70_000
        result = await client.call_tool("echo", {"text": text})
        assert result["content"] == [{"type": "text", "text": text}]
    finally:
        await client.disconnect()


@pytest.mark.asyncio
async def test_async_client_scrubs_host_secrets_and_keeps_explicit_server_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("UNRELATED_SECRET", "must-not-leak")
    config = MCPServerConfig(
        name="fake",
        command=sys.executable,
        args=["-u", "-c", FAKE_MCP_SERVER],
        env={"SERVER_TOKEN": "explicit-value"},
    )
    client = MCPClient(config)
    await client.connect()
    try:
        result = await client.call_tool("echo", {"text": "__environment__"})
        assert result["content"][0]["text"] == "explicit-value|missing"
    finally:
        await client.disconnect()


@pytest.mark.asyncio
async def test_runtime_registers_namespaced_tool(tmp_path: Path) -> None:
    config = MCPServerConfig(
        name="fake",
        command=sys.executable,
        args=["-u", "-c", FAKE_MCP_SERVER],
        env={},
    )
    runtime = MCPRuntime({"fake": config}, SafetyGuard(tmp_path))
    tools = await runtime.start()
    try:
        tool = tools["mcp__fake__echo"]
        result = await tool.run(text="hello")
        assert result.success is True
        assert result.output == "hello"
        assert (await runtime.list_resources())[0]["uri"] == "file:///example"
        assert (await runtime.list_prompts())[0]["name"] == "review"
        listed_resources = await tools["mcp_list_resources"].run(server="fake")
        listed_templates = await tools["mcp_list_resource_templates"].run()
        listed_prompts = await tools["mcp_list_prompts"].run()
        assert "file:///example" in listed_resources.output
        assert "file:///{path}" in listed_templates.output
        assert "review" in listed_prompts.output
        resource = await tools["mcp_read_resource"].run(
            server="fake", uri="file:///example"
        )
        assert "resource text" in resource.output
        prompt = await tools["mcp_get_prompt"].run(server="fake", name="review")
        assert "review it" in prompt.output
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_runtime_applies_plugin_mcp_working_directory_and_environment(
    tmp_path: Path,
) -> None:
    plugin = tmp_path / "plugin"
    plugin.mkdir()
    server = FAKE_MCP_SERVER.replace(
        'message["params"]["arguments"]["text"]',
        "__import__('os').getcwd() + '|' + __import__('os').environ['ASH_PLUGIN_ROOT']",
    )
    config = MCPServerConfig(
        name="example__fake",
        command=sys.executable,
        args=["-u", "-c", server],
        env={"ASH_PLUGIN_ROOT": str(plugin)},
        cwd=str(plugin),
    )
    runtime = MCPRuntime({"example__fake": config}, SafetyGuard(tmp_path))
    tools = await runtime.start()
    try:
        result = await tools["mcp__example__fake__echo"].run(text="ignored")
        assert result.output == f"{plugin}|{plugin}"
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_loop_reloads_mcp_tools_without_restarting_session(
    tmp_path: Path,
) -> None:
    config = MCPServerConfig(
        name="fake",
        command=sys.executable,
        args=["-u", "-c", FAKE_MCP_SERVER],
        env={},
    )
    loop = AshLoop(
        session_store=SessionStore(tmp_path / "sessions.db"),
        provider=IdleProvider(),
        safety_guard=SafetyGuard(tmp_path),
        ui=HeadlessUI(output_format="text", stream=io.StringIO()),
        project_root=tmp_path,
        mcp_configs={"fake": config},
    )
    await loop.start_session()
    assert "mcp__fake__echo" in loop.tools

    errors = await loop.reload_mcp_servers({})

    assert errors == {}
    assert "mcp__fake__echo" not in loop.tools
    await loop.aclose()


@pytest.mark.asyncio
async def test_loop_rejects_oversized_mcp_reload_before_runtime_publication(
    tmp_path: Path,
) -> None:
    loop = AshLoop(
        session_store=SessionStore(tmp_path / "sessions.db"),
        provider=IdleProvider(),
        safety_guard=SafetyGuard(tmp_path),
        ui=HeadlessUI(output_format="text", stream=io.StringIO()),
        project_root=tmp_path,
    )
    configs = {
        f"server-{index}": MCPServerConfig(
            name=f"server-{index}",
            command=sys.executable,
            args=["-c", "pass"],
            env={},
        )
        for index in range(33)
    }
    try:
        with pytest.raises(ValueError, match=r"MCP server count exceeds 32: 33"):
            await loop.reload_mcp_servers(configs)

        assert loop._mcp_configs == {}
        assert loop._mcp_runtime is None
    finally:
        await loop.aclose()


@pytest.mark.asyncio
async def test_loop_persists_mcp_task_only_for_active_tool_call(
    tmp_path: Path,
) -> None:
    from ash.context.turn import TurnContext

    store = SessionStore(tmp_path / "sessions.db")
    loop = AshLoop(
        session_store=store,
        provider=IdleProvider(),
        safety_guard=SafetyGuard(tmp_path),
        ui=HeadlessUI(output_format="text", stream=io.StringIO()),
        project_root=tmp_path,
    )
    session = await loop.start_session()
    loop.turn_context = TurnContext(session.session_id, "turn-1")
    loop.turn_context.set("tool_call_id", "call-1")
    payload = {
        "call_id": "call-1",
        "server_name": "server",
        "remote_tool_name": "slow",
        "contract_fingerprint": "contract",
        "server_fingerprint": "server-fingerprint",
        "protocol_version": "2026-07-28",
        "task": {
            "taskId": "task-1",
            "status": "working",
            "createdAt": "2026-09-20T00:00:00Z",
            "lastUpdatedAt": "2026-09-20T00:00:01Z",
            "ttlMs": None,
        },
        "answered_inputs": {},
    }
    try:
        await loop._persist_mcp_task_state(payload)
        rows = store.list_mcp_tasks(session.session_id)
        assert len(rows) == 1
        assert rows[0]["task_id"] == "task-1"
        assert rows[0]["call_id"] == "call-1"
        assert rows[0]["turn_id"] == "turn-1"

        loop.turn_context.set("tool_call_id", "another-call")
        with pytest.raises(RuntimeError, match="does not match active tool"):
            await loop._persist_mcp_task_state(payload)
        assert len(store.list_mcp_tasks(session.session_id)) == 1
    finally:
        loop.turn_context = None
    await loop.aclose()


@pytest.mark.asyncio
async def test_mcp_reload_refuses_replaced_workspace_root(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    saved = tmp_path / "workspace-original"
    replacement = tmp_path / "workspace-replacement"
    workspace.mkdir()
    replacement.mkdir()
    loop = AshLoop(
        session_store=SessionStore(tmp_path / "sessions-root-swap.db"),
        provider=IdleProvider(),
        safety_guard=SafetyGuard(workspace),
        ui=HeadlessUI(output_format="text", stream=io.StringIO()),
        project_root=workspace,
    )

    workspace.rename(saved)
    replacement.rename(workspace)

    with pytest.raises(RuntimeError, match="workspace root changed after runtime startup"):
        await loop.reload_mcp_servers({})

    await loop.aclose()


@pytest.mark.asyncio
async def test_resume_modern_task_refuses_new_input_during_recovery() -> None:
    client = MCPClient(MCPServerConfig(name="server", command="fake", args=[], env={}))
    client.protocol_version = "2026-07-28"
    client.server_capabilities = {
        "extensions": {"io.modelcontextprotocol/tasks": {}}
    }
    methods: list[str] = []

    async def request(method: str, params: dict, **kwargs: Any) -> dict:
        del params, kwargs
        methods.append(method)
        assert method == "tasks/get"
        return {
            "taskId": "task-recovery-input",
            "status": "input_required",
            "createdAt": "2026-09-20T00:00:00Z",
            "lastUpdatedAt": "2026-09-20T00:00:01Z",
            "ttlMs": 60_000,
            "pollIntervalMs": 0,
            "inputRequests": {"request": {}},
        }

    client.request = request  # type: ignore[method-assign]
    client._fulfill_modern_input_requests = AsyncMock(return_value={"request": {}})  # type: ignore[method-assign]
    client._start_modern_task_subscription = Mock()  # type: ignore[method-assign]
    client._stop_modern_task_subscription = AsyncMock()  # type: ignore[method-assign]

    with pytest.raises(MCPProtocolError, match="requires new input during recovery"):
        await client.resume_modern_task(
            {
                "taskId": "task-recovery-input",
                "status": "working",
                "createdAt": "2026-09-20T00:00:00Z",
                "lastUpdatedAt": "2026-09-20T00:00:00Z",
                "ttlMs": 60_000,
                "pollIntervalMs": 0,
            },
            {},
        )

    assert methods == ["tasks/get"]
    client._fulfill_modern_input_requests.assert_not_awaited()  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_loop_resumes_persisted_mcp_task_without_replaying_tool_call(
    tmp_path: Path,
) -> None:
    store = SessionStore(tmp_path / "sessions.db")
    session = store.create_session(str(tmp_path))
    turn_id = "turn-crash"
    call_id = "call-crash"
    server_name = "server.example"
    remote_name = "long/task"
    legacy_tool_name = f"mcp__{server_name}__{remote_name}"
    store.start_turn(session.session_id, turn_id, "run long MCP task")
    store.save_tool_call(
        session.session_id,
        ToolCallRecord(
            call_id=call_id,
            tool_name=legacy_tool_name,
            arguments={},
            approved=True,
            executed=False,
            dispatched=True,
            timestamp=datetime.now(timezone.utc),
        ),
        turn_id=turn_id,
    )
    task_id = "durable-task"
    methods: list[str] = []
    definition = {
        "name": remote_name,
        "description": "Long-running durable task",
        "inputSchema": {"type": "object", "additionalProperties": False},
    }

    first_client = MCPClient(
        MCPServerConfig(name=server_name, command="fake", args=[], env={})
    )
    first_client.protocol_version = "2026-07-28"
    first_client.server_capabilities = {
        "extensions": {"io.modelcontextprotocol/tasks": {}}
    }

    async def first_request(method: str, params: dict, **kwargs: Any) -> dict:
        del kwargs
        methods.append(method)
        assert method == "tools/call"
        assert params == {"name": remote_name, "arguments": {}}
        return {
            "resultType": "task",
            "taskId": task_id,
            "status": "working",
            "createdAt": "2026-09-20T00:00:00Z",
            "lastUpdatedAt": "2026-09-20T00:00:01Z",
            "ttlMs": 60_000,
            "pollIntervalMs": 0,
        }

    first_client.request = first_request  # type: ignore[method-assign]

    async def persist_then_crash(payload: dict[str, Any]) -> None:
        task = payload["task"]
        store.save_mcp_task(
            task_id=task["taskId"],
            session_id=session.session_id,
            turn_id=turn_id,
            call_id=payload["call_id"],
            server_name=payload["server_name"],
            remote_tool_name=payload["remote_tool_name"],
            contract_fingerprint=payload["contract_fingerprint"],
            server_fingerprint=payload["server_fingerprint"],
            protocol_version=payload["protocol_version"],
            task=task,
            answered_inputs=payload["answered_inputs"],
        )
        raise SystemExit("simulated process crash")

    first_tool = MCPTool(
        SafetyGuard(tmp_path),
        client=first_client,
        server_name=server_name,
        definition=definition,
        protocol_version="2026-07-28",
        task_state_handler=persist_then_crash,
    )
    with first_tool.event_context({"call_id": call_id}):
        with pytest.raises(SystemExit, match="simulated process crash"):
            await first_tool.run()

    rows = store.list_mcp_tasks(session.session_id)
    assert len(rows) == 1
    assert rows[0]["task_id"] == task_id
    assert methods == ["tools/call"]

    recovery_client = MCPClient(
        MCPServerConfig(name=server_name, command="fake", args=[], env={})
    )
    recovery_client.protocol_version = "2026-07-28"
    recovery_client.server_capabilities = {
        "extensions": {"io.modelcontextprotocol/tasks": {}}
    }

    async def recovery_request(method: str, params: dict, **kwargs: Any) -> dict:
        del kwargs
        methods.append(method)
        assert method == "tasks/get"
        assert params == {"taskId": task_id}
        return {
            "resultType": "complete",
            "taskId": task_id,
            "status": "completed",
            "createdAt": "2026-09-20T00:00:00Z",
            "lastUpdatedAt": "2026-09-20T00:00:02Z",
            "ttlMs": 60_000,
            "result": {
                "content": [{"type": "text", "text": "recovered result"}],
                "isError": False,
            },
        }

    recovery_client.request = recovery_request  # type: ignore[method-assign]
    guard = SafetyGuard(tmp_path)
    recovery_tool = MCPTool(
        guard,
        client=recovery_client,
        server_name=server_name,
        definition=definition,
        protocol_version="2026-07-28",
    )
    assert recovery_tool.name != legacy_tool_name
    assert PROVIDER_TOOL_NAME.fullmatch(recovery_tool.name)
    runtime = MCPRuntime({}, guard)
    runtime.clients[server_name] = recovery_client
    loop = AshLoop(
        session_store=store,
        provider=IdleProvider(),
        safety_guard=guard,
        ui=HeadlessUI(output_format="text", stream=io.StringIO()),
        project_root=tmp_path,
    )
    loop._mcp_runtime = runtime
    loop.tools[recovery_tool.name] = recovery_tool
    try:
        recovered = await loop.start_session(session.session_id)

        assert methods == ["tools/call", "tasks/get"]
        assert store.list_mcp_tasks(session.session_id) == []
        recovered_call = next(
            call for call in recovered.tool_calls if call.call_id == call_id
        )
        assert recovered_call.executed is True
        assert recovered_call.error is None
        assert "recovered result" in (recovered_call.result or "")
        tool_messages = [
            message
            for message in recovered.messages
            if message.role == "tool" and message.metadata.get("call_id") == call_id
        ]
        assert len(tool_messages) == 1
        assert "recovered result" in tool_messages[0].content

        recovered_again = await loop.start_session(session.session_id)
        repeated = [
            message
            for message in recovered_again.messages
            if message.role == "tool" and message.metadata.get("call_id") == call_id
        ]
        assert len(repeated) == 1
        assert methods == ["tools/call", "tasks/get"]
    finally:
        await loop.aclose()


@pytest.mark.asyncio
async def test_loop_preserves_deferred_mcp_task_until_server_returns(
    tmp_path: Path,
) -> None:
    store = SessionStore(tmp_path / "sessions.db")
    session = store.create_session(str(tmp_path))
    turn_id = "turn-deferred"
    call_id = "call-deferred"
    task_id = "task-deferred"
    definition = {
        "name": "long",
        "description": "Long-running durable task",
        "inputSchema": {"type": "object", "additionalProperties": False},
    }
    probe_tool = MCPTool(
        SafetyGuard(tmp_path),
        client=StubMCPClient({"content": []}),  # type: ignore[arg-type]
        server_name="server",
        definition=definition,
        protocol_version="2026-07-28",
    )
    recovery_config = MCPServerConfig(name="server", command="fake", args=[], env={})
    store.start_turn(session.session_id, turn_id, "resume durable task")
    store.save_tool_call(
        session.session_id,
        ToolCallRecord(
            call_id=call_id,
            tool_name="mcp__server__long",
            arguments={},
            approved=True,
            executed=False,
            dispatched=True,
            timestamp=datetime.now(timezone.utc),
        ),
        turn_id=turn_id,
    )
    store.save_mcp_task(
        task_id=task_id,
        session_id=session.session_id,
        turn_id=turn_id,
        call_id=call_id,
        server_name="server",
        remote_tool_name="long",
        contract_fingerprint=probe_tool.contract_fingerprint(),
        server_fingerprint=mcp_server_fingerprint(recovery_config, {}),
        protocol_version="2026-07-28",
        task={
            "taskId": task_id,
            "status": "working",
            "createdAt": "2026-09-20T00:00:00Z",
            "lastUpdatedAt": "2026-09-20T00:00:01Z",
            "ttlMs": 60_000,
            "pollIntervalMs": 0,
        },
        answered_inputs={},
    )

    guard = SafetyGuard(tmp_path)
    loop = AshLoop(
        session_store=store,
        provider=IdleProvider(),
        safety_guard=guard,
        ui=HeadlessUI(output_format="text", stream=io.StringIO()),
        project_root=tmp_path,
    )
    with pytest.raises(RuntimeError, match="waiting for durable MCP task recovery"):
        await loop.start_session(session.session_id)

    assert loop.current_session is None
    assert len(store.list_mcp_tasks(session.session_id)) == 1
    pending = store.tool_call_for_recovery(session.session_id, turn_id, call_id)
    assert pending is not None
    assert bool(pending["executed"]) is False
    assert pending["error"] is None

    methods: list[str] = []
    client = MCPClient(recovery_config)
    client.protocol_version = "2026-07-28"
    client.server_capabilities = {
        "extensions": {"io.modelcontextprotocol/tasks": {}}
    }

    async def request(method: str, params: dict, **kwargs: Any) -> dict:
        del kwargs
        methods.append(method)
        assert method == "tasks/get"
        assert params == {"taskId": task_id}
        return {
            "resultType": "complete",
            "taskId": task_id,
            "status": "completed",
            "createdAt": "2026-09-20T00:00:00Z",
            "lastUpdatedAt": "2026-09-20T00:00:02Z",
            "ttlMs": 60_000,
            "result": {
                "content": [{"type": "text", "text": "eventually recovered"}],
                "isError": False,
            },
        }

    client.request = request  # type: ignore[method-assign]
    runtime = MCPRuntime({}, guard)
    runtime.clients["server"] = client
    recovery_tool = MCPTool(
        guard,
        client=client,
        server_name="server",
        definition=definition,
        protocol_version="2026-07-28",
    )
    loop._mcp_runtime = runtime
    loop.tools[recovery_tool.name] = recovery_tool
    try:
        recovered = await loop.start_session(session.session_id)
        assert methods == ["tasks/get"]
        assert store.list_mcp_tasks(session.session_id) == []
        assert any(
            message.role == "tool"
            and message.metadata.get("call_id") == call_id
            and "eventually recovered" in message.content
            for message in recovered.messages
        )
    finally:
        await loop.aclose()


@pytest.mark.asyncio
async def test_loop_never_resumes_task_on_repointed_mcp_server_alias(
    tmp_path: Path,
) -> None:
    store = SessionStore(tmp_path / "sessions.db")
    session = store.create_session(str(tmp_path))
    turn_id = "turn-repointed"
    call_id = "call-repointed"
    task_id = "task-repointed"
    definition = {
        "name": "long",
        "description": "Long-running durable task",
        "inputSchema": {"type": "object", "additionalProperties": False},
    }
    original_config = MCPServerConfig(
        name="server",
        command="original-server",
        args=[],
        env={"TENANT": "original"},
    )
    replacement_config = MCPServerConfig(
        name="server",
        command="replacement-server",
        args=[],
        env={"TENANT": "replacement"},
    )
    replacement_client = MCPClient(replacement_config)
    replacement_client.protocol_version = "2026-07-28"
    replacement_client.server_capabilities = {
        "extensions": {"io.modelcontextprotocol/tasks": {}}
    }
    replacement_client.request = AsyncMock(  # type: ignore[method-assign]
        side_effect=AssertionError("replacement server must not receive task ID")
    )
    guard = SafetyGuard(tmp_path)
    replacement_tool = MCPTool(
        guard,
        client=replacement_client,
        server_name="server",
        definition=definition,
        protocol_version="2026-07-28",
    )
    store.start_turn(session.session_id, turn_id, "resume durable task")
    store.save_tool_call(
        session.session_id,
        ToolCallRecord(
            call_id=call_id,
            tool_name=replacement_tool.name,
            arguments={},
            approved=True,
            executed=False,
            dispatched=True,
            timestamp=datetime.now(timezone.utc),
        ),
        turn_id=turn_id,
    )
    store.save_mcp_task(
        task_id=task_id,
        session_id=session.session_id,
        turn_id=turn_id,
        call_id=call_id,
        server_name="server",
        remote_tool_name="long",
        contract_fingerprint=replacement_tool.contract_fingerprint(),
        server_fingerprint=mcp_server_fingerprint(original_config, {}),
        protocol_version="2026-07-28",
        task={
            "taskId": task_id,
            "status": "working",
            "createdAt": "2026-09-20T00:00:00Z",
            "lastUpdatedAt": "2026-09-20T00:00:01Z",
            "ttlMs": 60_000,
            "pollIntervalMs": 0,
        },
        answered_inputs={},
    )
    assert mcp_server_fingerprint(original_config, {}) != mcp_server_fingerprint(
        replacement_config,
        {},
    )

    runtime = MCPRuntime({}, guard)
    runtime.clients["server"] = replacement_client
    loop = AshLoop(
        session_store=store,
        provider=IdleProvider(),
        safety_guard=guard,
        ui=HeadlessUI(output_format="text", stream=io.StringIO()),
        project_root=tmp_path,
    )
    loop._mcp_runtime = runtime
    loop.tools[replacement_tool.name] = replacement_tool
    try:
        with pytest.raises(RuntimeError, match="waiting for durable MCP task recovery"):
            await loop.start_session(session.session_id)

        replacement_client.request.assert_not_awaited()  # type: ignore[attr-defined]
        assert len(store.list_mcp_tasks(session.session_id)) == 1
        pending = store.tool_call_for_recovery(session.session_id, turn_id, call_id)
        assert pending is not None
        assert bool(pending["executed"]) is False
        assert pending["error"] is None
    finally:
        await loop.aclose()


@pytest.mark.asyncio
async def test_loop_falls_back_to_unknown_for_legacy_v12_mcp_task(
    tmp_path: Path,
) -> None:
    store = SessionStore(tmp_path / "sessions.db")
    session = store.create_session(str(tmp_path))
    turn_id = "turn-v12"
    call_id = "call-v12"
    task_id = "task-v12"
    store.start_turn(session.session_id, turn_id, "legacy durable task")
    store.save_tool_call(
        session.session_id,
        ToolCallRecord(
            call_id=call_id,
            tool_name="mcp__server__long",
            arguments={},
            approved=True,
            executed=False,
            dispatched=True,
            timestamp=datetime.now(timezone.utc),
        ),
        turn_id=turn_id,
    )
    store.save_mcp_task(
        task_id=task_id,
        session_id=session.session_id,
        turn_id=turn_id,
        call_id=call_id,
        server_name="server",
        remote_tool_name="long",
        contract_fingerprint="legacy-contract",
        server_fingerprint="",
        protocol_version="2026-07-28",
        task={
            "taskId": task_id,
            "status": "working",
            "createdAt": "2026-09-20T00:00:00Z",
            "lastUpdatedAt": "2026-09-20T00:00:01Z",
            "ttlMs": 60_000,
        },
        answered_inputs={},
    )

    loop = AshLoop(
        session_store=store,
        provider=IdleProvider(),
        safety_guard=SafetyGuard(tmp_path),
        ui=HeadlessUI(output_format="text", stream=io.StringIO()),
        project_root=tmp_path,
    )
    try:
        recovered = await loop.start_session(session.session_id)

        assert store.list_mcp_tasks(session.session_id) == []
        call = next(call for call in recovered.tool_calls if call.call_id == call_id)
        assert call.executed is True
        assert call.error is not None
        assert "outcome is unknown" in call.error
    finally:
        await loop.aclose()


@pytest.mark.asyncio
async def test_loop_recovers_locally_finalized_mcp_call_without_server_or_duplicates(
    tmp_path: Path,
) -> None:
    store = SessionStore(tmp_path / "sessions.db")
    session = store.create_session(str(tmp_path))
    turn_id = "turn-local-final"
    call_id = "call-local-final"
    task_id = "task-local-final"
    store.start_turn(session.session_id, turn_id, "finish local persistence")
    store.save_tool_call(
        session.session_id,
        ToolCallRecord(
            call_id=call_id,
            tool_name="mcp__server__long",
            arguments={},
            approved=True,
            executed=True,
            dispatched=True,
            result="already persisted result",
            timestamp=datetime.now(timezone.utc),
        ),
        turn_id=turn_id,
    )

    task = {
        "taskId": task_id,
        "status": "completed",
        "createdAt": "2026-09-20T00:00:00Z",
        "lastUpdatedAt": "2026-09-20T00:00:02Z",
        "ttlMs": 60_000,
        "result": {
            "content": [{"type": "text", "text": "wire result"}],
            "isError": False,
        },
    }

    def persist_stale_task() -> None:
        store.save_mcp_task(
            task_id=task_id,
            session_id=session.session_id,
            turn_id=turn_id,
            call_id=call_id,
            server_name="server",
            remote_tool_name="long",
            contract_fingerprint="old-contract",
            server_fingerprint="",
            protocol_version="2026-07-28",
            task=task,
            answered_inputs={},
        )

    persist_stale_task()
    loop = AshLoop(
        session_store=store,
        provider=IdleProvider(),
        safety_guard=SafetyGuard(tmp_path),
        ui=HeadlessUI(output_format="text", stream=io.StringIO()),
        project_root=tmp_path,
    )
    try:
        recovered = await loop.start_session(session.session_id)
        messages = [
            message
            for message in recovered.messages
            if message.role == "tool" and message.metadata.get("call_id") == call_id
        ]
        assert len(messages) == 1
        assert "already persisted result" in messages[0].content
        assert store.list_mcp_tasks(session.session_id) == []

        persist_stale_task()
        recovered_again = await loop.start_session(session.session_id)
        messages_again = [
            message
            for message in recovered_again.messages
            if message.role == "tool" and message.metadata.get("call_id") == call_id
        ]
        assert len(messages_again) == 1
        assert store.list_mcp_tasks(session.session_id) == []
    finally:
        await loop.aclose()


@pytest.mark.asyncio
async def test_loop_applies_live_mcp_tool_refresh(tmp_path: Path) -> None:
    config = MCPServerConfig(
        name="dynamic",
        command=sys.executable,
        args=["-u", "-c", DYNAMIC_MCP_SERVER],
        env={},
    )
    loop = AshLoop(
        session_store=SessionStore(tmp_path / "sessions.db"),
        provider=IdleProvider(),
        safety_guard=SafetyGuard(tmp_path),
        ui=HeadlessUI(output_format="text", stream=io.StringIO()),
        project_root=tmp_path,
        mcp_configs={"dynamic": config},
    )
    await loop.start_session()
    try:
        await loop._mcp_runtime.wait_for_refreshes()
        assert "mcp__dynamic__old" not in loop.tools
        assert "mcp__dynamic__new" in loop.tools
    finally:
        await loop.aclose()


@pytest.mark.asyncio
async def test_reload_keeps_in_flight_mcp_snapshot_alive_until_turn_end(
    tmp_path: Path,
) -> None:
    config = MCPServerConfig(
        name="fake",
        command=sys.executable,
        args=["-u", "-c", FAKE_MCP_SERVER],
        env={},
    )
    loop = AshLoop(
        session_store=SessionStore(tmp_path / "sessions.db"),
        provider=IdleProvider(),
        safety_guard=SafetyGuard(tmp_path),
        ui=HeadlessUI(output_format="text", stream=io.StringIO()),
        project_root=tmp_path,
        mcp_configs={"fake": config},
    )
    await loop.start_session()
    old_tool = loop.tools["mcp__fake__echo"]
    loop._turn_running = True
    try:
        assert await loop.reload_mcp_servers({}) == {}
        assert "mcp__fake__echo" not in loop.tools
        assert "fake" not in loop._mcp_configs
        result = await old_tool.run(text="in flight")
        assert result.success is True
        assert result.output == "in flight"
        assert loop._retired_mcp_runtimes
    finally:
        loop._turn_running = False
        await loop._close_retired_mcp_runtimes()
        await loop.aclose()


@pytest.mark.asyncio
async def test_committed_mcp_reload_defers_failed_old_runtime_cleanup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    instances = []

    class ReloadRuntime:
        def __init__(self, configs, safety_guard, **kwargs) -> None:
            del safety_guard, kwargs
            self.configs = dict(configs)
            self.clients = {name: object() for name in configs}
            self.errors = {}
            self.close_calls = 0
            self.fail_next_close = len(instances) == 0
            instances.append(self)

        async def start(self) -> dict:
            return {}

        def resource_watches(self) -> list[dict[str, str]]:
            return []

        def server_tools_snapshot(self) -> dict[str, dict]:
            return {}

        def activate_notifications(self) -> None:
            return None

        async def close_retired_clients(self) -> None:
            return None

        async def close(self) -> None:
            self.close_calls += 1
            if self.fail_next_close:
                self.fail_next_close = False
                raise RuntimeError("old runtime cleanup failed")
            self.clients.clear()

    monkeypatch.setattr("ash.mcp.runtime.MCPRuntime", ReloadRuntime)
    initial = MCPServerConfig(name="initial", command="unused", args=[], env={})
    replacement = MCPServerConfig(
        name="replacement", command="unused", args=[], env={}
    )
    loop = AshLoop(
        session_store=SessionStore(tmp_path / "sessions.db"),
        provider=IdleProvider(),
        safety_guard=SafetyGuard(tmp_path),
        ui=HeadlessUI(output_format="text", stream=io.StringIO()),
        project_root=tmp_path,
        mcp_configs={"initial": initial},
    )
    await loop.start_session()
    old_runtime = loop._mcp_runtime
    assert old_runtime is instances[0]

    assert await loop.reload_mcp_servers({"replacement": replacement}) == {}

    new_runtime = loop._mcp_runtime
    assert new_runtime is instances[1]
    assert old_runtime in loop._retired_mcp_runtimes
    assert old_runtime.close_calls == 1

    await loop._close_retired_mcp_runtimes()

    assert old_runtime.close_calls == 2
    assert old_runtime not in loop._retired_mcp_runtimes
    await loop.aclose()


@pytest.mark.asyncio
async def test_mcp_notification_activation_failure_preserves_old_runtime(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    instances = []

    class CandidateTool:
        name = "mcp__replacement__candidate"

        def __init__(self) -> None:
            self.starts = 0

        def set_event_sink(self, _sink) -> None:
            return None

        async def start(self) -> None:
            self.starts += 1

    class ActivationRuntime:
        def __init__(self, configs, safety_guard, **kwargs) -> None:
            del safety_guard, kwargs
            self.configs = dict(configs)
            self.clients = {name: object() for name in configs}
            self.errors = {}
            self.close_calls = 0
            self.fail_activation = len(instances) == 1
            self.candidate_tool = CandidateTool() if self.fail_activation else None
            instances.append(self)

        async def start(self) -> dict:
            if self.candidate_tool is None:
                return {}
            return {self.candidate_tool.name: self.candidate_tool}

        def resource_watches(self) -> list[dict[str, str]]:
            return []

        def server_tools_snapshot(self) -> dict[str, dict]:
            return {}

        def activate_notifications(self) -> None:
            if self.fail_activation:
                raise RuntimeError("notification activation failed")

        async def close_retired_clients(self) -> None:
            return None

        async def close(self) -> None:
            self.close_calls += 1
            self.clients.clear()

    monkeypatch.setattr("ash.mcp.runtime.MCPRuntime", ActivationRuntime)
    initial = MCPServerConfig(name="initial", command="unused", args=[], env={})
    replacement = MCPServerConfig(
        name="replacement", command="unused", args=[], env={}
    )
    loop = AshLoop(
        session_store=SessionStore(tmp_path / "sessions.db"),
        provider=IdleProvider(),
        safety_guard=SafetyGuard(tmp_path),
        ui=HeadlessUI(output_format="text", stream=io.StringIO()),
        project_root=tmp_path,
        mcp_configs={"initial": initial},
    )
    await loop.start_session()
    old_runtime = loop._mcp_runtime
    assert old_runtime is instances[0]

    with pytest.raises(RuntimeError, match="notification activation failed"):
        await loop.reload_mcp_servers({"replacement": replacement})

    assert loop._mcp_runtime is old_runtime
    assert loop._mcp_configs == {"initial": initial}
    assert old_runtime.close_calls == 0
    assert instances[1].close_calls == 1
    candidate_tool = instances[1].candidate_tool
    assert candidate_tool is not None
    assert candidate_tool.starts == 1
    assert id(candidate_tool) not in loop._started_tool_ids
    await loop.aclose()


@pytest.mark.asyncio
async def test_no_session_mcp_reload_defers_failed_committed_cleanup(
    tmp_path: Path,
) -> None:
    class FailOnceRuntime:
        def __init__(self) -> None:
            self.close_calls = 0

        async def close(self) -> None:
            self.close_calls += 1
            if self.close_calls == 1:
                raise RuntimeError("old runtime cleanup failed")

    loop = AshLoop(
        session_store=SessionStore(tmp_path / "sessions.db"),
        provider=IdleProvider(),
        safety_guard=SafetyGuard(tmp_path),
        ui=HeadlessUI(output_format="text", stream=io.StringIO()),
        project_root=tmp_path,
    )
    old_runtime = FailOnceRuntime()
    loop._mcp_runtime = old_runtime
    replacement = MCPServerConfig(
        name="replacement", command="unused", args=[], env={}
    )

    assert await loop.reload_mcp_servers({"replacement": replacement}) == {}

    assert loop._mcp_runtime is None
    assert loop._mcp_configs == {"replacement": replacement}
    assert old_runtime in loop._retired_mcp_runtimes
    assert old_runtime.close_calls == 1

    await loop._close_retired_mcp_runtimes()

    assert old_runtime.close_calls == 2
    assert not loop._retired_mcp_runtimes
    await loop.aclose()


@pytest.mark.asyncio
async def test_turn_result_survives_failed_opportunistic_mcp_cleanup(
    tmp_path: Path,
) -> None:
    class RetiredRuntime:
        def __init__(self) -> None:
            self.close_calls = 0
            self.fail_cleanup = True

        async def close(self) -> None:
            self.close_calls += 1
            if self.fail_cleanup:
                raise RuntimeError("retired runtime cleanup failed")

    loop = AshLoop(
        session_store=SessionStore(tmp_path / "sessions.db"),
        provider=IdleProvider(),
        safety_guard=SafetyGuard(tmp_path),
        ui=HeadlessUI(output_format="text", stream=io.StringIO()),
        project_root=tmp_path,
    )
    await loop.start_session()
    retired = RetiredRuntime()
    loop._retired_mcp_runtimes.add(retired)

    assert await loop.run_turn("continue") == "idle"

    assert retired.close_calls == 1
    assert retired in loop._retired_mcp_runtimes

    with pytest.raises(RuntimeError, match="failed to close 1 retired MCP runtime"):
        await loop.aclose()

    assert retired.close_calls == 2
    assert retired in loop._retired_mcp_runtimes
    retired.fail_cleanup = False
    await loop.aclose()
    assert retired.close_calls == 3


@pytest.mark.asyncio
async def test_mcp_cleanup_cancellation_restores_prior_log_context(
    tmp_path: Path,
) -> None:
    class CancelledRuntime:
        async def close(self) -> None:
            raise asyncio.CancelledError

    loop = AshLoop(
        session_store=SessionStore(tmp_path / "sessions.db"),
        provider=IdleProvider(),
        safety_guard=SafetyGuard(tmp_path),
        ui=HeadlessUI(output_format="text", stream=io.StringIO()),
        project_root=tmp_path,
    )
    await loop.start_session()
    retired = CancelledRuntime()
    loop._retired_mcp_runtimes.add(retired)
    outer_context = {"session_id": "outer-session", "turn_id": "outer-turn"}
    replace_log_context(outer_context)
    try:
        with pytest.raises(asyncio.CancelledError):
            await loop.run_turn("continue")
        assert current_log_context() == outer_context
    finally:
        replace_log_context({})
        loop._retired_mcp_runtimes.clear()
        await loop.aclose()


@pytest.mark.asyncio
async def test_failed_mcp_reload_preserves_working_runtime(tmp_path: Path) -> None:
    working = MCPServerConfig(
        name="fake",
        command=sys.executable,
        args=["-u", "-c", FAKE_MCP_SERVER],
        env={},
    )
    loop = AshLoop(
        session_store=SessionStore(tmp_path / "sessions.db"),
        provider=IdleProvider(),
        safety_guard=SafetyGuard(tmp_path),
        ui=HeadlessUI(output_format="text", stream=io.StringIO()),
        project_root=tmp_path,
        mcp_configs={"fake": working},
    )
    await loop.start_session()
    broken = MCPServerConfig(
        name="broken",
        command=str(tmp_path / "missing-server"),
        args=[],
        env={},
    )
    try:
        errors = await loop.reload_mcp_servers({"broken": broken})
        assert "broken" in errors
        assert "mcp__fake__echo" in loop.tools
        result = await loop.tools["mcp__fake__echo"].run(text="still works")
        assert result.success is True
        assert result.output == "still works"
    finally:
        await loop.aclose()


@pytest.mark.asyncio
async def test_loop_shutdown_serializes_with_in_progress_mcp_reload(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    start_entered = asyncio.Event()
    release_start = asyncio.Event()
    instances = []

    class PausedRuntime:
        def __init__(self, configs, safety_guard, **kwargs) -> None:
            del safety_guard, kwargs
            self.configs = configs
            self.clients = {"paused": object()}
            self.errors = {}
            self.close_calls = 0
            instances.append(self)

        async def start(self) -> dict:
            start_entered.set()
            await release_start.wait()
            return {}

        def server_tools_snapshot(self) -> dict:
            return {}

        def activate_notifications(self) -> None:
            return None

        async def close(self) -> None:
            self.close_calls += 1
            self.clients.clear()

    monkeypatch.setattr("ash.mcp.runtime.MCPRuntime", PausedRuntime)
    loop = AshLoop(
        session_store=SessionStore(tmp_path / "sessions.db"),
        provider=IdleProvider(),
        safety_guard=SafetyGuard(tmp_path),
        ui=HeadlessUI(output_format="text", stream=io.StringIO()),
        project_root=tmp_path,
    )
    await loop.start_session()
    config = MCPServerConfig(name="paused", command="unused", args=[], env={})
    reload_task = asyncio.create_task(loop.reload_mcp_servers({"paused": config}))
    await asyncio.wait_for(start_entered.wait(), timeout=1)
    close_task = asyncio.create_task(loop.aclose())
    await asyncio.sleep(0)
    assert close_task.done() is False

    release_start.set()
    assert await reload_task == {}
    await asyncio.wait_for(close_task, timeout=1)

    assert len(instances) == 1
    assert instances[0].close_calls == 1
    assert loop._mcp_runtime is None
    assert loop._closed is True
    with pytest.raises(RuntimeError, match="after loop shutdown"):
        await loop.reload_mcp_servers({})
