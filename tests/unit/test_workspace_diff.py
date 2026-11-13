"""A coding agent can inspect its own scoped edits without requiring Git."""

from __future__ import annotations

from pathlib import Path
import io

import pytest

from ash.core.checkpoints import FileCheckpointMiddleware
from ash.config import AshConfig
from ash.core.loop import AshLoop
from ash.core.session import SessionStore
from ash.providers.base import ProviderABC, StreamChunk
from ash.providers.capabilities import ProviderCapabilities
from ash.safety.guard import SafetyGuard
from ash.tools.filesystem import WholeEditTool
from ash.tools.workspace_diff import WorkspaceDiffTool
from ash.ui.headless import HeadlessUI


async def edit_with_checkpoint(
    store: SessionStore,
    guard: SafetyGuard,
    session_id: str,
    turn_id: str,
    call_id: str,
    file_path: str,
    content: str,
) -> None:
    middleware = FileCheckpointMiddleware(
        store, guard, lambda: (session_id, turn_id, call_id)
    )
    tool = WholeEditTool(guard)
    arguments = {"file_path": file_path, "content": content}
    await middleware.before_tool("whole_edit", arguments, tool)
    result = await tool.run(**arguments)
    assert result.success
    await middleware.after_tool("whole_edit", arguments, result)


@pytest.mark.asyncio
async def test_workspace_diff_reviews_only_current_turn_without_git(tmp_path: Path) -> None:
    target = tmp_path / "billing.py"
    target.write_text("rate = 1\n", encoding="utf-8")
    store = SessionStore(tmp_path / "sessions.db")
    session = store.create_session(str(tmp_path))
    guard = SafetyGuard(tmp_path)

    store.start_turn(session.session_id, "previous", "first edit")
    await edit_with_checkpoint(store, guard, session.session_id, "previous", "a", "billing.py", "rate = 2\n")
    store.complete_turn("previous")
    store.start_turn(session.session_id, "current", "second edit")
    tool = WorkspaceDiffTool(
        guard, store, lambda: (session.session_id, "current")
    )
    empty = await tool.run()
    assert empty.success
    assert "No checkpointed file changes" in empty.output

    await edit_with_checkpoint(store, guard, session.session_id, "current", "b", "billing.py", "rate = 3\n")
    result = await tool.run()
    assert result.success
    assert "--- a/billing.py" in result.output
    assert "+++ b/billing.py" in result.output
    assert "-rate = 2" in result.output
    assert "+rate = 3" in result.output
    assert "-rate = 1" not in result.output
    assert "rate = 3\n" == target.read_text(encoding="utf-8")
    assert not (tmp_path / ".git").exists()


@pytest.mark.asyncio
async def test_workspace_diff_refuses_stale_or_unowned_file_state(tmp_path: Path) -> None:
    target = tmp_path / "worker.py"
    target.write_text("before\n", encoding="utf-8")
    store = SessionStore(tmp_path / "sessions.db")
    session = store.create_session(str(tmp_path))
    store.start_turn(session.session_id, "active", "edit")
    guard = SafetyGuard(tmp_path)
    await edit_with_checkpoint(store, guard, session.session_id, "active", "edit", "worker.py", "after\n")
    target.write_text("outside change\n", encoding="utf-8")
    tool = WorkspaceDiffTool(guard, store, lambda: (session.session_id, "active"))

    result = await tool.run()
    assert not result.success
    assert "changed after Ash's edit" in str(result.error)
    assert "-before" not in result.output
    assert target.read_text(encoding="utf-8") == "outside change\n"


@pytest.mark.asyncio
async def test_workspace_diff_refuses_change_between_conflict_check_and_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import ash.core.checkpoints as checkpoints

    target = tmp_path / "worker.py"
    target.write_text("original\n", encoding="utf-8")
    store = SessionStore(tmp_path / "race.db")
    session = store.create_session(str(tmp_path))
    store.start_turn(session.session_id, "active", "edit")
    guard = SafetyGuard(tmp_path)
    await edit_with_checkpoint(
        store, guard, session.session_id, "active", "edit",
        "worker.py", "ash edit\n",
    )
    original_check = checkpoints._checkpoint_chain_conflicts

    def change_after_precheck(*args, **kwargs):
        conflicts = original_check(*args, **kwargs)
        assert not conflicts
        target.write_text("external edit\n", encoding="utf-8")
        return conflicts

    monkeypatch.setattr(
        checkpoints, "_checkpoint_chain_conflicts", change_after_precheck
    )
    result = await WorkspaceDiffTool(
        guard, store, lambda: (session.session_id, "active")
    ).run()
    assert not result.success
    assert "changed after Ash's edit" in str(result.error)
    assert result.output == ""
    assert target.read_text(encoding="utf-8") == "external edit\n"


@pytest.mark.asyncio
async def test_workspace_diff_bounds_long_lines_and_handles_inactive_turn(
    tmp_path: Path,
) -> None:
    store = SessionStore(tmp_path / "sessions.db")
    session = store.create_session(str(tmp_path))
    store.start_turn(session.session_id, "active", "edit")
    guard = SafetyGuard(tmp_path)
    (tmp_path / "long.py").write_text("small\n", encoding="utf-8")
    await edit_with_checkpoint(
        store, guard, session.session_id, "active", "edit", "long.py",
        "long = '" + "x" * 100_000 + "'\n",
    )
    result = await WorkspaceDiffTool(
        guard, store, lambda: (session.session_id, "active")
    ).run()
    assert result.success
    assert result.truncated
    assert len(result.output.encode("utf-8")) <= 16_384
    assert "truncated" in result.output
    no_turn = await WorkspaceDiffTool(guard, store, lambda: None).run()
    assert not no_turn.success
    assert "active" in str(no_turn.error)


@pytest.mark.asyncio
async def test_workspace_diff_redacts_recorded_secrets_before_reporting(
    tmp_path: Path,
) -> None:
    store = SessionStore(tmp_path / "redaction.db")
    session = store.create_session(str(tmp_path))
    store.start_turn(session.session_id, "active", "edit")
    guard = SafetyGuard(tmp_path)
    target = tmp_path / "configuration.txt"
    target.write_text("api_key=older-secret\n", encoding="utf-8")
    await edit_with_checkpoint(
        store, guard, session.session_id, "active", "edit",
        "configuration.txt", "api_key=totally-fake-secret-123456789abcdef\n",
    )
    result = await WorkspaceDiffTool(
        guard, store, lambda: (session.session_id, "active")
    ).run()
    assert result.success
    assert "totally-fake-secret" not in result.output
    assert "older-secret" not in result.output
    assert "[REDACTED]" in result.output


@pytest.mark.asyncio
@pytest.mark.parametrize("limit_kind", ["saved_bytes", "current_bytes", "rows"])
async def test_workspace_diff_refuses_oversized_reviews_without_partial_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, limit_kind: str,
) -> None:
    import ash.core.checkpoints as checkpoints

    store = SessionStore(tmp_path / "bounded.db")
    session = store.create_session(str(tmp_path))
    store.start_turn(session.session_id, "active", "edit")
    guard = SafetyGuard(tmp_path)
    if limit_kind == "rows":
        monkeypatch.setattr(checkpoints, "MAX_AGENT_CHECKPOINT_DIFF_ROWS", 1)
        for index in (1, 2):
            name = f"module_{index}.py"
            (tmp_path / name).write_text("old\n", encoding="utf-8")
            await edit_with_checkpoint(
                store, guard, session.session_id, "active", f"edit-{index}",
                name, "new\n",
            )
        error_text = "checkpoint review limit"
    else:
        monkeypatch.setattr(
            checkpoints, "MAX_AGENT_CHECKPOINT_DIFF_CONTENT_BYTES", 64
        )
        before = "old" * 40 if limit_kind == "saved_bytes" else "old"
        after = "new" if limit_kind == "saved_bytes" else "new" * 40
        (tmp_path / "module.py").write_text(before, encoding="utf-8")
        await edit_with_checkpoint(
            store, guard, session.session_id, "active", "edit",
            "module.py", after,
        )
        error_text = (
            "saved-content size" if limit_kind == "saved_bytes"
            else "safe review size"
        )

    result = await WorkspaceDiffTool(
        guard, store, lambda: (session.session_id, "active")
    ).run()
    assert not result.success
    assert result.output == ""
    assert error_text in str(result.error)


@pytest.mark.asyncio
async def test_coding_loop_reviews_two_edits_with_no_git_or_shell_commands(
    tmp_path: Path,
) -> None:
    """One native tool batch can edit both modules and inspect scoped diffs."""

    class EditAndReview(ProviderABC):
        model_name = "edit-and-review"
        _ash_declared_capabilities = ProviderCapabilities(native_tools=True)

        def __init__(self) -> None:
            self.calls = 0

        def count_tokens(self, text: str) -> int:
            return max(1, len(str(text)) // 4)

        async def stream_chat(self, messages, temperature=0.0, tools=None):
            self.calls += 1
            if self.calls == 1:
                yield StreamChunk(
                    is_done=True,
                    native_tool_calls=[
                        {
                            "id": f"edit-{index}", "name": "whole_edit",
                            "arguments": {
                                "file_path": name,
                                "content": f"value = {index}\n",
                            },
                        }
                        for index, name in enumerate(("one.py", "two.py"), 1)
                    ] + [{
                        "id": "review", "name": "workspace_diff", "arguments": {},
                    }],
                )
            else:
                history = repr(messages)
                assert "--- a/one.py" in history
                assert "+++ b/two.py" in history
                assert "+value = 1" in history
                assert "+value = 2" in history
                yield StreamChunk(content="Both changes reviewed", is_done=True)

    store = SessionStore(tmp_path / "two-module.db")
    guard = SafetyGuard(tmp_path)
    config = AshConfig(
        workspace_root=tmp_path,
        db_directory=tmp_path / "db",
        model="local/edit-and-review", memory_backend="off",
    )
    provider = EditAndReview()
    loop: AshLoop
    diff = WorkspaceDiffTool(
        guard, store,
        lambda: (
            (loop.current_session.session_id, loop.turn_context.turn_id)
            if loop.current_session is not None and loop.turn_context is not None
            else None
        ),
    )
    loop = AshLoop(
        store, provider, guard,
        HeadlessUI(output_format="stream-json", stream=io.StringIO()),
        tmp_path,
        tools={"whole_edit": WholeEditTool(guard), "workspace_diff": diff},
        config=config,
        safety_tier="auto_edit",
    )
    loop.tool_middlewares.append(
        FileCheckpointMiddleware(
            store, guard,
            lambda: (
                (
                    loop.current_session.session_id,
                    loop.turn_context.turn_id,
                    str(loop.turn_context.get("tool_call_id", "")),
                )
                if loop.current_session is not None and loop.turn_context is not None
                else None
            ),
        )
    )
    try:
        session = await loop.start_session()
        result = await loop.run_turn("Create and review the two modules")
        assert result == "Both changes reviewed"
        assert provider.calls == 2
        calls = store.load_session(session.session_id).tool_calls
        assert [(call.tool_name, call.success) for call in calls] == [
            ("whole_edit", True), ("whole_edit", True),
            ("workspace_diff", True),
        ]
        assert store.verify_audit_log(session.session_id) == []
        assert (tmp_path / "one.py").read_text() == "value = 1\n"
        assert (tmp_path / "two.py").read_text() == "value = 2\n"
        assert not (tmp_path / ".git").exists()
    finally:
        await loop.aclose()
