"""Opt-in one-shot wall limits preserve cancellation and tool safety."""

from __future__ import annotations

import asyncio
import io
from pathlib import Path

import pytest

from ash.config import AshConfig
from ash.core.loop import AshLoop
from ash.core.session import SessionStore, get_db_connection
from ash.exceptions import TurnWallBudgetExceededError, classify_exception
from ash.providers.base import ProviderABC, StreamChunk
from ash.safety.guard import SafetyGuard
from ash.ui.headless import HeadlessUI


class BlockingProvider(ProviderABC):
    model_name = "budget-test-provider"

    def __init__(self) -> None:
        self.request_started = asyncio.Event()
        self.closed = False

    def count_tokens(self, text: str) -> int:
        return max(1, len(str(text)) // 4)

    async def stream_chat(self, messages, temperature=0.0, tools=None):
        self.request_started.set()
        try:
            await asyncio.sleep(10)
            yield StreamChunk(content="unreachable", is_done=True, stop_reason="stop")
        finally:
            self.closed = True


class CompletingProvider(ProviderABC):
    model_name = "budget-test-provider"

    def __init__(self) -> None:
        self.last_messages = []

    def count_tokens(self, text: str) -> int:
        return max(1, len(str(text)) // 4)

    async def stream_chat(self, messages, temperature=0.0, tools=None):
        self.last_messages = list(messages)
        yield StreamChunk(content="Completed the request", is_done=True, stop_reason="stop")


def _loop(tmp_path: Path, provider: ProviderABC | None = None) -> AshLoop:
    return AshLoop(
        SessionStore(tmp_path / "sessions.db"),
        provider or BlockingProvider(),
        SafetyGuard(project_root=tmp_path),
        HeadlessUI(output_format="json", stream=io.StringIO()),
        tmp_path,
        config=AshConfig(
            model="openai/budget-test-provider",
            workspace_root=tmp_path,
            db_directory=tmp_path / "db",
            memory_backend="off",
        ),
    )


@pytest.mark.asyncio
async def test_time_budget_cancels_real_model_turn_and_keeps_recovery(
    tmp_path: Path,
) -> None:
    provider = BlockingProvider()
    loop = _loop(tmp_path, provider)
    try:
        session = await loop.start_session()
        with pytest.raises(TurnWallBudgetExceededError, match="wall-time budget"):
            async with loop.wall_time_budget(0.04):
                await loop.run_turn("Fix a coding problem")
        assert provider.request_started.is_set()
        assert provider.closed
        assert not loop.is_turn_running
        assert loop._turn_wall_deadline is None
        assert not loop._turn_wall_notices
        assert not any(
            "Trusted execution budget" in message.content
            for message in loop.current_session.messages
        )
        with get_db_connection(loop.session_store.db_path) as conn:
            row = conn.execute(
                "SELECT status FROM turn_journal WHERE session_id = ?",
                (session.session_id,),
            ).fetchone()
        assert row is not None and row["status"] == "interrupted"
        assert [m.role for m in loop.session_store.load_session(session.session_id).messages] == [
            "user"
        ]
        events = loop.session_store.list_runtime_events(session.session_id)
        assert any(event.event.get("type") == "turn.cancelled" for event in events)
        info = classify_exception(TurnWallBudgetExceededError(0.04))
        assert info.category.value == "context"
        assert info.exit_code != 0
        assert "unknown" not in info.remedy.casefold()
        assert "side effects" in info.remedy
    finally:
        await loop.aclose()


@pytest.mark.asyncio
async def test_time_notices_are_bounded_and_not_durable(tmp_path: Path) -> None:
    loop = _loop(tmp_path)
    try:
        session = await loop.start_session()
        clock = asyncio.get_running_loop()
        async with loop.wall_time_budget(100):
            loop._notify_turn_wall_budget(session)
            loop._notify_turn_wall_budget(session)
            assert len(loop._turn_wall_notices) == 1
            loop._turn_wall_deadline = clock.time() + 30
            loop._notify_turn_wall_budget(session)
            loop._notify_turn_wall_budget(session)
            loop._turn_wall_deadline = clock.time() + 10
            loop._notify_turn_wall_budget(session)
            loop._notify_turn_wall_budget(session)
            assert len(loop._turn_wall_notices) == 3
            assert all(message.is_runtime_only for _, message in loop._turn_wall_notices)
            assert len(loop.session_store.load_session(session.session_id).messages) == 0
            # A context-restart child gets a fresh, accurate notice while
            # sharing the parent's overall deadline and cleanup scope.
            child = loop.session_store.create_session(str(tmp_path))
            loop._turn_wall_deadline = clock.time() + 75
            loop._notify_turn_wall_budget(child)
            assert len(loop._turn_wall_notices) == 4
            assert "seconds remain" in child.messages[-1].content
            assert "100 seconds total" in child.messages[-1].content
        assert loop._turn_wall_notices == []
        assert session.messages == []
        assert child.messages == []
        assert loop._turn_wall_deadline is None
        assert loop._turn_wall_notice_phase == -1
        assert loop._turn_wall_notice_session_id is None
    finally:
        await loop.aclose()


@pytest.mark.asyncio
async def test_successful_turn_receives_trusted_budget_without_persisting_notice(
    tmp_path: Path,
) -> None:
    provider = CompletingProvider()
    loop = _loop(tmp_path, provider)
    try:
        async with loop.wall_time_budget(2):
            result = await loop.run_turn("Do the requested coding work")
        assert "Completed" in result
        assert any(
            "Trusted execution budget" in str(message)
            for message in provider.last_messages
        )
        assert not any(
            "Trusted execution budget" in message.content
            for message in loop.current_session.messages
        )
        assert not any(
            "Trusted execution budget" in message.content
            for message in loop.session_store.load_session(
                loop.current_session.session_id
            ).messages
        )
    finally:
        await loop.aclose()


@pytest.mark.asyncio
async def test_budget_validation_and_nested_scope_refusal(tmp_path: Path) -> None:
    loop = _loop(tmp_path)
    try:
        for invalid in (0, -2, float("nan"), float("inf")):
            with pytest.raises(ValueError, match="positive and finite"):
                async with loop.wall_time_budget(invalid):
                    pass
        async with loop.wall_time_budget(5):
            with pytest.raises(RuntimeError, match="already active"):
                async with loop.wall_time_budget(4):
                    pass
        assert loop._turn_wall_deadline is None
        assert loop._turn_wall_notices == []
    finally:
        await loop.aclose()


@pytest.mark.asyncio
async def test_other_timeout_errors_are_not_misclassified(tmp_path: Path) -> None:
    loop = _loop(tmp_path)
    try:
        with pytest.raises(TimeoutError, match="unrelated provider timeout"):
            async with loop.wall_time_budget(5):
                raise TimeoutError("unrelated provider timeout")
    finally:
        await loop.aclose()
