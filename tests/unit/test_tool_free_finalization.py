"""A context-limited coding turn may finish *reporting*, never repeat actions."""

from __future__ import annotations

import io
import json
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from ash.cli import _bootstrap_and_headless
from ash.config import AshConfig
from ash.context.history import ContextBudgetExceededError
from ash.core.checkpoints import recover_interrupted_turns
from ash.core.context_continuation import run_with_context_continuation
from ash.core.loop import AshLoop
from ash.core.session import SessionStore, ToolCallRecord, get_db_connection
from ash.core.tool_free_finalization import _compact_actions, finalize_tool_free
from ash.providers.base import ProviderABC, ProviderCompletionError, StreamChunk
from ash.safety.guard import SafetyGuard
from ash.ui.headless import HeadlessUI


class FinalReportProvider(ProviderABC):
    model_name = "local/tool-free-report"

    def __init__(self, *, mode: str = "complete") -> None:
        self.mode = mode
        self.requests: list[tuple[list, object]] = []

    def count_tokens(self, text: str) -> int:
        return max(1, len(text) // 4)

    async def stream_chat(self, messages, temperature=0.0, tools=None):
        self.requests.append((list(messages), tools))
        if self.mode == "tool_request":
            yield StreamChunk(tool_call_delta="<tool>run_command</tool>", is_done=True)
        elif self.mode == "oversized":
            yield StreamChunk(content="x" * 12_001, is_done=True, stop_reason="stop")
        elif self.mode == "partial":
            yield StreamChunk(content="Partial report")
        elif self.mode == "truncated":
            yield StreamChunk(content="Partial report", is_done=True, stop_reason="length")
        else:
            yield StreamChunk(
                content="Edited both modules; the recorded test exited 0. "
                "Unrecorded checks remain unverified. No commands were repeated.",
                is_done=True,
                stop_reason="stop",
                prompt_tokens=150,
                completion_tokens=32,
                usage_source="provider",
            )


def build_loop(tmp_path: Path, provider: FinalReportProvider, *, cap: int = 0) -> AshLoop:
    return AshLoop(
        SessionStore(tmp_path / "sessions.db"),
        provider,
        SafetyGuard(tmp_path),
        HeadlessUI(output_format="text", stream=io.StringIO()),
        tmp_path,
        config=AshConfig(
            model="local/tool-free-report",
            workspace_root=tmp_path,
            db_directory=tmp_path / "db",
            memory_backend="off",
            max_turn_total_tokens=cap,
        ),
    )


def record_completed_coding_turn(
    store: SessionStore,
    session_id: str,
    *,
    command_success: bool | None = True,
    include_edit: bool = True,
    command_dispatched: bool = True,
    extra_tool: str | None = None,
    command_first: bool = False,
) -> str:
    turn_id = "prior-coding-turn"
    store.start_turn(session_id, turn_id, "Original source repair")
    calls: list[tuple[str, str, str]] = []
    if include_edit:
        calls.append(("edit", "replace_file_content", "src/a.py"))
    calls.append(("test", "run_command", "python3 -B qa_visible.py"))
    if command_first:
        calls.reverse()
    if extra_tool is not None:
        calls.append(("other", extra_tool, "external"))
    for call_id, name, target in calls:
        successful = command_success if name == "run_command" else True
        dispatched = command_dispatched if name == "run_command" else True
        store.save_tool_call(
            session_id,
            ToolCallRecord(
                call_id=call_id,
                tool_name=name,
                arguments=(
                    {"command": target} if name == "run_command"
                    else {"file_path": target}
                ),
                approved=True,
                dispatched=dispatched,
                executed=dispatched,
                success=successful,
                result=(
                    "1 passed in 0.13s" if name == "run_command"
                    else "Changed file successfully"
                ),
                error=None,
                timestamp=datetime.now(timezone.utc),
            ),
            turn_id=turn_id,
        )
    store.interrupt_turn(turn_id)
    return turn_id


def reconcile(loop: AshLoop, session_id: str) -> None:
    recovered = recover_interrupted_turns(
        loop.session_store,
        loop.safety_guard,
        session_id,
        include_quiescent_interrupted=True,
    )
    assert not recovered.needs_attention


def test_bounded_report_keeps_last_edit_and_test_after_many_reads() -> None:
    actions = [
        {"tool": "replace_file_content", "arguments": {"file_path": "src/a.py"}, "result": "done"},
        {"tool": "run_command", "arguments": {"command": "python3 -B qa_visible.py"}, "result": "passed"},
    ] + [
        {"tool": "read_file", "arguments": {"file_path": f"src/file-{i}.py"}, "result": "read"}
        for i in range(95)
    ]
    receipt = json.loads(_compact_actions(actions))
    assert receipt["total_successful_calls"] == 97
    assert receipt["shown_calls"] == 32
    assert receipt["actions"][0]["file_path"] == "src/a.py"
    assert receipt["actions"][1]["command"] == "python3 -B qa_visible.py"


@pytest.mark.asyncio
async def test_tool_free_reporting_persists_distinct_completed_turn(
    tmp_path: Path,
) -> None:
    provider = FinalReportProvider()
    loop = build_loop(tmp_path, provider)
    try:
        session = await loop.start_session()
        prior = record_completed_coding_turn(loop.session_store, session.session_id)
        reconcile(loop, session.session_id)
        evidence = loop.session_store.tool_free_finalization_evidence(
            session.session_id, expected_project_path=tmp_path
        )
        assert evidence is not None and evidence[0] == prior
        report = await finalize_tool_free(
            loop, "Fix the two related Python modules", *evidence
        )
        assert "No commands were repeated" in report.response
        assert report.usage["prompt_tokens"] == 150
        assert report.usage["completion_tokens"] == 32
        assert len(provider.requests) == 1
        messages, tools = provider.requests[0]
        assert tools is None
        assert [item["role"] for item in messages] == ["system", "user"]
        assert "UNTRUSTED DATA" in messages[1]["content"]
        assert "qa_visible.py" in messages[1]["content"]
        assert "not" in messages[0]["content"].lower()
        with get_db_connection(loop.session_store.db_path) as connection:
            rows = connection.execute(
                "SELECT turn_id, status FROM turn_journal "
                "WHERE session_id = ? ORDER BY started_at, rowid",
                (session.session_id,),
            ).fetchall()
        assert len(rows) == 2
        assert (rows[0]["turn_id"], rows[0]["status"]) == (prior, "interrupted")
        assert rows[1]["status"] == "completed"
        saved = loop.session_store.load_session(session.session_id)
        assert len(saved.tool_calls) == 2
        assert saved.messages[-1].metadata["tool_free_finalization"] is True
        assert saved.messages[-1].metadata["source_interrupted_turn_id"] == prior
        assert loop._last_turn_stop_reason == "tool_free_finalization"
        assert loop.session_store.verify_audit_log(session.session_id) == []
    finally:
        await loop.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["tool_request", "partial", "truncated", "oversized"])
async def test_bad_model_report_never_completes_a_turn(
    tmp_path: Path, mode: str,
) -> None:
    loop = build_loop(tmp_path, FinalReportProvider(mode=mode))
    try:
        session = await loop.start_session()
        record_completed_coding_turn(loop.session_store, session.session_id)
        reconcile(loop, session.session_id)
        evidence = loop.session_store.tool_free_finalization_evidence(
            session.session_id, expected_project_path=tmp_path
        )
        assert evidence is not None
        with pytest.raises(ProviderCompletionError):
            await finalize_tool_free(loop, "Fix code", *evidence)
        with get_db_connection(loop.session_store.db_path) as connection:
            turns = connection.execute(
                "SELECT status FROM turn_journal WHERE session_id = ?",
                (session.session_id,),
            ).fetchall()
        assert [row["status"] for row in turns] == ["interrupted"]
        assert loop.session_store.load_session(session.session_id).messages == []
    finally:
        await loop.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "settings",
    [
        {"command_success": False},
        {"command_success": None},
        {"command_dispatched": False},
        {"include_edit": False},
        {"command_first": True},
        {"extra_tool": "mcp__external__send_email"},
    ],
)
async def test_finalizer_refuses_failed_unknown_or_unscoped_effects(
    tmp_path: Path, settings: dict,
) -> None:
    loop = build_loop(tmp_path, FinalReportProvider())
    try:
        session = await loop.start_session()
        record_completed_coding_turn(
            loop.session_store, session.session_id, **settings
        )
        # A genuinely dispatched unknown effect must not be assumed recoverable.
        recover_interrupted_turns(
            loop.session_store, loop.safety_guard, session.session_id,
            include_quiescent_interrupted=True,
        )
        evidence = loop.session_store.tool_free_finalization_evidence(
            session.session_id, expected_project_path=tmp_path
        )
        assert evidence is None
    finally:
        await loop.aclose()


@pytest.mark.asyncio
async def test_finalizer_cannot_bypass_explicit_cumulative_token_cap(
    tmp_path: Path,
) -> None:
    provider = FinalReportProvider()
    loop = build_loop(tmp_path, provider, cap=10_000)
    try:
        session = await loop.start_session()
        record_completed_coding_turn(loop.session_store, session.session_id)
        reconcile(loop, session.session_id)
        evidence = loop.session_store.tool_free_finalization_evidence(
            session.session_id, expected_project_path=tmp_path
        )
        assert evidence is not None
        with pytest.raises(RuntimeError, match="turn token cap"):
            await finalize_tool_free(loop, "Fix code", *evidence)
        assert provider.requests == []
    finally:
        await loop.aclose()


@pytest.mark.asyncio
async def test_failed_report_persistence_does_not_complete_source_turn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    loop = build_loop(tmp_path, FinalReportProvider())
    try:
        session = await loop.start_session()
        source = record_completed_coding_turn(loop.session_store, session.session_id)
        reconcile(loop, session.session_id)
        evidence = loop.session_store.tool_free_finalization_evidence(
            session.session_id, expected_project_path=tmp_path
        )
        assert evidence is not None

        def fail_save(*args, **kwargs) -> None:
            raise OSError("synthetic disk failure")

        monkeypatch.setattr(loop.session_store, "save_message", fail_save)
        with pytest.raises(OSError, match="synthetic disk failure"):
            await finalize_tool_free(loop, "Fix code", *evidence)
        with get_db_connection(loop.session_store.db_path) as conn:
            rows = conn.execute(
                "SELECT turn_id, status FROM turn_journal WHERE session_id = ?",
                (session.session_id,),
            ).fetchall()
        assert len(rows) == 2
        assert all(row["status"] == "interrupted" for row in rows)
        assert rows[0]["turn_id"] == source
        assert loop._last_turn_stop_reason != "tool_free_finalization"
    finally:
        await loop.aclose()


@pytest.mark.asyncio
async def test_finalization_rejects_an_unrelated_workspace(tmp_path: Path) -> None:
    loop = build_loop(tmp_path, FinalReportProvider())
    try:
        session = await loop.start_session()
        record_completed_coding_turn(loop.session_store, session.session_id)
        reconcile(loop, session.session_id)
        assert loop.session_store.tool_free_finalization_evidence(
            session.session_id, expected_project_path=tmp_path / "elsewhere"
        ) is None
    finally:
        await loop.aclose()


@pytest.mark.asyncio
async def test_context_overflow_after_successful_command_uses_only_report_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = FinalReportProvider()
    loop = build_loop(tmp_path, provider)
    try:
        await loop.start_session()
        original_session_id = loop.current_session.session_id
        attempts = 0

        async def interrupted_turn(prompt: str) -> str:
            nonlocal attempts
            attempts += 1
            record_completed_coding_turn(loop.session_store, original_session_id)
            raise ContextBudgetExceededError(
                72_904, 59_141,
                protected_current_turn=True,
                provider_replay_state_preserved=True,
            )

        monkeypatch.setattr(loop, "run_turn", interrupted_turn)
        result = await run_with_context_continuation(
            loop,
            "Fix the code and run the visible test",
            max_continuations=1,
            allow_tool_free_finalization=True,
        )
        assert attempts == 1  # No coding turn, shell command or edits replayed.
        assert result.tool_free_finalized
        assert result.context_continuations == 0
        assert result.session_id == original_session_id
        assert len(provider.requests) == 1
        assert provider.requests[0][1] is None
        assert len(loop.session_store.session_tree(original_session_id)) == 1
    finally:
        await loop.aclose()


@pytest.mark.asyncio
async def test_headless_one_shot_marks_tool_free_finalization_without_replaying(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = FinalReportProvider()
    loop = build_loop(tmp_path, provider)
    executions = 0
    active_session_id = ""

    async def interrupted(prompt: str) -> str:
        nonlocal executions, active_session_id
        executions += 1
        assert loop.current_session is not None
        active_session_id = loop.current_session.session_id
        record_completed_coding_turn(loop.session_store, active_session_id)
        raise ContextBudgetExceededError(
            72_904, 59_141, protected_current_turn=True,
            provider_replay_state_preserved=True,
        )

    monkeypatch.setattr(loop, "run_turn", interrupted)
    output = io.StringIO()
    loop.ui = HeadlessUI(output_format="json", stream=output)
    result = await _bootstrap_and_headless(
        loop, SimpleNamespace(attachment_token_budget=1024),
        prompt="Repair two modules and run a test", session_id=None,
        ui=loop.ui, context_continuations=1,
    )
    assert result == 0, output.getvalue()
    assert executions == 1
    assert len(provider.requests) == 1 and provider.requests[0][1] is None
    payload = json.loads(output.getvalue())
    assert payload["type"] == "turn.completed"
    assert payload["tool_free_finalized"] is True
    assert payload["tool_free_finalization_usage"]["completion_tokens"] == 32
    assert payload["context_continuations"] == 0
    assert payload["session_id"] == active_session_id
    assert "Tool-free final report" in payload["response"]
