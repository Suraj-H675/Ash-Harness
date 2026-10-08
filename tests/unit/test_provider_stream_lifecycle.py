from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator, Sequence
from typing import Any

import pytest

from ash.providers.base import ProviderABC, StreamChunk, close_async_stream
from ash.providers.capabilities import ProviderCapabilities
from ash.providers.credential_helper import CredentialHelperProvider
from ash.providers.credential_pool import CredentialPoolProvider
from ash.providers.failover import FailoverProvider
from ash.providers.messages import MessageInput


class _ChildStreamProvider(ProviderABC):
    provider_family = "stream-lifecycle-test"
    _ash_declared_capabilities = ProviderCapabilities(native_tools=True)
    model_name = "stream-lifecycle-test"

    def __init__(self) -> None:
        self.closed = asyncio.Event()

    def count_tokens(self, text: str) -> int:
        return len(text)

    async def stream_chat(
        self,
        messages: Sequence[MessageInput],
        temperature: float = 0.0,
        tools: list[dict[str, Any]] | None = None,
    ) -> AsyncGenerator[StreamChunk, None]:
        del messages, temperature, tools
        try:
            yield StreamChunk(content="partial")
            await asyncio.Event().wait()
        finally:
            self.closed.set()


class _CredentialSource:
    generation = 1

    async def resolve(self, *, force_refresh: bool = False) -> str:
        del force_refresh
        return "test-credential"


def _wrapper(name: str, child: _ChildStreamProvider) -> ProviderABC:
    if name == "failover":
        return FailoverProvider([child])
    if name == "credential pool":
        return CredentialPoolProvider([child], ["TEST_CREDENTIAL"])
    if name == "credential helper":
        return CredentialHelperProvider(
            child,
            lambda credential: child,
            _CredentialSource(),
        )
    raise AssertionError(f"unknown wrapper: {name}")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "wrapper_name",
    ["failover", "credential pool", "credential helper"],
)
async def test_closing_wrapper_closes_active_child_stream(wrapper_name: str) -> None:
    child = _ChildStreamProvider()
    wrapper = _wrapper(wrapper_name, child)
    stream = wrapper.stream_chat([])

    chunk = await anext(stream)
    assert chunk.content == "partial"

    await stream.aclose()

    assert child.closed.is_set()


@pytest.mark.asyncio
async def test_close_failure_does_not_replace_primary_provider_error() -> None:
    class FailingClose:
        async def aclose(self) -> None:
            raise OSError("private transport detail")

    primary_error = TimeoutError("provider request timed out")

    await close_async_stream(
        FailingClose(),
        label="provider",
        primary_error=primary_error,
    )

    assert str(primary_error) == "provider request timed out"
    assert "provider stream cleanup also failed (OSError)" in primary_error.__notes__
    assert "private transport detail" not in " ".join(primary_error.__notes__)


@pytest.mark.asyncio
async def test_close_task_self_cancellation_is_a_cleanup_failure() -> None:
    class SelfCancellingClose:
        async def aclose(self) -> None:
            raise asyncio.CancelledError

    with pytest.raises(RuntimeError, match=r"provider stream cleanup failed \(CancelledError\)"):
        await close_async_stream(SelfCancellingClose(), label="provider")
