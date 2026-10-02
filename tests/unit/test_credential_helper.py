from __future__ import annotations

import os
from collections.abc import Sequence
from pathlib import Path
from typing import Any, AsyncGenerator

import pytest

from ash.providers.base import ProviderABC, StreamChunk
from ash.providers.capabilities import ProviderCapabilities
from ash.providers.credential_helper import (
    CredentialHelperError,
    CredentialHelperProvider,
    CredentialHelperSource,
)
from ash.providers.credential_pool import CredentialPoolProvider
from ash.providers.messages import MessageInput
from ash.providers.retry import (
    ProviderFailureCategory,
    classify_provider_failure,
)


class APIError(RuntimeError):
    def __init__(self, message: str, status_code: int) -> None:
        super().__init__(message)
        self.status_code = status_code


class FakeProvider(ProviderABC):
    provider_family = "openai"

    def __init__(
        self,
        credential: str,
        outcomes: list[BaseException | list[StreamChunk]],
    ) -> None:
        self.credential = credential
        self.outcomes = list(outcomes)
        self.calls = 0
        self.closed = 0
        self.max_tokens: int | None = None

    @property
    def model_name(self) -> str:
        return "model-a"

    @property
    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(native_tools=True, context_window=64_000)

    def count_tokens(self, text: str) -> int:
        return len(text)

    def configure_max_tokens(self, max_tokens: int) -> None:
        self.max_tokens = max_tokens

    async def stream_chat(
        self,
        messages: Sequence[MessageInput],
        temperature: float = 0.0,
        tools: list[dict[str, Any]] | None = None,
    ) -> AsyncGenerator[StreamChunk, None]:
        del messages, temperature, tools
        self.calls += 1
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        for chunk in outcome:
            yield chunk

    async def aclose(self) -> None:
        self.closed += 1


def _write_helper(path: Path, body: str) -> Path:
    path.write_text("#!/bin/sh\nset -eu\n" + body, encoding="utf-8")
    path.chmod(0o700)
    return path


def _counting_helper(path: Path, counter: Path) -> Path:
    return _write_helper(
        path,
        f"n=0; [ ! -f '{counter}' ] || n=$(cat '{counter}'); "
        f"n=$((n + 1)); printf '%s' \"$n\" > '{counter}'; "
        "printf 'key-%s\\n' \"$n\"\n",
    )


@pytest.mark.asyncio
async def test_helper_resolves_one_line_and_caches_until_ttl(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    state = tmp_path / "state"
    counter = tmp_path / "counter"
    helper = _counting_helper(tmp_path / "helper.sh", counter)
    now = 10.0
    source = CredentialHelperSource(
        [str(helper)],
        workspace_root=workspace,
        state_directory=state,
        ttl_seconds=30,
        clock=lambda: now,
    )

    assert await source.resolve() == "key-1"
    assert await source.resolve() == "key-1"
    assert counter.read_text(encoding="utf-8") == "1"

    now = 41.0
    assert await source.resolve() == "key-2"
    assert source.generation == 2


@pytest.mark.asyncio
async def test_helper_force_refresh_bypasses_ttl(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    counter = tmp_path / "counter"
    helper = _counting_helper(tmp_path / "helper.sh", counter)
    source = CredentialHelperSource(
        [str(helper)],
        workspace_root=workspace,
        state_directory=tmp_path / "state",
        ttl_seconds=300,
    )

    assert await source.resolve() == "key-1"
    assert await source.resolve(force_refresh=True) == "key-2"


@pytest.mark.asyncio
async def test_successful_helper_confirms_process_tree_cleanup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    helper = _write_helper(tmp_path / "helper.sh", "printf 'key-1\\n'\n")
    cleanup_calls: list[int | None] = []

    async def record_cleanup(process, *, plan):
        del plan
        cleanup_calls.append(process.returncode)

    monkeypatch.setattr(
        "ash.providers.credential_helper.terminate_process_tree",
        record_cleanup,
    )
    source = CredentialHelperSource(
        [str(helper)],
        workspace_root=workspace,
        state_directory=tmp_path / "state",
    )

    assert await source.resolve() == "key-1"
    assert cleanup_calls == [0]


@pytest.mark.asyncio
async def test_helper_uses_allowlisted_environment_only(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    helper = _write_helper(
        tmp_path / "helper.sh",
        "test \"$HELPER_ALLOWED\" = allowed\n"
        "if env | grep -q '^HELPER_BLOCKED='; then exit 9; fi\n"
        "printf 'from-helper\\n'\n",
    )
    old_allowed = os.environ.get("HELPER_ALLOWED")
    old_blocked = os.environ.get("HELPER_BLOCKED")
    os.environ["HELPER_ALLOWED"] = "allowed"
    os.environ["HELPER_BLOCKED"] = "secret"
    try:
        source = CredentialHelperSource(
            [str(helper)],
            env_names=["HELPER_ALLOWED"],
            workspace_root=workspace,
            state_directory=tmp_path / "state",
        )
        assert await source.resolve() == "from-helper"
    finally:
        if old_allowed is None:
            os.environ.pop("HELPER_ALLOWED", None)
        else:
            os.environ["HELPER_ALLOWED"] = old_allowed
        if old_blocked is None:
            os.environ.pop("HELPER_BLOCKED", None)
        else:
            os.environ["HELPER_BLOCKED"] = old_blocked


@pytest.mark.asyncio
async def test_helper_never_uses_workspace_executable_or_cwd(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    state = tmp_path / "state"
    workspace_helper = _write_helper(
        workspace / "helper.sh",
        "printf 'workspace-secret\\n'\n",
    )
    source = CredentialHelperSource(
        [str(workspace_helper)],
        workspace_root=workspace,
        state_directory=state,
    )

    with pytest.raises(CredentialHelperError, match="unavailable or untrusted"):
        await source.resolve()

    trusted_helper = _write_helper(
        tmp_path / "trusted-helper.sh",
        f"test \"$PWD\" = '{state}'\nprintf 'safe-key\\n'\n",
    )
    trusted = CredentialHelperSource(
        [str(trusted_helper)],
        workspace_root=workspace,
        state_directory=state,
    )
    assert await trusted.resolve() == "safe-key"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("body", "message"),
    [
        (
            "printf 'SECRET-ONE\\nSECRET-TWO\\n'\n",
            "exactly one credential line",
        ),
        ("printf ' has-space\\n'\n", "invalid credential line"),
        ("printf '\\377'\n", "valid UTF-8"),
        ("exit 7\n", "exited with status 7"),
    ],
)
async def test_helper_rejects_invalid_output_without_echoing_it(
    tmp_path: Path,
    body: str,
    message: str,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    helper = _write_helper(tmp_path / "helper.sh", body)
    source = CredentialHelperSource(
        [str(helper)],
        workspace_root=workspace,
        state_directory=tmp_path / "state",
    )

    with pytest.raises(CredentialHelperError, match=message) as exc_info:
        await source.resolve()

    assert "SECRET-ONE" not in str(exc_info.value)
    assert "SECRET-TWO" not in str(exc_info.value)
    assert classify_provider_failure(
        exc_info.value
    ).category is ProviderFailureCategory.CREDENTIAL_SOURCE
    assert classify_provider_failure(exc_info.value).retriable is False


@pytest.mark.asyncio
async def test_helper_timeout_is_bounded_and_secret_free(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    helper = _write_helper(
        tmp_path / "helper.sh",
        "printf 'SECRET-HELPER-VALUE' >&2\nsleep 5\n",
    )
    source = CredentialHelperSource(
        [str(helper)],
        workspace_root=workspace,
        state_directory=tmp_path / "state",
        timeout_seconds=0.1,
    )

    with pytest.raises(CredentialHelperError, match="timed out") as exc_info:
        await source.resolve()

    assert "SECRET-HELPER-VALUE" not in str(exc_info.value)


@pytest.mark.asyncio
async def test_broken_helper_falls_through_to_static_credential(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    source = CredentialHelperSource(
        [str(tmp_path / "missing-helper")],
        workspace_root=workspace,
        state_directory=tmp_path / "state",
    )
    prototype = FakeProvider("prototype", [])
    helper_provider = CredentialHelperProvider(
        prototype,
        lambda credential: FakeProvider(credential, []),
        source,
    )
    static = FakeProvider(
        "static-key",
        [[StreamChunk(content="static"), StreamChunk(is_done=True)]],
    )
    pool = CredentialPoolProvider(
        [helper_provider, static],
        ["helper:openai", "OPENAI_BACKUP"],
    )

    chunks = [chunk async for chunk in pool.stream_chat([])]

    assert chunks[0].content == "static"
    assert pool.active_profile == "OPENAI_BACKUP"
    helper_snapshot = pool.snapshot()[0]
    assert helper_snapshot["reason"] == "credential_source"
    assert helper_snapshot["retry_after"] <= 10.0


@pytest.mark.asyncio
async def test_helper_provider_force_refreshes_once_on_auth_failure(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    counter = tmp_path / "counter"
    helper = _counting_helper(tmp_path / "helper.sh", counter)
    source = CredentialHelperSource(
        [str(helper)],
        workspace_root=workspace,
        state_directory=tmp_path / "state",
        ttl_seconds=300,
    )
    prototype = FakeProvider("prototype", [])
    created: list[FakeProvider] = []

    def factory(credential: str) -> ProviderABC:
        outcomes = (
            [APIError("invalid key", 401)]
            if credential == "key-1"
            else [[StreamChunk(content="ok"), StreamChunk(is_done=True)]]
        )
        provider = FakeProvider(credential, outcomes)
        created.append(provider)
        return provider

    provider = CredentialHelperProvider(prototype, factory, source)
    provider.configure_max_tokens(123)

    chunks = [chunk async for chunk in provider.stream_chat([])]

    assert chunks[0].content == "ok"
    assert [item.credential for item in created] == ["key-1", "key-2"]
    assert [item.max_tokens for item in created] == [123, 123]
    assert created[0].closed == 1
    assert counter.read_text(encoding="utf-8") == "2"


@pytest.mark.asyncio
async def test_capability_refresh_does_not_rotate_healthy_helper_credential(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    counter = tmp_path / "counter"
    helper = _counting_helper(tmp_path / "helper.sh", counter)
    source = CredentialHelperSource(
        [str(helper)],
        workspace_root=workspace,
        state_directory=tmp_path / "state",
        ttl_seconds=300,
    )
    prototype = FakeProvider("prototype", [])
    provider = CredentialHelperProvider(
        prototype,
        lambda credential: FakeProvider(credential, []),
        source,
    )

    await provider.detect_capabilities()
    await provider.detect_capabilities(refresh=True)

    assert counter.read_text(encoding="utf-8") == "1"
    assert source.generation == 1


@pytest.mark.asyncio
async def test_helper_provider_never_refreshes_after_retained_state(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    helper = _write_helper(tmp_path / "helper.sh", "printf 'key-1\\n'\n")
    source = CredentialHelperSource(
        [str(helper)],
        workspace_root=workspace,
        state_directory=tmp_path / "state",
    )
    prototype = FakeProvider("prototype", [])

    class PartialProvider(FakeProvider):
        async def stream_chat(
            self,
            messages: Sequence[MessageInput],
            temperature: float = 0.0,
            tools: list[dict[str, Any]] | None = None,
        ) -> AsyncGenerator[StreamChunk, None]:
            del messages, temperature, tools
            self.calls += 1
            yield StreamChunk(
                provider_state=[
                    {
                        "type": "reasoning",
                        "id": "opaque",
                        "summary": [],
                        "encrypted_content": "state",
                    }
                ]
            )
            raise APIError("late auth", 401)

    provider = CredentialHelperProvider(
        prototype,
        lambda credential: PartialProvider(credential, []),
        source,
    )
    seen: list[StreamChunk] = []

    with pytest.raises(APIError, match="late auth"):
        async for chunk in provider.stream_chat([]):
            seen.append(chunk)

    assert len(seen) == 1
    assert source.generation == 1


@pytest.mark.asyncio
async def test_helper_provider_closes_prototype_and_active_child(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    helper = _write_helper(tmp_path / "helper.sh", "printf 'key-1\\n'\n")
    source = CredentialHelperSource(
        [str(helper)],
        workspace_root=workspace,
        state_directory=tmp_path / "state",
    )
    prototype = FakeProvider("prototype", [])
    created: list[FakeProvider] = []

    def factory(credential: str) -> ProviderABC:
        provider = FakeProvider(
            credential,
            [[StreamChunk(content="ok"), StreamChunk(is_done=True)]],
        )
        created.append(provider)
        return provider

    provider = CredentialHelperProvider(prototype, factory, source)
    _ = [chunk async for chunk in provider.stream_chat([])]
    await provider.aclose()
    await provider.aclose()

    assert prototype.closed == 1
    assert created[0].closed == 1
    assert provider._owned == [prototype]
    assert provider._active is prototype
