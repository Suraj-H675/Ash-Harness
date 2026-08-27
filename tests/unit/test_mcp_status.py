from __future__ import annotations

import json

import pytest

from ash.mcp.runtime import MCPRuntime
from ash.mcp.server import MCPServerConfig
from ash.safety.guard import SafetyGuard


def _config(name: str, transport: str = "stdio", auth: str = "none"):
    return MCPServerConfig(
        name=name,
        command="unused",
        args=[],
        env={},
        transport=transport,
        url=f"https://example.test/{name}" if transport != "stdio" else "",
        auth=auth,
    )


@pytest.mark.asyncio
async def test_mcp_runtime_status_snapshot_is_safe_and_bounded(tmp_path) -> None:
    runtime = MCPRuntime(
        {"healthy": _config("healthy"), "remote": _config("remote", "http", "oauth")},
        SafetyGuard(tmp_path),
    )
    runtime.clients["healthy"] = object()
    runtime._server_tools["healthy"] = {f"tool-{index}": index for index in range(3)}
    runtime.errors["healthy:tools/refresh"] = (
        "temporary catalog issue Bearer " + "s" * 32 + " " + "x" * 600
    )

    payload = runtime.status_snapshot()

    assert payload[0]["name"] == "healthy"
    assert payload[0]["transport"] == "stdio"
    assert payload[0]["auth"] == "none"
    assert payload[0]["state"] == "connected"
    assert payload[0]["tools"] == 3
    assert len(payload[0]["errors"]) == 1
    assert payload[0]["errors"][0].startswith("temporary catalog issue [REDACTED] ")
    assert payload[1] == {
        "name": "remote",
        "transport": "http",
        "auth": "oauth",
        "state": "failed",
        "tools": 0,
        "errors": [],
    }
    assert len(payload[0]["errors"][0]) == 512
    assert "s" * 32 not in json.dumps(payload)
    assert json.dumps(payload) == json.dumps(payload)

    for index in range(20):
        runtime.errors[f"remote:failure:{index}"] = f"failure {index}"
    remote_errors = runtime.status_snapshot()[1]["errors"]
    assert len(remote_errors) == 17
    assert remote_errors[-1] == "... 4 additional errors omitted"
