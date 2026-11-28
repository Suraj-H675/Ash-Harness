"""A context-limited coding turn may finish *reporting*, never repeat actions."""

from __future__ import annotations

import asyncio
import io
import json
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from ash.cli import _bootstrap_and_headless, _render_context_diagnostics
from ash.config import AshConfig
from ash.context.history import ContextBudgetExceededError, RequestTokenBudget
from ash.core.activity import summarize_context_diagnostics
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
        elif self.mode == "estimated":
            yield StreamChunk(
                content=(
                    "Edited both modules; the recorded test exited 0. "
                    "Unrecorded checks remain unverified. No commands were repeated."
                ),
                is_done=True,
                stop_reason="stop",
            )
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
    command_exit_code: int | None = 0,
    command_arguments: dict | None = None,
    edit_tool: str = "replace_file_content",
    edit_arguments: dict | None = None,
    extra_arguments: dict | None = None,
) -> str:
    turn_id = "prior-coding-turn"
    store.start_turn(session_id, turn_id, "Original source repair")
    calls: list[tuple[str, str, str]] = []
    if include_edit:
        calls.append(("edit", edit_tool, "src/a.py"))
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
                    (command_arguments if command_arguments is not None else {"command_line": target})
                    if name == "run_command" else (
                        edit_arguments if call_id == "edit" and edit_arguments is not None
                        else extra_arguments if call_id == "other" and extra_arguments is not None
                        else {"file_path": target}
                    )
                ),
                approved=True,
                dispatched=dispatched,
                executed=dispatched,
                success=successful,
                exit_code=command_exit_code if name == "run_command" and dispatched else None,
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
        {"tool": "run_command", "arguments": {"command_line": "python3 -B qa_visible.py"},
         "exit_code": 0, "result": "passed"},
    ] + [
        {"tool": "read_file", "arguments": {"file_path": f"src/file-{i}.py"}, "result": "read"}
        for i in range(95)
    ]
    receipt = json.loads(_compact_actions(actions))
    assert receipt["total_successful_calls"] == 97
    assert receipt["shown_calls"] == 32
    assert receipt["actions"][0]["file_path"] == "src/a.py"
    assert receipt["actions"][1]["command"] == "python3 -B qa_visible.py"
    assert receipt["actions"][1]["exit_code"] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("mode", "usage_source"),
    [("complete", "provider"), ("estimated", "estimated")],
)
async def test_tool_free_reporting_persists_distinct_completed_turn(
    tmp_path: Path, mode: str, usage_source: str,
) -> None:
    provider = FinalReportProvider(mode=mode)
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
        assert report.usage["prompt_tokens"] > 0
        assert report.usage["completion_tokens"] > 0
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

    resumed_provider = FinalReportProvider()
    resumed_loop = build_loop(tmp_path, resumed_provider)
    try:
        resumed_session = await resumed_loop.start_session(session.session_id)
        assert resumed_session.session_id == session.session_id
        assert resumed_provider.requests == []
        events = resumed_loop.session_store.list_recent_runtime_events(
            session.session_id, limit=100
        )
    finally:
        await resumed_loop.aclose()
    assembly = next(
        item.event
        for item in events
        if item.event.get("type") == "context.usage"
        and item.event.get("assembly_purpose") == "request"
    )
    request_start = next(
        item.event for item in events if item.event.get("type") == "model.request.started"
    )
    request_completion = next(
        item.event
        for item in events
        if item.event.get("type") == "model.request.completed"
    )
    report_turn_id = rows[1]["turn_id"]
    request_events = [
        item.event
        for item in events
        if item.event.get("context_id") == assembly["context_id"]
    ]
    turn_usage = [
        item.event for item in events if item.event.get("type") == "turn.usage"
    ]
    finalized = next(
        item.event
        for item in events
        if item.event.get("type") == "turn.finalized.tool_free"
    )
    assert assembly["context_id"] == request_start["context_id"]
    assert request_start["context_id"] == request_completion["context_id"]
    assert request_start["operation_id"] == request_completion["operation_id"]
    assert {event["turn_id"] for event in request_events} == {report_turn_id}
    assert request_start["requested_route"] == request_completion["effective_route"]
    assert request_completion["usage_source"] == usage_source
    assert request_completion["prompt_tokens"] > 0
    assert request_completion["cache_read_tokens"] == 0
    assert request_completion["cache_write_tokens"] == 0
    assert len(turn_usage) == 1
    assert turn_usage[0]["turn_id"] == report_turn_id
    assert turn_usage[0]["usage_source"] == usage_source
    assert turn_usage[0]["prompt_tokens"] == request_completion["prompt_tokens"]
    assert turn_usage[0]["completion_tokens"] == request_completion[
        "completion_tokens"
    ]
    assert turn_usage[0]["cache_read_tokens"] == 0
    assert turn_usage[0]["cache_write_tokens"] == 0
    assert turn_usage[0]["estimated_prompt_tokens"] == (
        request_completion["prompt_tokens"] if usage_source == "estimated" else 0
    )
    assert turn_usage[0]["estimated_completion_tokens"] == (
        request_completion["completion_tokens"] if usage_source == "estimated" else 0
    )
    assert finalized["turn_id"] == report_turn_id
    assert finalized["source_interrupted_turn_id"] == prior
    diagnostics = summarize_context_diagnostics(events, session_id=session.session_id)
    assert diagnostics["request"]["turn_id"] == report_turn_id
    assert diagnostics["turn_usage"]["turn_id"] == report_turn_id
    assert diagnostics["turn_usage"]["usage_source"] == usage_source
    context_output = _render_context_diagnostics(diagnostics)
    assert f"Last persisted turn cumulative usage ({usage_source})" in context_output
    assert "prompt cache subsets: 0 read, 0 written" in context_output
    if mode == "complete":
        assert turn_usage[0]["prompt_tokens"] == 150
        assert turn_usage[0]["completion_tokens"] == 32


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
                "SELECT turn_id, status FROM turn_journal WHERE session_id = ? "
                "ORDER BY started_at, rowid",
                (session.session_id,),
            ).fetchall()
        assert len(turns) == 2
        assert [row["status"] for row in turns] == ["interrupted", "interrupted"]
        events = loop.session_store.list_recent_runtime_events(
            session.session_id, limit=100
        )
        report_ids = {item.event.get("turn_id") for item in events}
        assert turns[1]["turn_id"] in report_ids
        request_events = [
            item.event
            for item in events
            if item.event.get("type", "").startswith("model.request.")
        ]
        assert request_events
        assert all(event.get("turn_id") == turns[1]["turn_id"] for event in request_events)
        assert not any(item.event.get("type") == "turn.usage" for item in events)
        assert not any(
            item.event.get("type") == "turn.finalized.tool_free" for item in events
        )
        assert loop.session_store.load_session(session.session_id).messages == []
    finally:
        await loop.aclose()


@pytest.mark.asyncio
async def test_report_admission_rejection_creates_no_report_turn_or_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = FinalReportProvider()
    loop = build_loop(tmp_path, provider)
    try:
        session = await loop.start_session()
        source = record_completed_coding_turn(loop.session_store, session.session_id)
        reconcile(loop, session.session_id)
        evidence = loop.session_store.tool_free_finalization_evidence(
            session.session_id, expected_project_path=tmp_path
        )
        assert evidence is not None
        monkeypatch.setattr(
            loop,
            "_resolve_request_token_budget",
            lambda: RequestTokenBudget(combined_limit=10, input_limit=1, response_limit=9),
        )

        with pytest.raises(ProviderCompletionError, match="exceed provider input"):
            await finalize_tool_free(loop, "Fix code", *evidence)

        with get_db_connection(loop.session_store.db_path) as connection:
            rows = connection.execute(
                "SELECT turn_id, status FROM turn_journal WHERE session_id = ?",
                (session.session_id,),
            ).fetchall()
        assert [(row["turn_id"], row["status"]) for row in rows] == [
            (source, "interrupted")
        ]
        assert provider.requests == []
        events = loop.session_store.list_recent_runtime_events(
            session.session_id, limit=100
        )
        rejection = next(
            item.event
            for item in events
            if item.event.get("type") == "context.request.rejected"
        )
        assert rejection["dispatch_status"] == "not_dispatched"
        assert rejection.get("turn_id") is None
        assert not any(
            item.event.get("type", "").startswith("model.request.")
            for item in events
        )
    finally:
        await loop.aclose()


@pytest.mark.asyncio
async def test_cancelled_report_stays_interrupted_and_keeps_prior_usage(
    tmp_path: Path,
) -> None:
    class BlockingProvider(FinalReportProvider):
        def __init__(self) -> None:
            super().__init__()
            self.started = asyncio.Event()

        async def stream_chat(self, messages, temperature=0.0, tools=None):
            self.requests.append((list(messages), tools))
            self.started.set()
            await asyncio.Event().wait()
            yield StreamChunk(content="unreachable", is_done=True)

    provider = BlockingProvider()
    loop = build_loop(tmp_path, provider)
    try:
        session = await loop.start_session()
        source = record_completed_coding_turn(loop.session_store, session.session_id)
        reconcile(loop, session.session_id)
        loop.session_store.save_runtime_events(
            [
                {
                    "event_id": "prior-report-source-usage",
                    "type": "turn.usage",
                    "session_id": session.session_id,
                    "turn_id": source,
                    "prompt_tokens": 77,
                    "completion_tokens": 8,
                    "cache_read_tokens": 2,
                    "cache_write_tokens": 1,
                    "estimated_prompt_tokens": 0,
                    "estimated_completion_tokens": 0,
                    "usage_source": "provider",
                }
            ]
        )
        evidence = loop.session_store.tool_free_finalization_evidence(
            session.session_id, expected_project_path=tmp_path
        )
        assert evidence is not None
        task = asyncio.create_task(finalize_tool_free(loop, "Fix code", *evidence))
        await asyncio.wait_for(provider.started.wait(), timeout=1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        with get_db_connection(loop.session_store.db_path) as connection:
            rows = connection.execute(
                "SELECT turn_id, status FROM turn_journal WHERE session_id = ? "
                "ORDER BY started_at, rowid",
                (session.session_id,),
            ).fetchall()
        assert len(rows) == 2
        assert [row["status"] for row in rows] == ["interrupted", "interrupted"]
        assert len(provider.requests) == 1
        events = loop.session_store.list_recent_runtime_events(
            session.session_id, limit=100
        )
        starts = [
            item.event
            for item in events
            if item.event.get("type") == "model.request.started"
        ]
        cancelled = [
            item.event
            for item in events
            if item.event.get("type") == "model.request.cancelled"
        ]
        assert len(starts) == len(cancelled) == 1
        assert starts[0]["turn_id"] == cancelled[0]["turn_id"] == rows[1]["turn_id"]
        assert starts[0]["context_id"] == cancelled[0]["context_id"]
        assert starts[0]["operation_id"] == cancelled[0]["operation_id"]
        assert not any(item.event.get("type") == "turn.usage" for item in events if item.event.get("turn_id") == rows[1]["turn_id"])
        assert not any(
            item.event.get("type") == "turn.finalized.tool_free" for item in events
        )
        diagnostics = summarize_context_diagnostics(events, session_id=session.session_id)
        assert diagnostics["request"]["state"] == "cancelled"
        assert diagnostics["request"]["usage_source"] == "unavailable"
        assert diagnostics["turn_usage"]["turn_id"] == source
        assert diagnostics["turn_usage"]["prompt_tokens"] == 77
    finally:
        await loop.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "settings",
    [
        {"command_success": False, "command_exit_code": 1},
        {"command_success": None, "command_exit_code": None},
        {"command_exit_code": None},
        {"command_arguments": {"command": "python3 -B qa_visible.py"}},
        {"command_arguments": {"command_line": ""}},
        {"command_arguments": {"command_line": "python3 -B qa_visible.py", "cwd": 42}},
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
@pytest.mark.parametrize(("patch_args", "admitted"), [
    ({"patch": "diff", "dry_run": True}, False),
    ({"patch": "diff", "dry_run": "true"}, False),
    ({"patch": "diff", "dry_run": False}, True),
    ({"patch": "diff", "dry_run": "false"}, True),
    ({"patch": "diff"}, True),
    ({"patch": "diff", "dry_run": "invalid"}, False),
    ({"dry_run": False}, False),
])
async def test_patch_validation_is_not_an_edit_for_tool_free_finalization(
    tmp_path: Path, patch_args: dict, admitted: bool,
) -> None:
    loop = build_loop(tmp_path, FinalReportProvider())
    try:
        session = await loop.start_session()
        record_completed_coding_turn(
            loop.session_store, session.session_id, edit_tool="apply_patch",
            edit_arguments=patch_args,
        )
        reconcile(loop, session.session_id)
        evidence = loop.session_store.tool_free_finalization_evidence(
            session.session_id, expected_project_path=tmp_path
        )
        assert (evidence is not None) is admitted
        if evidence is not None:
            assert evidence[1][0]["dry_run"] is False
            assert json.loads(_compact_actions(evidence[1]))["actions"][0]["dry_run"] is False
    finally:
        await loop.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("dry_run", [True, False])
async def test_post_check_patch_preview_preserves_verification_order(
    tmp_path: Path, dry_run: bool,
) -> None:
    loop = build_loop(tmp_path, FinalReportProvider())
    try:
        session = await loop.start_session()
        record_completed_coding_turn(
            loop.session_store, session.session_id, extra_tool="apply_patch",
            extra_arguments={"patch": "diff", "dry_run": dry_run},
        )
        reconcile(loop, session.session_id)
        evidence = loop.session_store.tool_free_finalization_evidence(
            session.session_id, expected_project_path=tmp_path
        )
        assert (evidence is not None) is dry_run
        if evidence is not None:
            last = json.loads(_compact_actions(evidence[1]))["actions"][-1]
            assert last["dry_run"] is True
            assert last["status"] == "recorded successful validation only; no edit"
    finally:
        await loop.aclose()


def test_report_dry_run_does_not_displace_last_real_edit() -> None:
    actions = [
        {"tool": "apply_patch", "arguments": {"patch": "real"}, "dry_run": False, "result": "done"},
        {"tool": "run_command", "arguments": {"command_line": "check"}, "exit_code": 0, "result": "ok"},
    ] + [
        {"tool": "apply_patch", "arguments": {"patch": "preview"}, "dry_run": True, "result": "valid"}
        for _ in range(40)
    ]
    report = json.loads(_compact_actions(actions))
    assert report["shown_calls"] == 32
    assert report["actions"][0]["dry_run"] is False
    assert report["actions"][1]["tool"] == "run_command"
    assert all(row["dry_run"] is True for row in report["actions"][2:])
    assert "patch" not in report["actions"][0]


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

        monkeypatch.setattr(loop.session_store, "_save_message_in_connection", fail_save)
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
        events = loop.session_store.list_recent_runtime_events(
            session.session_id, limit=100
        )
        report_turn_id = rows[1]["turn_id"]
        request_events = [
            item.event
            for item in events
            if item.event.get("type", "").startswith("model.request.")
        ]
        assert request_events
        assert all(event.get("turn_id") == report_turn_id for event in request_events)
        assert not any(
            item.event.get("type") == "turn.usage"
            and item.event.get("turn_id") == report_turn_id
            for item in events
        )
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


def test_report_redacts_before_truncating_and_marks_incomplete_fields() -> None:
    from ash.core.redaction import redact_text

    # Synthetic values deliberately place a recognizable secret across each
    # old slicing boundary. Never use a real credential in this fixture.
    secret = "fixture-sensitive-" + "q" * 80
    command = "x" * 488 + " password=" + secret + " z" * 300
    output = "password=" + secret + " " + "tail " * 195
    cwd = "x" * 350
    assert secret not in redact_text(command)
    assert secret not in redact_text(output)
    receipt = json.loads(_compact_actions([{
        "tool": "run_command", "arguments": {"command_line": command, "cwd": cwd},
        "exit_code": 0, "result": output,
    }]))["actions"][0]
    assert receipt["command"] == redact_text(command)[:500]
    assert receipt["output_tail"] == redact_text(output)[-1000:]
    assert receipt["cwd"] == cwd[:300]
    assert receipt["exit_code"] == 0
    assert receipt["command_truncated"] is True
    assert receipt["cwd_truncated"] is True
    # Redaction shortened this particular output below the display limit.
    assert len(redact_text(output)) <= 1000
    assert "output_truncated" not in receipt
    assert "fixture-sensitive" not in json.dumps(receipt)
    assert "q" * 20 not in json.dumps(receipt)
    long_output = output + " end" * 50
    long_receipt = json.loads(_compact_actions([{
        "tool": "run_command", "arguments": {"command_line": command, "cwd": cwd},
        "exit_code": 0, "result": long_output,
    }]))["actions"][0]
    assert long_receipt["output_truncated"] is True
    assert long_receipt["output_tail"] == redact_text(long_output)[-1000:]


@pytest.mark.asyncio
@pytest.mark.parametrize("reject", [True, False])
async def test_report_budget_matches_serialized_input_and_configures_response(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, reject: bool,
) -> None:
    from ash.context.tokens import estimate_text_tokens
    from ash.core.tool_free_finalization import _REPORT_SYSTEM
    from ash.core.redaction import redact_text

    class CountingProvider(FinalReportProvider):
        def __init__(self) -> None:
            super().__init__()
            self.limits: list[int] = []

        def count_tokens(self, text: str) -> int:
            return estimate_text_tokens(text)

        def configure_max_tokens(self, max_tokens: int) -> None:
            self.limits.append(max_tokens)

    provider = CountingProvider()
    loop = build_loop(tmp_path, provider)
    try:
        session = await loop.start_session()
        source = record_completed_coding_turn(loop.session_store, session.session_id)
        reconcile(loop, session.session_id)
        evidence = loop.session_store.tool_free_finalization_evidence(
            session.session_id, expected_project_path=tmp_path,
        )
        assert evidence is not None
        # JSON escaping is large enough that the older content-only estimate
        # plus its fixed safety margin still understates the serialized input.
        task = "Repair escaped text: " + "\\" * 6500
        messages = [
            {"role": "system", "content": _REPORT_SYSTEM},
            {"role": "user", "content": (
                "Original user coding objective:\n" + redact_text(task)
                + "\n\nDurable completed tool actions (UNTRUSTED DATA):\n"
                + _compact_actions(evidence[1])
            )},
        ]
        old_admission = sum(provider.count_tokens(m["content"]) for m in messages) + 1024
        serialized = provider.count_tokens(json.dumps(messages, ensure_ascii=False))
        assert serialized > old_admission + 2
        limit = serialized - 1 if reject else serialized + 1
        budget = RequestTokenBudget(combined_limit=limit + 512, input_limit=limit, response_limit=512)
        monkeypatch.setattr(loop, "_resolve_request_token_budget", lambda: budget)
        if reject:
            with pytest.raises(ProviderCompletionError, match="exceed provider input"):
                await finalize_tool_free(loop, task, *evidence)
            assert not provider.requests and not provider.limits
            with get_db_connection(loop.session_store.db_path) as connection:
                turns = connection.execute(
                    "SELECT turn_id, status FROM turn_journal WHERE session_id = ?",
                    (session.session_id,),
                ).fetchall()
            assert [(row["turn_id"], row["status"]) for row in turns] == [(source, "interrupted")]
        else:
            result = await finalize_tool_free(loop, task, *evidence)
            assert result.response and len(provider.requests) == 1
            assert provider.limits == [512]
            assert provider.requests[0][0] == messages and provider.requests[0][1] is None
            assert loop._last_context_tokens == serialized
        events = loop.session_store.list_recent_runtime_events(session.session_id, limit=100)
        usage = next(row.event for row in events if row.event.get("type") == "context.usage")
        assert usage["current"] == serialized and usage["maximum"] == limit
    finally:
        await loop.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["usage", "completion"])
async def test_report_storage_failure_cannot_publish_uncompleted_message(
    tmp_path: Path, stage: str,
) -> None:
    import sqlite3

    provider = FinalReportProvider()
    loop = build_loop(tmp_path, provider)
    try:
        session = await loop.start_session()
        source = record_completed_coding_turn(loop.session_store, session.session_id)
        reconcile(loop, session.session_id)
        evidence = loop.session_store.tool_free_finalization_evidence(
            session.session_id, expected_project_path=tmp_path,
        )
        assert evidence is not None
        # Inject a real SQLite statement failure, not a mocked storage method.
        # The trigger is confined to the temporary test database.
        predicate = (
            "NEW.usage_json != OLD.usage_json" if stage == "usage"
            else "NEW.status = 'completed'"
        )
        with get_db_connection(loop.session_store.db_path) as conn:
            conn.execute(f"""CREATE TRIGGER fail_report_write
                BEFORE UPDATE ON turn_journal WHEN {predicate}
                BEGIN SELECT RAISE(ABORT, 'fixture report storage failure'); END""")
        with pytest.raises(sqlite3.IntegrityError, match="fixture report storage failure"):
            await finalize_tool_free(loop, "Repair code", *evidence)
        assert len(provider.requests) == 1
        persisted = loop.session_store.load_session(session.session_id)
        assert persisted.messages == []
        assert len(persisted.tool_calls) == 2
        assert loop._last_turn_stop_reason != "tool_free_finalization"
        with get_db_connection(loop.session_store.db_path) as conn:
            turns = conn.execute(
                "SELECT turn_id,status,usage_json FROM turn_journal WHERE session_id=? "
                "ORDER BY started_at,rowid", (session.session_id,),
            ).fetchall()
            assert len(turns) == 2 and all(row["status"] == "interrupted" for row in turns)
            assert turns[0]["turn_id"] == source
            if stage == "completion":
                usage = json.loads(turns[1]["usage_json"])
                assert usage["prompt_tokens"] == 150 and usage["completion_tokens"] == 32
        events = loop.session_store.list_recent_runtime_events(session.session_id, limit=100)
        assert not any(row.event.get("type") == "turn.finalized.tool_free" for row in events)
        assert loop.session_store.verify_audit_log(session.session_id) == []
    finally:
        await loop.aclose()
