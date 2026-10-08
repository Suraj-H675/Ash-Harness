# tests/unit/test_mcp_modern.py
import asyncio
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, Mock

import httpx
import pytest

from ash import __version__
from ash.mcp import client as mcp_client_module
from ash.mcp.client import (
    MCPClient,
    MCPProtocolError,
    MCPTaskTimeout,
)
from ash.mcp.oauth import MCPOAuthSession
from ash.mcp.runtime import (
    MCPRuntime,
)
from ash.mcp.server import (
    MCPServerConfig,
)
from ash.safety.guard import SafetyGuard


def test_mcp_client_version_matches_package_identity() -> None:
    from ash.mcp.client import _client_version

    assert _client_version() == __version__


@pytest.mark.asyncio
async def test_stdio_negotiates_modern_protocol_without_initialize() -> None:
    server = r"""
import json, sys
for line in sys.stdin:
    message = json.loads(line)
    method = message.get("method")
    params = message.get("params", {})
    if method == "server/discover":
        meta = params.get("_meta", {})
        caps = meta.get("io.modelcontextprotocol/clientCapabilities", {})
        assert meta.get("io.modelcontextprotocol/protocolVersion") == "2026-07-28"
        assert caps.get("extensions", {}).get("io.modelcontextprotocol/tasks") == {}
        result = {
            "resultType": "complete",
            "supportedVersions": ["2026-07-28"],
            "capabilities": {"tools": {}},
            "_meta": {"io.modelcontextprotocol/serverInfo": {"name": "modern", "version": "1"}},
        }
    elif method == "initialize":
        result = None
        print(json.dumps({"jsonrpc": "2.0", "id": message["id"], "error": {"code": -32603, "message": "initialize forbidden"}}), flush=True)
        continue
    elif method == "tools/list":
        meta = params.get("_meta", {})
        assert meta.get("io.modelcontextprotocol/protocolVersion") == "2026-07-28"
        result = {
            "resultType": "complete",
            "tools": [{"name": "echo", "description": "echo", "inputSchema": {"type": "object"}}],
            "ttlMs": 1000,
            "cacheScope": "private",
        }
    else:
        result = {"resultType": "complete"}
    print(json.dumps({"jsonrpc": "2.0", "id": message["id"], "result": result}), flush=True)
"""
    client = MCPClient(
        MCPServerConfig(
            name="modern", command=sys.executable, args=["-u", "-c", server], env={}
        )
    )
    await client.connect()

    assert client.protocol_version == "2026-07-28"
    assert client.server_info == {"name": "modern", "version": "1"}
    tools = await client.list_tools()
    assert [tool["name"] for tool in tools] == ["echo"]
    await client.disconnect()


@pytest.mark.asyncio
async def test_http_modern_requests_use_stateless_routing_headers() -> None:
    seen: list[tuple[str, httpx.Headers, dict]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        seen.append((payload["method"], request.headers, payload))
        assert request.headers["MCP-Protocol-Version"] == "2026-07-28"
        assert request.headers["Mcp-Method"] == payload["method"]
        assert "Mcp-Session-Id" not in request.headers
        meta = payload["params"]["_meta"]
        assert meta["io.modelcontextprotocol/protocolVersion"] == "2026-07-28"
        if payload["method"] == "server/discover":
            result = {
                "resultType": "complete",
                "supportedVersions": ["2026-07-28", "2025-11-25"],
                "capabilities": {"tools": {}},
            }
        elif payload["method"] == "tools/call":
            assert request.headers["Mcp-Name"] == "echo"
            assert request.headers["Mcp-Param-Region"] == "us-east1"
            result = {
                "resultType": "complete",
                "content": [{"type": "text", "text": "works"}],
            }
        else:
            raise AssertionError(payload["method"])
        return httpx.Response(
            200,
            json={"jsonrpc": "2.0", "id": payload["id"], "result": result},
            request=request,
        )

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = MCPClient(
        MCPServerConfig(
            name="modern",
            command="",
            args=[],
            env={},
            transport="http",
            url="https://mcp.example.test/rpc",
        ),
        http_client=http,
    )
    await client.connect()
    result = await client.call_tool(
        "echo",
        {"region": "us-east1"},
        header_annotations=[(("region",), "Region")],
    )

    assert result["content"][0]["text"] == "works"
    assert [method for method, _, _ in seen] == ["server/discover", "tools/call"]
    await client.disconnect()
    await http.aclose()


@pytest.mark.asyncio
async def test_modern_task_extension_drives_input_update_and_completion() -> None:
    seen: list[tuple[str, httpx.Headers, dict]] = []
    persisted: list[tuple[str, dict[str, str]]] = []
    task_gets = 0
    task_id = "task-123"

    def task_state(status: str, **extra: object) -> dict:
        return {
            "taskId": task_id,
            "status": status,
            "createdAt": "2026-09-20T00:00:00Z",
            "lastUpdatedAt": "2026-09-20T00:00:01Z",
            "ttlMs": 60_000,
            "pollIntervalMs": 0,
            **extra,
        }

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal task_gets
        payload = json.loads(request.content)
        method = payload["method"]
        params = payload.get("params", {})
        seen.append((method, request.headers, payload))
        capabilities = params.get("_meta", {}).get(
            "io.modelcontextprotocol/clientCapabilities", {}
        )
        assert capabilities.get("extensions", {}).get(
            "io.modelcontextprotocol/tasks"
        ) == {}
        if method == "server/discover":
            result = {
                "resultType": "complete",
                "supportedVersions": ["2026-07-28"],
                "capabilities": {
                    "tools": {},
                    "extensions": {"io.modelcontextprotocol/tasks": {}},
                },
            }
        elif method == "tools/call":
            result = {"resultType": "task", **task_state("working")}
        elif method == "tasks/get":
            assert request.headers["Mcp-Name"] == task_id
            task_gets += 1
            if task_gets == 1:
                result = {
                    "resultType": "complete",
                    **task_state(
                        "input_required",
                        inputRequests={
                            "approve": {
                                "method": "elicitation/create",
                                "params": {
                                    "mode": "form",
                                    "message": "Approve?",
                                    "requestedSchema": {
                                        "type": "object",
                                        "properties": {
                                            "approved": {"type": "boolean"}
                                        },
                                        "required": ["approved"],
                                    },
                                },
                            }
                        },
                    ),
                }
            else:
                result = {
                    "resultType": "complete",
                    **task_state(
                        "completed",
                        result={
                            "content": [{"type": "text", "text": "finished"}],
                            "isError": False,
                        },
                    ),
                }
        elif method == "tasks/update":
            assert request.headers["Mcp-Name"] == task_id
            assert params["taskId"] == task_id
            assert params["inputResponses"] == {
                "approve": {
                    "action": "accept",
                    "content": {"approved": True},
                }
            }
            result = {"resultType": "complete"}
        else:
            raise AssertionError(method)
        return httpx.Response(
            200,
            json={"jsonrpc": "2.0", "id": payload["id"], "result": result},
            request=request,
        )

    async def elicit(params: dict) -> dict:
        assert params["message"] == "Approve?"
        return {"action": "accept", "content": {"approved": True}}

    async def persist_task_state(
        task: dict[str, Any], answered_inputs: dict[str, str]
    ) -> None:
        persisted.append((str(task["status"]), dict(answered_inputs)))

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = MCPClient(
        MCPServerConfig(
            name="modern-task",
            command="",
            args=[],
            env={},
            transport="http",
            url="https://mcp.example.test/rpc",
        ),
        http_client=http,
        elicitation_handler=elicit,
    )
    await client.connect()

    result = await client.call_tool(
        "long",
        {},
        modern_task_state_callback=persist_task_state,
    )

    assert result["content"][0]["text"] == "finished"
    task_requests = [entry for entry in seen if entry[0] != "subscriptions/listen"]
    assert [method for method, _, _ in task_requests] == [
        "server/discover",
        "tools/call",
        "tasks/get",
        "tasks/update",
        "tasks/get",
    ]
    assert any(method == "subscriptions/listen" for method, _, _ in seen)
    for method, headers, _ in task_requests[2:]:
        assert headers["Mcp-Method"] == method
        assert headers["Mcp-Name"] == task_id
    assert [status for status, _ in persisted] == [
        "working",
        "input_required",
        "input_required",
        "completed",
    ]
    assert persisted[0][1] == {}
    assert persisted[1][1] == {}
    assert set(persisted[2][1]) == {"approve"}
    assert persisted[3][1] == persisted[2][1]
    await client.disconnect()
    await http.aclose()


@pytest.mark.asyncio
async def test_modern_task_persistence_failure_requests_cancellation() -> None:
    client = MCPClient(MCPServerConfig(name="modern", command="fake", args=[], env={}))
    client.protocol_version = "2026-07-28"
    client.server_capabilities = {
        "extensions": {"io.modelcontextprotocol/tasks": {}}
    }
    request = AsyncMock(return_value={"resultType": "complete"})
    client.request = request  # type: ignore[method-assign]
    initial = {
        "resultType": "task",
        "taskId": "durability-failed",
        "status": "working",
        "createdAt": "2026-09-20T00:00:00Z",
        "lastUpdatedAt": "2026-09-20T00:00:01Z",
        "ttlMs": None,
    }

    async def fail_persistence(
        task: dict[str, Any], answered_inputs: dict[str, str]
    ) -> None:
        del task, answered_inputs
        raise OSError("database unavailable")

    with pytest.raises(MCPProtocolError, match="could not be persisted"):
        await client._await_modern_tool_task(
            initial,
            timeout=1,
            state_callback=fail_persistence,
        )

    assert request.await_count == 1
    assert request.await_args.args == (
        "tasks/cancel",
        {"taskId": "durability-failed"},
    )


@pytest.mark.asyncio
async def test_resume_modern_task_uses_task_id_without_replaying_tool_call() -> None:
    client = MCPClient(MCPServerConfig(name="modern", command="fake", args=[], env={}))
    client.protocol_version = "2026-07-28"
    client.server_capabilities = {
        "extensions": {"io.modelcontextprotocol/tasks": {}}
    }
    methods: list[str] = []
    task_id = "resume-me"

    async def request(method: str, params: dict, **kwargs: Any) -> dict:
        del kwargs
        methods.append(method)
        assert params == {"taskId": task_id}
        if method != "tasks/get":
            raise AssertionError(method)
        return {
            "resultType": "complete",
            "taskId": task_id,
            "status": "completed",
            "createdAt": "2026-09-20T00:00:00Z",
            "lastUpdatedAt": "2026-09-20T00:00:02Z",
            "ttlMs": 60_000,
            "result": {
                "content": [{"type": "text", "text": "recovered"}],
                "isError": False,
            },
        }

    client.request = request  # type: ignore[method-assign]
    persisted = {
        "taskId": task_id,
        "status": "working",
        "createdAt": "2026-09-20T00:00:00Z",
        "lastUpdatedAt": "2026-09-20T00:00:01Z",
        "ttlMs": 60_000,
        "pollIntervalMs": 0,
    }

    result = await client.resume_modern_task(persisted, {})

    assert result["content"][0]["text"] == "recovered"
    assert methods == ["tasks/get"]


@pytest.mark.asyncio
async def test_resume_modern_task_timeout_preserves_remote_task() -> None:
    client = MCPClient(MCPServerConfig(name="modern", command="fake", args=[], env={}))
    client.protocol_version = "2026-07-28"
    client.server_capabilities = {
        "extensions": {"io.modelcontextprotocol/tasks": {}}
    }
    task_id = "still-working"
    methods: list[str] = []
    client._start_modern_task_subscription = Mock()  # type: ignore[method-assign]
    client._stop_modern_task_subscription = AsyncMock()  # type: ignore[method-assign]

    async def request(method: str, params: dict, **kwargs: Any) -> dict:
        del kwargs
        methods.append(method)
        assert params == {"taskId": task_id}
        if method != "tasks/get":
            raise AssertionError(method)
        return {
            "resultType": "complete",
            "taskId": task_id,
            "status": "working",
            "createdAt": "2026-09-20T00:00:00Z",
            "lastUpdatedAt": "2026-09-20T00:00:01Z",
            "ttlMs": 60_000,
            "pollIntervalMs": 1000,
        }

    client.request = request  # type: ignore[method-assign]
    persisted = {
        "taskId": task_id,
        "status": "working",
        "createdAt": "2026-09-20T00:00:00Z",
        "lastUpdatedAt": "2026-09-20T00:00:01Z",
        "ttlMs": 60_000,
        "pollIntervalMs": 1000,
    }

    with pytest.raises(MCPTaskTimeout):
        await client.resume_modern_task(persisted, {}, timeout=0.001)

    assert "tasks/cancel" not in methods
    assert "tools/call" not in methods


@pytest.mark.asyncio
async def test_resume_modern_task_does_not_repeat_answered_input_request() -> None:
    client = MCPClient(MCPServerConfig(name="modern", command="fake", args=[], env={}))
    client.protocol_version = "2026-07-28"
    client.server_capabilities = {
        "extensions": {"io.modelcontextprotocol/tasks": {}}
    }
    task_id = "resume-input"
    methods: list[str] = []
    gets = 0
    input_request = {
        "method": "elicitation/create",
        "params": {
            "mode": "form",
            "message": "Approve?",
            "requestedSchema": {
                "type": "object",
                "properties": {"approved": {"type": "boolean"}},
                "required": ["approved"],
            },
        },
    }
    fingerprint = json.dumps(
        input_request,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )

    async def request(method: str, params: dict, **kwargs: Any) -> dict:
        nonlocal gets
        del kwargs
        methods.append(method)
        assert params == {"taskId": task_id}
        assert method == "tasks/get"
        gets += 1
        if gets == 1:
            return {
                "resultType": "complete",
                "taskId": task_id,
                "status": "input_required",
                "createdAt": "2026-09-20T00:00:00Z",
                "lastUpdatedAt": "2026-09-20T00:00:02Z",
                "ttlMs": 60_000,
                "pollIntervalMs": 0,
                "inputRequests": {"approve": input_request},
            }
        return {
            "resultType": "complete",
            "taskId": task_id,
            "status": "completed",
            "createdAt": "2026-09-20T00:00:00Z",
            "lastUpdatedAt": "2026-09-20T00:00:03Z",
            "ttlMs": 60_000,
            "result": {
                "content": [{"type": "text", "text": "approved earlier"}],
                "isError": False,
            },
        }

    client.request = request  # type: ignore[method-assign]
    client._start_modern_task_subscription = Mock()  # type: ignore[method-assign]
    client._stop_modern_task_subscription = AsyncMock()  # type: ignore[method-assign]
    persisted = {
        "taskId": task_id,
        "status": "working",
        "createdAt": "2026-09-20T00:00:00Z",
        "lastUpdatedAt": "2026-09-20T00:00:01Z",
        "ttlMs": 60_000,
        "pollIntervalMs": 0,
    }

    result = await client.resume_modern_task(
        persisted,
        {"approve": fingerprint},
    )

    assert result["content"][0]["text"] == "approved earlier"
    assert methods == ["tasks/get", "tasks/get"]
    assert "tasks/update" not in methods


@pytest.mark.asyncio
async def test_modern_task_result_requires_server_extension() -> None:
    client = MCPClient(MCPServerConfig(name="modern", command="fake", args=[], env={}))
    client.protocol_version = "2026-07-28"
    client.server_capabilities = {"tools": {}}

    with pytest.raises(MCPProtocolError, match="without advertising"):
        await client._resolve_modern_result(
            "tools/call",
            {"name": "long", "arguments": {}},
            {
                "resultType": "task",
                "taskId": "task-1",
                "status": "working",
                "createdAt": "2026-09-20T00:00:00Z",
                "lastUpdatedAt": "2026-09-20T00:00:01Z",
                "ttlMs": None,
            },
            input_required_round=0,
            expected_tool_contract=None,
            header_annotations=[],
        )


@pytest.mark.asyncio
async def test_modern_task_cancel_returns_acknowledgement_and_routes_task_id() -> None:
    seen: list[tuple[str, httpx.Headers]] = []
    task_id = "cancel-me"

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        method = payload["method"]
        seen.append((method, request.headers))
        if method == "server/discover":
            result = {
                "resultType": "complete",
                "supportedVersions": ["2026-07-28"],
                "capabilities": {
                    "extensions": {"io.modelcontextprotocol/tasks": {}}
                },
            }
        elif method == "tasks/cancel":
            assert payload["params"]["taskId"] == task_id
            result = {"resultType": "complete"}
        else:
            raise AssertionError(method)
        return httpx.Response(
            200,
            json={"jsonrpc": "2.0", "id": payload["id"], "result": result},
            request=request,
        )

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = MCPClient(
        MCPServerConfig(
            name="modern-task",
            command="",
            args=[],
            env={},
            transport="http",
            url="https://mcp.example.test/rpc",
        ),
        http_client=http,
    )
    await client.connect()

    result = await client.cancel_mcp_task(task_id)

    assert result == {"taskId": task_id, "acknowledged": True}
    assert seen[-1][1]["Mcp-Method"] == "tasks/cancel"
    assert seen[-1][1]["Mcp-Name"] == task_id
    await client.disconnect()
    await http.aclose()


@pytest.mark.asyncio
async def test_modern_task_deduplicates_repeated_input_request_keys() -> None:
    client = MCPClient(
        MCPServerConfig(name="modern", command="fake", args=[], env={}),
        elicitation_handler=AsyncMock(
            return_value={"action": "accept", "content": {"ok": True}}
        ),
    )
    client.protocol_version = "2026-07-28"
    client.server_capabilities = {
        "extensions": {"io.modelcontextprotocol/tasks": {}}
    }
    input_request = {
        "confirm": {
            "method": "elicitation/create",
            "params": {
                "mode": "form",
                "message": "Continue?",
                "requestedSchema": {
                    "type": "object",
                    "properties": {"ok": {"type": "boolean"}},
                },
            },
        }
    }
    responses = iter(
        [
            {"resultType": "complete"},
            {
                "taskId": "dedupe",
                "status": "input_required",
                "createdAt": "2026-09-20T00:00:00Z",
                "lastUpdatedAt": "2026-09-20T00:00:01Z",
                "ttlMs": None,
                "pollIntervalMs": 0,
                "inputRequests": input_request,
            },
            {
                "taskId": "dedupe",
                "status": "completed",
                "createdAt": "2026-09-20T00:00:00Z",
                "lastUpdatedAt": "2026-09-20T00:00:02Z",
                "ttlMs": None,
                "result": {"content": [{"type": "text", "text": "done"}]},
            },
        ]
    )

    async def request(method: str, params: dict, **_: object) -> dict:
        if method == "tasks/update":
            return next(responses)
        if method == "tasks/get":
            return next(responses)
        raise AssertionError(method)

    client.request = AsyncMock(side_effect=request)  # type: ignore[method-assign]
    initial = {
        "resultType": "task",
        "taskId": "dedupe",
        "status": "input_required",
        "createdAt": "2026-09-20T00:00:00Z",
        "lastUpdatedAt": "2026-09-20T00:00:01Z",
        "ttlMs": None,
        "pollIntervalMs": 0,
        "inputRequests": input_request,
    }

    result = await client._await_modern_tool_task(initial, timeout=1)

    assert result["content"][0]["text"] == "done"
    assert client.elicitation_handler.await_count == 1
    assert [call.args[0] for call in client.request.await_args_list] == [
        "tasks/update",
        "tasks/get",
        "tasks/get",
    ]


@pytest.mark.asyncio
async def test_modern_task_failure_preserves_jsonrpc_error() -> None:
    client = MCPClient(MCPServerConfig(name="modern", command="fake", args=[], env={}))
    client.protocol_version = "2026-07-28"
    client.server_capabilities = {
        "extensions": {"io.modelcontextprotocol/tasks": {}}
    }
    failed = {
        "resultType": "task",
        "taskId": "failed-task",
        "status": "failed",
        "createdAt": "2026-09-20T00:00:00Z",
        "lastUpdatedAt": "2026-09-20T00:00:01Z",
        "ttlMs": None,
        "error": {
            "code": -32044,
            "message": "remote execution failed",
            "data": {"kind": "upstream"},
        },
    }

    with pytest.raises(MCPProtocolError, match="remote execution failed") as caught:
        await client._await_modern_tool_task(failed, timeout=1)

    assert caught.value.code == -32044
    assert caught.value.has_data is True
    assert caught.value.data == {"kind": "upstream"}


@pytest.mark.asyncio
async def test_modern_task_timeout_cancels_without_post_deadline_poll(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = MCPClient(MCPServerConfig(name="modern", command="fake", args=[], env={}))
    client.protocol_version = "2026-07-28"
    client.server_capabilities = {
        "extensions": {"io.modelcontextprotocol/tasks": {}}
    }
    monkeypatch.setattr(client, "_modern_task_poll_delay", lambda task: 1.0)
    request = AsyncMock(return_value={})
    client.request = request  # type: ignore[method-assign]
    initial = {
        "resultType": "task",
        "taskId": "slow-task",
        "status": "working",
        "createdAt": "2026-09-20T00:00:00Z",
        "lastUpdatedAt": "2026-09-20T00:00:01Z",
        "ttlMs": None,
        "pollIntervalMs": 1000,
    }

    with pytest.raises(MCPTaskTimeout):
        await client._await_modern_tool_task(initial, timeout=0.01)

    assert [call.args[0] for call in request.await_args_list] == ["tasks/cancel"]
    assert request.await_args.args[1] == {"taskId": "slow-task"}


@pytest.mark.asyncio
async def test_modern_tool_call_never_sends_removed_task_opt_in() -> None:
    client = MCPClient(MCPServerConfig(name="modern", command="fake", args=[], env={}))
    client.protocol_version = "2026-07-28"
    client.server_capabilities = {
        "tools": {},
        "extensions": {"io.modelcontextprotocol/tasks": {}},
    }
    request = AsyncMock(
        return_value={"content": [{"type": "text", "text": "sync"}]}
    )
    client.request = request  # type: ignore[method-assign]

    result = await client.call_tool("long", {}, as_task=True)

    assert result["content"][0]["text"] == "sync"
    assert request.await_args.args[:2] == ("tools/call", {"name": "long", "arguments": {}})


@pytest.mark.asyncio
async def test_modern_mrtr_can_transition_to_task_over_stdio() -> None:
    server = r"""
import json, sys
for line in sys.stdin:
    message = json.loads(line)
    method = message.get("method")
    params = message.get("params", {})
    if method == "server/discover":
        result = {
            "resultType": "complete",
            "supportedVersions": ["2026-07-28"],
            "capabilities": {
                "tools": {},
                "extensions": {"io.modelcontextprotocol/tasks": {}},
            },
        }
    elif method == "tools/call":
        if "inputResponses" not in params:
            result = {
                "resultType": "input_required",
                "inputRequests": {
                    "confirm": {
                        "method": "elicitation/create",
                        "params": {
                            "mode": "form",
                            "message": "Continue into task?",
                            "requestedSchema": {
                                "type": "object",
                                "properties": {"ok": {"type": "boolean"}},
                                "required": ["ok"],
                            },
                        },
                    }
                },
                "requestState": "before-task",
            }
        else:
            assert params["requestState"] == "before-task"
            result = {
                "resultType": "task",
                "taskId": "stdio-task",
                "status": "working",
                "createdAt": "2026-09-20T00:00:00Z",
                "lastUpdatedAt": "2026-09-20T00:00:01Z",
                "ttlMs": 60000,
                "pollIntervalMs": 0,
            }
    elif method == "tasks/get":
        assert params["taskId"] == "stdio-task"
        result = {
            "resultType": "complete",
            "taskId": "stdio-task",
            "status": "completed",
            "createdAt": "2026-09-20T00:00:00Z",
            "lastUpdatedAt": "2026-09-20T00:00:02Z",
            "ttlMs": 60000,
            "result": {
                "content": [{"type": "text", "text": "task complete"}],
                "isError": False,
            },
        }
    else:
        result = {"resultType": "complete"}
    print(json.dumps({"jsonrpc": "2.0", "id": message["id"], "result": result}), flush=True)
"""

    async def elicit(params: dict) -> dict:
        assert params["message"] == "Continue into task?"
        return {"action": "accept", "content": {"ok": True}}

    client = MCPClient(
        MCPServerConfig(
            name="modern-task", command=sys.executable, args=["-u", "-c", server], env={}
        ),
        elicitation_handler=elicit,
    )
    await client.connect()
    try:
        result = await client.call_tool("long", {})
        assert result["content"][0]["text"] == "task complete"
    finally:
        await client.disconnect()


@pytest.mark.asyncio
async def test_modern_task_notification_completes_without_polling_over_stdio() -> None:
    server = r"""
import json, sys
listen_id = None
for line in sys.stdin:
    message = json.loads(line)
    method = message.get("method")
    params = message.get("params", {})
    if method == "server/discover":
        result = {
            "resultType": "complete",
            "supportedVersions": ["2026-07-28"],
            "capabilities": {
                "tools": {},
                "extensions": {"io.modelcontextprotocol/tasks": {}},
            },
        }
        print(json.dumps({"jsonrpc": "2.0", "id": message["id"], "result": result}), flush=True)
    elif method == "tools/call":
        result = {
            "resultType": "task",
            "taskId": "notify-task",
            "status": "working",
            "createdAt": "2026-09-20T00:00:00Z",
            "lastUpdatedAt": "2026-09-20T00:00:01Z",
            "ttlMs": 60000,
            "pollIntervalMs": 100000,
        }
        print(json.dumps({"jsonrpc": "2.0", "id": message["id"], "result": result}), flush=True)
    elif method == "subscriptions/listen":
        listen_id = message["id"]
        assert params["notifications"] == {"taskIds": ["notify-task"]}
        meta = {"io.modelcontextprotocol/subscriptionId": listen_id}
        print(json.dumps({
            "jsonrpc": "2.0",
            "method": "notifications/subscriptions/acknowledged",
            "params": {"notifications": {"taskIds": ["notify-task"]}, "_meta": meta},
        }), flush=True)
        print(json.dumps({
            "jsonrpc": "2.0",
            "method": "notifications/tasks",
            "params": {
                "taskId": "notify-task",
                "status": "completed",
                "createdAt": "2026-09-20T00:00:00Z",
                "lastUpdatedAt": "2026-09-20T00:00:02Z",
                "ttlMs": 60000,
                "pollIntervalMs": 100000,
                "result": {
                    "content": [{"type": "text", "text": "notified"}],
                    "isError": False,
                },
                "_meta": meta,
            },
        }), flush=True)
    elif method == "tasks/get":
        result = {
            "resultType": "complete",
            "taskId": "notify-task",
            "status": "completed",
            "createdAt": "2026-09-20T00:00:00Z",
            "lastUpdatedAt": "2026-09-20T00:00:03Z",
            "ttlMs": 60000,
            "result": {"content": [{"type": "text", "text": "polled"}]},
        }
        print(json.dumps({"jsonrpc": "2.0", "id": message["id"], "result": result}), flush=True)
    elif method == "notifications/cancelled":
        assert message["params"]["requestId"] == listen_id
        print(json.dumps({
            "jsonrpc": "2.0",
            "id": listen_id,
            "result": {
                "resultType": "complete",
                "_meta": {"io.modelcontextprotocol/subscriptionId": listen_id},
            },
        }), flush=True)
"""
    client = MCPClient(
        MCPServerConfig(
            name="modern-task-notify",
            command=sys.executable,
            args=["-u", "-c", server],
            env={},
        )
    )
    await client.connect()
    try:
        result = await asyncio.wait_for(client.call_tool("long", {}), timeout=1)
        assert result["content"][0]["text"] == "notified"
        assert client._task_subscription_tasks == {}
        assert client._task_subscription_request_ids == {}
        assert client._modern_task_updates == {}
    finally:
        await client.disconnect()


@pytest.mark.asyncio
async def test_modern_task_subscription_decline_falls_back_to_polling() -> None:
    server = r"""
import json, sys
for line in sys.stdin:
    message = json.loads(line)
    method = message.get("method")
    params = message.get("params", {})
    if method == "server/discover":
        result = {
            "resultType": "complete",
            "supportedVersions": ["2026-07-28"],
            "capabilities": {
                "tools": {},
                "extensions": {"io.modelcontextprotocol/tasks": {}},
            },
        }
    elif method == "tools/call":
        result = {
            "resultType": "task",
            "taskId": "poll-task",
            "status": "working",
            "createdAt": "2026-09-20T00:00:00Z",
            "lastUpdatedAt": "2026-09-20T00:00:01Z",
            "ttlMs": 60000,
            "pollIntervalMs": 10,
        }
    elif method == "subscriptions/listen":
        listen_id = message["id"]
        assert params["notifications"] == {"taskIds": ["poll-task"]}
        meta = {"io.modelcontextprotocol/subscriptionId": listen_id}
        print(json.dumps({
            "jsonrpc": "2.0",
            "method": "notifications/subscriptions/acknowledged",
            "params": {"notifications": {}, "_meta": meta},
        }), flush=True)
        result = {"resultType": "complete", "_meta": meta}
    elif method == "tasks/get":
        result = {
            "resultType": "complete",
            "taskId": "poll-task",
            "status": "completed",
            "createdAt": "2026-09-20T00:00:00Z",
            "lastUpdatedAt": "2026-09-20T00:00:02Z",
            "ttlMs": 60000,
            "result": {"content": [{"type": "text", "text": "polled"}]},
        }
    else:
        result = {"resultType": "complete"}
    print(json.dumps({"jsonrpc": "2.0", "id": message["id"], "result": result}), flush=True)
"""
    client = MCPClient(
        MCPServerConfig(
            name="modern-task-poll",
            command=sys.executable,
            args=["-u", "-c", server],
            env={},
        )
    )
    await client.connect()
    try:
        result = await asyncio.wait_for(client.call_tool("long", {}), timeout=1)
        assert result["content"][0]["text"] == "polled"
        assert client._task_subscription_tasks == {}
        assert client._task_subscription_request_ids == {}
        assert client._modern_task_updates == {}
    finally:
        await client.disconnect()


@pytest.mark.asyncio
async def test_modern_input_required_is_auto_fulfilled_and_retried() -> None:
    server = r"""
import json, sys
for line in sys.stdin:
    message = json.loads(line)
    method = message.get("method")
    params = message.get("params", {})
    if method == "server/discover":
        result = {
            "resultType": "complete",
            "supportedVersions": ["2026-07-28"],
            "capabilities": {"tools": {}},
        }
    elif method == "tools/call":
        if "inputResponses" not in params:
            result = {
                "resultType": "input_required",
                "inputRequests": {
                    "confirm": {
                        "method": "elicitation/create",
                        "params": {
                            "mode": "form",
                            "message": "Continue?",
                            "requestedSchema": {
                                "type": "object",
                                "properties": {"ok": {"type": "boolean"}},
                                "required": ["ok"],
                            },
                        },
                    }
                },
                "requestState": "opaque-state",
            }
        else:
            assert params["requestState"] == "opaque-state"
            assert params["inputResponses"] == {
                "confirm": {"action": "accept", "content": {"ok": True}}
            }
            result = {
                "resultType": "complete",
                "content": [{"type": "text", "text": "confirmed"}],
            }
    else:
        result = {"resultType": "complete"}
    print(json.dumps({"jsonrpc": "2.0", "id": message["id"], "result": result}), flush=True)
"""

    async def elicit(params: dict) -> dict:
        assert params["message"] == "Continue?"
        return {"action": "accept", "content": {"ok": True}}

    client = MCPClient(
        MCPServerConfig(
            name="modern", command=sys.executable, args=["-u", "-c", server], env={}
        ),
        elicitation_handler=elicit,
    )
    await client.connect()
    result = await client.call_tool("confirm", {})

    assert result == {"content": [{"type": "text", "text": "confirmed"}]}
    await client.disconnect()


@pytest.mark.asyncio
async def test_modern_result_requires_result_type() -> None:
    server = r"""
import json, sys
for line in sys.stdin:
    message = json.loads(line)
    method = message.get("method")
    if method == "server/discover":
        result = {"resultType": "complete", "supportedVersions": ["2026-07-28"], "capabilities": {"tools": {}}}
    else:
        result = {"content": [{"type": "text", "text": "missing discriminator"}]}
    print(json.dumps({"jsonrpc": "2.0", "id": message["id"], "result": result}), flush=True)
"""
    client = MCPClient(MCPServerConfig(name="modern", command=sys.executable, args=["-u", "-c", server], env={}))
    await client.connect()
    with pytest.raises(MCPProtocolError, match="missing resultType"):
        await client.call_tool("broken", {})
    await client.disconnect()


@pytest.mark.asyncio
async def test_modern_input_required_round_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(mcp_client_module, "INPUT_REQUIRED_RETRY_DELAY_SECONDS", 0)
    server = r"""
import json, sys
for line in sys.stdin:
    message = json.loads(line)
    if message.get("method") == "server/discover":
        result = {"resultType": "complete", "supportedVersions": ["2026-07-28"], "capabilities": {"tools": {}}}
    else:
        result = {"resultType": "input_required", "requestState": "retry"}
    print(json.dumps({"jsonrpc": "2.0", "id": message["id"], "result": result}), flush=True)
"""
    client = MCPClient(MCPServerConfig(name="modern", command=sys.executable, args=["-u", "-c", server], env={}))
    await client.connect()
    with pytest.raises(MCPProtocolError, match="exceeded 10 input_required rounds"):
        await client.call_tool("loop", {})
    await client.disconnect()


@pytest.mark.asyncio
async def test_modern_input_required_rejects_undeclared_client_capability() -> None:
    server = r"""
import json, sys
for line in sys.stdin:
    message = json.loads(line)
    if message.get("method") == "server/discover":
        result = {"resultType": "complete", "supportedVersions": ["2026-07-28"], "capabilities": {"tools": {}}}
    else:
        result = {"resultType": "input_required", "inputRequests": {"roots": {"method": "roots/list", "params": {}}}}
    print(json.dumps({"jsonrpc": "2.0", "id": message["id"], "result": result}), flush=True)
"""
    client = MCPClient(MCPServerConfig(name="modern", command=sys.executable, args=["-u", "-c", server], env={}))
    await client.connect()
    with pytest.raises(MCPProtocolError, match="undeclared client capability"):
        await client.call_tool("needs-roots", {})
    await client.disconnect()


@pytest.mark.asyncio
async def test_modern_http_400_jsonrpc_error_is_delivered_in_band() -> None:
    methods: list[str] = []
    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        methods.append(payload["method"])
        if payload["method"] == "server/discover":
            result = {"resultType": "complete", "supportedVersions": ["2026-07-28"], "capabilities": {"tools": {}}}
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": payload["id"], "result": result}, request=request)
        return httpx.Response(400, headers={"content-type": "application/json"}, json={"jsonrpc": "2.0", "id": payload["id"], "error": {"code": -32099, "message": "denied"}}, request=request)
    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = MCPClient(MCPServerConfig(name="modern", command="", args=[], env={}, transport="http", url="https://mcp.example.test/rpc"), http_client=http)
    await client.connect()
    with pytest.raises(MCPProtocolError, match=r"tools/call failed \(-32099\): denied") as exc_info:
        await client.call_tool("blocked", {})
    assert exc_info.value.code == -32099
    assert methods == ["server/discover", "tools/call"]
    await client.disconnect()
    await http.aclose()


@pytest.mark.asyncio
async def test_modern_http_timeout_does_not_post_cancel_notification() -> None:
    methods: list[str] = []
    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        methods.append(payload["method"])
        if payload["method"] == "server/discover":
            result = {"resultType": "complete", "supportedVersions": ["2026-07-28"], "capabilities": {"tools": {}}}
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": payload["id"], "result": result}, request=request)
        if payload["method"] == "tools/call":
            raise httpx.ReadTimeout("slow", request=request)
        raise AssertionError(payload["method"])
    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = MCPClient(MCPServerConfig(name="modern", command="", args=[], env={}, transport="http", url="https://mcp.example.test/rpc"), http_client=http)
    await client.connect()
    with pytest.raises(httpx.ReadTimeout):
        await client.call_tool("slow", {})
    assert methods == ["server/discover", "tools/call"]
    await client.disconnect()
    await http.aclose()


@pytest.mark.asyncio
async def test_stdio_discover_rejects_unsupported_future_version() -> None:
    probe_response = {
        "resultType": "complete",
        "supportedVersions": ["2026-08-01"],
        "capabilities": {},
    }
    server = f"""
import json, sys
for line in sys.stdin:
    message = json.loads(line)
    if message.get("method") == "server/discover":
        print(json.dumps({{"jsonrpc": "2.0", "id": message["id"], "result": {json.dumps(probe_response)}}}), flush=True)
"""
    client = MCPClient(
        MCPServerConfig(
            name="future", command=sys.executable, args=["-u", "-c", server], env={}
        )
    )
    with pytest.raises(
        MCPProtocolError, match="no mutually supported protocol version"
    ):
        await asyncio.wait_for(client.connect(), timeout=1)


@pytest.mark.asyncio
async def test_stdio_unsupported_modern_version_fails_deterministically() -> None:
    server = r"""
import json, sys
for line in sys.stdin:
    message = json.loads(line)
    if message.get("method") == "server/discover":
        error = {
            "code": -32022,
            "message": "unsupported version",
            "data": {"supported": ["2026-07-28"]},
        }
        print(json.dumps({"jsonrpc": "2.0", "id": message["id"], "error": error}), flush=True)
"""
    client = MCPClient(
        MCPServerConfig(
            name="modern", command=sys.executable, args=["-u", "-c", server], env={}
        )
    )
    with pytest.raises(
        MCPProtocolError, match="no mutually supported protocol version"
    ):
        await asyncio.wait_for(client.connect(), timeout=1)


@pytest.mark.asyncio
async def test_http_modern_probe_recognizes_supported_headers_and_oauth() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.method != "POST":
            return httpx.Response(204)
        payload = json.loads(request.content)
        if payload["method"] == "initialize":
            return httpx.Response(
                200,
                json={
                    "jsonrpc": "2.0",
                    "id": payload["id"],
                    "result": {"protocolVersion": "2025-11-25", "capabilities": {}},
                },
            )
        if payload["method"] == "notifications/initialized":
            return httpx.Response(202)
        assert request.headers["Authorization"] == "Bearer probe-token"
        assert request.headers["x-required"] == "probe"
        meta = payload["params"]["_meta"]
        assert meta["io.modelcontextprotocol/protocolVersion"] == "2026-07-28"
        return httpx.Response(
            200,
            json={"jsonrpc": "2.0", "id": payload["id"], "result": {}},
        )

    oauth = SimpleNamespace(
        http_client=None,
        authorization_header=AsyncMock(return_value="Bearer probe-token"),
    )
    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = MCPClient(
        MCPServerConfig(
            name="remote",
            command="",
            args=[],
            env={},
            transport="http",
            url="https://mcp.example.test/rpc",
            headers={"x-required": "probe"},
        ),
        http_client=http,
        oauth_session=cast(MCPOAuthSession, oauth),
    )
    await client.connect()

    assert len([request for request in requests if request.method == "POST"]) == 3
    await client.disconnect()
    await http.aclose()


@pytest.mark.asyncio
async def test_http_unrecognized_400_falls_back_to_legacy_initialize() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.method != "POST":
            return httpx.Response(204)
        if request.content.startswith(b'{"jsonrpc":"2.0","id":0'):
            return httpx.Response(400, text="legacy gateway rejection")
        payload = json.loads(request.content)
        if payload["method"] == "initialize":
            return httpx.Response(
                200,
                headers={"Mcp-Session-Id": "session-1"},
                json={
                    "jsonrpc": "2.0",
                    "id": payload["id"],
                    "result": {
                        "protocolVersion": "2025-06-18",
                        "capabilities": {},
                    },
                },
            )
        if payload["method"] == "notifications/initialized":
            return httpx.Response(202)
        return httpx.Response(
            200,
            json={"jsonrpc": "2.0", "id": payload["id"], "result": {"tools": []}},
        )

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = MCPClient(
        MCPServerConfig(
            name="remote",
            command="",
            args=[],
            env={},
            transport="http",
            url="https://mcp.example.test/rpc",
        ),
        http_client=http,
    )
    await client.connect()
    protocol_version = client.protocol_version
    await client.disconnect()

    assert [
        json.loads(request.content)["method"]
        for request in requests
        if request.method == "POST"
    ] == [
        "server/discover",
        "initialize",
        "notifications/initialized",
    ]
    assert protocol_version == "2025-06-18"
    await http.aclose()


@pytest.mark.asyncio
async def test_http_modern_unsupported_version_fails_without_fallback() -> None:
    methods = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        methods.append(payload["method"])
        return httpx.Response(
            400,
            json={
                "jsonrpc": "2.0",
                "id": payload["id"],
                "error": {
                    "code": -32022,
                    "message": "unsupported version",
                    "data": {"requested": "2026-07-28", "supported": ["2026-07-28"]},
                },
            },
        )

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = MCPClient(
        MCPServerConfig(
            name="remote",
            command="",
            args=[],
            env={},
            transport="http",
            url="https://mcp.example.test/rpc",
        ),
        http_client=http,
    )
    with pytest.raises(
        MCPProtocolError, match="no mutually supported protocol version"
    ):
        await asyncio.wait_for(client.connect(), timeout=1)

    assert methods == ["server/discover"]
    await http.aclose()


@pytest.mark.parametrize("code", [-32020, -32021])
@pytest.mark.asyncio
async def test_http_recognized_modern_error_fails_without_fallback(
    code: int,
) -> None:
    methods = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        methods.append(payload["method"])
        return httpx.Response(
            400,
            json={
                "jsonrpc": "2.0",
                "id": payload["id"],
                "error": {"code": code, "message": "modern rejection"},
            },
        )

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = MCPClient(
        MCPServerConfig(
            name="remote",
            command="",
            args=[],
            env={},
            transport="http",
            url="https://mcp.example.test/rpc",
        ),
        http_client=http,
    )
    with pytest.raises(MCPProtocolError, match="modern discovery request"):
        await asyncio.wait_for(client.connect(), timeout=1)

    assert methods == ["server/discover"]
    await http.aclose()


@pytest.mark.asyncio
async def test_http_probe_network_failure_falls_back_to_initialize() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if request.method != "POST":
            return httpx.Response(204)
        if request.content.startswith(b'{"jsonrpc":"2.0","id":0'):
            raise httpx.ConnectError("network unavailable", request=request)
        payload = json.loads(request.content)
        if "id" not in payload:
            return httpx.Response(202)
        return httpx.Response(
            200,
            json={
                "jsonrpc": "2.0",
                "id": payload["id"],
                "result": {"protocolVersion": "2025-06-18", "capabilities": {}},
            },
        )

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = MCPClient(
        MCPServerConfig(
            name="remote",
            command="",
            args=[],
            env={},
            transport="http",
            url="https://mcp.example.test/rpc",
        ),
        http_client=http,
    )
    await client.connect()
    protocol_version = client.protocol_version
    await client.disconnect()

    assert calls >= 2
    assert protocol_version == "2025-06-18"
    await http.aclose()


@pytest.mark.asyncio
async def test_mcp_runtime_wires_opt_in_client_interaction_capabilities(
    tmp_path: Path,
) -> None:
    sampling = AsyncMock(return_value={"role": "assistant"})
    elicitation = AsyncMock(return_value={"action": "decline"})
    runtime = MCPRuntime(
        {},
        SafetyGuard(tmp_path),
        sampling_handler=sampling,
        elicitation_handler=elicitation,
    )
    config = MCPServerConfig(name="fake", command="fake", args=[], env={})

    client = runtime._configure_client("fake", config)

    assert client.client_capabilities["sampling"] == {}
    assert client.client_capabilities["elicitation"] == {"form": {}}
    await client.sampling_handler({"maxTokens": 1})
    await client.elicitation_handler({"message": "hello"})
    sampling.assert_awaited_once_with("fake", {"maxTokens": 1})
    elicitation.assert_awaited_once_with("fake", {"message": "hello"})


@pytest.mark.asyncio
async def test_modern_stdio_subscription_delivers_list_change_and_cancels() -> None:
    server = r"""
import json, sys
listen_id = None
for line in sys.stdin:
    message = json.loads(line)
    method = message.get("method")
    if method == "server/discover":
        result = {
            "resultType": "complete",
            "supportedVersions": ["2026-07-28"],
            "capabilities": {"tools": {"listChanged": True}},
        }
        print(json.dumps({"jsonrpc": "2.0", "id": message["id"], "result": result}), flush=True)
    elif method == "subscriptions/listen":
        listen_id = message["id"]
        assert message["params"]["notifications"] == {"toolsListChanged": True}
        meta = {"io.modelcontextprotocol/subscriptionId": listen_id}
        print(json.dumps({
            "jsonrpc": "2.0",
            "method": "notifications/subscriptions/acknowledged",
            "params": {"notifications": {"toolsListChanged": True}, "_meta": meta},
        }), flush=True)
        print(json.dumps({
            "jsonrpc": "2.0",
            "method": "notifications/tools/list_changed",
            "params": {"_meta": meta},
        }), flush=True)
    elif method == "notifications/cancelled":
        assert message["params"]["requestId"] == listen_id
        print(json.dumps({
            "jsonrpc": "2.0",
            "id": listen_id,
            "result": {
                "resultType": "complete",
                "_meta": {"io.modelcontextprotocol/subscriptionId": listen_id},
            },
        }), flush=True)
"""
    changed = asyncio.Event()
    seen: list[str] = []

    async def on_notification(method: str, params: dict) -> None:
        seen.append(method)
        if method == "notifications/tools/list_changed":
            changed.set()

    client = MCPClient(
        MCPServerConfig(
            name="modern", command=sys.executable, args=["-u", "-c", server], env={}
        ),
        notification_handler=on_notification,
    )
    await client.connect()
    await asyncio.wait_for(changed.wait(), timeout=0.5)
    assert seen == ["notifications/tools/list_changed"]
    await client.disconnect()


@pytest.mark.asyncio
@pytest.mark.parametrize("subscription_kind", ["task", "resource"])
async def test_modern_subscription_stop_settles_before_propagating_cancellation(
    subscription_kind: str,
) -> None:
    client = MCPClient(
        MCPServerConfig(name="modern", command="fake", args=[], env={})
    )
    client._process = object()  # type: ignore[assignment]
    notify_started = asyncio.Event()
    allow_notify = asyncio.Event()

    async def blocked_notify(*_args: Any, **_kwargs: Any) -> None:
        notify_started.set()
        await allow_notify.wait()

    client.notify = blocked_notify  # type: ignore[method-assign]

    async def watch() -> None:
        await asyncio.Event().wait()

    watcher = asyncio.create_task(watch())
    if subscription_kind == "task":
        key = "task-1"
        client._task_subscription_tasks[key] = watcher
        client._task_subscription_request_ids[key] = 41
        client._task_subscription_ids_by_request[41] = key
        client._task_subscription_acks[key] = asyncio.Event()
        stopping = asyncio.create_task(client._stop_modern_task_subscription(key))
    else:
        key = "file:///tmp/resource.txt"
        client._resource_subscription_tasks[key] = watcher
        client._resource_subscription_request_ids[key] = 42
        client._resource_subscription_uris_by_id[42] = key
        client._resource_subscription_acks[key] = asyncio.Event()
        stopping = asyncio.create_task(client._stop_modern_resource_subscription(key))

    await asyncio.wait_for(notify_started.wait(), timeout=1)
    stopping.cancel()
    await asyncio.sleep(0)
    stopping.cancel()
    await asyncio.sleep(0)

    assert stopping.done() is False
    assert watcher.done() is False

    allow_notify.set()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(stopping, timeout=1)

    assert watcher.done() is True
    if subscription_kind == "task":
        assert key not in client._task_subscription_tasks
        assert key not in client._task_subscription_request_ids
        assert 41 not in client._task_subscription_ids_by_request
    else:
        assert key not in client._resource_subscription_tasks
        assert key not in client._resource_subscription_request_ids
        assert 42 not in client._resource_subscription_uris_by_id


@pytest.mark.asyncio
async def test_modern_http_subscription_uses_listen_stream_without_cancel_post() -> None:
    changed = asyncio.Event()
    stream_closed = asyncio.Event()
    methods: list[str] = []

    class SubscriptionStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            listen_id = 1
            meta = {"io.modelcontextprotocol/subscriptionId": listen_id}
            ack = {
                "jsonrpc": "2.0",
                "method": "notifications/subscriptions/acknowledged",
                "params": {
                    "notifications": {"toolsListChanged": True},
                    "_meta": meta,
                },
            }
            change = {
                "jsonrpc": "2.0",
                "method": "notifications/tools/list_changed",
                "params": {"_meta": meta},
            }
            yield f"data: {json.dumps(ack)}\n\n".encode()
            yield f"data: {json.dumps(change)}\n\n".encode()
            await asyncio.Event().wait()

        async def aclose(self) -> None:
            stream_closed.set()

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        methods.append(payload["method"])
        if payload["method"] == "server/discover":
            result = {
                "resultType": "complete",
                "supportedVersions": ["2026-07-28"],
                "capabilities": {"tools": {"listChanged": True}},
            }
            return httpx.Response(
                200,
                json={"jsonrpc": "2.0", "id": payload["id"], "result": result},
                request=request,
            )
        assert payload["method"] == "subscriptions/listen"
        assert request.headers["MCP-Protocol-Version"] == "2026-07-28"
        assert request.headers["Mcp-Method"] == "subscriptions/listen"
        assert payload["params"]["notifications"] == {"toolsListChanged": True}
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            stream=SubscriptionStream(),
            request=request,
        )

    async def on_notification(method: str, params: dict) -> None:
        if method == "notifications/tools/list_changed":
            changed.set()

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = MCPClient(
        MCPServerConfig(
            name="modern",
            command="",
            args=[],
            env={},
            transport="http",
            url="https://mcp.example.test/rpc",
        ),
        http_client=http,
        notification_handler=on_notification,
    )
    await client.connect()
    await asyncio.wait_for(changed.wait(), timeout=0.5)
    await client.disconnect()
    await asyncio.wait_for(stream_closed.wait(), timeout=0.5)
    await http.aclose()

    assert methods == ["server/discover", "subscriptions/listen"]


@pytest.mark.asyncio
async def test_modern_subscription_rejects_unrequested_ack_filter() -> None:
    server = r"""
import json, sys
listen_id = None
for line in sys.stdin:
    message = json.loads(line)
    method = message.get("method")
    if method == "server/discover":
        result = {
            "resultType": "complete",
            "supportedVersions": ["2026-07-28"],
            "capabilities": {"tools": {"listChanged": True}},
        }
        print(json.dumps({"jsonrpc": "2.0", "id": message["id"], "result": result}), flush=True)
    elif method == "subscriptions/listen":
        listen_id = message["id"]
        meta = {"io.modelcontextprotocol/subscriptionId": listen_id}
        print(json.dumps({
            "jsonrpc": "2.0",
            "method": "notifications/subscriptions/acknowledged",
            "params": {
                "notifications": {
                    "toolsListChanged": True,
                    "promptsListChanged": True,
                },
                "_meta": meta,
            },
        }), flush=True)
    elif method == "notifications/cancelled":
        print(json.dumps({
            "jsonrpc": "2.0",
            "id": listen_id,
            "result": {"resultType": "complete"},
        }), flush=True)
"""
    client = MCPClient(
        MCPServerConfig(
            name="modern", command=sys.executable, args=["-u", "-c", server], env={}
        ),
        notification_handler=lambda _method, _params: None,
    )
    with pytest.raises(MCPProtocolError, match="acknowledged unrequested filter"):
        await client.connect()


@pytest.mark.asyncio
async def test_modern_http_subscription_loss_is_reported_after_ack() -> None:
    release = asyncio.Event()
    lost = asyncio.Event()
    errors: list[str] = []

    class DroppingSubscriptionStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            meta = {"io.modelcontextprotocol/subscriptionId": 1}
            ack = {
                "jsonrpc": "2.0",
                "method": "notifications/subscriptions/acknowledged",
                "params": {
                    "notifications": {"toolsListChanged": True},
                    "_meta": meta,
                },
            }
            yield f"data: {json.dumps(ack)}\n\n".encode()
            await release.wait()

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        if payload["method"] == "server/discover":
            result = {
                "resultType": "complete",
                "supportedVersions": ["2026-07-28"],
                "capabilities": {"tools": {"listChanged": True}},
            }
            return httpx.Response(
                200,
                json={"jsonrpc": "2.0", "id": payload["id"], "result": result},
                request=request,
            )
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            stream=DroppingSubscriptionStream(),
            request=request,
        )

    async def on_failure(error: BaseException) -> None:
        errors.append(str(error))
        lost.set()

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = MCPClient(
        MCPServerConfig(
            name="modern",
            command="",
            args=[],
            env={},
            transport="http",
            url="https://mcp.example.test/rpc",
        ),
        http_client=http,
        notification_handler=lambda _method, _params: None,
        subscription_failure_handler=on_failure,
    )
    await client.connect()
    release.set()
    await asyncio.wait_for(lost.wait(), timeout=0.5)
    assert errors == ["MCP subscription HTTP stream ended without a graceful result"]
    await client.disconnect()
    await http.aclose()


@pytest.mark.asyncio
async def test_modern_resource_watch_uses_explicit_subscription_and_delivers_update() -> None:
    server = r"""
import json, sys
listen_id = None
for line in sys.stdin:
    message = json.loads(line)
    method = message.get("method")
    if method == "server/discover":
        result = {
            "resultType": "complete",
            "supportedVersions": ["2026-07-28"],
            "capabilities": {"resources": {"subscribe": True}},
        }
        print(json.dumps({"jsonrpc": "2.0", "id": message["id"], "result": result}), flush=True)
    elif method == "subscriptions/listen":
        listen_id = message["id"]
        assert message["params"]["notifications"] == {
            "resourceSubscriptions": ["file:///watched.txt"]
        }
        meta = {"io.modelcontextprotocol/subscriptionId": listen_id}
        print(json.dumps({
            "jsonrpc": "2.0",
            "method": "notifications/subscriptions/acknowledged",
            "params": {"notifications": {"resourceSubscriptions": ["file:///watched.txt"]}, "_meta": meta},
        }), flush=True)
        print(json.dumps({
            "jsonrpc": "2.0",
            "method": "notifications/resources/updated",
            "params": {"uri": "file:///watched.txt", "_meta": meta},
        }), flush=True)
    elif method == "notifications/cancelled":
        assert message["params"]["requestId"] == listen_id
        print(json.dumps({
            "jsonrpc": "2.0",
            "id": listen_id,
            "result": {"resultType": "complete"},
        }), flush=True)
"""
    updated = asyncio.Event()
    seen: list[str] = []

    async def on_notification(method: str, params: dict) -> None:
        if method == "notifications/resources/updated":
            seen.append(str(params.get("uri")))
            updated.set()

    client = MCPClient(
        MCPServerConfig(
            name="modern-resource-watch",
            command=sys.executable,
            args=["-u", "-c", server],
            env={},
        ),
        notification_handler=on_notification,
    )
    await client.connect()
    try:
        await client.watch_resource("file:///watched.txt")
        await asyncio.wait_for(updated.wait(), timeout=0.5)
        assert client.watched_resources == ("file:///watched.txt",)
        assert seen == ["file:///watched.txt"]
        await client.unwatch_resource("file:///watched.txt")
        assert client.watched_resources == ()
    finally:
        await client.disconnect()


@pytest.mark.asyncio
async def test_runtime_resource_watch_emits_update_event(tmp_path) -> None:
    server = r"""
import json, sys
for line in sys.stdin:
    message = json.loads(line)
    method = message.get("method")
    if method == "server/discover":
        result = {
            "resultType": "complete",
            "supportedVersions": ["2026-07-28"],
            "capabilities": {"resources": {"subscribe": True}},
        }
        print(json.dumps({"jsonrpc": "2.0", "id": message["id"], "result": result}), flush=True)
    elif method == "subscriptions/listen":
        listen_id = message["id"]
        uri = message["params"]["notifications"]["resourceSubscriptions"][0]
        meta = {"io.modelcontextprotocol/subscriptionId": listen_id}
        print(json.dumps({
            "jsonrpc": "2.0",
            "method": "notifications/subscriptions/acknowledged",
            "params": {"notifications": {"resourceSubscriptions": [uri]}, "_meta": meta},
        }), flush=True)
        print(json.dumps({
            "jsonrpc": "2.0",
            "method": "notifications/resources/updated",
            "params": {"uri": uri, "_meta": meta},
        }), flush=True)
    elif method == "notifications/cancelled":
        print(json.dumps({
            "jsonrpc": "2.0",
            "id": message["params"]["requestId"],
            "result": {"resultType": "complete"},
        }), flush=True)
"""
    events: list[dict] = []
    runtime = MCPRuntime(
        {
            "modern": MCPServerConfig(
                name="modern",
                command=sys.executable,
                args=["-u", "-c", server],
                env={},
            )
        },
        SafetyGuard(tmp_path),
        event_sink=events.append,
    )
    await runtime.start()
    try:
        await runtime.watch_resource("modern", "file:///watched.txt")
        for _ in range(50):
            if any(event.get("type") == "mcp.resource.updated" for event in events):
                break
            await asyncio.sleep(0.01)
        assert runtime.resource_watches() == [
            {"server": "modern", "uri": "file:///watched.txt"}
        ]
        assert any(
            event.get("type") == "mcp.resource.updated"
            and event.get("server") == "modern"
            and event.get("uri") == "file:///watched.txt"
            for event in events
        )
        await runtime.unwatch_resource("modern", "file:///watched.txt")
        assert runtime.resource_watches() == []
        assert any(event.get("type") == "mcp.resource.watch_stopped" for event in events)
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_legacy_resource_watch_uses_subscribe_and_unsubscribe() -> None:
    server = r"""
import json, sys
for line in sys.stdin:
    message = json.loads(line)
    method = message.get("method")
    if method == "server/discover":
        print(json.dumps({"jsonrpc": "2.0", "id": message["id"], "error": {"code": -32601, "message": "legacy"}}), flush=True)
    elif method == "initialize":
        result = {
            "protocolVersion": "2025-11-25",
            "capabilities": {"resources": {"subscribe": True}},
            "serverInfo": {"name": "legacy-watch", "version": "1"},
        }
        print(json.dumps({"jsonrpc": "2.0", "id": message["id"], "result": result}), flush=True)
    elif method == "resources/subscribe":
        uri = message["params"]["uri"]
        print(json.dumps({"jsonrpc": "2.0", "id": message["id"], "result": {}}), flush=True)
        print(json.dumps({
            "jsonrpc": "2.0",
            "method": "notifications/resources/updated",
            "params": {"uri": uri},
        }), flush=True)
    elif method == "resources/unsubscribe":
        print(json.dumps({"jsonrpc": "2.0", "id": message["id"], "result": {}}), flush=True)
"""
    updated = asyncio.Event()
    seen: list[str] = []

    async def on_notification(method: str, params: dict) -> None:
        if method == "notifications/resources/updated":
            seen.append(str(params.get("uri")))
            updated.set()

    client = MCPClient(
        MCPServerConfig(
            name="legacy-resource-watch",
            command=sys.executable,
            args=["-u", "-c", server],
            env={},
        ),
        notification_handler=on_notification,
    )
    await client.connect()
    try:
        await client.watch_resource("file:///legacy.txt")
        await asyncio.wait_for(updated.wait(), timeout=0.5)
        assert client.watched_resources == ("file:///legacy.txt",)
        assert seen == ["file:///legacy.txt"]
        await client.unwatch_resource("file:///legacy.txt")
        assert client.watched_resources == ()
    finally:
        await client.disconnect()


@pytest.mark.asyncio
async def test_modern_resource_watch_rejects_mismatched_acknowledgment() -> None:
    server = r"""
import json, sys
for line in sys.stdin:
    message = json.loads(line)
    method = message.get("method")
    if method == "server/discover":
        result = {
            "resultType": "complete",
            "supportedVersions": ["2026-07-28"],
            "capabilities": {"resources": {"subscribe": True}},
        }
        print(json.dumps({"jsonrpc": "2.0", "id": message["id"], "result": result}), flush=True)
    elif method == "subscriptions/listen":
        listen_id = message["id"]
        meta = {"io.modelcontextprotocol/subscriptionId": listen_id}
        print(json.dumps({
            "jsonrpc": "2.0",
            "method": "notifications/subscriptions/acknowledged",
            "params": {
                "notifications": {"resourceSubscriptions": ["file:///other.txt"]},
                "_meta": meta,
            },
        }), flush=True)
"""
    client = MCPClient(
        MCPServerConfig(
            name="modern-resource-watch-bad-ack",
            command=sys.executable,
            args=["-u", "-c", server],
            env={},
        ),
        notification_handler=lambda method, params: None,
    )
    await client.connect()
    try:
        with pytest.raises(MCPProtocolError, match="did not match the requested URI"):
            await client.watch_resource("file:///watched.txt")
        assert client.watched_resources == ()
    finally:
        await client.disconnect()


def test_resource_watch_limit_fails_closed() -> None:
    client = MCPClient(
        MCPServerConfig(name="limit", command=sys.executable, args=["-c", "pass"], env={})
    )
    client._initialized = True
    client.protocol_version = "2026-07-28"
    client.server_capabilities = {"resources": {"subscribe": True}}
    client._watched_resources = {f"file:///{index}.txt" for index in range(32)}

    with pytest.raises(MCPProtocolError, match="resource watch limit reached"):
        asyncio.run(client.watch_resource("file:///overflow.txt"))


@pytest.mark.asyncio
async def test_http_session_recovery_restores_legacy_resource_watch() -> None:
    trace: list[tuple[str, str | None]] = []
    initialize_count = 0
    uri = "file:///watched-after-recovery.txt"

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal initialize_count
        if request.method == "DELETE":
            return httpx.Response(405)
        if request.content.startswith(b'{"jsonrpc":"2.0","id":0'):
            return httpx.Response(
                200, json={"jsonrpc": "2.0", "id": 0, "result": {}}
            )
        payload = json.loads(request.content)
        method = payload["method"]
        session = request.headers.get("Mcp-Session-Id")
        trace.append((method, session))
        if method == "initialize":
            initialize_count += 1
            return httpx.Response(
                200,
                headers={"Mcp-Session-Id": f"session-{initialize_count}"},
                json={
                    "jsonrpc": "2.0",
                    "id": payload["id"],
                    "result": {
                        "protocolVersion": "2025-11-25",
                        "capabilities": {"resources": {"subscribe": True}},
                    },
                },
            )
        if method == "notifications/initialized":
            return httpx.Response(202)
        if method == "resources/subscribe":
            assert payload["params"]["uri"] == uri
            return httpx.Response(
                200,
                json={"jsonrpc": "2.0", "id": payload["id"], "result": {}},
            )
        if method == "resources/read" and session == "session-1":
            return httpx.Response(404)
        if method == "resources/read" and session == "session-2":
            return httpx.Response(
                200,
                json={
                    "jsonrpc": "2.0",
                    "id": payload["id"],
                    "result": {
                        "contents": [{"uri": uri, "text": "restored"}]
                    },
                },
            )
        raise AssertionError((method, session))

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = MCPClient(
        MCPServerConfig(
            name="recover-watch",
            command="",
            args=[],
            env={},
            transport="http",
            url="https://mcp.example.test/rpc",
        ),
        http_client=http,
    )
    await client.connect()
    try:
        await client.watch_resource(uri)
        result = await client.read_resource(uri)
        assert result["contents"][0]["text"] == "restored"
        assert client.watched_resources == (uri,)
        assert client._http_session_id == "session-2"
    finally:
        await client.disconnect()
        await http.aclose()

    assert trace == [
        ("initialize", None),
        ("notifications/initialized", "session-1"),
        ("resources/subscribe", "session-1"),
        ("resources/read", "session-1"),
        ("initialize", None),
        ("notifications/initialized", "session-2"),
        ("resources/subscribe", "session-2"),
        ("resources/read", "session-2"),
    ]


@pytest.mark.asyncio
async def test_http_session_recovery_drops_watch_if_replacement_loses_capability() -> None:
    initialize_count = 0
    uri = "file:///lost-capability.txt"

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal initialize_count
        if request.method == "DELETE":
            return httpx.Response(204)
        if request.content.startswith(b'{"jsonrpc":"2.0","id":0'):
            return httpx.Response(
                200, json={"jsonrpc": "2.0", "id": 0, "result": {}}
            )
        payload = json.loads(request.content)
        method = payload["method"]
        session = request.headers.get("Mcp-Session-Id")
        if method == "initialize":
            initialize_count += 1
            resources = {"subscribe": True} if initialize_count == 1 else {}
            return httpx.Response(
                200,
                headers={"Mcp-Session-Id": f"session-{initialize_count}"},
                json={
                    "jsonrpc": "2.0",
                    "id": payload["id"],
                    "result": {
                        "protocolVersion": "2025-11-25",
                        "capabilities": {"resources": resources},
                    },
                },
            )
        if method == "notifications/initialized":
            return httpx.Response(202)
        if method == "resources/subscribe":
            assert session == "session-1"
            return httpx.Response(
                200,
                json={"jsonrpc": "2.0", "id": payload["id"], "result": {}},
            )
        if method == "resources/read" and session == "session-1":
            return httpx.Response(404)
        raise AssertionError((method, session))

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = MCPClient(
        MCPServerConfig(
            name="recover-watch-lost",
            command="",
            args=[],
            env={},
            transport="http",
            url="https://mcp.example.test/rpc",
        ),
        http_client=http,
    )
    await client.connect()
    try:
        await client.watch_resource(uri)
        with pytest.raises(
            MCPProtocolError,
            match="replacement session no longer supports watched resources",
        ):
            await client.read_resource(uri)
        assert client.watched_resources == ()
        assert client._initialized is False
        assert client._http_session_id == ""
    finally:
        await client.disconnect()
        await http.aclose()


@pytest.mark.asyncio
async def test_deferred_runtime_buffers_resource_updates_until_publication(tmp_path) -> None:
    events: list[dict[str, object]] = []
    runtime = MCPRuntime(
        {"server": MCPServerConfig(name="server", command="fake", args=[], env={})},
        SafetyGuard(tmp_path),
        event_sink=events.append,
        defer_notifications=True,
    )
    uri = "file:///startup-update.txt"
    client = SimpleNamespace(
        server_capabilities={"resources": {"subscribe": True}},
        watched_resources=(uri,),
    )
    runtime.clients["server"] = client
    runtime._started = True

    await runtime._handle_notification(
        "server",
        client,
        "notifications/resources/updated",
        {"uri": uri},
    )
    await runtime._handle_notification(
        "server",
        client,
        "notifications/resources/updated",
        {"uri": uri},
    )

    assert events == []
    assert runtime._startup_resource_updates == {("server", uri)}

    runtime.activate_notifications()

    assert events == [
        {
            "type": "mcp.resource.updated",
            "server": "server",
            "uri": uri,
        }
    ]
    assert runtime._startup_resource_updates == set()


@pytest.mark.asyncio
async def test_runtime_clear_resource_watches_unsubscribes_session_state(tmp_path) -> None:
    events: list[dict[str, object]] = []

    class FakeClient:
        def __init__(self) -> None:
            self._watched = {"file:///a.txt", "file:///b.txt"}

        @property
        def watched_resources(self) -> tuple[str, ...]:
            return tuple(sorted(self._watched))

        async def unwatch_resource(self, uri: str) -> None:
            self._watched.discard(uri)

    runtime = MCPRuntime(
        {"server": MCPServerConfig(name="server", command="fake", args=[], env={})},
        SafetyGuard(tmp_path),
        event_sink=events.append,
    )
    client = FakeClient()
    runtime.clients["server"] = client
    runtime._refresh_locks["server"] = asyncio.Lock()

    await runtime.clear_resource_watches()

    assert runtime.resource_watches() == []
    assert [event["type"] for event in events] == [
        "mcp.resource.watch_stopped",
        "mcp.resource.watch_stopped",
    ]


@pytest.mark.asyncio
async def test_runtime_watch_resource_emits_started_event_only_once(tmp_path) -> None:
    events: list[dict[str, object]] = []

    class FakeClient:
        def __init__(self) -> None:
            self._watched: set[str] = set()

        @property
        def watched_resources(self) -> tuple[str, ...]:
            return tuple(sorted(self._watched))

        async def watch_resource(self, uri: str) -> None:
            self._watched.add(uri)

    runtime = MCPRuntime(
        {"server": MCPServerConfig(name="server", command="fake", args=[], env={})},
        SafetyGuard(tmp_path),
        event_sink=events.append,
    )
    client = FakeClient()
    runtime.clients["server"] = client
    runtime._refresh_locks["server"] = asyncio.Lock()
    uri = "file:///idempotent.txt"

    await runtime.watch_resource("server", uri)
    await runtime.watch_resource("server", uri)

    assert events == [
        {
            "type": "mcp.resource.watch_started",
            "server": "server",
            "uri": uri,
        }
    ]
