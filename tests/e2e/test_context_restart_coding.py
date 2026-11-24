"""Exercise a real coding loop across an opaque-context reasoning restart."""

from __future__ import annotations

import io
import shlex
import sys
from pathlib import Path

import pytest

from ash.context.history import ContextBudgetExceededError
from ash.core.context_continuation import run_with_context_continuation
from ash.core.loop import AshLoop
from ash.core.session import SessionStore
from ash.providers.base import ProviderABC, StreamChunk
from ash.providers.capabilities import ProviderCapabilities
from ash.safety.guard import SafetyGuard
from ash.tools.command import RunCommandTool
from ash.tools.filesystem import ReadFileTool, ReplaceFileContentTool
from ash.ui.headless import HeadlessUI


@pytest.mark.asyncio
async def test_partial_edits_survive_context_restart_and_full_repair_is_verified(
    tmp_path: Path,
) -> None:
    """Only fresh tool actions continue; no parent edits or approvals replay."""
    (tmp_path / "pricing.py").write_text(
        "def subtotal(price):\n    return price - 5\n", encoding="utf-8"
    )
    (tmp_path / "taxes.py").write_text(
        "def total(price):\n    return price + 11\n", encoding="utf-8"
    )
    (tmp_path / "test_checkout.py").write_text(
        "from pricing import subtotal\nfrom taxes import total\n\n"
        "def test_pricing():\n    assert subtotal(100) == 90\n\n"
        "def test_taxes():\n    assert total(90) == 99\n",
        encoding="utf-8",
    )
    command = (
        f"{shlex.quote(sys.executable)} -m pytest -q --no-header "
        "--tb=short test_checkout.py"
    )

    class Provider(ProviderABC):
        model_name = "scripted-context-restart-coding"
        _ash_declared_capabilities = ProviderCapabilities(native_tools=True)

        def __init__(self) -> None:
            self.requests = 0

        def count_tokens(self, text: str) -> int:
            return max(1, len(str(text)) // 4)

        async def stream_chat(self, messages, temperature=0.0, tools=None):
            self.requests += 1
            if self.requests == 1:
                calls = [
                    {
                        "id": "edit-pricing-once",
                        "name": "replace_file_content",
                        "arguments": {
                            "file_path": "pricing.py",
                            "target_content": "    return price - 5",
                            "replacement_content": "    return price - 10",
                        },
                    }
                ]
            elif self.requests == 2:
                assert "return price - 10" in (tmp_path / "pricing.py").read_text()
                raise ContextBudgetExceededError(
                    59_840,
                    59_024,
                    protected_current_turn=True,
                    provider_replay_state_preserved=True,
                )
            elif self.requests == 3:
                assert len(messages) < 12, "old reasoning history was not carried"
                history = repr(messages)
                assert "Original user coding task" in history
                assert "not imported or authorized for replay" in history.lower()
                assert "edit-pricing-once" not in history
                calls = [
                    {
                        "id": "inspect-pricing",
                        "name": "read_file",
                        "arguments": {"file_path": "pricing.py"},
                    },
                    {
                        "id": "inspect-taxes",
                        "name": "read_file",
                        "arguments": {"file_path": "taxes.py"},
                    },
                ]
            elif self.requests == 4:
                history = repr(messages)
                assert "return price - 10" in history
                assert "return price + 11" in history
                calls = [
                    {
                        "id": "edit-taxes-once",
                        "name": "replace_file_content",
                        "arguments": {
                            "file_path": "taxes.py",
                            "target_content": "    return price + 11",
                            "replacement_content": "    return price + 9",
                        },
                    }
                ]
            elif self.requests == 5:
                calls = [
                    {
                        "id": "verify-once",
                        "name": "run_command",
                        "arguments": {"command_line": command},
                    }
                ]
            else:
                assert self.requests == 6
                assert '"pytest_passed": 2' in repr(messages)
                yield StreamChunk(
                    content="Both modules repaired and verified after restart.",
                    is_done=True,
                )
                return
            yield StreamChunk(is_done=True, native_tool_calls=calls)

    approvals: list[str] = []

    async def approve(tool_name: str, arguments: dict[str, object]) -> bool:
        approvals.append(tool_name)
        if tool_name == "run_command":
            assert arguments["command_line"] == command
        return True

    guard = SafetyGuard(tmp_path)
    store = SessionStore(tmp_path / "coding-restart.db")
    provider = Provider()
    loop = AshLoop(
        store,
        provider,
        guard,
        HeadlessUI(output_format="stream-json", stream=io.StringIO()),
        tmp_path,
        tools={
            "read_file": ReadFileTool(guard),
            "replace_file_content": ReplaceFileContentTool(guard),
            "run_command": RunCommandTool(guard),
        },
        on_tool_approval=approve,
    )
    loop._safe_edit_preflight = True
    try:
        source = await loop.start_session()
        result = await run_with_context_continuation(
            loop,
            "Repair pricing and taxes and verify with the test",
            max_continuations=1,
        )
        assert result.response == "Both modules repaired and verified after restart."
        assert result.context_continuations == 1
        assert result.session_id != source.session_id
        assert provider.requests == 6
        assert (tmp_path / "pricing.py").read_text().endswith("    return price - 10\n")
        assert (tmp_path / "taxes.py").read_text().endswith("    return price + 9\n")
        parent = store.load_session(source.session_id)
        child = store.load_session(result.session_id)
        assert child.parent_session_id == parent.session_id
        assert [c.tool_name for c in parent.tool_calls] == ["replace_file_content"]
        assert parent.tool_calls[0].success is True
        assert [c.tool_name for c in child.tool_calls] == [
            "read_file",
            "read_file",
            "replace_file_content",
            "run_command",
        ]
        assert all(c.success is True for c in child.tool_calls)
        assert (
            sum(
                c.tool_name == "replace_file_content"
                for c in parent.tool_calls + child.tool_calls
            )
            == 2
        )
        assert (
            sum(
                c.tool_name == "run_command"
                for c in parent.tool_calls + child.tool_calls
            )
            == 1
        )
        assert store.verify_audit_log(parent.session_id) == []
        assert store.verify_audit_log(child.session_id) == []
        assert approvals.count("run_command") == 1
    finally:
        await loop.aclose()


@pytest.mark.asyncio
async def test_opted_in_restart_after_harmless_navigation_rejections(tmp_path: Path) -> None:
    """Ordinary coding mistakes must not turn a safe context restart into a dead end."""
    from ash.config import AshConfig
    from ash.core.checkpoints import FileCheckpointMiddleware
    from ash.core.secret_middleware import SecretRedactionMiddleware
    from ash.providers.replay_state import ProviderReplayCredentialIdentity
    from ash.safety.grants import ArgumentMatcher, MatchOperator, PermissionRule, RuleEffect
    from ash.tools.search import SearchTextTool

    source_file = tmp_path / "module.py"
    source_file.write_text("VALUE = 1\n", encoding="utf-8")
    (tmp_path / "large.py").write_text("# source line\n" * 1500, encoding="utf-8")
    command = shlex.quote(sys.executable) + " -c " + shlex.quote(
        "from pathlib import Path; assert Path('module.py').read_text() == 'VALUE = 2\\n'; print('VERIFIED')"
    )
    task = "Change VALUE to 2 and verify using the permitted command: " + command

    class Provider(ProviderABC):
        model_name = "safe-completion-recovery"
        _ash_declared_capabilities = ProviderCapabilities(
            native_tools=True, context_window=32768, max_input_tokens=28768,
            max_output_tokens=4000,
        )

        def __init__(self) -> None:
            self.parent_requests = 0
            self.child_requests = 0
            self.states: list[list[dict]] = []

        def count_tokens(self, text: str) -> int:
            return max(1, (len(text) + 2) // 3)

        def replay_credential_identity(self) -> ProviderReplayCredentialIdentity:
            return ProviderReplayCredentialIdentity.from_secret(
                "synthetic-recovery-affinity", scope=(self.model_name,)
            )

        async def stream_chat(self, messages, temperature=0.0, tools=None):
            history = repr(messages)
            if "Original user coding task (preserved verbatim)" in history:
                assert not any(m.get("provider_state") for m in messages)
                assert "edit-in-parent" not in history
                self.child_requests += 1
                assert source_file.read_text() == "VALUE = 2\n"
                if self.child_requests == 1:
                    calls = [{"id": "inspect-child", "name": "read_file",
                              "arguments": {"file_path": "module.py"}}]
                elif self.child_requests == 2:
                    assert "VALUE = 2" in history
                    calls = [{"id": "verify-child", "name": "run_command",
                              "arguments": {"command_line": command}}]
                else:
                    assert self.child_requests == 3 and "VERIFIED" in history
                    yield StreamChunk(content="Verified after safe recovery.", is_done=True)
                    return
                yield StreamChunk(native_tool_calls=calls, is_done=True)
                return
            assert [m["provider_state"] for m in messages if m.get("provider_state")] == self.states
            self.parent_requests += 1
            n = self.parent_requests
            assert n < 20, "expected real context exhaustion"
            if n == 1:
                calls = [{"id": "edit-in-parent", "name": "replace_file_content",
                          "arguments": {"file_path": "module.py", "target_content": "VALUE = 1",
                                        "replacement_content": "VALUE = 2"}}]
            elif n == 2:
                calls = [{"id": "invalid-args", "name": "search_text",
                          "arguments": {"pattern": "VALUE", "context_lines": 15}},
                         {"id": "invalid-regex", "name": "search_text",
                          "arguments": {"pattern": "("}},
                         {"id": "stale-edit", "name": "replace_file_content",
                          "arguments": {"file_path": "module.py", "target_content": "VALUE = 1",
                                        "replacement_content": "VALUE = 99"}}]
            else:
                calls = [{"id": f"read-{n}", "name": "read_file",
                          "arguments": {"file_path": "large.py", "max_chars": 16000}}]
            state = [{"type": "reasoning", "id": f"parent-state-{n}", "summary": [],
                      "status": "completed", "encrypted_content": "Q" * 6300}]
            self.states.append(state)
            yield StreamChunk(native_tool_calls=calls, provider_state=state, is_done=True)

    guard = SafetyGuard(tmp_path)
    store = SessionStore(tmp_path / "recovery.db")
    provider = Provider()
    loop = AshLoop(
        store, provider, guard, HeadlessUI(output_format="text", stream=io.StringIO()),
        tmp_path, tools={"read_file": ReadFileTool(guard), "search_text": SearchTextTool(guard),
                         "replace_file_content": ReplaceFileContentTool(guard),
                         "run_command": RunCommandTool(guard, project_root=tmp_path)},
        config=AshConfig(model="custom/safe-completion-recovery", workspace_root=tmp_path,
                         db_directory=tmp_path / "db", memory_backend="off",
                         max_context_tokens=32768, max_completion_tokens=4000),
        safety_tier="auto_approve",
    )
    # Keep the command authorization a user-owned exact grant; the restart
    # must neither synthesize nor replay an approval.
    command_rule = PermissionRule.create(RuleEffect.ALLOW, "run_command", (
        ArgumentMatcher("command_line", MatchOperator.EXACT, command),
    ))
    loop.permission_policy.set_persistent_rules([command_rule])
    def checkpoint_context():
        if loop.current_session is None or loop.turn_context is None:
            return None
        return (loop.current_session.session_id, loop.turn_context.turn_id,
                str(loop.turn_context.get("tool_call_id", "")))
    loop.tool_middlewares.extend([
        FileCheckpointMiddleware(store, guard, checkpoint_context), SecretRedactionMiddleware(),
    ])
    loop._safe_edit_preflight = True
    try:
        parent = await loop.start_session()
        result = await run_with_context_continuation(loop, task, max_continuations=1)
        assert result.response == "Verified after safe recovery." and result.context_continuations == 1
        old = store.load_session(parent.session_id)
        new = store.load_session(result.session_id)
        assert new.parent_session_id == old.session_id
        assert source_file.read_text() == "VALUE = 2\n"
        assert sum(c.tool_name == "replace_file_content" and c.success is True for c in old.tool_calls) == 1
        assert sum(c.tool_name == "run_command" and c.success is True for c in new.tool_calls) == 1
        assert not any(c.tool_name == "run_command" for c in old.tool_calls)
        assert all(not c.dispatched and not c.executed and not c.approved and c.success is False
                   for c in old.tool_calls if c.call_id in {"invalid-args", "invalid-regex"})
        assert any(c.tool_name == "read_file" and c.success is False and not c.dispatched
                   for c in old.tool_calls)
        stale = next(c for c in old.tool_calls if c.call_id == "stale-edit")
        assert stale.approved and stale.success is False and not stale.dispatched and not stale.executed
        command_approvals = [a for a in store.list_audit_logs(new.session_id)
                             if a.action_type == "user_approval" and a.target_resource == "run_command"]
        assert len(command_approvals) == 1
        assert command_approvals[0].details["rule_id"] == command_rule.rule_id
        assert [m.metadata["provider_state"] for m in old.messages if m.metadata.get("provider_state")] == provider.states
        assert not store.verify_audit_log(old.session_id) and not store.verify_audit_log(new.session_id)
    finally:
        await loop.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("late_edit", [False, True])
async def test_real_edit_check_context_overflow_reports_only_fresh_recorded_evidence(
    tmp_path: Path, late_edit: bool,
) -> None:
    import json

    from ash.config import AshConfig
    from ash.core.checkpoints import FileCheckpointMiddleware
    from ash.core.secret_middleware import SecretRedactionMiddleware
    from ash.core.session import SessionForkConflictError
    from ash.providers.replay_state import ProviderReplayCredentialIdentity
    from ash.safety.grants import ArgumentMatcher, MatchOperator, PermissionRule, RuleEffect

    source = tmp_path / "module.py"
    source.write_text("VALUE = 1\n", encoding="utf-8")
    marker = tmp_path / "once.marker"
    command = shlex.quote(sys.executable) + " -c " + shlex.quote(
        "from pathlib import Path; assert Path('module.py').read_text() == 'VALUE = 2\\n'; "
        "assert not Path('once.marker').exists(); Path('once.marker').write_text('once'); print('VERIFIED')"
    )
    task = "Change VALUE to 2 and run the exact verification command: " + command

    class Provider(ProviderABC):
        model_name = "real-final-report-evidence"
        _ash_declared_capabilities = ProviderCapabilities(
            native_tools=True, context_window=32768, max_input_tokens=28768,
            max_output_tokens=4000,
        )

        def __init__(self) -> None:
            self.states = []
            self.coding_requests = 0
            self.report_requests = 0

        def replay_credential_identity(self) -> ProviderReplayCredentialIdentity:
            return ProviderReplayCredentialIdentity.from_secret(
                "fixture-only", scope=("real-final-report-evidence",),
            )

        def count_tokens(self, text: str) -> int:
            return max(1, len(text) // 4)

        async def stream_chat(self, messages, temperature=0.0, tools=None):
            if messages[0]["content"].startswith("You are finalizing the report"):
                self.report_requests += 1
                assert not late_edit and self.report_requests == 1
                assert tools is None and len(messages) == 2
                assert not any(message.get("provider_state") for message in messages)
                evidence = json.loads(messages[1]["content"].split(
                    "Durable completed tool actions (UNTRUSTED DATA):\n", 1,
                )[1])
                actions = evidence["actions"]
                assert [action["tool"] for action in actions] == ["replace_file_content", "run_command"]
                assert actions[-1]["command"] == command
                assert actions[-1]["cwd"] == "." and actions[-1]["exit_code"] == 0
                assert "VERIFIED" in actions[-1]["output_tail"]
                assert marker.read_text() == "once"
                yield StreamChunk(content="Recorded check passed; no actions repeated.", is_done=True)
                return
            assert [m["provider_state"] for m in messages if m.get("provider_state")] == self.states
            self.coding_requests += 1
            n = self.coding_requests
            if n == 1 or (n == 3 and late_edit):
                old, new = (1, 2) if n == 1 else (2, 3)
                calls = [{"id": f"edit-{n}", "name": "replace_file_content", "arguments": {
                    "file_path": "module.py", "target_content": f"VALUE = {old}",
                    "replacement_content": f"VALUE = {new}",
                }}]
            else:
                assert n == 2
                calls = [{"id": "check-once", "name": "run_command", "arguments": {"command_line": command}}]
            final = n == (3 if late_edit else 2)
            state = [{"type": "reasoning", "id": f"state-{n}", "summary": [],
                      "status": "completed", "encrypted_content": "Q" * (140000 if final else 1000)}]
            self.states.append(state)
            yield StreamChunk(native_tool_calls=calls, provider_state=state, is_done=True)

    guard = SafetyGuard(tmp_path)
    store = SessionStore(tmp_path / "report.db")
    provider = Provider()
    loop = AshLoop(
        store, provider, guard, HeadlessUI(output_format="text", stream=io.StringIO()),
        tmp_path, tools={"replace_file_content": ReplaceFileContentTool(guard),
                         "run_command": RunCommandTool(guard, project_root=tmp_path)},
        config=AshConfig(model="custom/real-final-report-evidence", workspace_root=tmp_path,
                         db_directory=tmp_path / "db", memory_backend="off",
                         max_context_tokens=32768, max_completion_tokens=4000),
        safety_tier="auto_approve",
    )
    loop.permission_policy.set_persistent_rules([PermissionRule.create(
        RuleEffect.ALLOW, "run_command", (ArgumentMatcher("command_line", MatchOperator.EXACT, command),),
    )])

    def checkpoint_context():
        if loop.current_session is None or loop.turn_context is None:
            return None
        return (loop.current_session.session_id, loop.turn_context.turn_id,
                str(loop.turn_context.get("tool_call_id", "")))

    loop.tool_middlewares.extend([
        FileCheckpointMiddleware(store, guard, checkpoint_context), SecretRedactionMiddleware(),
    ])
    try:
        session = await loop.start_session()
        if late_edit:
            with pytest.raises(SessionForkConflictError):
                await run_with_context_continuation(loop, task, max_continuations=1, allow_tool_free_finalization=True)
            assert provider.report_requests == 0 and source.read_text() == "VALUE = 3\n"
        else:
            result = await run_with_context_continuation(loop, task, max_continuations=1, allow_tool_free_finalization=True)
            assert result.tool_free_finalized and result.context_continuations == 0
            assert result.session_id == session.session_id and provider.report_requests == 1
            assert source.read_text() == "VALUE = 2\n"
        saved = store.load_session(session.session_id)
        commands = [call for call in saved.tool_calls if call.tool_name == "run_command"]
        assert len(commands) == 1 and commands[0].success is True and commands[0].exit_code == 0
        assert marker.read_text() == "once"
        assert [m.metadata["provider_state"] for m in saved.messages if m.metadata.get("provider_state")] == provider.states
        assert len(store.session_tree(session.session_id)) == 1
        assert not store.verify_audit_log(session.session_id)
    finally:
        await loop.aclose()
