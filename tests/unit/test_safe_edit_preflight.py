"""Proven pre-dispatch edit refusals and conservative automatic handoff."""

from __future__ import annotations

import io
from pathlib import Path

import pytest

from ash.context.turn import TurnContext
from ash.core.checkpoints import recover_interrupted_turns
from ash.core.loop import AshLoop
from ash.core.session import SessionForkConflictError, SessionStore
from ash.hooks.registry import HookRegistry, LifecycleHook
from ash.providers.base import ProviderABC, StreamChunk
from ash.safety.grants import PermissionRule, RuleEffect
from ash.safety.guard import SafetyGuard
from ash.tools.base import ToolMiddleware, ToolResult
from ash.tools.filesystem import ReplaceFileContentTool
from ash.ui.headless import HeadlessUI


class QuietProvider(ProviderABC):
    model_name = "safe-edit-test"

    def count_tokens(self, text: str) -> int:
        return 1

    async def stream_chat(self, messages, temperature=0.0, tools=None):
        yield StreamChunk(content="done", is_done=True)


class DetectMiddleware(ToolMiddleware):
    def __init__(self) -> None:
        self.before = 0
        self.after = 0

    async def before_tool(self, tool_name, arguments, tool) -> None:
        self.before += 1

    async def after_tool(self, tool_name, arguments, result) -> None:
        self.after += 1


def make_loop(tmp_path: Path, *, hooks=None, middleware=None, tool=None) -> AshLoop:
    guard = SafetyGuard(project_root=tmp_path)
    replace = tool or ReplaceFileContentTool(guard)
    return AshLoop(
        SessionStore(tmp_path / "sessions.db"), QuietProvider(), guard,
        HeadlessUI(output_format="text", stream=io.StringIO()), tmp_path,
        tools={replace.name: replace},
        safety_tier="auto_approve",
        hooks=hooks,
        tool_middlewares=[middleware] if middleware else [],
    )


def edit_args(target: str) -> dict[str, object]:
    return {
        "file_path": "module.py",
        "start_line": 1,
        "end_line": 1,
        "target_content": target,
        "replacement_content": "after = 1",
    }


async def dispatch_one(loop: AshLoop, args: dict[str, object]):
    session = loop.current_session
    assert session is not None
    return await loop._execute_tool_calls([
        {"call_id": "edit-1", "name": "replace_file_content", "arguments": args}
    ], session, persist_tool_messages=True)


@pytest.mark.asyncio
async def test_exact_edit_preflight_checks_without_writing(tmp_path: Path) -> None:
    path = tmp_path / "module.py"
    path.write_text("before = 0\n")
    tool = ReplaceFileContentTool(SafetyGuard(tmp_path))
    refusal = await tool.preflight(**edit_args("wrong = 0"))
    assert refusal is not None and not refusal.success
    assert "does not match" in (refusal.error or "")
    assert path.read_text() == "before = 0\n"
    assert await tool.preflight(**edit_args("before = 0")) is None
    assert path.read_text() == "before = 0\n"
    result = await tool.run(**edit_args("before = 0"))
    assert result.success and path.read_text() == "after = 1\n"


@pytest.mark.asyncio
async def test_rejected_edit_is_not_dispatched_and_allows_safe_fork(
    tmp_path: Path,
) -> None:
    path = tmp_path / "module.py"
    path.write_text("before = 0\n")
    middleware = DetectMiddleware()
    loop = make_loop(tmp_path, middleware=middleware)
    try:
        session = await loop.start_session()
        turn_id = "interrupted-coding"
        loop.session_store.start_turn(session.session_id, turn_id, "Fix module.py")
        loop.turn_context = TurnContext(session_id=session.session_id, turn_id=turn_id)
        loop._safe_edit_preflight = True
        result = await dispatch_one(loop, edit_args("wrong = 0"))
        assert not result[0]["success"]
        assert middleware.before == middleware.after == 0
        assert path.read_text() == "before = 0\n"
        saved = loop.session_store.load_session(session.session_id).tool_calls[0]
        assert saved.approved and not saved.dispatched and not saved.executed
        assert saved.success is False
        assert loop.session_store.verify_audit_log(session.session_id) == []

        loop.session_store.interrupt_turn(turn_id)
        recovery = recover_interrupted_turns(
            loop.session_store, loop.safety_guard, session.session_id,
            include_quiescent_interrupted=True,
        )
        assert not recovery.needs_attention
        child = loop.session_store.restart_context_session(
            session.session_id, expected_project_path=tmp_path,
            user_handoff="Inspect the original task and incomplete changes",
            automatic=True,
        )
        assert child.parent_session_id == session.session_id
        assert child.tool_calls == []
        assert path.read_text() == "before = 0\n"
    finally:
        await loop.aclose()


@pytest.mark.asyncio
async def test_rejected_edit_without_opt_in_remains_blocked(tmp_path: Path) -> None:
    (tmp_path / "module.py").write_text("before = 0\n")
    loop = make_loop(tmp_path)
    try:
        session = await loop.start_session()
        turn_id = "ordinary-coding"
        loop.session_store.start_turn(session.session_id, turn_id, "Fix module.py")
        loop.turn_context = TurnContext(session_id=session.session_id, turn_id=turn_id)
        result = await dispatch_one(loop, edit_args("wrong = 0"))
        assert not result[0]["success"]
        saved = loop.session_store.load_session(session.session_id).tool_calls[0]
        assert saved.dispatched and saved.executed and saved.success is False
        loop.session_store.interrupt_turn(turn_id)
        recover_interrupted_turns(
            loop.session_store, loop.safety_guard, session.session_id,
            include_quiescent_interrupted=True,
        )
        with pytest.raises(SessionForkConflictError, match="without external actions"):
            loop.session_store.restart_context_session(
                session.session_id, expected_project_path=tmp_path,
                user_handoff="Check the incomplete repair", automatic=True,
            )
    finally:
        await loop.aclose()


@pytest.mark.asyncio
async def test_tool_error_hook_can_have_side_effects_so_handoff_is_refused(
    tmp_path: Path,
) -> None:
    (tmp_path / "module.py").write_text("before = 0\n")
    marker = tmp_path / "hook-ran.txt"

    def observed(_payload: dict) -> None:
        marker.write_text("effect")

    hooks = HookRegistry()
    hooks.register_lifecycle(LifecycleHook(event="tool_error", callback=observed))
    loop = make_loop(tmp_path, hooks=hooks)
    try:
        session = await loop.start_session()
        loop._safe_edit_preflight = True
        result = await dispatch_one(loop, edit_args("wrong = 0"))
        assert not result[0]["success"]
        assert marker.read_text() == "effect"
        saved = loop.session_store.load_session(session.session_id).tool_calls[0]
        assert saved.dispatched is False and saved.success is None
    finally:
        await loop.aclose()


@pytest.mark.asyncio
async def test_custom_approval_callback_prevents_safe_rejection_attestation(
    tmp_path: Path,
) -> None:
    (tmp_path / "module.py").write_text("before = 0\n")
    marker = tmp_path / "approval-callback-ran.txt"

    async def approval(tool_name, arguments) -> bool:
        marker.write_text("approval had a side effect")
        return True

    loop = make_loop(tmp_path)
    loop.on_tool_approval = approval
    try:
        session = await loop.start_session()
        loop._safe_edit_preflight = True
        result = await dispatch_one(loop, edit_args("wrong = 0"))
        assert not result[0]["success"]
        assert marker.read_text() == "approval had a side effect"
        saved = loop.session_store.load_session(session.session_id).tool_calls[0]
        assert saved.approved and not saved.dispatched and not saved.executed
        assert saved.success is None
    finally:
        await loop.aclose()


@pytest.mark.asyncio
async def test_valid_edit_still_uses_normal_middleware_and_dispatch(tmp_path: Path) -> None:
    path = tmp_path / "module.py"
    path.write_text("before = 0\n")
    middleware = DetectMiddleware()
    loop = make_loop(tmp_path, middleware=middleware)
    try:
        session = await loop.start_session()
        loop._safe_edit_preflight = True
        result = await dispatch_one(loop, edit_args("before = 0"))
        assert result[0]["success"]
        assert path.read_text() == "after = 1\n"
        assert middleware.before == middleware.after == 1
        saved = loop.session_store.load_session(session.session_id).tool_calls[0]
        assert saved.approved and saved.dispatched and saved.executed
        assert saved.success is True
    finally:
        await loop.aclose()


@pytest.mark.asyncio
async def test_changed_file_after_preflight_is_rejected_by_normal_tool(
    tmp_path: Path,
) -> None:
    path = tmp_path / "module.py"
    path.write_text("before = 0\n")

    class ChangeBeforeTool(DetectMiddleware):
        async def before_tool(self, tool_name, arguments, tool) -> None:
            await super().before_tool(tool_name, arguments, tool)
            path.write_text("changed externally\n")

    middleware = ChangeBeforeTool()
    loop = make_loop(tmp_path, middleware=middleware)
    try:
        session = await loop.start_session()
        loop._safe_edit_preflight = True
        result = await dispatch_one(loop, edit_args("before = 0"))
        assert not result[0]["success"]
        assert path.read_text() == "changed externally\n"
        assert middleware.before == middleware.after == 1
        record = loop.session_store.load_session(session.session_id).tool_calls[0]
        assert record.dispatched and record.executed and record.success is False
    finally:
        await loop.aclose()


@pytest.mark.asyncio
async def test_custom_edit_tool_cannot_claim_builtin_preflight_proof(
    tmp_path: Path,
) -> None:
    (tmp_path / "module.py").write_text("before = 0\n")
    marker = tmp_path / "custom-executed.txt"

    class CustomReplacement(ReplaceFileContentTool):
        async def preflight(self, **kwargs):
            raise AssertionError("custom preflight must not be called")

        async def run(self, **kwargs):
            marker.write_text("executed")
            return ToolResult(success=False, output="", error="custom failure")

    guard = SafetyGuard(tmp_path)
    loop = make_loop(tmp_path, tool=CustomReplacement(guard))
    try:
        session = await loop.start_session()
        loop._safe_edit_preflight = True
        result = await dispatch_one(loop, edit_args("wrong = 0"))
        assert not result[0]["success"]
        assert marker.read_text() == "executed"
        record = loop.session_store.load_session(session.session_id).tool_calls[0]
        assert record.dispatched and record.executed and record.success is False
    finally:
        await loop.aclose()


@pytest.mark.asyncio
async def test_permission_denial_is_not_upgraded_to_safe_preflight(
    tmp_path: Path,
) -> None:
    (tmp_path / "module.py").write_text("before = 0\n")
    loop = make_loop(tmp_path)
    try:
        session = await loop.start_session()
        loop._safe_edit_preflight = True
        loop.permission_policy.set_persistent_rules([
            PermissionRule.create(RuleEffect.DENY, "replace_file_content")
        ])
        result = await dispatch_one(loop, edit_args("wrong = 0"))
        assert not result[0]["success"]
        record = loop.session_store.load_session(session.session_id).tool_calls[0]
        assert not record.approved
        assert not record.executed and not record.dispatched
        assert record.success is None
        assert (tmp_path / "module.py").read_text() == "before = 0\n"
    finally:
        await loop.aclose()
