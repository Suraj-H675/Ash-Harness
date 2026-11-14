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
