"""Fail-closed malformed Responses arguments through the actual Ash request loop."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from ash.config import AshConfig
from ash.core.loop import AshLoop
from ash.core.session import SessionStore
from ash.exceptions import ErrorCategory, classify_exception
from ash.providers.base import ProviderMalformedToolArgumentsError
from ash.providers.capabilities import ProviderCapabilities
from ash.providers.failover import FailoverProvider
from ash.providers.openai_chatgpt import OpenAIChatGPTProvider
from ash.providers.openai_responses import stream_openai_responses
from ash.providers.retry import classify_provider_failure
from ash.safety.guard import SafetyGuard
from ash.ui.terminal import TerminalUI


def event(name: str, **fields: Any) -> Any:
    return SimpleNamespace(type=name, **fields)


def call(arguments: str, *, call_id: str = "call-1") -> Any:
    return event(
        "response.output_item.done",
        item=SimpleNamespace(
            type="function_call",
            name="read_file",
            call_id=call_id,
            arguments=arguments,
        ),
    )


def completed() -> Any:
    return event("response.completed", response=SimpleNamespace(usage=None))


class Responses:
    def __init__(self, scripts: list[list[Any]]) -> None:
        self.scripts = scripts
        self.calls = 0

    async def create(self, **kwargs: Any) -> Any:
        del kwargs
        index = self.calls
        self.calls += 1

        async def events():
            for item in self.scripts[index]:
                yield item

        return events()


class Credentials:
    async def access_token(self) -> str:
        return "test-only-token"


class EventUI(TerminalUI):
    def __init__(self):
        super().__init__(safety_tier="auto_approve")
        self.events: list[dict[str, Any]] = []

    def emit_event(self, payload: dict[str, Any]) -> None:
        self.events.append(payload)


@pytest.mark.parametrize(
    "raw",
    [
        "{",
        "[]",
        "null",
        "123",
        '"text"',
        '{"file_path":"a", "file_path":"SECRET_TOKEN"}',
        '{"file_path": NaN}',
    ],
)
@pytest.mark.asyncio
async def test_invalid_arguments_fail_closed_and_have_accurate_remedy(raw: str) -> None:
    responses = Responses([[call(raw), completed()]])
    chunks = []
    with pytest.raises(ProviderMalformedToolArgumentsError) as raised:
        async for chunk in stream_openai_responses(
            SimpleNamespace(responses=responses),
            model_name="test",
            messages=[{"role": "user", "content": "inspect"}],
            tools=[],
        ):
            chunks.append(chunk)
    assert chunks == []
    assert classify_provider_failure(raised.value).retriable is True
    info = classify_exception(raised.value)
    assert info.category is ErrorCategory.PROVIDER
    assert info.retriable is False
    assert "tool" in info.remedy.lower()
    assert "API key" not in info.remedy
    assert "network" not in info.remedy.lower()
    assert raw not in str(raised.value)
    # Suppressing traceback display with `from None` is insufficient: the
    # parser exception may retain the entire raw model response in `doc`.
    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None


@pytest.mark.asyncio
async def test_valid_then_invalid_call_is_atomic_and_undispatched() -> None:
    responses = Responses(
        [
            [
                call('{"file_path":"a.py"}', call_id="first"),
                call("{", call_id="second"),
                completed(),
            ]
        ]
    )
    chunks = []
    with pytest.raises(ProviderMalformedToolArgumentsError):
        async for chunk in stream_openai_responses(
            SimpleNamespace(responses=responses),
            model_name="test",
            messages=[{"role": "user", "content": "inspect"}],
            tools=[],
        ):
            chunks.append(chunk)
    assert chunks == []


def make_loop(tmp_path: Any, scripts: list[list[Any]], max_attempts: int = 3):
    responses = Responses(scripts)
    provider = OpenAIChatGPTProvider(
        "gpt-test",
        auth_session=Credentials(),  # type: ignore[arg-type]
        client_factory=lambda _access: SimpleNamespace(responses=responses),
    )
    provider._ash_declared_capabilities = ProviderCapabilities(native_tools=True)
    ui = EventUI()
    loop = AshLoop(
        SessionStore(tmp_path / "session.db"),
        provider,
        SafetyGuard(project_root=tmp_path),
        ui,
        tmp_path,
        config=AshConfig(
            model="openai/gpt-test",
            provider_max_attempts=max_attempts,
            provider_retry_base_delay=0,
            provider_retry_max_delay=0,
        ),
    )
    return loop, responses, ui


@pytest.mark.asyncio
async def test_recovers_one_bad_response_without_dispatch(tmp_path) -> None:
    loop, responses, ui = make_loop(
        tmp_path,
        [
            [call("{"), completed()],
            [event("response.output_text.delta", delta="recovered"), completed()],
        ],
    )
    result = await loop._stream_one_completion([])
    assert result.text == "recovered"
    assert result.tool_calls == []
    assert result.provider_state == []
    assert responses.calls == 2
    assert [e["attempt"] for e in ui.events if e["type"] == "provider.retrying"] == [2]
    assert not any(e["type"].startswith("tool.") for e in ui.events)


@pytest.mark.asyncio
async def test_two_bad_responses_stop_after_only_one_retry(tmp_path) -> None:
    loop, responses, ui = make_loop(
        tmp_path,
        [
            [call("{"), completed()],
            [call("{"), completed()],
        ],
    )
    with pytest.raises(ProviderMalformedToolArgumentsError):
        await loop._stream_one_completion([])
    assert responses.calls == 2
    assert [e["attempt"] for e in ui.events if e["type"] == "provider.retrying"] == [2]
    assert not any(e["type"].startswith("tool.") for e in ui.events)


@pytest.mark.asyncio
async def test_partial_text_then_bad_call_never_retries(tmp_path) -> None:
    loop, responses, ui = make_loop(
        tmp_path,
        [
            [
                event("response.output_text.delta", delta="partial"),
                call("{"),
                completed(),
            ]
        ],
    )
    with pytest.raises(ProviderMalformedToolArgumentsError):
        await loop._stream_one_completion([])
    assert responses.calls == 1
    assert not any(e["type"] == "provider.retrying" for e in ui.events)


@pytest.mark.asyncio
async def test_single_attempt_configuration_prevents_extra_request(tmp_path) -> None:
    loop, responses, ui = make_loop(tmp_path, [[call("{"), completed()]], 1)
    with pytest.raises(ProviderMalformedToolArgumentsError):
        await loop._stream_one_completion([])
    assert responses.calls == 1
    assert not any(e["type"] == "provider.retrying" for e in ui.events)


def make_failover_loop(tmp_path: Any, *, include_backup: bool):
    attempts = Responses([[call("{"), completed()] for _ in range(5)])
    primary = OpenAIChatGPTProvider(
        "gpt-primary",
        auth_session=Credentials(),  # type: ignore[arg-type]
        client_factory=lambda _access: SimpleNamespace(responses=attempts),
    )
    primary._ash_declared_capabilities = ProviderCapabilities(native_tools=True)
    providers = [primary]
    backup_responses = Responses(
        [[event("response.output_text.delta", delta="backup"), completed()]]
    )
    if include_backup:
        backup = OpenAIChatGPTProvider(
            "gpt-backup",
            auth_session=Credentials(),  # type: ignore[arg-type]
            client_factory=lambda _access: SimpleNamespace(responses=backup_responses),
        )
        backup._ash_declared_capabilities = ProviderCapabilities(native_tools=True)
        providers.append(backup)
    ui = EventUI()
    loop = AshLoop(
        SessionStore(tmp_path / "failover.db"),
        FailoverProvider(providers),
        SafetyGuard(project_root=tmp_path),
        ui,
        tmp_path,
        config=AshConfig(
            model="openai/gpt-primary",
            provider_max_attempts=3,
            provider_retry_base_delay=0,
            provider_retry_max_delay=0,
        ),
    )
    return loop, attempts, backup_responses, ui


@pytest.mark.parametrize("include_backup", [False, True])
@pytest.mark.asyncio
async def test_failover_preserves_malformed_class_and_one_retry_budget(
    tmp_path, include_backup: bool
) -> None:
    loop, attempts, backup, ui = make_failover_loop(
        tmp_path, include_backup=include_backup
    )
    with pytest.raises(ProviderMalformedToolArgumentsError) as raised:
        await loop._stream_one_completion([])
    assert attempts.calls == 2
    assert backup.calls == 0
    assert [e["attempt"] for e in ui.events if e["type"] == "provider.retrying"] == [2]
    assert not any(e["type"].startswith("tool.") for e in ui.events)
    info = classify_exception(raised.value)
    assert info.category is ErrorCategory.PROVIDER
    assert "API key" not in info.remedy
    assert "network" not in info.remedy.lower()


@pytest.mark.asyncio
async def test_malformed_arguments_never_open_provider_circuit(tmp_path) -> None:
    loop, responses, ui = make_loop(
        tmp_path, [[call("{"), completed()] for _ in range(5)], max_attempts=1
    )
    for _ in range(5):
        with pytest.raises(ProviderMalformedToolArgumentsError):
            await loop._stream_one_completion([])
    assert responses.calls == 5
    status = loop.provider_circuit_breaker.snapshot(loop._provider_circuit_key)
    assert status["failures"] == 0
    assert status["open"] is False
    assert not any(e["type"] == "provider.circuit_opened" for e in ui.events)


@pytest.mark.asyncio
async def test_failover_recovers_without_switching_model(tmp_path) -> None:
    loop, attempts, backup, ui = make_failover_loop(tmp_path, include_backup=True)
    attempts.scripts = [
        [call("{"), completed()],
        [event("response.output_text.delta", delta="recovered"), completed()],
    ]
    result = await loop._stream_one_completion([])
    assert result.text == "recovered"
    assert result.tool_calls == []
    assert attempts.calls == 2
    assert backup.calls == 0
    assert [e["attempt"] for e in ui.events if e["type"] == "provider.retrying"] == [2]


@pytest.mark.asyncio
async def test_credential_pool_inside_failover_does_not_multiply_bad_requests(
    tmp_path,
) -> None:
    from ash.providers.credential_pool import CredentialPoolProvider

    responses = Responses([[call("{"), completed()] for _ in range(5)])
    native = OpenAIChatGPTProvider(
        "gpt-primary",
        auth_session=Credentials(),  # type: ignore[arg-type]
        client_factory=lambda _access: SimpleNamespace(responses=responses),
    )
    native._ash_declared_capabilities = ProviderCapabilities(native_tools=True)
    nested = FailoverProvider([CredentialPoolProvider([native], ["profile-one"])])
    ui = EventUI()
    loop = AshLoop(
        SessionStore(tmp_path / "nested.db"),
        nested,
        SafetyGuard(project_root=tmp_path),
        ui,
        tmp_path,
        config=AshConfig(
            model="openai/gpt-primary",
            provider_max_attempts=3,
            provider_retry_base_delay=0,
            provider_retry_max_delay=0,
        ),
    )
    with pytest.raises(ProviderMalformedToolArgumentsError):
        await loop._stream_one_completion([])
    assert responses.calls == 2
    assert not any(e["type"].startswith("tool.") for e in ui.events)
