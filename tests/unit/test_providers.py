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
from pydantic import BaseModel
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

    class EchoArgs(BaseModel):
        text: str

    class _EchoTool(BaseTool):
        name = "echo"
        description = "returns the input verbatim"
        args_schema = EchoArgs

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
        workspace = _tmp_workspace()
        guard = SafetyGuard(project_root=workspace)
        store = SessionStore(_tmp_db())
        echo = _EchoTool(guard)
        loop = AshLoop(
            session_store=store,
            provider=provider,
            safety_guard=guard,
            ui=ui,
            project_root=workspace,
            tools={echo.name: echo},
            safety_tier="auto_approve",
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


def test_anonymous_openai_injected_client_does_not_allocate_http_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ash.providers.openai import OpenAIProvider

    def unexpected_http_client(*args: object, **kwargs: object) -> object:
        raise AssertionError("injected provider client must not allocate an HTTP client")

    monkeypatch.setattr(
        "ash.providers.openai.openai.DefaultAsyncHttpxClient",
        unexpected_http_client,
    )
    injected = SimpleNamespace()

    provider = OpenAIProvider(
        model_name="local-model",
        api_key="",
        base_url="http://127.0.0.1:8000/v1",
        allow_anonymous=True,
        client=injected,
    )

    assert provider._client is injected


def test_ash_owned_sdk_clients_disable_nested_retries(monkeypatch) -> None:
    import sys

    from ash.providers.anthropic import AnthropicProvider
    from ash.providers.deepseek import DeepSeekProvider
    from ash.providers.groq import GroqProvider
    from ash.providers.openai import OpenAIProvider

    openai_calls: list[dict[str, Any]] = []
    anthropic_calls: list[dict[str, Any]] = []
    openai_http_calls: list[dict[str, Any]] = []
    anthropic_http_calls: list[dict[str, Any]] = []

    def openai_client(**kwargs):
        openai_calls.append(kwargs)
        return SimpleNamespace()

    def anthropic_client(**kwargs):
        anthropic_calls.append(kwargs)
        return SimpleNamespace()

    def openai_http_client(**kwargs):
        openai_http_calls.append(kwargs)
        return SimpleNamespace(kind="openai-http")

    def anthropic_http_client(**kwargs):
        anthropic_http_calls.append(kwargs)
        return SimpleNamespace(kind="anthropic-http")

    monkeypatch.setattr("ash.providers.openai.openai.AsyncOpenAI", openai_client)
    monkeypatch.setattr(
        "ash.providers.openai.openai.DefaultAsyncHttpxClient",
        openai_http_client,
    )
    monkeypatch.setattr("ash.providers.deepseek.openai.AsyncOpenAI", openai_client)
    monkeypatch.setattr("ash.providers.groq.openai.AsyncOpenAI", openai_client)
    monkeypatch.setitem(
        sys.modules,
        "anthropic",
        SimpleNamespace(
            AsyncAnthropic=anthropic_client,
            DefaultAsyncHttpxClient=anthropic_http_client,
        ),
    )

    openai_provider = OpenAIProvider(model_name="gpt", api_key="key")
    deepseek_provider = DeepSeekProvider(model_name="deepseek", api_key="key")
    groq_provider = GroqProvider(model_name="groq", api_key="key")
    anthropic_provider = AnthropicProvider(model_name="claude", api_key="key")

    assert openai_calls == []
    assert openai_http_calls == []
    assert anthropic_calls == []
    assert anthropic_http_calls == []

    openai_provider._resolve_client()
    deepseek_provider._resolve_client()
    groq_provider._resolve_client()
    anthropic_provider._resolve_client()

    assert len(openai_calls) == 3
    assert all(call["max_retries"] == 0 for call in openai_calls)
    assert all("http_client" in call for call in openai_calls)
    assert openai_http_calls == [{"follow_redirects": False}] * 3
    assert anthropic_http_calls == [{"follow_redirects": False}]
    assert len(anthropic_calls) == 1
    assert anthropic_calls[0]["max_retries"] == 0
    assert anthropic_calls[0]["api_key"] == "key"
    assert anthropic_calls[0]["http_client"].kind == "anthropic-http"


def test_ollama_owned_http_client_is_lazy(monkeypatch: pytest.MonkeyPatch) -> None:
    from ash.providers.ollama import OllamaProvider

    calls: list[dict[str, Any]] = []
    client = SimpleNamespace()

    def build_client(**kwargs):
        calls.append(kwargs)
        return client

    monkeypatch.setattr("ash.providers.ollama.httpx.AsyncClient", build_client)

    provider = OllamaProvider(model_name="local")

    assert calls == []
    assert provider._client is None
    assert provider._resolve_client() is client
    assert calls == [{"timeout": 60.0}]
    assert provider._resolve_client() is client
    assert calls == [{"timeout": 60.0}]


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
        self.closed = 0

    def __aiter__(self):
        async def generate():
            for chunk in self._chunks:
                yield chunk

        return generate()

    async def aclose(self) -> None:
        self.closed += 1


class _FakeOpenAICompletions:
    def __init__(self, chunks: list[Any]) -> None:
        self._chunks = chunks
        self.kwargs: dict[str, Any] = {}
        self.last_stream: _AsyncChunkStream | None = None

    async def create(self, **kwargs: Any) -> _AsyncChunkStream:
        self.kwargs = kwargs
        self.last_stream = _AsyncChunkStream(self._chunks)
        return self.last_stream


class _FakeOpenAIModels:
    def __init__(self, model: Any) -> None:
        self.model = model
        self.requested: list[str] = []

    async def retrieve(self, model: str) -> Any:
        self.requested.append(model)
        return self.model


class _FakeOpenAIClient:
    def __init__(self, chunks: list[Any], *, model_info: Any | None = None) -> None:
        self.completions = _FakeOpenAICompletions(chunks)
        self.chat = SimpleNamespace(completions=self.completions)
        if model_info is not None:
            self.models = _FakeOpenAIModels(model_info)
        self.closed = False

    async def close(self) -> None:
        self.closed = True


class _FailingOpenAICompletions:
    def __init__(self, message: str) -> None:
        self.message = message

    async def create(self, **kwargs: Any) -> Any:
        del kwargs
        raise RuntimeError(self.message)


def _failing_openai_client(message: str) -> Any:
    completions = _FailingOpenAICompletions(message)
    return SimpleNamespace(chat=SimpleNamespace(completions=completions))


@pytest.mark.asyncio
async def test_provider_errors_redact_exact_configured_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ash.providers.anthropic import AnthropicProvider
    from ash.providers.deepseek import DeepSeekProvider
    from ash.providers.groq import GroqProvider
    from ash.providers.openai import OpenAIProvider

    api_key = "tiny-k"
    header_secret = "hdr-42"

    openai_provider = OpenAIProvider(
        model_name="gpt-test",
        api_key=api_key,
        default_headers={"X-Api-Key": header_secret},
        client=_failing_openai_client(
            f"upstream echoed {api_key} and {header_secret}"
        ),
    )
    with pytest.raises(RuntimeError) as openai_error:
        _ = [chunk async for chunk in openai_provider.stream_chat([])]
    openai_message = str(openai_error.value)
    assert api_key not in openai_message
    assert header_secret not in openai_message
    assert "[REDACTED]" in openai_message

    monkeypatch.setattr(
        "ash.providers.deepseek.openai.DefaultAsyncHttpxClient",
        lambda **kwargs: SimpleNamespace(**kwargs),
        raising=False,
    )
    monkeypatch.setattr(
        "ash.providers.deepseek.openai.AsyncOpenAI",
        lambda **kwargs: _failing_openai_client(f"upstream echoed {api_key}"),
    )
    deepseek_provider = DeepSeekProvider("deepseek-test", api_key)
    with pytest.raises(RuntimeError) as deepseek_error:
        _ = [chunk async for chunk in deepseek_provider.stream_chat([])]
    assert api_key not in str(deepseek_error.value)
    assert "[REDACTED]" in str(deepseek_error.value)

    monkeypatch.setattr(
        "ash.providers.groq.openai.DefaultAsyncHttpxClient",
        lambda **kwargs: SimpleNamespace(**kwargs),
        raising=False,
    )
    monkeypatch.setattr(
        "ash.providers.groq.openai.AsyncOpenAI",
        lambda **kwargs: _failing_openai_client(f"upstream echoed {api_key}"),
    )
    groq_provider = GroqProvider("groq-test", api_key)
    with pytest.raises(RuntimeError) as groq_error:
        _ = [chunk async for chunk in groq_provider.stream_chat([])]
    assert api_key not in str(groq_error.value)
    assert "[REDACTED]" in str(groq_error.value)

    class FailingAnthropicMessages:
        def stream(self, **kwargs: Any) -> Any:
            del kwargs
            raise RuntimeError(f"upstream echoed {api_key}")

    anthropic_provider = AnthropicProvider(
        model_name="claude-test",
        api_key=api_key,
        client=SimpleNamespace(messages=FailingAnthropicMessages()),
    )
    with pytest.raises(RuntimeError) as anthropic_error:
        _ = [chunk async for chunk in anthropic_provider.stream_chat([])]
    assert api_key not in str(anthropic_error.value)
    assert "[REDACTED]" in str(anthropic_error.value)


@pytest.mark.asyncio
async def test_anthropic_provider_error_redacts_ambient_api_key(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    del tmp_path
    from ash.providers.anthropic import AnthropicProvider

    ambient_key = "ambient-short"
    monkeypatch.setenv("ANTHROPIC_API_KEY", ambient_key)

    class FailingAnthropicMessages:
        def stream(self, **kwargs: Any) -> Any:
            del kwargs
            raise RuntimeError(f"upstream echoed {ambient_key}")

    provider = AnthropicProvider(
        model_name="claude-test",
        api_key="",
        client=SimpleNamespace(messages=FailingAnthropicMessages()),
    )

    with pytest.raises(RuntimeError) as exc_info:
        _ = [chunk async for chunk in provider.stream_chat([])]

    assert ambient_key not in str(exc_info.value)
    assert "[REDACTED]" in str(exc_info.value)


def _openai_chunk(
    *,
    content: str = "",
    finish_reason: str | None = None,
    usage: Any = None,
    reasoning: str | None = None,
    reasoning_content: str | None = None,
) -> Any:
    choices = []
    if (
        finish_reason is not None
        or content
        or reasoning is not None
        or reasoning_content is not None
    ):
        delta = SimpleNamespace(content=content, tool_calls=None)
        if reasoning is not None:
            delta.reasoning = reasoning
        if reasoning_content is not None:
            delta.reasoning_content = reasoning_content
        choices.append(
            SimpleNamespace(
                delta=delta,
                finish_reason=finish_reason,
            )
        )
    return SimpleNamespace(choices=choices, usage=usage)


def _openai_tool_chunk(arguments: str) -> Any:
    tool_call = SimpleNamespace(
        index=0,
        id="call-1",
        function=SimpleNamespace(name="read_file", arguments=arguments),
    )
    return SimpleNamespace(
        choices=[
            SimpleNamespace(
                delta=SimpleNamespace(content="", tool_calls=[tool_call]),
                finish_reason=None,
            )
        ],
        usage=None,
    )


def _google_tool_chunk(arguments: str, *, signature: str) -> Any:
    tool_call = SimpleNamespace(
        index=0,
        id="call-1",
        function=SimpleNamespace(name="read_file", arguments=arguments),
        extra_content={"google": {"thought_signature": signature}},
    )
    return SimpleNamespace(
        choices=[
            SimpleNamespace(
                delta=SimpleNamespace(content="", tool_calls=[tool_call]),
                finish_reason=None,
            )
        ],
        usage=None,
    )


@pytest.mark.asyncio
async def test_google_effort_selection_reaches_openai_compatible_request() -> None:
    from ash.providers.capabilities import google_capabilities
    from ash.providers.openai_compatible import CatalogOpenAIProvider

    model = "gemini-3.8-flash"
    client = _FakeOpenAIClient([_openai_chunk(content="ok", finish_reason="stop")])
    provider = CatalogOpenAIProvider(
        model_name=model,
        api_key="test-key",
        provider_family="google",
        base_url="https://generativelanguage.googleapis.com/v1beta/openai/",
        catalog_endpoint=(
            "https://generativelanguage.googleapis.com/v1beta/models/" + model
        ),
        catalog_format="google",
        catalog_headers={"x-goog-api-key": "test-key"},
        declared_capabilities=google_capabilities(model),
        client=client,
    )
    provider.configure_reasoning_effort("high")

    _ = [
        chunk
        async for chunk in provider.stream_chat(
            [{"role": "user", "content": "hello"}]
        )
    ]

    assert client.completions.kwargs["reasoning_effort"] == "high"


@pytest.mark.parametrize(
    ("provider_name", "error_name"),
    [
        ("openai", "OpenAI"),
        ("deepseek", "DeepSeek"),
        ("groq", "Groq"),
    ],
)
@pytest.mark.asyncio
async def test_openai_compatible_providers_bound_streamed_tool_arguments(
    monkeypatch: pytest.MonkeyPatch,
    provider_name: str,
    error_name: str,
) -> None:
    import ash.providers.openai as openai_module
    from ash.providers.deepseek import DeepSeekProvider
    from ash.providers.groq import GroqProvider
    from ash.providers.openai import OpenAIProvider

    monkeypatch.setattr(openai_module, "MAX_OPENAI_COMPATIBLE_STREAM_BYTES", 32)
    client = _FakeOpenAIClient([_openai_tool_chunk("x" * 64)])
    if provider_name == "openai":
        provider = OpenAIProvider("test", "key", client=client)
    elif provider_name == "deepseek":
        monkeypatch.setattr(
            "ash.providers.deepseek.openai.AsyncOpenAI", lambda **_: client
        )
        provider = DeepSeekProvider("test", "key")
    else:
        monkeypatch.setattr("ash.providers.groq.openai.AsyncOpenAI", lambda **_: client)
        provider = GroqProvider("test", "key")

    with pytest.raises(RuntimeError, match=rf"{error_name} stream exceeded 32 bytes"):
        _ = [chunk async for chunk in provider.stream_chat([])]


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
    assert client.completions.last_stream is not None
    assert client.completions.last_stream.closed == 1

    await provider.aclose()
    assert client.closed is False


@pytest.mark.asyncio
async def test_openai_provider_cancellation_closes_sdk_stream() -> None:
    from ash.providers.openai import OpenAIProvider

    class BlockingStream:
        def __init__(self) -> None:
            self.started = asyncio.Event()
            self.release = asyncio.Event()
            self.closed = 0

        def __aiter__(self):
            return self

        async def __anext__(self):
            self.started.set()
            await self.release.wait()
            raise StopAsyncIteration

        async def aclose(self) -> None:
            self.closed += 1
            self.release.set()

    class Completions:
        def __init__(self, stream: BlockingStream) -> None:
            self.stream = stream

        async def create(self, **kwargs: Any) -> BlockingStream:
            del kwargs
            return self.stream

    sdk_stream = BlockingStream()
    provider = OpenAIProvider(
        model_name="test",
        api_key="test-key",
        client=SimpleNamespace(
            chat=SimpleNamespace(completions=Completions(sdk_stream))
        ),
    )
    provider_stream = provider.stream_chat([{"role": "user", "content": "hi"}])
    task = asyncio.create_task(anext(provider_stream))
    await asyncio.wait_for(sdk_stream.started.wait(), timeout=1)

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert sdk_stream.closed == 1
    await provider_stream.aclose()


@pytest.mark.asyncio
async def test_openai_responses_provider_uses_native_tools_and_normalizes_cache_usage() -> None:
    from ash.providers.openai_responses import OpenAIResponsesProvider

    class Events:
        def __init__(self, events: list[Any]) -> None:
            self.events = iter(events)
            self.closed = 0

        def __aiter__(self):
            return self

        async def __anext__(self):
            try:
                return next(self.events)
            except StopIteration as exc:
                raise StopAsyncIteration from exc

        async def aclose(self) -> None:
            self.closed += 1

    class Responses:
        def __init__(self, events: list[Any]) -> None:
            self.events = events
            self.kwargs: dict[str, Any] = {}
            self.last_stream: Events | None = None

        async def create(self, **kwargs: Any) -> Events:
            self.kwargs = kwargs
            self.last_stream = Events(self.events)
            return self.last_stream

    class Client:
        def __init__(self, events: list[Any]) -> None:
            self.responses = Responses(events)

    reasoning = SimpleNamespace(
        type="reasoning",
        id="rs_1",
        encrypted_content="opaque",
        summary=[],
        status="completed",
    )
    function_call = SimpleNamespace(
        type="function_call",
        call_id="call_1",
        name="read_file",
        arguments='{"file_path":"a.py"}',
    )
    usage = SimpleNamespace(
        input_tokens=100,
        output_tokens=20,
        input_tokens_details=SimpleNamespace(
            cached_tokens=40,
            cache_write_tokens=30,
        ),
    )
    client = Client(
        [
            SimpleNamespace(type="response.output_text.delta", delta="working"),
            SimpleNamespace(type="response.output_item.done", item=reasoning),
            SimpleNamespace(type="response.output_item.done", item=function_call),
            SimpleNamespace(
                type="response.completed",
                response=SimpleNamespace(usage=usage),
            ),
        ]
    )
    provider = OpenAIResponsesProvider(
        "gpt-6.1-sol",
        "test-key",
        client=client,
    )
    provider.configure_max_tokens(4096)
    provider.configure_prompt_cache(
        enabled=True,
        cache_key="ash-project-test",
        retention="memory",
    )
    provider.configure_reasoning_effort("high")

    chunks = [
        chunk
        async for chunk in provider.stream_chat(
            [{"role": "user", "content": "inspect"}],
            tools=[
                {
                    "type": "function",
                    "function": {
                        "name": "read_file",
                        "description": "Read a file",
                        "parameters": {
                            "type": "object",
                            "properties": {"file_path": {"type": "string"}},
                        },
                    },
                }
            ],
        )
    ]

    request = client.responses.kwargs
    assert request["store"] is False
    assert request["stream"] is True
    assert request["max_output_tokens"] == 4096
    assert request["prompt_cache_key"] == "ash-project-test"
    assert request["prompt_cache_options"] == {"mode": "implicit", "ttl": "30m"}
    assert request["reasoning"] == {"effort": "high"}
    assert "temperature" not in request
    assert request["tools"][0]["name"] == "read_file"
    terminal = chunks[-1]
    assert terminal.is_done is True
    assert terminal.prompt_tokens == 100
    assert terminal.completion_tokens == 20
    assert terminal.cache_read_tokens == 40
    assert terminal.cache_write_tokens == 30
    assert terminal.native_tool_calls is not None
    assert terminal.native_tool_calls[0].call_id == "call_1"
    assert terminal.provider_state is not None
    assert client.responses.last_stream is not None
    assert client.responses.last_stream.closed == 1


@pytest.mark.asyncio
async def test_openai_responses_provider_rejects_unsupported_sampling_controls() -> None:
    from ash.providers.openai_responses import OpenAIResponsesProvider

    provider = OpenAIResponsesProvider("gpt-6.1-sol", "test-key", client=SimpleNamespace())

    with pytest.raises(ValueError, match="custom temperature"):
        _ = [
            chunk
            async for chunk in provider.stream_chat(
                [{"role": "user", "content": "hello"}],
                temperature=0.2,
            )
        ]
    with pytest.raises(ValueError, match="24h"):
        provider.configure_prompt_cache(enabled=True, retention="extended")


@pytest.mark.parametrize(
    ("provider_name", "reasoning_field"),
    [
        ("openai", "reasoning"),
        ("deepseek", "reasoning_content"),
        ("groq", "reasoning"),
    ],
)
@pytest.mark.asyncio
async def test_openai_compatible_providers_preserve_streamed_reasoning(
    monkeypatch: pytest.MonkeyPatch,
    provider_name: str,
    reasoning_field: str,
) -> None:
    from ash.providers.deepseek import DeepSeekProvider
    from ash.providers.groq import GroqProvider
    from ash.providers.openai import OpenAIProvider

    client = _FakeOpenAIClient(
        [
            _openai_chunk(**{reasoning_field: "consider "}),
            _openai_chunk(**{reasoning_field: "carefully"}),
            _openai_chunk(content="answer", finish_reason="stop"),
        ]
    )
    if provider_name == "openai":
        provider = OpenAIProvider("test", "key", client=client)
    elif provider_name == "deepseek":
        monkeypatch.setattr(
            "ash.providers.deepseek.openai.AsyncOpenAI", lambda **_: client
        )
        provider = DeepSeekProvider("test", "key")
    else:
        monkeypatch.setattr("ash.providers.groq.openai.AsyncOpenAI", lambda **_: client)
        provider = GroqProvider("test", "key")

    chunks = [chunk async for chunk in provider.stream_chat([])]

    assert chunks[0].reasoning is None
    assert chunks[1].reasoning is None
    assert chunks[-1].reasoning == [
        {"type": "thinking", "thinking": "consider carefully"}
    ]
    assert chunks[-1].content == "answer"


@pytest.mark.asyncio
async def test_google_seals_and_replays_openai_compatible_thought_signature(
    tmp_path: Path,
) -> None:
    import json

    from ash.providers.capabilities import google_capabilities
    from ash.providers.openai_compatible import CatalogOpenAIProvider
    from ash.providers.replay_state import ProviderReplayStateCipher

    signature = "opaque-google-thought-signature"
    terminal = SimpleNamespace(
        choices=[
            SimpleNamespace(
                delta=SimpleNamespace(content="", tool_calls=None),
                finish_reason="tool_calls",
            )
        ],
        usage=None,
    )
    state_dir = tmp_path / "provider-state"
    first_client = _FakeOpenAIClient(
        [_google_tool_chunk("{}", signature=signature), terminal]
    )
    first = CatalogOpenAIProvider(
        "gemini-3.8-flash",
        "key",
        provider_family="google",
        base_url="https://generativelanguage.googleapis.com/v1beta/openai",
        catalog_endpoint="https://example.test/models",
        catalog_format="openai",
        catalog_headers={},
        declared_capabilities=google_capabilities("gemini-3.8-flash"),
        replay_state_cipher=ProviderReplayStateCipher(
            state_dir,
            trusted_root=tmp_path,
        ),
        google_thought_signature_replay=True,
        client=first_client,
    )
    tools = [
        {
            "type": "function",
            "function": {
                "name": "read_file",
                "parameters": {"type": "object", "properties": {}},
            },
        }
    ]

    first_chunks = [
        chunk
        async for chunk in first.stream_chat(
            [{"role": "user", "content": "read it"}],
            tools=tools,
        )
    ]
    first_terminal = first_chunks[-1]
    assert first_terminal.provider_state is not None
    assert signature not in json.dumps(first_terminal.provider_state)
    assert first_terminal.native_tool_calls is not None

    second_client = _FakeOpenAIClient(
        [_openai_chunk(content="done", finish_reason="stop")]
    )
    second = CatalogOpenAIProvider(
        "gemini-3.8-flash",
        "key",
        provider_family="google",
        base_url="https://generativelanguage.googleapis.com/v1beta/openai",
        catalog_endpoint="https://example.test/models",
        catalog_format="openai",
        catalog_headers={},
        declared_capabilities=google_capabilities("gemini-3.8-flash"),
        replay_state_cipher=ProviderReplayStateCipher(
            state_dir,
            trusted_root=tmp_path,
        ),
        google_thought_signature_replay=True,
        client=second_client,
    )
    history = [
        {"role": "user", "content": "read it"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                call.to_wire() for call in first_terminal.native_tool_calls
            ],
            "provider_state": first_terminal.provider_state,
        },
        {
            "role": "tool",
            "tool_call_id": "call-1",
            "content": "file contents",
        },
    ]

    _ = [chunk async for chunk in second.stream_chat(history, tools=tools)]

    replayed = second_client.completions.kwargs["messages"][1]
    assert replayed["tool_calls"][0]["extra_content"] == {
        "google": {"thought_signature": signature}
    }


@pytest.mark.asyncio
async def test_google_gemini3_tool_history_fails_closed_without_signature_state() -> None:
    from ash.providers.capabilities import google_capabilities
    from ash.providers.openai_compatible import CatalogOpenAIProvider

    provider = CatalogOpenAIProvider(
        "gemini-3.8-flash",
        "key",
        provider_family="google",
        base_url="https://generativelanguage.googleapis.com/v1beta/openai",
        catalog_endpoint="https://example.test/models",
        catalog_format="openai",
        catalog_headers={},
        declared_capabilities=google_capabilities("gemini-3.8-flash"),
        google_thought_signature_replay=True,
        client=_FakeOpenAIClient([]),
    )
    history = [
        {"role": "user", "content": "read it"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {"call_id": "call-1", "name": "read_file", "arguments": {}}
            ],
        },
        {"role": "tool", "tool_call_id": "call-1", "content": "contents"},
    ]

    with pytest.raises(RuntimeError, match="missing required thought-signature"):
        _ = [chunk async for chunk in provider.stream_chat(history, tools=[])]


@pytest.mark.asyncio
async def test_deepseek_seals_and_replays_reasoning_across_provider_restart(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import json

    from ash.providers.deepseek import DeepSeekProvider
    from ash.providers.replay_state import ProviderReplayStateCipher

    reasoning = "exact private reasoning that must be replayed"
    finish_tool = SimpleNamespace(
        choices=[
            SimpleNamespace(
                delta=SimpleNamespace(content="", tool_calls=None),
                finish_reason="tool_calls",
            )
        ],
        usage=None,
    )
    first_client = _FakeOpenAIClient(
        [
            _openai_chunk(reasoning_content=reasoning),
            _openai_tool_chunk("{}"),
            finish_tool,
        ]
    )
    state_dir = tmp_path / "provider-state"
    first_cipher = ProviderReplayStateCipher(state_dir, trusted_root=tmp_path)
    monkeypatch.setattr(
        "ash.providers.deepseek.openai.AsyncOpenAI",
        lambda **_: first_client,
    )
    first = DeepSeekProvider(
        "deepseek-flash",
        "key",
        replay_state_cipher=first_cipher,
    )
    tools = [
        {
            "type": "function",
            "function": {
                "name": "read_file",
                "parameters": {"type": "object", "properties": {}},
            },
        }
    ]

    first_chunks = [
        chunk
        async for chunk in first.stream_chat(
            [{"role": "user", "content": "read it"}],
            tools=tools,
        )
    ]
    terminal = first_chunks[-1]
    assert terminal.provider_state is not None
    assert reasoning not in json.dumps(terminal.provider_state)
    assert terminal.native_tool_calls is not None

    second_client = _FakeOpenAIClient(
        [_openai_chunk(content="done", finish_reason="stop")]
    )
    monkeypatch.setattr(
        "ash.providers.deepseek.openai.AsyncOpenAI",
        lambda **_: second_client,
    )
    second = DeepSeekProvider(
        "deepseek-flash",
        "key",
        replay_state_cipher=ProviderReplayStateCipher(
            state_dir,
            trusted_root=tmp_path,
        ),
    )
    history = [
        {"role": "user", "content": "read it"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [call.to_wire() for call in terminal.native_tool_calls],
            "provider_state": terminal.provider_state,
        },
        {
            "role": "tool",
            "tool_call_id": "call-1",
            "content": "file contents",
        },
    ]

    _ = [chunk async for chunk in second.stream_chat(history, tools=tools)]

    replayed = second_client.completions.kwargs["messages"][1]
    assert replayed["reasoning_content"] == reasoning
    assert replayed["tool_calls"][0]["id"] == "call-1"


@pytest.mark.asyncio
async def test_deepseek_tool_reasoning_fails_closed_without_replay_store(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ash.providers.deepseek import DeepSeekProvider

    finish_tool = SimpleNamespace(
        choices=[
            SimpleNamespace(
                delta=SimpleNamespace(content="", tool_calls=None),
                finish_reason="tool_calls",
            )
        ],
        usage=None,
    )
    client = _FakeOpenAIClient(
        [
            _openai_chunk(reasoning_content="must replay"),
            _openai_tool_chunk("{}"),
            finish_tool,
        ]
    )
    monkeypatch.setattr(
        "ash.providers.deepseek.openai.AsyncOpenAI",
        lambda **_: client,
    )
    provider = DeepSeekProvider("deepseek-flash", "key")

    with pytest.raises(RuntimeError, match="requires durable reasoning replay"):
        _ = [
            chunk
            async for chunk in provider.stream_chat(
                [{"role": "user", "content": "use a tool"}],
                tools=[
                    {
                        "type": "function",
                        "function": {
                            "name": "read_file",
                            "parameters": {"type": "object", "properties": {}},
                        },
                    }
                ],
            )
        ]


def test_deepseek_current_model_capabilities_are_exact_and_unknown_is_conservative() -> None:
    from ash.providers.capabilities import ProviderCapabilities
    from ash.providers.deepseek import DeepSeekProvider

    assert DeepSeekProvider("deepseek-flash", "key").capabilities == ProviderCapabilities(
        native_tools=True,
        vision=True,
        reasoning=True,
        context_window=1_000_000,
        max_output_tokens=384_000,
    )
    assert DeepSeekProvider("deepseek-v4-pro", "key").capabilities == ProviderCapabilities(
        native_tools=True,
        reasoning=True,
        context_window=1_000_000,
        max_output_tokens=384_000,
    )
    assert DeepSeekProvider("future-model", "key").capabilities == ProviderCapabilities()


@pytest.mark.asyncio
async def test_openai_compatible_reasoning_counts_toward_stream_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import ash.providers.openai as openai_module
    from ash.providers.openai import OpenAIProvider

    monkeypatch.setattr(openai_module, "MAX_OPENAI_COMPATIBLE_STREAM_BYTES", 8)
    provider = OpenAIProvider(
        "test",
        "key",
        client=_FakeOpenAIClient([_openai_chunk(reasoning="x" * 9)]),
    )

    with pytest.raises(RuntimeError, match="OpenAI stream exceeded 8 bytes"):
        _ = [chunk async for chunk in provider.stream_chat([])]


@pytest.mark.asyncio
async def test_openai_compatible_rejects_non_text_reasoning() -> None:
    from ash.providers.openai import OpenAIProvider

    delta = SimpleNamespace(content="", tool_calls=None, reasoning={"bad": "shape"})
    chunk = SimpleNamespace(
        choices=[SimpleNamespace(delta=delta, finish_reason=None)],
        usage=None,
    )
    provider = OpenAIProvider(
        "test",
        "key",
        client=_FakeOpenAIClient([chunk]),
    )

    with pytest.raises(RuntimeError, match="non-text reasoning"):
        _ = [item async for item in provider.stream_chat([])]


@pytest.mark.asyncio
async def test_deepseek_preserves_provider_cache_read_usage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ash.providers.deepseek import DeepSeekProvider

    usage = SimpleNamespace(
        prompt_tokens=100,
        completion_tokens=20,
        prompt_tokens_details=SimpleNamespace(cached_tokens=60),
        prompt_cache_hit_tokens=60,
    )
    client = _FakeOpenAIClient(
        [
            _openai_chunk(content="answer"),
            _openai_chunk(finish_reason="stop", usage=usage),
        ]
    )
    monkeypatch.setattr(
        "ash.providers.deepseek.openai.AsyncOpenAI", lambda **_: client
    )
    provider = DeepSeekProvider("deepseek-flash", "key")

    chunks = [chunk async for chunk in provider.stream_chat([])]

    assert chunks[-1].prompt_tokens == 100
    assert chunks[-1].completion_tokens == 20
    assert chunks[-1].cache_read_tokens == 60
    assert chunks[-1].usage_source == "provider"


@pytest.mark.asyncio
async def test_groq_uses_live_limits_with_exact_model_capabilities(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ash.providers.capabilities import ProviderCapabilities
    from ash.providers.groq import GroqProvider

    client = _FakeOpenAIClient(
        [],
        model_info=SimpleNamespace(
            model_extra={
                "active": True,
                "context_window": 120_000,
                "max_completion_tokens": 60_000,
            },
        ),
    )
    monkeypatch.setattr(
        "ash.providers.groq.openai.AsyncOpenAI",
        lambda **_: client,
    )
    provider = GroqProvider("openai/gpt-oss-120b", "key")

    assert provider.capabilities == ProviderCapabilities(
        native_tools=True,
        reasoning=True,
        context_window=131_072,
        max_output_tokens=65_536,
    )
    assert await provider.detect_capabilities() == ProviderCapabilities(
        native_tools=True,
        reasoning=True,
        context_window=120_000,
        max_output_tokens=60_000,
    )
    assert client.models.requested == ["openai/gpt-oss-120b"]


@pytest.mark.asyncio
async def test_groq_unknown_model_keeps_semantics_conservative(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ash.providers.capabilities import ProviderCapabilities
    from ash.providers.groq import GroqProvider

    client = _FakeOpenAIClient(
        [],
        model_info=SimpleNamespace(
            active=True,
            context_window=98_304,
            max_completion_tokens=8_192,
        ),
    )
    monkeypatch.setattr(
        "ash.providers.groq.openai.AsyncOpenAI",
        lambda **_: client,
    )
    provider = GroqProvider("future-model", "key")

    assert provider.capabilities == ProviderCapabilities()
    assert await provider.detect_capabilities() == ProviderCapabilities(
        context_window=98_304,
        max_output_tokens=8_192,
    )


@pytest.mark.asyncio
async def test_groq_inactive_model_fails_capabilities_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ash.providers.capabilities import ProviderCapabilities
    from ash.providers.groq import GroqProvider

    client = _FakeOpenAIClient(
        [],
        model_info=SimpleNamespace(
            active=False,
            context_window=131_072,
            max_completion_tokens=65_536,
        ),
    )
    monkeypatch.setattr(
        "ash.providers.groq.openai.AsyncOpenAI",
        lambda **_: client,
    )
    provider = GroqProvider("openai/gpt-oss-120b", "key")

    assert await provider.detect_capabilities() == ProviderCapabilities()


def test_groq_current_known_model_capabilities_are_exact() -> None:
    from ash.providers.capabilities import ProviderCapabilities, groq_capabilities

    assert groq_capabilities("openai/gpt-oss-20b") == ProviderCapabilities(
        native_tools=True,
        reasoning=True,
        context_window=131_072,
        max_output_tokens=65_536,
    )
    assert groq_capabilities("llama-3.3-70b-versatile") == ProviderCapabilities(
        native_tools=True,
        context_window=131_072,
        max_output_tokens=32_768,
    )
    assert groq_capabilities("llama-3.1-8b-instant") == ProviderCapabilities(
        native_tools=True,
        context_window=131_072,
        max_output_tokens=131_072,
    )
    assert groq_capabilities("qwen/qwen3.8-27b") == ProviderCapabilities(
        native_tools=True,
        vision=True,
        reasoning=True,
        context_window=131_072,
        max_output_tokens=16_384,
    )
    assert groq_capabilities("groq/compound-mini") == ProviderCapabilities()


@pytest.mark.asyncio
async def test_groq_preserves_cache_usage_and_uses_current_completion_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ash.providers.groq import GroqProvider

    usage = SimpleNamespace(
        prompt_tokens=100,
        completion_tokens=20,
        prompt_tokens_details=SimpleNamespace(cached_tokens=64),
    )
    client = _FakeOpenAIClient(
        [_openai_chunk(content="answer", finish_reason="stop", usage=usage)]
    )
    monkeypatch.setattr(
        "ash.providers.groq.openai.AsyncOpenAI",
        lambda **_: client,
    )
    provider = GroqProvider("openai/gpt-oss-20b", "key")
    provider.configure_max_tokens(4096)

    chunks = [chunk async for chunk in provider.stream_chat([])]

    assert chunks[-1].cache_read_tokens == 64
    assert client.completions.kwargs["max_completion_tokens"] == 4096
    assert "max_tokens" not in client.completions.kwargs


@pytest.mark.parametrize(
    "provider_name",
    [
        "deepseek",
        "groq",
    ],
)
@pytest.mark.asyncio
async def test_openai_compatible_providers_handle_usage_only_terminal_chunk(
    monkeypatch: pytest.MonkeyPatch,
    provider_name: str,
) -> None:
    from ash.providers.deepseek import DeepSeekProvider
    from ash.providers.groq import GroqProvider

    usage = SimpleNamespace(prompt_tokens=1200, completion_tokens=10)
    client = _FakeOpenAIClient(
        [
            _openai_chunk(content="hello"),
            _openai_chunk(finish_reason="stop"),
            _openai_chunk(usage=usage),
        ]
    )
    if provider_name == "deepseek":
        monkeypatch.setattr(
            "ash.providers.deepseek.openai.AsyncOpenAI", lambda **_: client
        )
        provider = DeepSeekProvider("test", "key")
    else:
        monkeypatch.setattr("ash.providers.groq.openai.AsyncOpenAI", lambda **_: client)
        provider = GroqProvider("test", "key")

    chunks = [
        chunk
        async for chunk in provider.stream_chat([{"role": "user", "content": "hi"}])
    ]

    assert "".join(chunk.content for chunk in chunks) == "hello"
    assert chunks[-1].is_done is True
    assert chunks[-1].prompt_tokens == 1200
    assert chunks[-1].completion_tokens == 10
    assert chunks[-1].usage_source == "provider"

    await provider.aclose()
    assert client.closed is True


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


def test_provider_adapters_reject_plaintext_remote_credentials() -> None:
    from ash.providers.anthropic import AnthropicProvider
    from ash.providers.deepseek import DeepSeekProvider
    from ash.providers.groq import GroqProvider
    from ash.providers.openai import OpenAIProvider
    from ash.providers.readiness import ProviderConfigurationError

    with pytest.raises(ProviderConfigurationError, match="must use HTTPS"):
        OpenAIProvider(
            model_name="model",
            api_key="secret",
            base_url="http://gateway.example/v1",
        )
    with pytest.raises(ProviderConfigurationError, match="must use HTTPS"):
        AnthropicProvider(
            model_name="model",
            api_key="secret",
            base_url="http://gateway.example/v1",
        )
    with pytest.raises(ProviderConfigurationError, match="must use HTTPS"):
        DeepSeekProvider(
            model_name="model",
            api_key="secret",
            base_url="http://gateway.example/v1",
        )
    with pytest.raises(ProviderConfigurationError, match="must use HTTPS"):
        GroqProvider(
            model_name="model",
            api_key="secret",
            base_url="http://gateway.example/v1",
        )


@pytest.mark.parametrize(
    "base_url",
    [
        "https://gateway.example:not-a-port/v1",
        "https://gateway.example:99999/v1",
        "https://gateway.example:0/v1",
    ],
)
def test_provider_adapters_reject_invalid_base_url_ports(base_url: str) -> None:
    from ash.providers.anthropic import AnthropicProvider
    from ash.providers.deepseek import DeepSeekProvider
    from ash.providers.groq import GroqProvider
    from ash.providers.ollama import OllamaProvider
    from ash.providers.openai import OpenAIProvider
    from ash.providers.readiness import ProviderConfigurationError

    constructors = [
        lambda: OpenAIProvider("model", "secret", base_url=base_url),
        lambda: AnthropicProvider("model", "secret", base_url=base_url),
        lambda: DeepSeekProvider("model", "secret", base_url=base_url),
        lambda: GroqProvider("model", "secret", base_url=base_url),
        lambda: OllamaProvider("model", base_url=base_url),
    ]

    for construct in constructors:
        with pytest.raises(ProviderConfigurationError, match="base URL"):
            construct()


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
async def test_anthropic_does_not_forward_deprecated_temperature() -> None:
    from ash.providers.anthropic import AnthropicProvider

    messages = _FakeAnthropicMessages(SimpleNamespace())
    provider = AnthropicProvider(
        model_name="claude-opus-5-5",
        api_key="test-key",
        client=SimpleNamespace(messages=messages),
    )
    provider.configure_reasoning_effort("xhigh")

    _ = [
        chunk
        async for chunk in provider.stream_chat(
            [{"role": "user", "content": "hello"}],
            temperature=0.2,
        )
    ]

    assert "temperature" not in messages.kwargs
    assert messages.kwargs["output_config"] == {"effort": "xhigh"}


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
    http_calls: list[dict[str, Any]] = []

    def fake_async_anthropic(**kwargs: Any) -> Any:
        sdk_calls.append(kwargs)
        if "api_key" not in kwargs:
            inherited_keys.append(os.environ["ANTHROPIC_API_KEY"])
        return SimpleNamespace(messages=_FakeAnthropicMessages(SimpleNamespace()))

    def fake_default_http_client(**kwargs: Any) -> Any:
        http_calls.append(kwargs)
        return SimpleNamespace(kind="anthropic-http")

    monkeypatch.setitem(
        sys.modules,
        "anthropic",
        SimpleNamespace(
            AsyncAnthropic=fake_async_anthropic,
            DefaultAsyncHttpxClient=fake_default_http_client,
        ),
    )

    provider = AnthropicProvider(model_name="claude-test", api_key="")
    chunks = [
        chunk
        async for chunk in provider.stream_chat(
            [{"role": "user", "content": "hello"}]
        )
    ]

    assert "".join(chunk.content for chunk in chunks) == "hello"
    assert http_calls == [{"follow_redirects": False}]
    assert len(sdk_calls) == 1
    assert sdk_calls[0]["max_retries"] == 0
    assert sdk_calls[0]["http_client"].kind == "anthropic-http"
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


@pytest.mark.asyncio
async def test_anthropic_capabilities_use_provider_model_metadata() -> None:
    from ash.providers.anthropic import AnthropicProvider
    from ash.providers.capabilities import ProviderCapabilities, infer_capabilities

    class Models:
        async def retrieve(self, model: str) -> Any:
            assert model == "claude-sonnet-5-5"
            return SimpleNamespace(
                id="claude-sonnet-5-5",
                capabilities=SimpleNamespace(
                    image_input=SimpleNamespace(supported=True),
                    thinking=SimpleNamespace(supported=True),
                ),
                max_input_tokens=1_000_000,
                max_tokens=128_000,
            )

    client = SimpleNamespace(
        messages=_FakeAnthropicMessages(SimpleNamespace()),
        models=Models(),
    )
    provider = AnthropicProvider(
        model_name="claude-sonnet-5-5",
        api_key="test-key",
        client=client,
    )

    assert await provider.detect_capabilities() == ProviderCapabilities(
        native_tools=True,
        vision=True,
        reasoning=True,
        context_window=1_000_000,
        max_output_tokens=128_000,
        reasoning_effort=infer_capabilities(
            "anthropic", "claude-sonnet-5-5"
        ).reasoning_effort,
    )


@pytest.mark.asyncio
async def test_anthropic_unknown_model_metadata_keeps_tools_conservative() -> None:
    from ash.providers.anthropic import AnthropicProvider
    from ash.providers.capabilities import ProviderCapabilities

    class Models:
        async def retrieve(self, model: str) -> Any:
            assert model == "future-claude"
            return SimpleNamespace(
                id="future-claude",
                capabilities=SimpleNamespace(
                    image_input=SimpleNamespace(supported=True),
                    thinking=SimpleNamespace(supported=True),
                ),
                max_input_tokens=2_000_000,
                max_tokens=256_000,
            )

    provider = AnthropicProvider(
        model_name="future-claude",
        api_key="test-key",
        client=SimpleNamespace(
            messages=_FakeAnthropicMessages(SimpleNamespace()), models=Models()
        ),
    )

    assert await provider.detect_capabilities() == ProviderCapabilities(
        native_tools=False,
        vision=True,
        reasoning=True,
        context_window=2_000_000,
        max_output_tokens=256_000,
    )


@pytest.mark.asyncio
async def test_anthropic_capability_probe_without_models_api_uses_static_truth() -> None:
    from ash.providers.anthropic import AnthropicProvider
    from ash.providers.capabilities import ProviderCapabilities, infer_capabilities

    provider = AnthropicProvider(
        model_name="claude-sonnet-5-5",
        api_key="test-key",
        client=SimpleNamespace(messages=_FakeAnthropicMessages(SimpleNamespace())),
    )

    assert await provider.detect_capabilities() == ProviderCapabilities(
        native_tools=True,
        vision=True,
        reasoning=True,
        context_window=1_000_000,
        max_output_tokens=128_000,
        reasoning_effort=infer_capabilities(
            "anthropic", "claude-sonnet-5-5"
        ).reasoning_effort,
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
