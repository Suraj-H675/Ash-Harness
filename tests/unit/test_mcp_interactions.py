from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import pytest

from ash.mcp.client import MCPProtocolError
from ash.mcp.interactions import MCPInteractionController
from ash.providers.base import ProviderABC, StreamChunk


class SamplingProvider(ProviderABC):
    provider_family = "test"

    def __init__(self, *, response: str = "sampled response") -> None:
        self.response = response
        self.messages: list[dict[str, Any]] = []
        self.temperature = 0.0
        self.tools: list[dict[str, Any]] | None = None
        self.max_tokens = 0
        self.closed = False

    @property
    def model_name(self) -> str:
        return "sampling-model"

    def count_tokens(self, text: str) -> int:
        return len(text.split())

    def configure_max_tokens(self, max_tokens: int) -> None:
        self.max_tokens = max_tokens

    async def stream_chat(
        self,
        messages,
        temperature: float = 0.0,
        tools: list[dict[str, Any]] | None = None,
    ) -> AsyncIterator[StreamChunk]:
        self.messages = [dict(message) for message in messages]
        self.temperature = temperature
        self.tools = tools
        yield StreamChunk(content=self.response)
        yield StreamChunk(is_done=True, stop_reason="stop")

    async def aclose(self) -> None:
        self.closed = True


def _sampling_params(**updates: Any) -> dict[str, Any]:
    params: dict[str, Any] = {
        "messages": [
            {
                "role": "user",
                "content": {"type": "text", "text": "Summarize this."},
            }
        ],
        "systemPrompt": "Be concise.",
        "maxTokens": 5000,
        "temperature": 0.4,
    }
    params.update(updates)
    return params


@pytest.mark.asyncio
async def test_sampling_requires_review_before_and_after_generation() -> None:
    provider = SamplingProvider()
    reviews: list[tuple[str, str, dict[str, Any]]] = []

    async def review(server: str, stage: str, payload: dict[str, Any]) -> bool:
        reviews.append((server, stage, payload))
        return True

    controller = MCPInteractionController(
        sampling_enabled=True,
        elicitation_enabled=False,
        provider_factory=lambda: provider,
        sampling_review=review,
        sampling_max_tokens=256,
    )

    result = await controller.handle_sampling("docs", _sampling_params())

    assert [stage for _, stage, _ in reviews] == ["request", "response"]
    assert all(server == "docs" for server, _, _ in reviews)
    assert provider.max_tokens == 256
    assert provider.temperature == 0.4
    assert provider.tools is None
    assert provider.messages[0]["role"] == "system"
    assert provider.messages[0]["content"] == "Be concise."
    assert provider.messages[1]["role"] == "user"
    content = provider.messages[1]["content"]
    assert isinstance(content, list)
    assert len(content) == 1
    assert content[0].type == "text"
    assert content[0].text == "Summarize this."
    assert result == {
        "role": "assistant",
        "content": {"type": "text", "text": "sampled response"},
        "model": "test/sampling-model",
        "stopReason": "endTurn",
    }
    assert provider.closed is True


@pytest.mark.asyncio
async def test_sampling_rejection_before_generation_never_builds_provider() -> None:
    built = 0

    def provider_factory() -> SamplingProvider:
        nonlocal built
        built += 1
        return SamplingProvider()

    async def reject(_server: str, _stage: str, _payload: dict[str, Any]) -> bool:
        return False

    controller = MCPInteractionController(
        sampling_enabled=True,
        elicitation_enabled=False,
        provider_factory=provider_factory,
        sampling_review=reject,
        sampling_max_tokens=128,
    )

    with pytest.raises(MCPProtocolError) as captured:
        await controller.handle_sampling("docs", _sampling_params())

    assert captured.value.code == -1
    assert "rejected" in str(captured.value).casefold()
    assert built == 0


@pytest.mark.asyncio
async def test_sampling_rejection_after_generation_closes_provider() -> None:
    provider = SamplingProvider()
    decisions = iter([True, False])

    async def review(_server: str, _stage: str, _payload: dict[str, Any]) -> bool:
        return next(decisions)

    controller = MCPInteractionController(
        sampling_enabled=True,
        elicitation_enabled=False,
        provider_factory=lambda: provider,
        sampling_review=review,
        sampling_max_tokens=128,
    )

    with pytest.raises(MCPProtocolError) as captured:
        await controller.handle_sampling("docs", _sampling_params())

    assert captured.value.code == -1
    assert provider.closed is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "updates",
    [
        {"includeContext": "thisServer"},
        {"tools": [{"name": "danger", "inputSchema": {"type": "object"}}]},
        {"toolChoice": {"mode": "auto"}},
        {"task": {"ttl": 1000}},
        {"stopSequences": ["STOP"]},
    ],
)
async def test_sampling_refuses_unadvertised_or_unsupported_features(
    updates: dict[str, Any],
) -> None:
    controller = MCPInteractionController(
        sampling_enabled=True,
        elicitation_enabled=False,
        provider_factory=SamplingProvider,
        sampling_review=lambda *_args: True,
        sampling_max_tokens=128,
    )

    with pytest.raises(MCPProtocolError) as captured:
        await controller.handle_sampling("docs", _sampling_params(**updates))

    assert captured.value.code == -32602


@pytest.mark.asyncio
async def test_elicitation_validates_safe_form_response() -> None:
    seen: list[tuple[str, str, dict[str, Any]]] = []

    async def elicit(
        server: str, message: str, schema: dict[str, Any]
    ) -> dict[str, Any]:
        seen.append((server, message, schema))
        return {
            "action": "accept",
            "content": {"name": "Suraj", "count": 3, "confirm": True},
        }

    controller = MCPInteractionController(
        sampling_enabled=False,
        elicitation_enabled=True,
        elicitation_callback=elicit,
    )
    params = {
        "mode": "form",
        "message": "Choose safe values",
        "requestedSchema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "minLength": 1, "maxLength": 20},
                "count": {"type": "integer", "minimum": 1, "maximum": 5},
                "confirm": {"type": "boolean"},
            },
            "required": ["name", "count"],
        },
    }

    result = await controller.handle_elicitation("docs", params)

    assert result == {
        "action": "accept",
        "content": {"name": "Suraj", "count": 3, "confirm": True},
    }
    assert seen and seen[0][0] == "docs"


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["password", "api_token", "privateKey", "otp"])
async def test_elicitation_refuses_sensitive_form_fields(field: str) -> None:
    called = False

    async def elicit(*_args: Any) -> dict[str, Any]:
        nonlocal called
        called = True
        return {"action": "decline"}

    controller = MCPInteractionController(
        sampling_enabled=False,
        elicitation_enabled=True,
        elicitation_callback=elicit,
    )

    with pytest.raises(MCPProtocolError) as captured:
        await controller.handle_elicitation(
            "docs",
            {
                "message": "Enter data",
                "requestedSchema": {
                    "type": "object",
                    "properties": {field: {"type": "string"}},
                },
            },
        )

    assert captured.value.code == -32602
    assert "sensitive" in str(captured.value).casefold()
    assert called is False


@pytest.mark.asyncio
async def test_elicitation_refuses_nested_or_url_requests() -> None:
    controller = MCPInteractionController(
        sampling_enabled=False,
        elicitation_enabled=True,
        elicitation_callback=lambda *_args: {"action": "decline"},
    )

    with pytest.raises(MCPProtocolError) as url_error:
        await controller.handle_elicitation(
            "docs",
            {"mode": "url", "message": "Sign in", "url": "https://example.com"},
        )
    assert url_error.value.code == -32602

    with pytest.raises(MCPProtocolError) as nested_error:
        await controller.handle_elicitation(
            "docs",
            {
                "message": "Nested",
                "requestedSchema": {
                    "type": "object",
                    "properties": {
                        "profile": {
                            "type": "object",
                            "properties": {"name": {"type": "string"}},
                        }
                    },
                },
            },
        )
    assert nested_error.value.code == -32602


@pytest.mark.asyncio
async def test_elicitation_revalidates_callback_content() -> None:
    controller = MCPInteractionController(
        sampling_enabled=False,
        elicitation_enabled=True,
        elicitation_callback=lambda *_args: {
            "action": "accept",
            "content": {"count": 99},
        },
    )

    with pytest.raises(MCPProtocolError) as captured:
        await controller.handle_elicitation(
            "docs",
            {
                "message": "Count",
                "requestedSchema": {
                    "type": "object",
                    "properties": {
                        "count": {"type": "integer", "maximum": 5}
                    },
                    "required": ["count"],
                },
            },
        )

    assert captured.value.code == -32602
    assert "response" in str(captured.value).casefold()


class AbruptSamplingProvider(SamplingProvider):
    async def stream_chat(
        self,
        messages,
        temperature: float = 0.0,
        tools: list[dict[str, Any]] | None = None,
    ) -> AsyncIterator[StreamChunk]:
        self.messages = [dict(message) for message in messages]
        self.temperature = temperature
        self.tools = tools
        yield StreamChunk(content="partial but unterminated")


@pytest.mark.asyncio
async def test_sampling_rejects_provider_eof_without_terminal_completion() -> None:
    provider = AbruptSamplingProvider()
    controller = MCPInteractionController(
        sampling_enabled=True,
        elicitation_enabled=False,
        provider_factory=lambda: provider,
        sampling_review=lambda *_args: True,
        sampling_max_tokens=64,
    )

    with pytest.raises(MCPProtocolError, match="terminal completion") as captured:
        await controller.handle_sampling("docs", _sampling_params(maxTokens=32))

    assert captured.value.code == -32603
    assert provider.closed is True


class ScriptedSamplingProvider(SamplingProvider):
    def __init__(self, chunks: list[StreamChunk]) -> None:
        super().__init__()
        self._chunks = chunks

    async def stream_chat(
        self,
        messages,
        temperature: float = 0.0,
        tools: list[dict[str, Any]] | None = None,
    ) -> AsyncIterator[StreamChunk]:
        self.messages = [dict(message) for message in messages]
        self.temperature = temperature
        self.tools = tools
        for chunk in self._chunks:
            yield chunk


@pytest.mark.asyncio
@pytest.mark.parametrize("reason", ["content_filter", "error", "mystery_stop"])
async def test_sampling_rejects_unsuccessful_or_unknown_terminal_outcomes(reason: str) -> None:
    provider = ScriptedSamplingProvider([
        StreamChunk(content="partial"),
        StreamChunk(is_done=True, stop_reason=reason),
    ])
    controller = MCPInteractionController(
        sampling_enabled=True,
        elicitation_enabled=False,
        provider_factory=lambda: provider,
        sampling_review=lambda *_args: True,
        sampling_max_tokens=64,
    )

    with pytest.raises(MCPProtocolError) as captured:
        await controller.handle_sampling("docs", _sampling_params(maxTokens=32))

    assert captured.value.code == -32603
    assert provider.closed is True


@pytest.mark.asyncio
async def test_sampling_rejects_output_after_terminal_chunk() -> None:
    provider = ScriptedSamplingProvider([
        StreamChunk(content="done", is_done=True, stop_reason="stop"),
        StreamChunk(content="late"),
    ])
    controller = MCPInteractionController(
        sampling_enabled=True,
        elicitation_enabled=False,
        provider_factory=lambda: provider,
        sampling_review=lambda *_args: True,
        sampling_max_tokens=64,
    )

    with pytest.raises(MCPProtocolError, match="after terminal") as captured:
        await controller.handle_sampling("docs", _sampling_params(maxTokens=32))

    assert captured.value.code == -32603
    assert provider.closed is True


@pytest.mark.asyncio
async def test_sampling_cancellation_closes_provider_and_releases_lock() -> None:
    release = __import__("asyncio").Event()

    class BlockingSamplingProvider(SamplingProvider):
        async def stream_chat(self, messages, temperature=0.0, tools=None):
            self.messages = [dict(message) for message in messages]
            await release.wait()
            yield StreamChunk(is_done=True, stop_reason="stop")

    provider = BlockingSamplingProvider()
    controller = MCPInteractionController(
        sampling_enabled=True,
        elicitation_enabled=False,
        provider_factory=lambda: provider,
        sampling_review=lambda *_args: True,
        sampling_max_tokens=64,
    )
    task = __import__("asyncio").create_task(
        controller.handle_sampling("docs", _sampling_params(maxTokens=32))
    )
    await __import__("asyncio").sleep(0)
    task.cancel()
    with pytest.raises(__import__("asyncio").CancelledError):
        await task

    assert provider.closed is True
    assert controller._sampling_lock.locked() is False


@pytest.mark.asyncio
async def test_real_stdio_mcp_sampling_and_elicitation_round_trip(tmp_path) -> None:
    import sys

    from ash.mcp.runtime import MCPRuntime
    from ash.mcp.server import MCPServerConfig
    from ash.safety.guard import SafetyGuard

    server_code = r'''
import json, sys

def send(value):
    print(json.dumps(value, separators=(",", ":")), flush=True)

def receive():
    line = sys.stdin.readline()
    if not line:
        raise SystemExit(1)
    return json.loads(line)

capabilities_ok = False
for line in sys.stdin:
    message = json.loads(line)
    method = message.get("method")
    if method == "server/discover":
        send({"jsonrpc":"2.0","id":message["id"],"error":{"code":-32601,"message":"legacy"}})
        continue
    if method == "initialize":
        caps = message["params"].get("capabilities", {})
        capabilities_ok = caps.get("sampling") == {} and caps.get("elicitation") == {"form": {}}
        send({"jsonrpc":"2.0","id":message["id"],"result":{"protocolVersion":"2025-11-25","capabilities":{"tools":{}}}})
        continue
    if method == "notifications/initialized":
        continue
    if method == "tools/list":
        send({"jsonrpc":"2.0","id":message["id"],"result":{"tools":[{"name":"interactive","inputSchema":{"type":"object","properties":{}}}]}})
        continue
    if method == "tools/call":
        if not capabilities_ok:
            send({"jsonrpc":"2.0","id":message["id"],"error":{"code":-32000,"message":"missing interaction capabilities"}})
            continue
        send({"jsonrpc":"2.0","id":"sample-1","method":"sampling/createMessage","params":{"messages":[{"role":"user","content":{"type":"text","text":"Say hello"}}],"maxTokens":32}})
        sample = receive()
        send({"jsonrpc":"2.0","id":"elicit-1","method":"elicitation/create","params":{"mode":"form","message":"Pick a label","requestedSchema":{"type":"object","properties":{"label":{"type":"string","minLength":1}},"required":["label"]}}})
        elicited = receive()
        sample_text = sample["result"]["content"]["text"]
        label = elicited["result"]["content"]["label"]
        send({"jsonrpc":"2.0","id":message["id"],"result":{"content":[{"type":"text","text":f"{sample_text}|{label}"}]}})
        continue
'''
    provider = SamplingProvider(response="hello from sample")

    async def review(_server: str, _stage: str, _payload: dict[str, Any]) -> bool:
        return True

    async def elicit(
        _server: str, _message: str, _schema: dict[str, Any]
    ) -> dict[str, Any]:
        return {"action": "accept", "content": {"label": "approved"}}

    controller = MCPInteractionController(
        sampling_enabled=True,
        elicitation_enabled=True,
        provider_factory=lambda: provider,
        sampling_review=review,
        elicitation_callback=elicit,
        sampling_max_tokens=64,
    )
    config = MCPServerConfig(
        name="fixture",
        command=sys.executable,
        args=["-u", "-c", server_code],
        env={},
    )
    runtime = MCPRuntime(
        {"fixture": config},
        SafetyGuard(tmp_path),
        sampling_handler=controller.handle_sampling,
        elicitation_handler=controller.handle_elicitation,
    )
    await runtime.start()
    try:
        result = await runtime.clients["fixture"].call_tool("interactive", {})
    finally:
        await runtime.close()

    assert result["content"] == [
        {"type": "text", "text": "hello from sample|approved"}
    ]
    assert provider.closed is True
