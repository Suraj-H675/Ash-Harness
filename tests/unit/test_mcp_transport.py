# tests/unit/test_mcp_transport.py
import asyncio
import json
import sys
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import httpx
import pytest

from ash.mcp import client as mcp_client_module
from ash.mcp.client import (
    MCPClient,
    MCPProtocolError,
)
from ash.mcp.runtime import (
    MCPRuntime,
    MCPTool,
    _extract_mcp_header_annotations,
)
from ash.mcp.server import (
    MCPServerConfig,
)
from ash.safety.guard import SafetyGuard
from ash.sandbox.process_utils import ProcessTreeTerminationError

from .mcp_test_fixtures import DYNAMIC_MCP_SERVER, INTERACTIVE_MCP_SERVER


@pytest.mark.asyncio
async def test_streamable_http_tracks_session_and_parses_sse() -> None:
    seen_session = []
    seen_protocol = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "DELETE":
            seen_session.append(request.headers.get("Mcp-Session-Id"))
            seen_protocol.append(request.headers.get("MCP-Protocol-Version"))
            return httpx.Response(204)
        if request.content.startswith(b'{"jsonrpc":"2.0","id":0'):
            return httpx.Response(
                200,
                json={"jsonrpc": "2.0", "id": 0, "result": {}},
            )
        payload = json.loads(request.content)
        if "id" not in payload:
            return httpx.Response(202)
        if payload["method"] == "initialize":
            body = (
                'event: message\ndata: {"jsonrpc":"2.0","id":1,'
                '"result":{"protocolVersion":"2025-06-18","capabilities":{}}}\n\n'
            )
            return httpx.Response(
                200,
                text=body,
                headers={
                    "content-type": "text/event-stream",
                    "Mcp-Session-Id": "session-1",
                },
            )
        assert request.headers["Mcp-Session-Id"] == "session-1"
        assert request.headers["MCP-Protocol-Version"] == "2025-06-18"
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
    assert await client.list_tools() == []
    await client.disconnect()
    assert seen_session == ["session-1"]
    assert seen_protocol == ["2025-06-18"]
    await http.aclose()


@pytest.mark.asyncio
async def test_mcp_tool_redacts_exact_configured_credential_header_from_output(
    tmp_path: Path,
) -> None:
    secret = "tiny-h"

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["X-Api-Key"] == secret
        if request.content.startswith(b'{"jsonrpc":"2.0","id":0'):
            return httpx.Response(
                200,
                json={"jsonrpc": "2.0", "id": 0, "result": {}},
            )
        payload = json.loads(request.content)
        if "id" not in payload:
            return httpx.Response(202)
        if payload["method"] == "initialize":
            return httpx.Response(
                200,
                json={
                    "jsonrpc": "2.0",
                    "id": payload["id"],
                    "result": {
                        "protocolVersion": "2025-11-25",
                        "capabilities": {"tools": {}},
                    },
                },
            )
        return httpx.Response(
            200,
            json={
                "jsonrpc": "2.0",
                "id": payload["id"],
                "result": {
                    "content": [
                        {"type": "text", "text": f"remote output {secret}"}
                    ]
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
            headers={"X-Api-Key": secret},
        ),
        http_client=http,
    )
    await client.connect()
    try:
        tool = MCPTool(
            SafetyGuard(tmp_path),
            client=client,
            server_name="remote",
            definition={"name": "read", "inputSchema": {"type": "object"}},
        )

        result = await tool.run()

        assert result.success is True
        assert secret not in result.output
        assert "[REDACTED]" in result.output
    finally:
        await client.disconnect()
        await http.aclose()


@pytest.mark.asyncio
async def test_http_post_rejects_oversized_response_before_json_parsing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(mcp_client_module, "MAX_HTTP_RESPONSE_BYTES", 32)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "application/json"},
            content=b"x" * 33,
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
    try:
        with pytest.raises(MCPProtocolError, match="response exceeded 32 bytes"):
            await client._post_http(
                {"jsonrpc": "2.0", "id": 1, "method": "ping"},
                is_initialize=True,
                bypass_session_readiness=True,
            )
    finally:
        await client.disconnect()
        await http.aclose()


@pytest.mark.asyncio
async def test_sse_line_reader_rejects_unterminated_event() -> None:
    response = httpx.Response(200, content=b"data: " + b"x" * 33)

    with pytest.raises(MCPProtocolError, match="SSE event exceeded 32 bytes"):
        async for _line in mcp_client_module._iter_bounded_sse_lines(response, 32):
            pass


@pytest.mark.asyncio
async def test_sse_line_reader_limits_individual_events_not_coalesced_chunks() -> None:
    payload = b"data: one\n\ndata: two\n\ndata: three\n\n"
    assert len(payload) > 16
    response = httpx.Response(200, stream=httpx.ByteStream(payload))

    lines = [
        line
        async for line in mcp_client_module._iter_bounded_sse_lines(response, 16)
    ]

    assert lines == ["data: one", "", "data: two", "", "data: three", ""]


def test_http_sse_parser_rejects_oversized_event(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(mcp_client_module, "MAX_HTTP_RESPONSE_BYTES", 128)
    monkeypatch.setattr(mcp_client_module, "MAX_HTTP_SSE_EVENT_BYTES", 32)
    response = httpx.Response(
        200,
        headers={"content-type": "text/event-stream"},
        content=b"data: " + b"x" * 33 + b"\n\n",
    )

    with pytest.raises(MCPProtocolError, match="SSE event exceeded 32 bytes"):
        mcp_client_module._parse_http_messages(response)


@pytest.mark.parametrize(
    ("content_type", "body"),
    [
        (
            "application/json",
            b'{"jsonrpc":"2.0","id":1,"result":{},"result":{"x":1}}',
        ),
        (
            "text/event-stream",
            b'data: {"jsonrpc":"2.0","id":1,"result":{},"result":{"x":1}}\n\n',
        ),
    ],
)
def test_http_parsers_reject_duplicate_json_keys(content_type: str, body: bytes) -> None:
    response = httpx.Response(
        200,
        headers={"content-type": content_type},
        content=body,
    )

    with pytest.raises(MCPProtocolError, match="invalid JSON"):
        mcp_client_module._parse_http_messages(response)


@pytest.mark.asyncio
async def test_http_get_stream_dispatches_events_and_honors_405() -> None:
    requests: list[httpx.Request] = []
    mode = {"get": True}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            requests.append(request)
            if not mode["get"]:
                return httpx.Response(405)
            body = (
                "retry: 5\n"
                'id: event-1\ndata: {"jsonrpc":"2.0","method":"notifications/message",'
                '"params":{"level":"info","data":"ready"}}\n'
                "\n"
            )
            return httpx.Response(
                200,
                text=body,
                headers={"content-type": "text/event-stream"},
            )
        if request.method == "DELETE":
            return httpx.Response(204)
        payload = json.loads(request.content)
        if "id" not in payload:
            return httpx.Response(202)
        if payload["method"] == "initialize":
            return httpx.Response(
                200,
                headers={"Mcp-Session-Id": "stream-session"},
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
            json={"jsonrpc": "2.0", "id": payload["id"], "result": []},
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
    notifications = []
    client.notification_handler = lambda method, params: notifications.append(method)
    await client.connect()
    await asyncio.sleep(0.01)
    assert notifications.count("notifications/message") >= 1
    assert requests[0].headers["Accept"] == "text/event-stream"
    assert requests[0].headers["Mcp-Session-Id"] == "stream-session"
    assert requests[0].headers["MCP-Protocol-Version"] == "2025-06-18"
    assert "Last-Event-ID" not in requests[0].headers

    mode["get"] = False
    client._sse_supported = True
    client._sse_generation += 1
    client._sse_task = asyncio.create_task(client._read_http_events())
    for _ in range(20):
        await asyncio.sleep(0.01)
        if client._sse_supported is False:
            break
    assert requests[-1].headers.get("Last-Event-ID") == "event-1"
    assert client._sse_supported is False

    await client.disconnect()
    await http.aclose()


@pytest.mark.asyncio
async def test_http_get_stream_handles_huge_valid_retry_without_overflow(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    retry = "9" * 400

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            text=f"retry: {retry}\n\n",
            headers={"content-type": "text/event-stream"},
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
    sleeps: list[float] = []

    async def fake_sleep(delay: float) -> None:
        sleeps.append(delay)
        client._initialized = False

    monkeypatch.setattr(mcp_client_module.asyncio, "sleep", fake_sleep)
    client._initialized = True
    await client._read_http_events()

    assert client._sse_retry_ms == int(retry)
    assert sleeps == [mcp_client_module.MAX_SSE_RETRY_SLEEP_SLICE_MS / 1000]
    await http.aclose()


@pytest.mark.asyncio
async def test_http_recovers_expired_session_without_replaying_tool_call() -> None:
    trace: list[tuple[str, str | None, int | None]] = []
    initialize_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal initialize_count
        if request.method == "DELETE":
            return httpx.Response(405)
        if request.content.startswith(b'{"jsonrpc":"2.0","id":0'):
            return httpx.Response(
                200,
                json={"jsonrpc": "2.0", "id": 0, "result": {}},
            )
        payload = json.loads(request.content)
        method = payload["method"]
        session = request.headers.get("Mcp-Session-Id")
        trace.append((method, session, payload.get("id")))
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
                        "capabilities": {"tools": {}},
                    },
                },
            )
        if method == "notifications/initialized":
            return httpx.Response(202)
        if session == "session-1":
            return httpx.Response(404)
        return httpx.Response(
            200,
            headers={"Mcp-Session-Id": "must-not-replace-session-2"},
            json={
                "jsonrpc": "2.0",
                "id": payload["id"],
                "result": {"content": [{"type": "text", "text": "ok"}]},
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
    try:
        with pytest.raises(MCPProtocolError, match="operation was not replayed"):
            await client.call_tool("echo", {})
        assert client._http_session_id == "session-2"
    finally:
        await client.disconnect()
        await http.aclose()

    calls = [item for item in trace if item[0] == "tools/call"]
    assert [(method, session) for method, session, _ in trace] == [
        ("initialize", None),
        ("notifications/initialized", "session-1"),
        ("tools/call", "session-1"),
        ("initialize", None),
        ("notifications/initialized", "session-2"),
    ]
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_http_concurrent_expiry_uses_one_recovery_handshake() -> None:
    initialize_count = 0
    old_calls = 0
    both_old_calls = asyncio.Event()

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal initialize_count, old_calls
        if request.method == "DELETE":
            return httpx.Response(405)
        payload = json.loads(request.content)
        method = payload["method"]
        session = request.headers.get("Mcp-Session-Id")
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
                        "capabilities": {"tools": {}},
                    },
                },
            )
        if method == "notifications/initialized":
            return httpx.Response(202)
        if session == "session-1":
            old_calls += 1
            if old_calls == 2:
                both_old_calls.set()
            await asyncio.wait_for(both_old_calls.wait(), timeout=1)
            return httpx.Response(404)
        return httpx.Response(
            200,
            json={"jsonrpc": "2.0", "id": payload["id"], "result": {}},
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
    try:
        with pytest.raises(MCPProtocolError, match="operation was not replayed"):
            await asyncio.gather(
                client.call_tool("first", {}), client.call_tool("second", {})
            )
        assert initialize_count == 2
    finally:
        await client.disconnect()
        await http.aclose()


@pytest.mark.asyncio
async def test_http_session_404_recovers_without_replaying_tool_attempt() -> None:
    initialize_count = 0
    tool_attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal initialize_count, tool_attempts
        if request.method == "DELETE":
            return httpx.Response(405)
        if request.content.startswith(b'{"jsonrpc":"2.0","id":0'):
            return httpx.Response(
                200,
                json={"jsonrpc": "2.0", "id": 0, "result": {}},
            )
        payload = json.loads(request.content)
        if payload["method"] == "initialize":
            initialize_count += 1
            return httpx.Response(
                200,
                headers={"Mcp-Session-Id": f"session-{initialize_count}"},
                json={
                    "jsonrpc": "2.0",
                    "id": payload["id"],
                    "result": {
                        "protocolVersion": "2025-11-25",
                        "capabilities": {"tools": {}},
                    },
                },
            )
        if payload["method"] == "notifications/initialized":
            return httpx.Response(202)
        tool_attempts += 1
        return httpx.Response(404)

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
    try:
        with pytest.raises(MCPProtocolError, match="operation was not replayed"):
            await client.call_tool("write", {})
        assert tool_attempts == 1
        assert initialize_count == 2
        assert client._http_session_id == "session-2"
    finally:
        await client.disconnect()
        await http.aclose()


@pytest.mark.asyncio
async def test_http_rejects_invalid_initialize_session_id() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if not request.content:
            return httpx.Response(204)
        if request.content.startswith(b'{"jsonrpc":"2.0","id":0'):
            return httpx.Response(
                200,
                json={"jsonrpc": "2.0", "id": 0, "result": {}},
            )
        payload = json.loads(request.content)
        return httpx.Response(
            200,
            headers={"Mcp-Session-Id": b"not-visible-\xff"},
            json={
                "jsonrpc": "2.0",
                "id": payload["id"],
                "result": {
                    "protocolVersion": "2025-11-25",
                    "capabilities": {},
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
    with pytest.raises(MCPProtocolError, match="visible ASCII"):
        await client.connect()
    await http.aclose()


@pytest.mark.asyncio
async def test_http_malformed_sse_never_replays_tool_call() -> None:
    tool_attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal tool_attempts
        if request.method == "DELETE":
            return httpx.Response(405)
        if request.content.startswith(b'{"jsonrpc":"2.0","id":0'):
            return httpx.Response(
                200,
                json={"jsonrpc": "2.0", "id": 0, "result": {}},
            )
        payload = json.loads(request.content)
        if payload["method"] == "initialize":
            return httpx.Response(
                200,
                json={
                    "jsonrpc": "2.0",
                    "id": payload["id"],
                    "result": {
                        "protocolVersion": "2025-11-25",
                        "capabilities": {"tools": {}},
                    },
                },
            )
        if payload["method"] == "notifications/initialized":
            return httpx.Response(202)
        tool_attempts += 1
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            text="event: message\ndata: {not-json}\n\n",
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
    try:
        with pytest.raises(MCPProtocolError, match="SSE event contained invalid JSON"):
            await client.call_tool("write", {})
        assert tool_attempts == 1
    finally:
        await client.disconnect()
        await http.aclose()


@pytest.mark.asyncio
async def test_concurrent_http_connect_initializes_once() -> None:
    initialize_count = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal initialize_count
        if request.method == "DELETE":
            return httpx.Response(405)
        if request.content.startswith(b'{"jsonrpc":"2.0","id":0'):
            return httpx.Response(
                200,
                json={"jsonrpc": "2.0", "id": 0, "result": {}},
            )
        payload = json.loads(request.content)
        if payload["method"] == "initialize":
            initialize_count += 1
            await asyncio.sleep(0)
            return httpx.Response(
                200,
                json={
                    "jsonrpc": "2.0",
                    "id": payload["id"],
                    "result": {
                        "protocolVersion": "2025-11-25",
                        "capabilities": {},
                    },
                },
            )
        return httpx.Response(202)

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
    await asyncio.gather(client.connect(), client.connect())
    try:
        assert initialize_count == 1
    finally:
        await client.disconnect()
        await http.aclose()


@pytest.mark.asyncio
async def test_paginated_list_restarts_after_session_recovery() -> None:
    initialize_count = 0
    cursors: list[tuple[str, str | None]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal initialize_count
        if request.method == "DELETE":
            return httpx.Response(405)
        if request.content.startswith(b'{"jsonrpc":"2.0","id":0'):
            return httpx.Response(
                200,
                json={"jsonrpc": "2.0", "id": 0, "result": {}},
            )
        payload = json.loads(request.content)
        method = payload["method"]
        session = request.headers.get("Mcp-Session-Id")
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
                        "capabilities": {"tools": {}},
                    },
                },
            )
        if method == "notifications/initialized":
            return httpx.Response(202)
        cursor = payload.get("params", {}).get("cursor")
        cursors.append((session or "", cursor))
        if session == "session-1" and cursor == "page-2":
            return httpx.Response(404)
        prefix = "old" if session == "session-1" else "new"
        result = (
            {
                "tools": [{"name": f"{prefix}-first"}],
                "nextCursor": "page-2",
            }
            if cursor is None
            else {"tools": [{"name": f"{prefix}-second"}]}
        )
        return httpx.Response(
            200,
            json={"jsonrpc": "2.0", "id": payload["id"], "result": result},
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
    try:
        tools = await client.list_tools()
        assert [tool["name"] for tool in tools] == ["new-first", "new-second"]
        assert cursors == [
            ("session-1", None),
            ("session-1", "page-2"),
            ("session-2", None),
            ("session-2", "page-2"),
        ]
    finally:
        await client.disconnect()
        await http.aclose()


@pytest.mark.asyncio
async def test_list_rejects_non_object_catalog_entries() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if not request.content:
            return httpx.Response(204)
        if request.content.startswith(b'{"jsonrpc":"2.0","id":0'):
            return httpx.Response(
                200,
                json={"jsonrpc": "2.0", "id": 0, "result": {}},
            )
        payload = json.loads(request.content)
        if payload["method"] == "initialize":
            result = {
                "protocolVersion": "2025-11-25",
                "capabilities": {"tools": {}},
            }
        elif payload["method"] == "notifications/initialized":
            return httpx.Response(202)
        else:
            result = {"tools": [{"name": "valid"}, "invalid"]}
        return httpx.Response(
            200,
            json={"jsonrpc": "2.0", "id": payload["id"], "result": result},
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
    try:
        with pytest.raises(MCPProtocolError, match="non-object tools entry"):
            await client.list_tools()
    finally:
        await client.disconnect()
        await http.aclose()


@pytest.mark.asyncio
async def test_list_tools_bounds_catalog_entry_count() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if not request.content:
            return httpx.Response(204)
        payload = json.loads(request.content)
        if payload["method"] == "initialize":
            result = {
                "protocolVersion": "2025-11-25",
                "capabilities": {"tools": {}},
            }
        elif payload["method"] == "notifications/initialized":
            return httpx.Response(202)
        else:
            result = {
                "tools": [
                    {"name": f"tool-{index}"}
                    for index in range(
                        mcp_client_module.MAX_MCP_TOOL_DEFINITIONS + 1
                    )
                ]
            }
        return httpx.Response(
            200,
            json={"jsonrpc": "2.0", "id": payload["id"], "result": result},
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
    try:
        with pytest.raises(MCPProtocolError, match="more than 256 entries"):
            await client.list_tools()
    finally:
        await client.disconnect()
        await http.aclose()


@pytest.mark.asyncio
async def test_failed_initialize_deletes_pending_server_session() -> None:
    deleted_sessions: list[str | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "DELETE":
            deleted_sessions.append(request.headers.get("Mcp-Session-Id"))
            return httpx.Response(204)
        if not request.content:
            return httpx.Response(204)
        payload = json.loads(request.content)
        return httpx.Response(
            200,
            headers={"Mcp-Session-Id": "allocated-session"},
            json={
                "jsonrpc": "2.0",
                "id": payload["id"],
                "result": {"protocolVersion": "unsupported", "capabilities": {}},
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
    with pytest.raises(MCPProtocolError, match="unsupported protocol version"):
        await client.connect()
    assert deleted_sessions == ["allocated-session"]
    await http.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("capability", [None, False, []])
async def test_initialize_rejects_non_object_capabilities(capability: object) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method != "POST":
            return httpx.Response(204)
        payload = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "jsonrpc": "2.0",
                "id": payload["id"],
                "result": {
                    "protocolVersion": "2025-11-25",
                    "capabilities": {"tools": capability},
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
        MCPProtocolError, match="capabilities must contain objects: tools"
    ):
        await client.connect()
    await http.aclose()


@pytest.mark.asyncio
async def test_failed_replacement_initialize_deletes_allocated_session() -> None:
    initialize_count = 0
    deleted_sessions: list[str | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal initialize_count
        if request.method == "DELETE":
            deleted_sessions.append(request.headers.get("Mcp-Session-Id"))
            return httpx.Response(204)
        if request.content.startswith(b'{"jsonrpc":"2.0","id":0'):
            return httpx.Response(
                200,
                json={"jsonrpc": "2.0", "id": 0, "result": {}},
            )
        payload = json.loads(request.content)
        method = payload["method"]
        session = request.headers.get("Mcp-Session-Id")
        if method == "initialize":
            initialize_count += 1
            capabilities = {"tools": None} if initialize_count == 2 else {"tools": {}}
            return httpx.Response(
                200,
                headers={"Mcp-Session-Id": f"session-{initialize_count}"},
                json={
                    "jsonrpc": "2.0",
                    "id": payload["id"],
                    "result": {
                        "protocolVersion": "2025-11-25",
                        "capabilities": capabilities,
                    },
                },
            )
        if method == "notifications/initialized":
            return httpx.Response(202)
        if session == "session-1":
            return httpx.Response(404)
        return httpx.Response(
            200,
            json={
                "jsonrpc": "2.0",
                "id": payload["id"],
                "result": {"content": []},
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
    try:
        with pytest.raises(
            MCPProtocolError, match="capabilities must contain objects: tools"
        ):
            await client.call_tool("echo", {})
        assert deleted_sessions == ["session-2"]

        assert await client.call_tool("echo", {}) == {"content": []}
        assert initialize_count == 3
    finally:
        await client.disconnect()
        await http.aclose()
    assert deleted_sessions == ["session-2", "session-3"]


@pytest.mark.asyncio
async def test_cancelled_session_recovery_restores_readiness() -> None:
    initialize_count = 0
    replacement_initialize_started = asyncio.Event()

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal initialize_count
        if request.method == "DELETE":
            return httpx.Response(204)
        payload = json.loads(request.content)
        method = payload["method"]
        session = request.headers.get("Mcp-Session-Id")
        if method == "initialize":
            initialize_count += 1
            if initialize_count == 2:
                replacement_initialize_started.set()
                await asyncio.Event().wait()
            return httpx.Response(
                200,
                headers={"Mcp-Session-Id": f"session-{initialize_count}"},
                json={
                    "jsonrpc": "2.0",
                    "id": payload["id"],
                    "result": {
                        "protocolVersion": "2025-11-25",
                        "capabilities": {"tools": {}},
                    },
                },
            )
        if method == "notifications/initialized":
            return httpx.Response(202)
        if session == "session-1":
            return httpx.Response(404)
        return httpx.Response(
            200,
            json={
                "jsonrpc": "2.0",
                "id": payload["id"],
                "result": {"content": []},
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
    task = asyncio.create_task(client.call_tool("echo", {}))
    try:
        await asyncio.wait_for(replacement_initialize_started.wait(), timeout=1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=1)
        assert client._session_ready.is_set()

        assert await client.call_tool("echo", {}) == {"content": []}
        assert initialize_count == 3
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await client.disconnect()
        await http.aclose()


@pytest.mark.asyncio
async def test_cancelled_http_recovery_owns_one_session_delete_until_settled() -> None:
    client = MCPClient(
        MCPServerConfig(
            name="remote",
            command="",
            args=[],
            env={},
            transport="http",
            url="https://mcp.example.test/rpc",
        ),
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(204))),
    )
    initialize_started = asyncio.Event()
    delete_started = asyncio.Event()
    delete_release = asyncio.Event()
    delete_finished = asyncio.Event()
    delete_calls = 0

    async def blocked_initialize() -> None:
        client._http_session_id = "recovery-session"
        initialize_started.set()
        await asyncio.Event().wait()

    async def blocked_delete(session_id: str) -> None:
        nonlocal delete_calls
        assert session_id == "recovery-session"
        delete_calls += 1
        delete_started.set()
        await delete_release.wait()
        delete_finished.set()

    client._initialize_protocol = blocked_initialize  # type: ignore[method-assign]
    client._delete_http_session = blocked_delete  # type: ignore[method-assign]
    recovery = asyncio.create_task(
        client._recover_http_session(mcp_client_module.MCPSessionExpired("old", 0))
    )
    await initialize_started.wait()
    recovery.cancel()
    await delete_started.wait()
    recovery.cancel()
    await asyncio.sleep(0)
    assert not recovery.done()
    assert delete_calls == 1
    assert not delete_finished.is_set()

    delete_release.set()
    with pytest.raises(asyncio.CancelledError):
        await recovery
    assert delete_finished.is_set()
    assert client._session_ready.is_set()
    await client._http.aclose()  # type: ignore[union-attr]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("response_factory", "message"),
    [
        (
            lambda request_id: httpx.Response(
                200,
                json=[{"jsonrpc": "2.0", "id": request_id, "result": {}}],
            ),
            "must contain one JSON-RPC object",
        ),
        (
            lambda request_id: httpx.Response(
                200,
                json={"jsonrpc": "2.0", "id": True, "result": {}},
            ),
            "id must be a string or integer",
        ),
        (
            lambda request_id: httpx.Response(
                200,
                text='{"jsonrpc":"2.0","id":2,"result":{}}',
                headers={"content-type": "text/plain"},
            ),
            "must use application/json or text/event-stream",
        ),
        (
            lambda request_id: httpx.Response(
                200,
                text='{"jsonrpc":"2.0","id":2,"result":{}}',
                headers={"content-type": "application/jsonp"},
            ),
            "must use application/json or text/event-stream",
        ),
        (
            lambda request_id: httpx.Response(
                200,
                text='data: {"jsonrpc":"2.0","id":2,"result":{}}\n\n',
                headers={"content-type": "text/event-stream-evil"},
            ),
            "must use application/json or text/event-stream",
        ),
    ],
)
async def test_http_rejects_invalid_jsonrpc_envelopes(
    response_factory, message: str
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.content.startswith(b'{"jsonrpc":"2.0","id":0'):
            return httpx.Response(
                200,
                json={"jsonrpc": "2.0", "id": 0, "result": {}},
            )
        payload = json.loads(request.content)
        if payload["method"] == "initialize":
            return httpx.Response(
                200,
                json={
                    "jsonrpc": "2.0",
                    "id": payload["id"],
                    "result": {
                        "protocolVersion": "2025-11-25",
                        "capabilities": {"tools": {}},
                    },
                },
            )
        if payload["method"] == "notifications/initialized":
            return httpx.Response(202)
        return response_factory(payload["id"])

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
    try:
        with pytest.raises(MCPProtocolError, match=message):
            await client.call_tool("echo", {})
    finally:
        await client.disconnect()
        await http.aclose()




@pytest.mark.asyncio
async def test_stdio_dispatches_server_requests_and_notifications(
    tmp_path: Path,
) -> None:
    notifications: list[tuple[str, dict]] = []
    client = MCPClient(
        MCPServerConfig(
            name="interactive",
            command=sys.executable,
            args=["-u", "-c", INTERACTIVE_MCP_SERVER],
            env={},
        ),
        roots=(tmp_path,),
        notification_handler=lambda method, params: notifications.append(
            (method, params)
        ),
    )

    await client.connect()
    try:
        instructions = client.server_instructions
        tools = await client.list_tools()
        for _ in range(20):
            if notifications:
                break
            await asyncio.sleep(0.01)
    finally:
        await client.disconnect()

    assert instructions == "server guidance"
    assert tools[0]["description"] == tmp_path.resolve().as_uri()
    assert notifications == [
        ("notifications/message", {"level": "info", "data": "ready"})
    ]


@pytest.mark.asyncio
async def test_http_tool_listing_follows_pagination() -> None:
    cursors: list[str | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        method = payload.get("method")
        if method == "initialize":
            return httpx.Response(
                200,
                json={
                    "jsonrpc": "2.0",
                    "id": payload["id"],
                    "result": {
                        "protocolVersion": "2025-11-25",
                        "capabilities": {"tools": {}},
                    },
                },
            )
        if "id" not in payload:
            return httpx.Response(202)
        cursor = payload.get("params", {}).get("cursor")
        cursors.append(cursor)
        result = (
            {"tools": [{"name": "first"}], "nextCursor": "page-2"}
            if cursor is None
            else {"tools": [{"name": "second"}]}
        )
        return httpx.Response(
            200,
            json={"jsonrpc": "2.0", "id": payload["id"], "result": result},
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
    try:
        tools = await client.list_tools()
    finally:
        await client.disconnect()
        await http.aclose()

    assert [tool["name"] for tool in tools] == ["first", "second"]
    assert cursors == [None, None, "page-2"]


@pytest.mark.asyncio
async def test_mcp_paginated_catalog_rejects_oversized_aggregate_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = MCPClient(
        MCPServerConfig(name="fake", command="fake", args=[], env={}),
    )
    client.request = AsyncMock(  # type: ignore[method-assign]
        side_effect=[
            {"resources": [{"uri": "file:///first"}], "nextCursor": "page-two"},
            {"resources": [{"uri": "file:///second"}]},
        ]
    )
    monkeypatch.setattr(mcp_client_module, "MAX_PAGINATION_RESULT_BYTES", 49)

    with pytest.raises(MCPProtocolError, match="aggregate result exceeds"):
        await client.list_resources()


@pytest.mark.asyncio
async def test_request_timeout_sends_cancellation_notification() -> None:
    client = MCPClient(MCPServerConfig(name="fake", command="fake", args=[], env={}))
    client._request_stdio = AsyncMock(side_effect=asyncio.TimeoutError())
    client.notify = AsyncMock()

    with pytest.raises(asyncio.TimeoutError):
        await client.request("tools/call", {"name": "slow"})

    client.notify.assert_awaited_once_with(
        "notifications/cancelled",
        {"requestId": 1, "reason": "tools/call timed out"},
        _allow_session_recovery=False,
    )


@pytest.mark.asyncio
async def test_stdio_send_failure_cleans_pending_future() -> None:
    client = MCPClient(MCPServerConfig(name="fake", command="fake", args=[], env={}))
    client._process = Mock(stdin=object())
    client._send_message = AsyncMock(side_effect=BrokenPipeError("closed"))

    with pytest.raises(BrokenPipeError):
        await client._request_stdio(9, {"jsonrpc": "2.0", "id": 9})

    assert client._pending == {}


@pytest.mark.asyncio
async def test_mcp_rejects_oversized_outbound_message_before_writing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import ash.mcp.client as mcp_client

    monkeypatch.setattr(mcp_client, "MAX_OUTBOUND_MESSAGE_BYTES", 64)
    stdin = Mock()
    stdin.drain = AsyncMock()
    client = MCPClient(MCPServerConfig(name="fake", command="fake", args=[], env={}))
    client._process = Mock(stdin=stdin)

    with pytest.raises(MCPProtocolError, match="outbound message exceeds 64 bytes"):
        await client._send_message(
            {
                "jsonrpc": "2.0",
                "method": "notifications/message",
                "params": {"value": "x" * 100},
            }
        )

    stdin.write.assert_not_called()
    stdin.drain.assert_not_awaited()


@pytest.mark.asyncio
async def test_stdio_revalidates_tool_contract_inside_write_lock() -> None:
    client = MCPClient(MCPServerConfig(name="fake", command="fake", args=[], env={}))
    stdin = Mock()
    client._process = Mock(stdin=stdin)
    client._session_generation = 1
    catalog_valid = True
    client.tool_contract_validator = lambda name, fingerprint, generation: catalog_valid
    await client._write_lock.acquire()
    task = asyncio.create_task(
        client.call_tool("echo", {}, expected_contract="fingerprint")
    )
    try:
        await asyncio.sleep(0)
        catalog_valid = False
        client._write_lock.release()
        with pytest.raises(MCPProtocolError, match="active verified server contract"):
            await asyncio.wait_for(task, timeout=1)
        stdin.write.assert_not_called()
    finally:
        if client._write_lock.locked():
            client._write_lock.release()
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_stdio_reader_fails_pending_request_on_framing_error() -> None:
    class BrokenReader:
        async def readline(self) -> bytes:
            raise ValueError("line exceeds configured limit")

    client = MCPClient(MCPServerConfig(name="fake", command="fake", args=[], env={}))
    client._process = Mock(stdout=BrokenReader())
    future = asyncio.get_running_loop().create_future()
    client._pending[3] = future

    await client._read_stdio()

    with pytest.raises(MCPProtocolError, match="invalid stdio framing"):
        await future
    assert client._pending == {}


@pytest.mark.asyncio
async def test_stdio_reader_fails_pending_request_on_invalid_utf8() -> None:
    class InvalidUtf8Reader:
        async def readline(self) -> bytes:
            return b"\xff\n"

    client = MCPClient(MCPServerConfig(name="fake", command="fake", args=[], env={}))
    client._process = Mock(stdout=InvalidUtf8Reader())
    future = asyncio.get_running_loop().create_future()
    client._pending[3] = future

    await client._read_stdio()

    with pytest.raises(MCPProtocolError, match="invalid JSON"):
        await future
    assert client._pending == {}


@pytest.mark.asyncio
async def test_stdio_reader_fails_pending_request_on_duplicate_json_keys() -> None:
    class DuplicateKeyReader:
        sent = False

        async def readline(self) -> bytes:
            if self.sent:
                return b""
            self.sent = True
            return b'{"jsonrpc":"2.0","id":3,"result":{},"result":{"x":1}}\n'

    client = MCPClient(MCPServerConfig(name="fake", command="fake", args=[], env={}))
    client._process = Mock(stdout=DuplicateKeyReader())
    future = asyncio.get_running_loop().create_future()
    client._pending[3] = future

    await client._read_stdio()

    with pytest.raises(MCPProtocolError, match="duplicate JSON object key"):
        await future
    assert client._pending == {}


@pytest.mark.asyncio
async def test_cancelled_stdio_connect_cleans_process_and_reader_tasks() -> None:
    client = MCPClient(
        MCPServerConfig(
            name="blocked",
            command=sys.executable,
            args=["-u", "-c", "import time; time.sleep(60)"],
            env={},
        )
    )
    task = asyncio.create_task(client.connect())
    for _ in range(100):
        if client._process is not None and client._reader_task is not None:
            break
        await asyncio.sleep(0.01)
    process = client._process
    assert process is not None

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=2)

    assert client._process is None
    assert client._reader_task is None
    assert client._stderr_task is None
    assert process.returncode is not None


@pytest.mark.asyncio
async def test_cancelled_legacy_sse_connect_waits_for_reader_cleanup() -> None:
    client = MCPClient(
        MCPServerConfig(
            name="legacy-blocked",
            command="",
            args=[],
            env={},
            transport="sse",
            url="https://legacy.example.test/sse",
        )
    )
    started = asyncio.Event()
    cancellation_seen = asyncio.Event()
    release = asyncio.Event()

    async def blocked_reader(generation: int) -> None:
        del generation
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancellation_seen.set()
            await release.wait()

    client._read_legacy_sse_events = blocked_reader  # type: ignore[method-assign]
    task = asyncio.create_task(client.connect())
    await asyncio.wait_for(started.wait(), timeout=1)
    owned_http = client._http
    assert owned_http is not None

    task.cancel()
    await asyncio.wait_for(cancellation_seen.wait(), timeout=1)
    await asyncio.sleep(0)
    assert not task.done()

    release.set()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=1)

    assert client._sse_task is None
    assert client._legacy_sse_discovery is None
    assert client._http is None
    assert owned_http.is_closed


@pytest.mark.asyncio
async def test_cancelled_mcp_disconnect_waits_for_process_cleanup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = MCPClient(MCPServerConfig(name="blocked", command="server", args=[], env={}))
    process = Mock()
    client._process = process
    cleanup_started = asyncio.Event()
    cleanup_finished = asyncio.Event()
    release_cleanup = asyncio.Event()

    async def cleanup(*args: object, **kwargs: object) -> None:
        cleanup_started.set()
        await release_cleanup.wait()
        cleanup_finished.set()

    monkeypatch.setattr(mcp_client_module, "terminate_process_tree", cleanup)
    task = asyncio.create_task(client.disconnect())
    await asyncio.wait_for(cleanup_started.wait(), timeout=1)

    task.cancel()
    await asyncio.sleep(0)
    assert not cleanup_finished.is_set()

    release_cleanup.set()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=1)

    assert cleanup_finished.is_set()
    assert client._process is None
    assert client._reader_task is None
    assert client._stderr_task is None


@pytest.mark.asyncio
async def test_mcp_disconnect_retains_process_when_tree_cleanup_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = MCPClient(MCPServerConfig(name="broken", command="server", args=[], env={}))
    process = Mock()
    client._process = process
    monkeypatch.setattr(
        mcp_client_module,
        "terminate_process_tree",
        AsyncMock(side_effect=ProcessTreeTerminationError("unconfirmed")),
    )

    with pytest.raises(MCPProtocolError, match="process cleanup failed"):
        await client.disconnect()

    assert client._process is process
    assert client._disconnect_cleanup_task is None


@pytest.mark.asyncio
async def test_cancelled_runtime_start_cleans_initialized_clients(
    tmp_path: Path,
) -> None:
    blocked_server = r"""
import json, sys, time
for line in sys.stdin:
    message = json.loads(line)
    method = message.get("method")
    if method == "server/discover":
        print(json.dumps({"jsonrpc": "2.0", "id": message["id"], "error": {"code": -32601, "message": "legacy"}}), flush=True)
        continue
    if method == "initialize":
        result = {
            "protocolVersion": "2025-11-25",
            "capabilities": {"tools": {}},
        }
        print(json.dumps({"jsonrpc": "2.0", "id": message["id"], "result": result}), flush=True)
    elif method == "tools/list":
        time.sleep(60)
"""
    runtime = MCPRuntime(
        {
            "blocked": MCPServerConfig(
                name="blocked",
                command=sys.executable,
                args=["-u", "-c", blocked_server],
                env={},
            )
        },
        SafetyGuard(tmp_path),
    )
    task = asyncio.create_task(runtime.start())
    client = None
    for _ in range(200):
        client = runtime.clients.get("blocked")
        if client is not None and client._initialized and client._pending:
            break
        await asyncio.sleep(0.01)
    assert client is not None and client._process is not None
    process = client._process

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=2)

    assert runtime.clients == {}
    assert client._process is None
    assert client._reader_task is None
    assert client._stderr_task is None
    assert process.returncode is not None


@pytest.mark.asyncio
async def test_initialize_timeout_does_not_send_cancellation_notification() -> None:
    client = MCPClient(MCPServerConfig(name="fake", command="fake", args=[], env={}))
    client._request_stdio = AsyncMock(side_effect=asyncio.TimeoutError())
    client.notify = AsyncMock()

    with pytest.raises(asyncio.TimeoutError):
        await client.request("initialize")

    client.notify.assert_not_awaited()


@pytest.mark.asyncio
async def test_sessionless_http_timeout_sends_cancellation_without_reinitialize() -> (
    None
):
    trace: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if not request.content:
            return httpx.Response(204)
        payload = json.loads(request.content)
        method = payload["method"]
        trace.append(method)
        if method == "initialize":
            return httpx.Response(
                200,
                json={
                    "jsonrpc": "2.0",
                    "id": payload["id"],
                    "result": {
                        "protocolVersion": "2025-11-25",
                        "capabilities": {"tools": {}},
                    },
                },
            )
        if method == "notifications/initialized":
            return httpx.Response(202)
        if method == "notifications/cancelled":
            assert request.headers.get("Mcp-Session-Id") is None
            return httpx.Response(202)
        raise httpx.ReadTimeout("timed out", request=request)

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
    try:
        with pytest.raises(httpx.ReadTimeout):
            await client.call_tool("slow", {})
            assert trace == [
                "ping",
                "initialize",
                "notifications/initialized",
                "tools/call",
                "notifications/cancelled",
            ]
    finally:
        await client.disconnect()
        await http.aclose()


@pytest.mark.asyncio
async def test_server_cancellation_stops_incoming_request_without_response() -> None:
    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def handle_request(method: str, params: dict) -> dict:
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()
        return {}

    client = MCPClient(
        MCPServerConfig(name="fake", command="fake", args=[], env={}),
        server_request_handler=handle_request,
    )
    client._send_message = AsyncMock()
    client._dispatch_incoming(
        {"jsonrpc": "2.0", "id": "server-1", "method": "custom", "params": {}}
    )
    await asyncio.wait_for(started.wait(), timeout=1)

    client._dispatch_incoming(
        {
            "jsonrpc": "2.0",
            "method": "notifications/cancelled",
            "params": {"requestId": "server-1", "reason": "no longer needed"},
        }
    )
    await asyncio.wait_for(cancelled.wait(), timeout=1)
    await asyncio.sleep(0)

    client._send_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_mcp_bounds_pending_notification_handler_tasks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gate = asyncio.Event()
    started = 0

    async def handle_notification(method: str, params: dict) -> None:
        nonlocal started
        started += 1
        await gate.wait()

    monkeypatch.setattr(mcp_client_module, "MAX_PENDING_MCP_NOTIFICATIONS", 2)
    client = MCPClient(
        MCPServerConfig(name="fake", command="fake", args=[], env={}),
        notification_handler=handle_notification,
    )
    message = {
        "jsonrpc": "2.0",
        "method": "notifications/message",
        "params": {"level": "info", "data": "ready"},
    }

    for _ in range(10):
        client._dispatch_incoming(message)
    await asyncio.sleep(0)

    assert started == 2
    assert len(client._server_tasks) == 2
    gate.set()
    await asyncio.gather(*tuple(client._server_tasks))
    await asyncio.sleep(0)
    assert client._server_tasks == set()


@pytest.mark.asyncio
async def test_mcp_bounds_pending_server_request_tasks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gate = asyncio.Event()
    started = 0

    async def handle_request(method: str, params: dict) -> dict:
        nonlocal started
        started += 1
        await gate.wait()
        return {}

    monkeypatch.setattr(mcp_client_module, "MAX_PENDING_MCP_SERVER_REQUESTS", 2)
    monkeypatch.setattr(mcp_client_module, "MAX_PENDING_MCP_OVERLOAD_RESPONSES", 3)
    client = MCPClient(
        MCPServerConfig(name="fake", command="fake", args=[], env={}),
        server_request_handler=handle_request,
    )
    client._send_message = AsyncMock()

    for index in range(100):
        client._dispatch_incoming(
            {
                "jsonrpc": "2.0",
                "id": f"server-{index}",
                "method": "custom",
                "params": {},
            }
        )
    observed_tasks = len(client._server_tasks)
    await asyncio.sleep(0)
    observed_started = started
    observed_requests = len(client._incoming_requests)
    await asyncio.sleep(0)
    overload_responses = [
        call.args[0]
        for call in client._send_message.await_args_list
        if isinstance(call.args[0], dict) and "error" in call.args[0]
    ]

    gate.set()
    await asyncio.gather(*tuple(client._server_tasks))
    await asyncio.sleep(0)

    assert observed_started == 2
    assert observed_tasks == 5
    assert observed_requests == 2
    assert len(overload_responses) == 3
    assert all(response["error"]["code"] == -32000 for response in overload_responses)
    assert client._server_tasks == set()
    assert client._incoming_requests == {}


@pytest.mark.asyncio
async def test_mcp_duplicate_server_request_id_does_not_replace_owner() -> None:
    gate = asyncio.Event()
    started = 0

    async def handle_request(method: str, params: dict) -> dict:
        nonlocal started
        started += 1
        await gate.wait()
        return {}

    client = MCPClient(
        MCPServerConfig(name="fake", command="fake", args=[], env={}),
        server_request_handler=handle_request,
    )
    client._send_message = AsyncMock()
    message = {
        "jsonrpc": "2.0",
        "id": "same-id",
        "method": "custom",
        "params": {},
    }

    client._dispatch_incoming(message)
    await asyncio.sleep(0)
    owner = client._incoming_requests["same-id"]
    client._dispatch_incoming(message)
    await asyncio.sleep(0)
    observed_started = started
    observed_owner = client._incoming_requests.get("same-id")

    gate.set()
    await asyncio.gather(*tuple(client._server_tasks))
    await asyncio.sleep(0)

    assert observed_started == 1
    assert observed_owner is owner
    assert client._send_message.await_count == 1
    assert client._server_tasks == set()
    assert client._incoming_requests == {}


@pytest.mark.asyncio
@pytest.mark.parametrize("request_id", ["x" * 513, 2**53])
async def test_mcp_rejects_unsafe_server_request_ids_before_task_admission(
    request_id: str | int,
) -> None:
    handler = AsyncMock(return_value={})
    client = MCPClient(
        MCPServerConfig(name="fake", command="fake", args=[], env={}),
        server_request_handler=handler,
    )
    client._send_message = AsyncMock()

    client._dispatch_incoming(
        {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": "custom",
            "params": {},
        }
    )
    await asyncio.sleep(0)

    handler.assert_not_awaited()
    client._send_message.assert_not_awaited()
    assert client._server_tasks == set()
    assert client._incoming_requests == {}


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["roots/list", "sampling/createMessage", "elicitation/create"])
async def test_mcp_rejects_unassociated_contextual_server_requests(method: str) -> None:
    sampling = AsyncMock(return_value={"role": "assistant", "content": {"type": "text", "text": "ok"}, "model": "test"})
    elicitation = AsyncMock(return_value={"action": "cancel"})
    client = MCPClient(
        MCPServerConfig(name="fake", command="fake", args=[], env={}),
        roots=(Path.cwd(),),
        sampling_handler=sampling,
        elicitation_handler=elicitation,
    )
    client._send_message = AsyncMock()

    client._dispatch_incoming(
        {"jsonrpc": "2.0", "id": "server-1", "method": method, "params": {}}
    )
    await asyncio.sleep(0)
    await asyncio.gather(*tuple(client._server_tasks))

    sampling.assert_not_awaited()
    elicitation.assert_not_awaited()
    response = client._send_message.await_args.args[0]
    assert response["id"] == "server-1"
    assert response["error"]["code"] == -32600
    assert "associated" in response["error"]["message"]


@pytest.mark.asyncio
async def test_mcp_allows_sampling_nested_under_active_client_request() -> None:
    sampling = AsyncMock(
        return_value={
            "role": "assistant",
            "content": {"type": "text", "text": "ok"},
            "model": "test-model",
        }
    )
    client = MCPClient(
        MCPServerConfig(name="fake", command="fake", args=[], env={}),
        sampling_handler=sampling,
    )
    client._send_message = AsyncMock()
    pending = asyncio.get_running_loop().create_future()
    client._pending[41] = pending
    try:
        client._dispatch_incoming(
            {
                "jsonrpc": "2.0",
                "id": "server-2",
                "method": "sampling/createMessage",
                "params": {"messages": [], "maxTokens": 1},
            }
        )
        await asyncio.sleep(0)
        await asyncio.gather(*tuple(client._server_tasks))
    finally:
        client._pending.pop(41, None)
        pending.cancel()

    sampling.assert_awaited_once()
    response = client._send_message.await_args.args[0]
    assert response["result"]["model"] == "test-model"


@pytest.mark.asyncio
async def test_mcp_server_request_preserves_protocol_error_code_and_data() -> None:
    async def reject(params: dict) -> dict:
        del params
        raise MCPProtocolError("invalid sampling params", code=-32602, data={"field": "tools"})

    client = MCPClient(
        MCPServerConfig(name="fake", command="fake", args=[], env={}),
        sampling_handler=reject,
    )
    client._send_message = AsyncMock()
    pending = asyncio.get_running_loop().create_future()
    client._pending[7] = pending
    try:
        client._dispatch_incoming(
            {
                "jsonrpc": "2.0",
                "id": "server-3",
                "method": "sampling/createMessage",
                "params": {},
            }
        )
        await asyncio.sleep(0)
        await asyncio.gather(*tuple(client._server_tasks))
    finally:
        client._pending.pop(7, None)
        pending.cancel()

    response = client._send_message.await_args.args[0]
    assert response["error"] == {
        "code": -32602,
        "message": "invalid sampling params",
        "data": {"field": "tools"},
    }




@pytest.mark.asyncio
async def test_runtime_applies_startup_tool_list_change_atomically(
    tmp_path: Path,
) -> None:
    live_tools: dict[str, object] = {}
    replacements: list[tuple[set[str], set[str]]] = []

    async def replace(server: str, previous: dict, replacement: dict) -> None:
        assert server == "dynamic"
        replacements.append((set(previous), set(replacement)))
        for name in previous:
            live_tools.pop(name, None)
        live_tools.update(replacement)

    events: list[dict] = []
    runtime = MCPRuntime(
        {
            "dynamic": MCPServerConfig(
                name="dynamic",
                command=sys.executable,
                args=["-u", "-c", DYNAMIC_MCP_SERVER],
                env={},
            )
        },
        SafetyGuard(tmp_path),
        tool_change_handler=replace,
        event_sink=events.append,
    )
    live_tools.update(await runtime.start())
    try:
        await runtime.wait_for_refreshes()
        assert "mcp__dynamic__old" not in live_tools
        assert "mcp__dynamic__new" in live_tools
        result = await live_tools["mcp__dynamic__new"].run(text="hello")
        assert result.success is True
        assert result.output == "new"
        assert replacements == [({"mcp__dynamic__old"}, {"mcp__dynamic__new"})]
        assert any(
            event.get("type") == "mcp.catalog.changed"
            and event.get("added") == ["mcp__dynamic__new"]
            and event.get("removed") == ["mcp__dynamic__old"]
            for event in events
        )
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_failed_dynamic_catalog_preserves_last_good_tools(
    tmp_path: Path,
) -> None:
    broken_server = DYNAMIC_MCP_SERVER.replace(
        '"required": ["text"]',
        '"required": ["text"] if state == "old" else "invalid"',
    )
    replacements: list[dict] = []

    async def replace(server: str, previous: dict, replacement: dict) -> None:
        replacements.append(replacement)

    runtime = MCPRuntime(
        {
            "dynamic": MCPServerConfig(
                name="dynamic",
                command=sys.executable,
                args=["-u", "-c", broken_server],
                env={},
            )
        },
        SafetyGuard(tmp_path),
        tool_change_handler=replace,
    )
    tools = await runtime.start()
    try:
        await runtime.wait_for_refreshes()
        assert "mcp__dynamic__old" in tools
        assert replacements == []
        assert "invalid tool catalog" in runtime.errors["dynamic:tools/refresh"]
        quarantined = await tools["mcp__dynamic__old"].run(text="blocked")
        assert quarantined.success is False
        assert "no longer matches the active verified" in quarantined.error
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_tool_list_change_quarantines_calls_until_refresh_finishes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    refresh_started = asyncio.Event()
    release_refresh = asyncio.Event()

    class PausedRefreshClient:
        server_capabilities = {"tools": {"listChanged": True}}
        protocol_version = "2025-11-25"
        session_generation = 1

        def __init__(self, config, *, roots=()) -> None:
            self.notification_handler = None
            self.session_reinitialized_handler = None
            self.tool_contract_validator = None
            self.list_calls = 0
            self.tool_calls = 0

        async def connect(self) -> None:
            return None

        def supports_server_capability(self, name: str) -> bool:
            return name == "tools"

        async def list_tools(self) -> list[dict]:
            self.list_calls += 1
            if self.list_calls == 2:
                refresh_started.set()
                await release_refresh.wait()
            return [{"name": "echo", "inputSchema": {"type": "object"}}]

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
            self.tool_calls += 1
            return {"content": []}

        async def disconnect(self) -> None:
            return None

    monkeypatch.setattr("ash.mcp.runtime.MCPClient", PausedRefreshClient)
    runtime = MCPRuntime(
        {"paused": MCPServerConfig(name="paused", command="unused", args=[], env={})},
        SafetyGuard(tmp_path),
    )
    tools = await runtime.start()
    client = runtime.clients["paused"]
    try:
        await client.notification_handler("notifications/tools/list_changed", {})
        await asyncio.wait_for(refresh_started.wait(), timeout=1)

        quarantined = await tools["mcp__paused__echo"].run()
        assert quarantined.success is False
        assert "no longer matches the active verified" in quarantined.error
        assert client.tool_calls == 0

        release_refresh.set()
        await runtime.wait_for_refreshes()
        restored = await tools["mcp__paused__echo"].run()
        assert restored.success is True
        assert client.tool_calls == 1
    finally:
        release_refresh.set()
        await runtime.close()


@pytest.mark.asyncio
async def test_resource_and_prompt_list_changes_emit_live_revisions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class CatalogClient:
        server_capabilities = {
            "resources": {"listChanged": True},
            "prompts": {"listChanged": True},
        }
        protocol_version = "2025-11-25"

        def __init__(self, config, *, roots=()) -> None:
            self.config = config
            self.notification_handler = None

        async def connect(self) -> None:
            return None

        def supports_server_capability(self, name: str) -> bool:
            return name in self.server_capabilities

        async def disconnect(self) -> None:
            return None

    monkeypatch.setattr("ash.mcp.runtime.MCPClient", CatalogClient)
    events: list[dict] = []
    runtime = MCPRuntime(
        {"catalog": MCPServerConfig(name="catalog", command="unused", args=[], env={})},
        SafetyGuard(tmp_path),
        event_sink=events.append,
    )
    await runtime.start()
    client = runtime.clients["catalog"]
    assert client.notification_handler is not None
    try:
        await client.notification_handler("notifications/resources/list_changed", {})
        await client.notification_handler("notifications/prompts/list_changed", {})
        assert [(event["capability"], event["revision"]) for event in events] == [
            ("resources", 1),
            ("prompts", 2),
        ]
    finally:
        await runtime.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("contract_mode", ["unchanged", "renamed", "removed"])
async def test_runtime_reconciles_catalog_without_http_tool_replay(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    contract_mode: str,
) -> None:
    initialize_count = 0
    tool_attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal initialize_count, tool_attempts
        if request.method == "DELETE":
            return httpx.Response(405)
        if request.content.startswith(b'{"jsonrpc":"2.0","id":0'):
            return httpx.Response(
                200,
                json={"jsonrpc": "2.0", "id": 0, "result": {}},
            )
        payload = json.loads(request.content)
        method = payload["method"]
        session = request.headers.get("Mcp-Session-Id")
        if method == "initialize":
            initialize_count += 1
            capabilities = (
                {}
                if contract_mode == "removed" and initialize_count == 2
                else {"tools": {}}
            )
            return httpx.Response(
                200,
                headers={"Mcp-Session-Id": f"session-{initialize_count}"},
                json={
                    "jsonrpc": "2.0",
                    "id": payload["id"],
                    "result": {
                        "protocolVersion": "2025-11-25",
                        "capabilities": capabilities,
                    },
                },
            )
        if method == "notifications/initialized":
            return httpx.Response(202)
        if method == "tools/list":
            assert not (contract_mode == "removed" and session == "session-2")
            name = (
                "replacement"
                if contract_mode == "renamed" and session == "session-2"
                else "echo"
            )
            result = {
                "tools": [
                    {
                        "name": name,
                        "description": name,
                        "inputSchema": {
                            "type": "object",
                            "properties": {"text": {"type": "string"}},
                            "required": ["text"],
                        },
                    }
                ]
            }
            return httpx.Response(
                200,
                json={"jsonrpc": "2.0", "id": payload["id"], "result": result},
            )
        tool_attempts += 1
        if session == "session-1":
            return httpx.Response(404)
        return httpx.Response(
            200,
            json={
                "jsonrpc": "2.0",
                "id": payload["id"],
                "result": {"content": [{"type": "text", "text": "ok"}]},
            },
        )

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    real_client = MCPClient

    def client_factory(config, **kwargs):
        return real_client(config, http_client=http, **kwargs)

    monkeypatch.setattr("ash.mcp.runtime.MCPClient", client_factory)
    live: dict[str, object] = {}

    async def replace(server: str, previous: dict, replacement: dict) -> None:
        for name in previous:
            live.pop(name, None)
        live.update(replacement)

    runtime = MCPRuntime(
        {
            "remote": MCPServerConfig(
                name="remote",
                command="",
                args=[],
                env={},
                transport="http",
                url="https://mcp.example.test/rpc",
            )
        },
        SafetyGuard(tmp_path),
        tool_change_handler=replace,
    )
    live.update(await runtime.start())
    old_tool = live["mcp__remote__echo"]
    try:
        result = await old_tool.run(text="hello")
        assert result.success is False
        assert "operation was not replayed" in result.error
        assert tool_attempts == 1
        if contract_mode != "unchanged":
            assert "mcp__remote__echo" not in live
            assert ("mcp__remote__replacement" in live) is (contract_mode == "renamed")
            stale_result = await old_tool.run(text="again")
            assert stale_result.success is False
            assert "no longer matches the active verified" in stale_result.error
            assert tool_attempts == 1
        else:
            assert "mcp__remote__echo" in live
        assert initialize_count == 2
    finally:
        await runtime.close()
        await http.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("replacement_supports_tools", [True, False])
async def test_runtime_start_recovers_session_expiry_during_initial_catalog(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    replacement_supports_tools: bool,
) -> None:
    initialize_count = 0
    list_sessions: list[str | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal initialize_count
        if request.method == "DELETE":
            return httpx.Response(405)
        if request.content.startswith(b'{"jsonrpc":"2.0","id":0'):
            return httpx.Response(
                200,
                json={"jsonrpc": "2.0", "id": 0, "result": {}},
            )
        payload = json.loads(request.content)
        method = payload["method"]
        session = request.headers.get("Mcp-Session-Id")
        if method == "initialize":
            initialize_count += 1
            capabilities = (
                {"tools": {}}
                if initialize_count == 1 or replacement_supports_tools
                else {}
            )
            return httpx.Response(
                200,
                headers={"Mcp-Session-Id": f"session-{initialize_count}"},
                json={
                    "jsonrpc": "2.0",
                    "id": payload["id"],
                    "result": {
                        "protocolVersion": "2025-11-25",
                        "capabilities": capabilities,
                    },
                },
            )
        if method == "notifications/initialized":
            return httpx.Response(202)
        assert method == "tools/list"
        list_sessions.append(session)
        if session == "session-1":
            return httpx.Response(404)
        return httpx.Response(
            200,
            json={
                "jsonrpc": "2.0",
                "id": payload["id"],
                "result": {
                    "tools": [{"name": "echo", "inputSchema": {"type": "object"}}]
                },
            },
        )

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    real_client = MCPClient

    def client_factory(config, **kwargs):
        return real_client(config, http_client=http, **kwargs)

    monkeypatch.setattr("ash.mcp.runtime.MCPClient", client_factory)
    runtime = MCPRuntime(
        {
            "remote": MCPServerConfig(
                name="remote",
                command="",
                args=[],
                env={},
                transport="http",
                url="https://mcp.example.test/rpc",
            )
        },
        SafetyGuard(tmp_path),
    )
    try:
        tools = await asyncio.wait_for(runtime.start(), timeout=1)
        assert ("mcp__remote__echo" in tools) is replacement_supports_tools
        assert initialize_count == 2
        expected_sessions = (
            ["session-1", "session-2"] if replacement_supports_tools else ["session-1"]
        )
        assert list_sessions == expected_sessions
        assert runtime.errors == {}
    finally:
        await runtime.close()
        await http.aclose()


@pytest.mark.asyncio
async def test_runtime_blocks_concurrent_stale_call_until_catalog_reconciles(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    initialize_count = 0
    tool_attempts = 0
    replacement_list_started = asyncio.Event()
    release_replacement_list = asyncio.Event()

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal initialize_count, tool_attempts
        if request.method == "DELETE":
            return httpx.Response(405)
        if request.content.startswith(b'{"jsonrpc":"2.0","id":0'):
            return httpx.Response(
                200,
                json={"jsonrpc": "2.0", "id": 0, "result": {}},
            )
        payload = json.loads(request.content)
        method = payload["method"]
        session = request.headers.get("Mcp-Session-Id")
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
                        "capabilities": {"tools": {}},
                    },
                },
            )
        if method == "notifications/initialized":
            return httpx.Response(202)
        if method == "tools/list":
            if session == "session-2":
                replacement_list_started.set()
                await asyncio.wait_for(release_replacement_list.wait(), timeout=1)
            name = "replacement" if session == "session-2" else "echo"
            return httpx.Response(
                200,
                json={
                    "jsonrpc": "2.0",
                    "id": payload["id"],
                    "result": {
                        "tools": [
                            {
                                "name": name,
                                "inputSchema": {"type": "object"},
                            }
                        ]
                    },
                },
            )
        tool_attempts += 1
        if session == "session-1":
            return httpx.Response(404)
        return httpx.Response(
            200,
            json={"jsonrpc": "2.0", "id": payload["id"], "result": {}},
        )

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    real_client = MCPClient

    def client_factory(config, **kwargs):
        return real_client(config, http_client=http, **kwargs)

    monkeypatch.setattr("ash.mcp.runtime.MCPClient", client_factory)
    runtime = MCPRuntime(
        {
            "remote": MCPServerConfig(
                name="remote",
                command="",
                args=[],
                env={},
                transport="http",
                url="https://mcp.example.test/rpc",
            )
        },
        SafetyGuard(tmp_path),
    )
    tools = await runtime.start()
    old_tool = tools["mcp__remote__echo"]
    first = asyncio.create_task(old_tool.run())
    try:
        await asyncio.wait_for(replacement_list_started.wait(), timeout=1)
        second = asyncio.create_task(old_tool.run())
        await asyncio.sleep(0.05)
        assert second.done() is False
        assert tool_attempts == 1

        release_replacement_list.set()
        first_result, second_result = await asyncio.gather(first, second)
        assert first_result.success is False
        assert "operation was not replayed" in first_result.error
        assert second_result.success is False
        assert "no longer matches the active verified" in second_result.error
        assert tool_attempts == 1
    finally:
        release_replacement_list.set()
        if not first.done():
            first.cancel()
            await asyncio.gather(first, return_exceptions=True)
        await runtime.close()
        await http.aclose()


@pytest.mark.asyncio
async def test_output_schema_only_refresh_is_reported_as_changed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class ChangingClient:
        server_capabilities = {"tools": {"listChanged": True}}
        protocol_version = "2025-11-25"

        def __init__(self, config, *, roots=()) -> None:
            self.notification_handler = None
            self.session_reinitialized_handler = None
            self.calls = 0

        async def connect(self) -> None:
            return None

        def supports_server_capability(self, name: str) -> bool:
            return name == "tools"

        async def list_tools(self) -> list[dict]:
            self.calls += 1
            return [
                {
                    "name": "same",
                    "description": "same",
                    "inputSchema": {"type": "object"},
                    "outputSchema": {
                        "type": "object",
                        "properties": {"version": {"const": self.calls}},
                    },
                }
            ]

        async def disconnect(self) -> None:
            return None

    monkeypatch.setattr("ash.mcp.runtime.MCPClient", ChangingClient)
    events: list[dict] = []
    runtime = MCPRuntime(
        {
            "changing": MCPServerConfig(
                name="changing", command="unused", args=[], env={}
            )
        },
        SafetyGuard(tmp_path),
        event_sink=events.append,
    )
    await runtime.start()
    client = runtime.clients["changing"]
    try:
        await client.notification_handler("notifications/tools/list_changed", {})
        await runtime.wait_for_refreshes()
        tool_events = [
            event
            for event in events
            if event.get("type") == "mcp.catalog.changed"
            and event.get("capability") == "tools"
        ]
        assert tool_events[-1]["changed"] == ["mcp__changing__same"]
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_refresh_storm_is_bounded_and_reported(tmp_path: Path) -> None:
    storm_server = DYNAMIC_MCP_SERVER.replace("if list_count == 1:", "if True:")
    events: list[dict] = []
    runtime = MCPRuntime(
        {
            "storm": MCPServerConfig(
                name="storm",
                command=sys.executable,
                args=["-u", "-c", storm_server],
                env={},
            )
        },
        SafetyGuard(tmp_path),
        event_sink=events.append,
    )
    await runtime.start()
    try:
        await asyncio.wait_for(runtime.wait_for_refreshes(), timeout=2)
        assert "refresh storm" in runtime.errors["storm:tools/refresh"]
        assert any(
            event.get("type") == "mcp.catalog.refresh_suppressed" for event in events
        )
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_legacy_sse_discovers_endpoint_and_receives_async_response() -> None:
    requests: list[tuple[str, str]] = []
    initialized = asyncio.Event()

    class StreamingSSETransport(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request):
            requests.append((request.method, str(request.url)))
            if request.url.path != "/sse":
                return httpx.Response(202)

            class SSEStream(httpx.AsyncByteStream):
                async def __aiter__(self):
                    yield b"event: endpoint\ndata: /messages?session=legacy-1\n\n"
                    yield (
                        'event: message\ndata: {"jsonrpc":"2.0","id":1,'
                        '"result":{"protocolVersion":"2025-06-18","capabilities":{}}}\n\n'
                    ).encode()
                    yield (
                        'event: message\ndata: {"jsonrpc":"2.0","id":2,'
                        '"result":{"tools":[]}}\n\n'
                    ).encode()
                    yield (
                        'event: message\ndata: {"jsonrpc":"2.0",'
                        '"method":"notifications/message","params":{"level":"info",'
                        '"data":"ready"}}\n\n'
                    ).encode()
                    await initialized.wait()

            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                stream=SSEStream(),
            )

    http = httpx.AsyncClient(transport=StreamingSSETransport())
    client = MCPClient(
        MCPServerConfig(
            name="legacy",
            command="",
            args=[],
            env={},
            transport="sse",
            url="https://legacy.example.test/sse",
        ),
        http_client=http,
    )
    notifications: list[str] = []
    client.notification_handler = lambda method, params: notifications.append(method)
    await asyncio.wait_for(client.connect(), timeout=1)
    tools = await asyncio.wait_for(client.list_tools(), timeout=1)
    initialized.set()
    await asyncio.wait_for(
        client.notify(
            "notifications/initialized",
            {},
            _allow_session_recovery=False,
        ),
        timeout=1,
    )
    assert tools == []
    assert client._pending == {}
    assert client._legacy_sse_endpoint == (
        "https://legacy.example.test/messages?session=legacy-1"
    )
    assert "notifications/message" in notifications
    await client.disconnect()
    await http.aclose()
    assert all(method != "DELETE" for method, _ in requests)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("event_body", "message"),
    [
        ("", "requires an endpoint event"),
        (
            "event: wrong\ndata: https://other.test/messages\n\n",
            "requires an endpoint event",
        ),
        (
            "event: endpoint\ndata: https://attacker.test/messages\n\n",
            "origin does not match",
        ),
    ],
)
async def test_legacy_sse_rejects_invalid_discovery(
    event_body: str,
    message: str,
) -> None:
    http = httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200,
                text=event_body,
                headers={"content-type": "text/event-stream"},
            )
        )
    )
    client = MCPClient(
        MCPServerConfig(
            name="legacy",
            command="",
            args=[],
            env={},
            transport="sse",
            url="https://legacy.example.test/sse",
        ),
        http_client=http,
    )
    with pytest.raises(MCPProtocolError, match=message):
        await client.connect()
    await http.aclose()


def test_mcp_header_annotations_reject_invalid_locations_and_names() -> None:
    with pytest.raises(ValueError, match="invalid x-mcp-header"):
        _extract_mcp_header_annotations(
            {
                "type": "object",
                "properties": {"value": {"type": "string", "x-mcp-header": "bad name"}},
            },
            label="test schema",
        )
    with pytest.raises(ValueError, match="x-mcp-header"):
        _extract_mcp_header_annotations(
            {
                "type": "object",
                "properties": {
                    "items": {"type": "array", "items": {"x-mcp-header": "Value"}}
                },
            },
            label="test schema",
        )


@pytest.mark.asyncio
async def test_streamable_http_tool_call_sends_validated_parameter_headers() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.method == "DELETE" or not request.content:
            return httpx.Response(204)
        if request.method == "GET":
            return httpx.Response(405)
        payload = json.loads(request.content)
        if "id" not in payload:
            return httpx.Response(202)
        if payload["method"] == "initialize":
            return httpx.Response(
                200,
                headers={"Mcp-Session-Id": "session-1"},
                json={
                    "jsonrpc": "2.0",
                    "id": payload["id"],
                    "result": {"protocolVersion": "2025-11-25", "capabilities": {}},
                },
            )
        return httpx.Response(
            200,
            headers={"Mcp-Session-Id": "session-1"},
            json={
                "jsonrpc": "2.0",
                "id": payload["id"],
                "result": {"content": [{"type": "text", "text": "ok"}]},
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
        timeout=1,
    )
    await client.connect()
    await client.call_tool(
        "execute_sql",
        {
            "region": "us-west1",
            "greeting": "Hello, 世界",
            "count": 7,
            "enabled": True,
        },
        header_annotations=[
            (
                (
                    "properties",
                    "region",
                ),
                "Region",
            ),
            (
                (
                    "properties",
                    "greeting",
                ),
                "Greeting",
            ),
            (
                (
                    "properties",
                    "count",
                ),
                "Count",
            ),
            (
                (
                    "properties",
                    "enabled",
                ),
                "Enabled",
            ),
        ],
    )
    await client.disconnect()
    await http.aclose()
    call = next(
        request
        for request in requests
        if request.url.path == "/rpc" and b'"tools/call"' in request.content
    )
    assert call.headers["Mcp-Method"] == "tools/call"
    assert call.headers["Mcp-Name"] == "execute_sql"
    assert call.headers["Mcp-Param-Region"] == "us-west1"
    assert call.headers["Mcp-Param-Greeting"].startswith("=?base64?")
    assert call.headers["Mcp-Param-Count"] == "7"
    assert call.headers["Mcp-Param-Enabled"] == "true"


@pytest.mark.asyncio
async def test_legacy_sse_tool_calls_reject_http_parameter_headers() -> None:
    class StreamingSSETransport(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request):
            if request.url.path != "/sse":
                return httpx.Response(202)

            class SSEStream(httpx.AsyncByteStream):
                async def __aiter__(self):
                    yield b"event: endpoint\ndata: /messages\n\n"
                    yield (
                        'event: message\ndata: {"jsonrpc":"2.0","id":1,'
                        '"result":{"protocolVersion":"2024-11-05","capabilities":{}}}\n\n'
                    ).encode()
                    await asyncio.Event().wait()

            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                stream=SSEStream(),
            )

    http = httpx.AsyncClient(transport=StreamingSSETransport())
    client = MCPClient(
        MCPServerConfig(
            name="legacy",
            command="",
            args=[],
            env={},
            transport="sse",
            url="https://legacy.example.test/sse",
        ),
        http_client=http,
        timeout=0.2,
    )
    await client.connect()
    with pytest.raises(MCPProtocolError, match="require the http transport"):
        await client.call_tool(
            "tool",
            {},
            header_annotations=[(("properties", "region"), "Region")],
        )
    await client.disconnect()
    await http.aclose()


def test_runtime_mcp_tool_extracts_nested_header_annotations(
    tmp_path: Path,
) -> None:
    captured: dict[str, object] = {}

    class HeaderClient:
        protocol_version = "2026-07-28"
        session_generation = 1

        async def connect(self) -> None:
            return None

        async def list_tools(self) -> list[dict]:
            return [
                {
                    "name": "sql",
                    "inputSchema": {
                        "type": "object",
                        "properties": {
                            "options": {
                                "type": "object",
                                "properties": {
                                    "region": {
                                        "type": "string",
                                        "x-mcp-header": "Region",
                                    }
                                },
                            }
                        },
                    },
                }
            ]

        async def call_tool(
            self,
            name: str,
            arguments: dict,
            *,
            expected_contract: str | None = None,
            as_task: bool = False,
            header_annotations: list | None = None,
        ) -> dict:
            del name, expected_contract, as_task
            captured["arguments"] = arguments
            captured["annotations"] = header_annotations
            return {"content": [{"type": "text", "text": "ok"}]}

        async def disconnect(self) -> None:
            return None

    runtime = MCPRuntime(
        {"remote": MCPServerConfig(name="remote", command="unused", args=[], env={})},
        SafetyGuard(tmp_path),
    )
    tool = MCPTool(
        runtime.safety_guard,
        client=HeaderClient(),  # type: ignore[arg-type]
        server_name="remote",
        definition={
            "name": "sql",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "options": {
                        "type": "object",
                        "properties": {
                            "region": {"type": "string", "x-mcp-header": "Region"}
                        },
                    }
                },
            },
        },
        protocol_version="2026-07-28",
    )
    result = asyncio.run(tool.run(options={"region": "us-east1"}))
    assert result.success is True
    assert captured["annotations"] == [(("options", "region"), "Region")]
