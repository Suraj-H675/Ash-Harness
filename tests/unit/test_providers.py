"""Mock provider classes for Sprint 15 V14 testing strategy.

This module exercises the provider contract with three flavours of
chaos that real APIs exhibit in the wild:

* :class:`RateLimitedProvider` — refuses calls once a per-window
  quota is hit, exercising the rate-limiter + circuit-breaker paths.
* :class:`ContextOverflowProvider` — raises when the prompt exceeds
  a token budget, exercising the loop's overflow handling.
* :class:`RecordingProvider` — captures every call so tests can
  assert on prompt construction, temperature, model selection, and
  callback firing order.

The tests live alongside the mocks so the chaos behaviour is the
single source of truth — anyone changing the provider contract here
will see the tests fail in this file.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any, AsyncGenerator, Callable

import pytest
from pathlib import Path

from ash.providers.base import (
    CompletionStopCategory,
    ProviderABC,
    StreamChunk,
    TokenCounterLike,
    completion_stop_category,
)


# ---------------------------------------------------------------------------
# Shared mock provider contract
# ---------------------------------------------------------------------------


class _BaseFakeProvider(ProviderABC):
    """Records every ``stream_chat`` call so tests can inspect prompts."""

    def __init__(self, scripts: list[list[str]] | None = None) -> None:
        self._scripts: list[list[str]] = [list(s) for s in (scripts or [])]
        self._call_count = 0
        self.received_messages: list[list[dict[str, Any]]] = []
        self.received_temperatures: list[float] = []
        self.call_log: list[dict[str, Any]] = []
        self.model = "fake-v14"

    @property
    def model_name(self) -> str:
        return self.model

    def count_tokens(self, text: str) -> int:
        return len(text.split())

    async def stream_chat(
        self,
        messages: list[dict[str, Any]],
        temperature: float = 0.0,
        tools: list[dict[str, Any]] | None = None,
    ) -> AsyncGenerator[StreamChunk, None]:
        self.received_messages.append(list(messages))
        self.received_temperatures.append(temperature)
        self.call_log.append(
            {
                "messages": [dict(m) for m in messages],
                "temperature": temperature,
                "prompt_tokens": sum(
                    self.count_tokens(m.get("content", "")) for m in messages
                ),
            }
        )
        idx = min(self._call_count, len(self._scripts) - 1)
        if not self._scripts:
            yield StreamChunk(content="", is_done=True, model=self.model)
            return
        script = self._scripts[max(0, idx)] if idx >= 0 else []
        self._call_count += 1
        for fragment in script:
            yield StreamChunk(content=fragment, model=self.model)
        yield StreamChunk(content="", is_done=True, model=self.model)


# ---------------------------------------------------------------------------
# Chaos providers
# ---------------------------------------------------------------------------


@dataclass
class RateLimitState:
    """Mutable window counter for :class:`RateLimitedProvider`."""

    max_calls: int
    window_seconds: float
    calls: list[float] = field(default_factory=list)

    def admit(self, now: float) -> bool:
        """Return True if the call fits inside the current window."""

        self.calls = [t for t in self.calls if now - t < self.window_seconds]
        if len(self.calls) >= self.max_calls:
            return False
        self.calls.append(now)
        return True


class RateLimitedProvider(ProviderABC):
    """
    Provider that mimics an API rate limit.

    The first ``max_calls`` calls in any ``window_seconds`` window
    succeed; the next call returns a single ``is_done=True`` chunk
    with ``stop_reason='rate_limit'`` and the generator exits. The
    loop's circuit breaker is the natural place to detect this and
    surface a friendly error, so the tests assert the chunk's
    ``stop_reason`` field is propagated.
    """

    def __init__(
        self,
        scripts: list[list[str]],
        *,
        max_calls: int = 2,
        window_seconds: float = 60.0,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self._scripts = [list(s) for s in scripts]
        self._state = RateLimitState(max_calls=max_calls, window_seconds=window_seconds)
        self._clock = clock or _monotonic
        self.received_messages: list[list[dict[str, Any]]] = []

    @property
    def model_name(self) -> str:
        return "rate-limited-fake"

    def count_tokens(self, text: str) -> int:
        return len(text.split())

    async def stream_chat(
        self,
        messages: list[dict[str, Any]],
        temperature: float = 0.0,
        tools: list[dict[str, Any]] | None = None,
    ) -> AsyncGenerator[StreamChunk, None]:
        self.received_messages.append(list(messages))
        if not self._state.admit(self._clock()):
            yield StreamChunk(
                content="",
                is_done=True,
                stop_reason="rate_limit",
                model=self.model_name,
            )
            return
        idx = min(len(self.received_messages) - 1, len(self._scripts) - 1)
        script = self._scripts[max(0, idx)]
        for fragment in script:
            yield StreamChunk(content=fragment, model=self.model_name)
        yield StreamChunk(content="", is_done=True, model=self.model_name)


def _monotonic() -> float:
    import time

    return time.monotonic()


class ContextOverflowProvider(ProviderABC):
    """
    Provider that raises :class:`ContextOverflowError` once the
    accumulated prompt tokens cross a budget.

    Mirrors the real-world behaviour of Anthropic's 200K-token
    context window. The first call under the budget streams normally;
    subsequent calls blow up so the loop can react.
    """

    def __init__(
        self,
        scripts: list[list[str]],
        *,
        token_budget: int = 200,
        token_counter: TokenCounterLike | None = None,
    ) -> None:
        self._scripts = [list(s) for s in scripts]
        self._token_budget = token_budget
        self._token_counter = token_counter
        self.received_messages: list[list[dict[str, Any]]] = []

    @property
    def model_name(self) -> str:
        return "overflow-fake"

    def count_tokens(self, text: str) -> int:
        if self._token_counter is None:
            return len(text.split())
        return self._token_counter.count(text)

    async def stream_chat(
        self,
        messages: list[dict[str, Any]],
        temperature: float = 0.0,
        tools: list[dict[str, Any]] | None = None,
    ) -> AsyncGenerator[StreamChunk, None]:
        self.received_messages.append(list(messages))
        total_tokens = sum(self.count_tokens(m.get("content", "")) for m in messages)
        if total_tokens > self._token_budget:
            raise ContextOverflowError(
                f"prompt exceeded {self._token_budget} tokens (got {total_tokens})"
            )
        if not self._scripts:
            yield StreamChunk(content="", is_done=True, model=self.model_name)
            return
        script = self._scripts[0]
        for fragment in script:
            yield StreamChunk(content=fragment, model=self.model_name)
        yield StreamChunk(content="", is_done=True, model=self.model_name)


class ContextOverflowError(RuntimeError):
    """Raised when the prompt exceeds the configured token budget."""


# ---------------------------------------------------------------------------
# Provider contract tests
# ---------------------------------------------------------------------------


def test_base_fake_provider_yields_done_marker() -> None:
    async def runner() -> list[StreamChunk]:
        provider = _BaseFakeProvider(scripts=[["hi"]])
        chunks: list[StreamChunk] = []
        async for chunk in provider.stream_chat(
            [{"role": "user", "content": "say hi"}]
        ):
            chunks.append(chunk)
        return chunks

    chunks = asyncio.run(runner())
    assert any(c.content == "hi" for c in chunks)
    assert chunks[-1].is_done is True
    assert chunks[-1].model == "fake-v14"


def test_base_fake_provider_records_messages_and_temperature() -> None:
    async def runner() -> _BaseFakeProvider:
        provider = _BaseFakeProvider(scripts=[["x"]])
        async for _ in provider.stream_chat(
            [{"role": "user", "content": "x"}], temperature=0.42
        ):
            pass
        return provider

    provider = asyncio.run(runner())
    assert len(provider.received_messages) == 1
    assert provider.received_temperatures == [0.42]


def test_provider_token_counter_uses_word_count() -> None:
    provider = _BaseFakeProvider()
    assert provider.count_tokens("hello cruel world") == 3
    assert provider.count_tokens("") == 0


def test_provider_abstract_cannot_be_instantiated() -> None:
    with pytest.raises(TypeError):
        ProviderABC()  # type: ignore[abstract]


@pytest.mark.parametrize(
    ("reason", "expected"),
    [
        (None, CompletionStopCategory.COMPLETE),
        ("stop", CompletionStopCategory.COMPLETE),
        ("tool_calls", CompletionStopCategory.COMPLETE),
        ("max_tokens", CompletionStopCategory.TRUNCATED),
        ("content_filter", CompletionStopCategory.FILTERED),
        ("rate_limit", CompletionStopCategory.ERROR),
        ("vendor_unknown_reason", CompletionStopCategory.ERROR),
    ],
)
def test_completion_stop_reasons_normalize_fail_closed(reason, expected) -> None:
    assert completion_stop_category(reason) == expected


# ---------------------------------------------------------------------------
# Rate-limit tests
# ---------------------------------------------------------------------------


def test_rate_limited_provider_admits_within_window() -> None:
    fake_clock = [1000.0]

    def _clock() -> float:
        return fake_clock[0]

    provider = RateLimitedProvider(
        scripts=[["a"], ["b"], ["c"]],
        max_calls=2,
        window_seconds=60.0,
        clock=_clock,
    )

    async def runner() -> list[StreamChunk]:
        all_chunks: list[StreamChunk] = []
        for _ in range(3):
            chunks: list[StreamChunk] = []
            async for c in provider.stream_chat([{"role": "user", "content": "x"}]):
                chunks.append(c)
            all_chunks.extend(chunks)
        return all_chunks

    chunks = asyncio.run(runner())
    # First two calls stream content; third yields only the rate-limit
    # done marker with stop_reason='rate_limit'.
    rate_limit_chunks = [c for c in chunks if c.stop_reason == "rate_limit"]
    assert len(rate_limit_chunks) == 1
    content_chunks = [c for c in chunks if c.content]
    assert len(content_chunks) == 2  # 'a' and 'b'


def test_rate_limited_provider_window_resets_after_expiry() -> None:
    fake_clock = [1000.0]

    def _clock() -> float:
        return fake_clock[0]

    provider = RateLimitedProvider(
        scripts=[["first"], ["second"]],
        max_calls=1,
        window_seconds=10.0,
        clock=_clock,
    )

    async def runner() -> list[StreamChunk]:
        all_chunks: list[StreamChunk] = []
        # First call at t=1000 - admitted, returns "first"
        async for c in provider.stream_chat([{"role": "user", "content": "x"}]):
            all_chunks.append(c)
        # Second call at t=1005 - within window, rate-limited
        async for c in provider.stream_chat([{"role": "user", "content": "y"}]):
            all_chunks.append(c)
        # Third call at t=1011 - past the window, admitted, returns "second"
        fake_clock[0] = 1011.0
        async for c in provider.stream_chat([{"role": "user", "content": "z"}]):
            all_chunks.append(c)
        return all_chunks

    chunks = asyncio.run(runner())
    content = [c.content for c in chunks if c.content]
    assert "first" in content
    assert "second" in content
    assert any(c.stop_reason == "rate_limit" for c in chunks)


def test_rate_limit_state_admit_tracks_window() -> None:
    state = RateLimitState(max_calls=3, window_seconds=10.0)
    for now in [0.0, 1.0, 2.0]:
        assert state.admit(now) is True
    assert state.admit(3.0) is False
    # After the window slides, the older calls drop out.
    assert state.admit(11.0) is True


# ---------------------------------------------------------------------------
# Context-overflow tests
# ---------------------------------------------------------------------------


def test_context_overflow_raises_under_pressure() -> None:
    provider = ContextOverflowProvider(
        scripts=[["ok"]],
        token_budget=20,
    )

    async def runner_short() -> str:
        chunks: list[StreamChunk] = []
        async for c in provider.stream_chat(
            [{"role": "user", "content": "tiny"}],
        ):
            chunks.append(c)
        return "".join(c.content for c in chunks)

    assert "ok" in asyncio.run(runner_short())

    async def runner_overflow() -> None:
        gen = provider.stream_chat(
            [{"role": "user", "content": " ".join(["word"] * 50)}],
        )
        await gen.__anext__()

    with pytest.raises(ContextOverflowError, match="exceeded"):
        asyncio.run(runner_overflow())


def test_context_overflow_uses_custom_token_counter() -> None:
    class _CharCounter:
        def count(self, text: str) -> int:
            return len(text)

    provider = ContextOverflowProvider(
        scripts=[["x"]],
        token_budget=10,
        token_counter=_CharCounter(),
    )

    async def runner() -> None:
        gen = provider.stream_chat(
            [{"role": "user", "content": "a" * 20}],
        )
        await gen.__anext__()

    with pytest.raises(ContextOverflowError):
        asyncio.run(runner())


# ---------------------------------------------------------------------------
# Provider + loop integration (tool callbacks)
# ---------------------------------------------------------------------------


def test_loop_drives_provider_through_tool_callbacks() -> None:
    """The loop should re-invoke the provider with the tool result in
    the conversation history when a tool call completes.

    This is the V14 'tool callbacks' test: confirm the provider sees
    the tool response in the next turn's messages.
    """

    from ash.core.loop import AshLoop
    from ash.core.session import SessionStore
    from ash.safety.guard import SafetyGuard
    from ash.tools.base import BaseTool, ToolResult
    from ash.ui.terminal import TerminalUI

    class _EchoTool(BaseTool):
        name = "echo"
        description = "returns the input verbatim"
        args_schema = type(
            "Args",
            (),
            {"model_fields": {"text": ()}},
        )

        async def run(self, **kwargs: Any) -> ToolResult:
            return ToolResult(success=True, output=kwargs.get("text", ""))

    async def runner() -> _BaseFakeProvider:
        provider = _BaseFakeProvider(
            scripts=[
                # First call: model emits a tool call.
                ['<call_tool name="echo"><arg name="text">hello</arg></call_tool>'],
                # Second call: model emits a final response.
                ["<response>echoed: hello</response>"],
            ]
        )
        # Console / safety guard are minimal so the test runs in isolation.
        from rich.console import Console
        import io

        ui = TerminalUI(
            safety_tier="auto_approve",
            console=Console(file=io.StringIO(), force_terminal=False, width=120),
        )
        guard = SafetyGuard(project_root=_tmp_workspace())
        store = SessionStore(_tmp_db())
        echo = _EchoTool(guard)
        loop = AshLoop(
            session_store=store,
            provider=provider,
            safety_guard=guard,
            ui=ui,
            project_root=_tmp_workspace(),
            tools={echo.name: echo},
        )
        await loop.start_session()
        await loop.run_turn("say hello")
        return provider

    provider = asyncio.run(runner())
    # Provider was called twice (initial + after tool result).
    assert len(provider.received_messages) == 2
    second_call = provider.received_messages[1]
    # The second call's messages include a role='tool' message carrying
    # the rendered tool response.
    assert any(m["role"] == "tool" and "hello" in m["content"] for m in second_call)
    # The first call's messages include the original user prompt.
    assert any(
        m["role"] == "user" and "say hello" in m["content"]
        for m in provider.received_messages[0]
    )


# ---------------------------------------------------------------------------
# Tiny helpers
# ---------------------------------------------------------------------------


def _tmp_workspace() -> Path:
    import tempfile
    from pathlib import Path

    return Path(tempfile.mkdtemp(prefix="ash-provider-"))


def _tmp_db() -> Path:
    import tempfile
    from pathlib import Path

    return Path(tempfile.mkdtemp(prefix="ash-provider-")) / "s.db"


# ---------------------------------------------------------------------------
# OpenAI provider tests (M-11)
# ---------------------------------------------------------------------------


def test_openai_provider_initializes():
    from ash.providers.openai import OpenAIProvider

    provider = OpenAIProvider(model_name="gpt-4o", api_key="test-key")
    assert provider.model_name == "gpt-4o"
    assert provider.count_tokens("hello world") > 0


def test_ash_owned_sdk_clients_disable_nested_retries(monkeypatch) -> None:
    import sys

    from ash.providers.anthropic import AnthropicProvider
    from ash.providers.deepseek import DeepSeekProvider
    from ash.providers.groq import GroqProvider
    from ash.providers.openai import OpenAIProvider

    openai_calls: list[dict[str, Any]] = []
    anthropic_calls: list[dict[str, Any]] = []

    def openai_client(**kwargs):
        openai_calls.append(kwargs)
        return SimpleNamespace()

    def anthropic_client(**kwargs):
        anthropic_calls.append(kwargs)
        return SimpleNamespace()

    monkeypatch.setattr("ash.providers.openai.openai.AsyncOpenAI", openai_client)
    monkeypatch.setattr("ash.providers.deepseek.openai.AsyncOpenAI", openai_client)
    monkeypatch.setattr("ash.providers.groq.openai.AsyncOpenAI", openai_client)
    monkeypatch.setitem(
        sys.modules,
        "anthropic",
        SimpleNamespace(AsyncAnthropic=anthropic_client),
    )

    OpenAIProvider(model_name="gpt", api_key="key")
    DeepSeekProvider(model_name="deepseek", api_key="key")
    GroqProvider(model_name="groq", api_key="key")
    AnthropicProvider(model_name="claude", api_key="key")._resolve_client()

    assert len(openai_calls) == 3
    assert all(call["max_retries"] == 0 for call in openai_calls)
    assert anthropic_calls == [{"max_retries": 0, "api_key": "key"}]


def test_openai_message_translation_preserves_tool_call_ids():
    from ash.providers.openai import prepare_openai_messages

    prepared = prepare_openai_messages(
        [
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "call_id": "call-1",
                        "name": "read_file",
                        "arguments": {"file_path": "README.md"},
                    }
                ],
            },
            {
                "role": "tool",
                "content": "contents",
                "tool_call_id": "call-1",
            },
        ]
    )

    assert prepared[0]["tool_calls"][0]["id"] == "call-1"
    assert prepared[0]["tool_calls"][0]["function"]["name"] == "read_file"
    assert prepared[1]["tool_call_id"] == "call-1"


def test_anthropic_message_translation_uses_tool_blocks():
    from ash.providers.anthropic import prepare_anthropic_messages

    system, prepared = prepare_anthropic_messages(
        [
            {"role": "system", "content": "system"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "call_id": "call-1",
                        "name": "read_file",
                        "arguments": {"file_path": "README.md"},
                    }
                ],
            },
            {
                "role": "tool",
                "content": "contents",
                "tool_call_id": "call-1",
            },
        ]
    )

    assert system == "system"
    assert prepared[0]["content"][0]["type"] == "tool_use"
    assert prepared[1]["content"][0]["type"] == "tool_result"
    assert prepared[1]["content"][0]["tool_use_id"] == "call-1"


def test_provider_message_translation_converts_canonical_images() -> None:
    from ash.providers.anthropic import prepare_anthropic_messages
    from ash.providers.openai import prepare_openai_messages

    messages = [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "inspect"},
                {"type": "image", "media_type": "image/png", "data": "YWJj"},
            ],
        }
    ]

    openai_messages = prepare_openai_messages(messages)
    _, anthropic_messages = prepare_anthropic_messages(messages)

    assert openai_messages[0]["content"][1] == {
        "type": "image_url",
        "image_url": {"url": "data:image/png;base64,YWJj"},
    }
    assert anthropic_messages[0]["content"][1] == {
        "type": "image",
        "source": {
            "type": "base64",
            "media_type": "image/png",
            "data": "YWJj",
        },
    }


@pytest.mark.asyncio
async def test_openai_provider_stream_chat_signature():
    from ash.providers.openai import OpenAIProvider
    from ash.providers.base import ProviderABC

    provider = OpenAIProvider(model_name="gpt-4o", api_key="test-key")
    assert isinstance(provider, ProviderABC)
    # Verify abstract methods are implemented
    assert hasattr(provider, "stream_chat")
    assert hasattr(provider, "count_tokens")
    assert hasattr(provider, "model_name")


class _AsyncChunkStream:
    def __init__(self, chunks: list[Any]) -> None:
        self._chunks = chunks

    def __aiter__(self):
        async def generate():
            for chunk in self._chunks:
                yield chunk

        return generate()


class _FakeOpenAICompletions:
    def __init__(self, chunks: list[Any]) -> None:
        self._chunks = chunks
        self.kwargs: dict[str, Any] = {}

    async def create(self, **kwargs: Any) -> _AsyncChunkStream:
        self.kwargs = kwargs
        return _AsyncChunkStream(self._chunks)


class _FakeOpenAIClient:
    def __init__(self, chunks: list[Any]) -> None:
        self.completions = _FakeOpenAICompletions(chunks)
        self.chat = SimpleNamespace(completions=self.completions)
        self.closed = False

    async def close(self) -> None:
        self.closed = True


def _openai_chunk(
    *,
    content: str = "",
    finish_reason: str | None = None,
    usage: Any = None,
) -> Any:
    choices = []
    if finish_reason is not None or content:
        choices.append(
            SimpleNamespace(
                delta=SimpleNamespace(content=content, tool_calls=None),
                finish_reason=finish_reason,
            )
        )
    return SimpleNamespace(choices=choices, usage=usage)


@pytest.mark.asyncio
async def test_openai_prompt_cache_and_usage_only_chunk() -> None:
    from ash.providers.openai import OpenAIProvider

    usage = SimpleNamespace(
        prompt_tokens=1200,
        completion_tokens=10,
        prompt_tokens_details=SimpleNamespace(cached_tokens=1024),
    )
    client = _FakeOpenAIClient(
        [
            _openai_chunk(content="hello"),
            _openai_chunk(finish_reason="stop"),
            _openai_chunk(usage=usage),
        ]
    )
    provider = OpenAIProvider(
        model_name="gpt-5.2",
        api_key="test-key",
        client=client,
    )
    provider.configure_prompt_cache(
        enabled=True,
        cache_key="ash-project-test",
        retention="extended",
    )

    chunks = [
        chunk
        async for chunk in provider.stream_chat([{"role": "user", "content": "hi"}])
    ]

    assert "".join(chunk.content for chunk in chunks) == "hello"
    assert chunks[-1].is_done is True
    assert chunks[-1].prompt_tokens == 1200
    assert chunks[-1].completion_tokens == 10
    assert chunks[-1].cache_read_tokens == 1024
    assert chunks[-1].usage_source == "provider"
    assert client.completions.kwargs["stream_options"] == {"include_usage": True}
    assert client.completions.kwargs["prompt_cache_key"] == "ash-project-test"
    assert client.completions.kwargs["prompt_cache_retention"] == "24h"

    await provider.aclose()
    assert client.closed is False


@pytest.mark.asyncio
async def test_openai_compatible_endpoint_omits_openai_cache_options() -> None:
    from ash.providers.openai import OpenAIProvider

    client = _FakeOpenAIClient([_openai_chunk(finish_reason="stop")])
    provider = OpenAIProvider(
        model_name="compatible-model",
        api_key="test-key",
        base_url="http://localhost:1234/v1",
        client=client,
    )

    _ = [chunk async for chunk in provider.stream_chat([])]

    assert "stream_options" not in client.completions.kwargs
    assert "prompt_cache_key" not in client.completions.kwargs
    assert "prompt_cache_retention" not in client.completions.kwargs


class _FakeAnthropicStream:
    def __init__(self, final_message: Any) -> None:
        self._final_message = final_message
        self.text_stream = self._texts()

    async def _texts(self):
        yield "hello"

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback) -> None:
        return None

    async def get_final_message(self) -> Any:
        return self._final_message


class _FakeAnthropicMessages:
    def __init__(self, final_message: Any) -> None:
        self._final_message = final_message
        self.kwargs: dict[str, Any] = {}

    def stream(self, **kwargs: Any) -> _FakeAnthropicStream:
        self.kwargs = kwargs
        return _FakeAnthropicStream(self._final_message)


@pytest.mark.asyncio
async def test_anthropic_custom_endpoint_does_not_inherit_ambient_api_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import os
    import sys

    from ash.providers.anthropic import AnthropicProvider
    from ash.providers.readiness import ProviderConfigurationError

    ambient_key = "ambient-anthropic-key"
    monkeypatch.setenv("ANTHROPIC_API_KEY", ambient_key)
    sdk_calls: list[dict[str, Any]] = []
    inherited_keys: list[str] = []

    def fake_async_anthropic(**kwargs: Any) -> Any:
        sdk_calls.append(kwargs)
        if "api_key" not in kwargs:
            inherited_keys.append(os.environ["ANTHROPIC_API_KEY"])
        return SimpleNamespace(messages=_FakeAnthropicMessages(SimpleNamespace()))

    monkeypatch.setitem(
        sys.modules,
        "anthropic",
        SimpleNamespace(AsyncAnthropic=fake_async_anthropic),
    )

    provider = AnthropicProvider(
        model_name="claude-test",
        api_key="",
        base_url="https://gateway.example/v1",
    )

    with pytest.raises(
        ProviderConfigurationError,
        match="Anthropic API key is required when using a custom base URL",
    ) as exc_info:
        _ = [
            chunk
            async for chunk in provider.stream_chat(
                [{"role": "user", "content": "hello"}]
            )
        ]

    assert ambient_key not in str(exc_info.value)
    assert sdk_calls == []
    assert inherited_keys == []


@pytest.mark.asyncio
async def test_anthropic_injected_client_bypasses_auth_validation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import sys

    from ash.providers.anthropic import AnthropicProvider

    sdk_calls: list[dict[str, Any]] = []

    def fake_async_anthropic(**kwargs: Any) -> Any:
        sdk_calls.append(kwargs)
        raise AssertionError("injected client should bypass SDK construction")

    monkeypatch.setitem(
        sys.modules,
        "anthropic",
        SimpleNamespace(AsyncAnthropic=fake_async_anthropic),
    )
    client = SimpleNamespace(messages=_FakeAnthropicMessages(SimpleNamespace()))
    provider = AnthropicProvider(
        model_name="claude-test",
        api_key="",
        base_url="https://gateway.example/v1",
        client=client,
    )

    chunks = [
        chunk
        async for chunk in provider.stream_chat(
            [{"role": "user", "content": "hello"}]
        )
    ]

    assert "".join(chunk.content for chunk in chunks) == "hello"
    assert sdk_calls == []


@pytest.mark.asyncio
async def test_anthropic_default_endpoint_preserves_sdk_api_key_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import os
    import sys

    from ash.providers.anthropic import AnthropicProvider

    ambient_key = "ambient-anthropic-key"
    monkeypatch.setenv("ANTHROPIC_API_KEY", ambient_key)
    sdk_calls: list[dict[str, Any]] = []
    inherited_keys: list[str] = []

    def fake_async_anthropic(**kwargs: Any) -> Any:
        sdk_calls.append(kwargs)
        if "api_key" not in kwargs:
            inherited_keys.append(os.environ["ANTHROPIC_API_KEY"])
        return SimpleNamespace(messages=_FakeAnthropicMessages(SimpleNamespace()))

    monkeypatch.setitem(
        sys.modules,
        "anthropic",
        SimpleNamespace(AsyncAnthropic=fake_async_anthropic),
    )

    provider = AnthropicProvider(model_name="claude-test", api_key="")
    chunks = [
        chunk
        async for chunk in provider.stream_chat(
            [{"role": "user", "content": "hello"}]
        )
    ]

    assert "".join(chunk.content for chunk in chunks) == "hello"
    assert sdk_calls == [{"max_retries": 0}]
    assert inherited_keys == [ambient_key]


@pytest.mark.asyncio
async def test_anthropic_prompt_cache_normalizes_usage() -> None:
    from ash.providers.anthropic import AnthropicProvider

    final_message = SimpleNamespace(
        usage=SimpleNamespace(
            input_tokens=50,
            cache_read_input_tokens=1000,
            cache_creation_input_tokens=500,
            output_tokens=20,
        ),
        stop_reason="end_turn",
        content=[
            SimpleNamespace(
                type="thinking",
                thinking="Consider cache behavior first.",
            ),
            SimpleNamespace(type="redacted_thinking", data="opaque"),
            SimpleNamespace(
                type="web_search_tool_result",
                content=[{"url": "https://example.com/source"}],
            ),
            SimpleNamespace(
                type="tool_use", id="call_1", name="read_file", input={}
            ),
        ],
    )
    messages = _FakeAnthropicMessages(final_message)
    client = SimpleNamespace(messages=messages)
    provider = AnthropicProvider(
        model_name="claude-sonnet-4-6",
        api_key="test-key",
        client=client,
    )
    provider.configure_prompt_cache(enabled=True, retention="extended")

    chunks = [
        chunk
        async for chunk in provider.stream_chat([{"role": "user", "content": "hi"}])
    ]

    assert "".join(chunk.content for chunk in chunks) == "hello"
    assert messages.kwargs["cache_control"] == {"type": "ephemeral", "ttl": "1h"}
    assert chunks[-1].prompt_tokens == 1550
    assert chunks[-1].completion_tokens == 20
    assert chunks[-1].cache_read_tokens == 1000
    assert chunks[-1].usage_source == "provider"
    assert chunks[-1].cache_write_tokens == 500
    assert chunks[-1].reasoning is not None
    assert chunks[-1].reasoning[0] == {
        "type": "thinking",
        "thinking": "Consider cache behavior first.",
    }
    assert {"type": "redacted_thinking", "data": "opaque"} in chunks[
        -1
    ].reasoning
    assert any(
        block["type"] == "web_search_tool_result"
        for block in chunks[-1].reasoning
    )


def test_prompt_cache_retention_validation() -> None:
    from ash.providers.anthropic import AnthropicProvider
    from ash.providers.openai import OpenAIProvider

    anthropic = AnthropicProvider("claude-test", "test-key", client=object())
    openai_provider = OpenAIProvider(
        "gpt-test", "test-key", client=_FakeOpenAIClient([])
    )

    with pytest.raises(ValueError, match="retention"):
        anthropic.configure_prompt_cache(enabled=True, retention="forever")
    with pytest.raises(ValueError, match="retention"):
        openai_provider.configure_prompt_cache(enabled=True, retention="forever")


# ---------------------------------------------------------------------------
# Ollama provider tests (M-12)
# ---------------------------------------------------------------------------


def test_ollama_provider_initializes():
    from ash.providers.ollama import OllamaProvider

    provider = OllamaProvider(model_name="llama3", base_url="http://localhost:11434")
    assert provider.model_name == "llama3"
    assert provider.count_tokens("hello") > 0
