"""A failed real test must give a coding agent actionable repair locations."""

from __future__ import annotations

import io
import os
import shlex
import stat
import sys
from pathlib import Path

import pytest

from ash.core.loop import AshLoop
from ash.core.session import SessionStore
from ash.providers.base import ProviderABC, StreamChunk
from ash.providers.capabilities import ProviderCapabilities
from ash.safety.guard import SafetyGuard
from ash.tools.base import ToolMiddleware, ToolMiddlewareSkip
from ash.tools.command import RunCommandTool
from ash.tools.filesystem import (
    ReadFileTool,
    ReplaceFileContentTool,
    ReplaceFileEditsTool,
    WriteFileTool,
)
from ash.tools.search import SearchTextTool
from ash.ui.headless import HeadlessUI


@pytest.mark.asyncio
async def test_coding_agent_uses_search_line_to_edit_valid_unicode_source(
    tmp_path: Path,
) -> None:
    """Real search -> exact line edit -> authorized Python check must agree."""
    module = tmp_path / "module.py"
    original = 'greeting = "hello\u2028world"\nanswer = 1\n'
    compile(original, str(module), "exec")
    module.write_text(original, encoding="utf-8")
    (tmp_path / "verify.py").write_text(
        "from module import answer, greeting\n"
        "assert answer == 2\n"
        "assert len(greeting) == 11\n"
        "print('PHYSICAL-LINE-CHECK-PASSED')\n",
        encoding="utf-8",
    )
    command = f"{shlex.quote(sys.executable)} -B verify.py"

    class Provider(ProviderABC):
        model_name = "scripted-physical-source-lines"
        _ash_declared_capabilities = ProviderCapabilities(native_tools=True)

        def __init__(self) -> None:
            self.requests = 0

        def count_tokens(self, text: str) -> int:
            return max(1, len(str(text)) // 4)

        async def stream_chat(self, messages, temperature=0.0, tools=None):
            self.requests += 1
            history = repr(messages)
            if self.requests == 1:
                tool_calls = [{"id": "find", "name": "search_text", "arguments": {
                    "pattern": "answer = 1", "fixed_strings": True,
                    "directory_path": "module.py",
                }}]
            elif self.requests == 2:
                assert "module.py:2:answer = 1" in history
                tool_calls = [{"id": "bounded-edit", "name": "replace_file_content",
                               "arguments": {"file_path": "module.py",
                                             "start_line": 2, "end_line": 2,
                                             "target_content": "answer = 1",
                                             "replacement_content": "answer = 2"}}]
            elif self.requests == 3:
                assert module.read_text(encoding="utf-8") == original.replace(
                    "answer = 1", "answer = 2"
                )
                assert '"success": true' in history.lower()
                tool_calls = [{"id": "check", "name": "run_command",
                               "arguments": {"command_line": command}}]
            else:
                assert self.requests == 4
                assert "PHYSICAL-LINE-CHECK-PASSED" in history
                yield StreamChunk(content="Correct source line edited and verified.",
                                  is_done=True)
                return
            yield StreamChunk(is_done=True, native_tool_calls=tool_calls)

    async def approve(name: str, arguments: dict[str, object]) -> bool:
        if name == "run_command":
            assert arguments["command_line"] == command
        return True

    guard = SafetyGuard(tmp_path)
    store = SessionStore(tmp_path / "physical-lines.db")
    provider = Provider()
    loop = AshLoop(
        store, provider, guard,
        HeadlessUI(output_format="stream-json", stream=io.StringIO()),
        tmp_path,
        tools={
            "search_text": SearchTextTool(guard),
            "replace_file_content": ReplaceFileContentTool(guard),
            "run_command": RunCommandTool(guard),
        },
        on_tool_approval=approve,
    )
    try:
        session = await loop.start_session()
        response = await loop.run_turn("Find and fix answer, then run verify.py")
        assert response == "Correct source line edited and verified."
        assert provider.requests == 4
        records = store.load_session(session.session_id).tool_calls
        assert [call.tool_name for call in records] == [
            "search_text", "replace_file_content", "run_command",
        ]
        assert [call.success for call in records] == [True, True, True]
        assert records[-1].exit_code == 0
        assert "PHYSICAL-LINE-CHECK-PASSED" in (records[-1].result or "")
        assert store.verify_audit_log(session.session_id) == []
    finally:
        await loop.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("omit_initial_indentation", [False, True])
@pytest.mark.parametrize("noisy_command_output", [False, True])
async def test_real_pytest_failures_guide_two_module_repair_and_recheck(
    tmp_path: Path, omit_initial_indentation: bool, noisy_command_output: bool,
) -> None:
    """Exercise native model->command->structured errors->edits->recheck."""

    (tmp_path / "pricing.py").write_text(
        "def discounted_subtotal(price):\n    return price - 5\n",
        encoding="utf-8",
    )
    (tmp_path / "taxes.py").write_text(
        "def final_price(subtotal):\n    return subtotal + 11\n",
        encoding="utf-8",
    )
    (tmp_path / "test_checkout.py").write_text(
        "from pricing import discounted_subtotal\n"
        "from taxes import final_price\n\n"
        "def test_discount():\n"
        "    assert discounted_subtotal(100) == 90\n\n"
        "def test_tax():\n"
        "    assert final_price(90) == 99\n",
        encoding="utf-8",
    )
    command = (
        f"{shlex.quote(sys.executable)} -m pytest -q --no-header "
        "--tb=short test_checkout.py"
    )
    if noisy_command_output:
        # A real subprocess produces the pytest evidence after thousands of
        # unrelated stderr lines. This must survive model-visible extraction.
        (tmp_path / "noisy_runner.py").write_text(
            "import subprocess, sys\n"
            "sys.stderr.write('unrelated warning\\n' * 2400)\n"
            "sys.stderr.write('token=supersecretvalue\\n')\n"
            "result = subprocess.run([sys.executable, '-m', 'pytest', '-q', "
            "'--no-header', '--tb=short', 'test_checkout.py'], "
            "capture_output=True, text=True, check=False)\n"
            "sys.stdout.write(result.stdout)\n"
            "sys.stderr.write(result.stderr)\n"
            "sys.exit(result.returncode)\n",
            encoding="utf-8",
        )
        command = f"{shlex.quote(sys.executable)} -B noisy_runner.py"

    class TestRepairProvider(ProviderABC):
        model_name = "semantic-repair-scripted"
        _ash_declared_capabilities = ProviderCapabilities(native_tools=True)

        def __init__(self) -> None:
            self.requests = 0

        def count_tokens(self, text: str) -> int:
            return max(1, len(str(text)) // 4)

        async def stream_chat(self, messages, temperature=0.0, tools=None):
            self.requests += 1
            if self.requests == 1:
                tool_calls = [{
                    "id": "check-initial", "name": "run_command",
                    "arguments": {"command_line": command},
                }]
            elif self.requests == 2 or (
                omit_initial_indentation and self.requests == 3
            ):
                history = repr(messages)
                if self.requests == 2:
                    assert '"pytest_failed": 2' in history
                    assert '"line": 5' in history
                    assert '"line": 8' in history
                    assert '"path": "test_checkout.py"' in history
                    assert '"code": "AssertionError"' in history
                    if noisy_command_output:
                        assert "supersecretvalue" not in history
                        assert "[REDACTED]" in history
                else:
                    assert "Possible leading indentation mismatch at line 2" in history
                    assert "no edit was made" in history
                    assert (tmp_path / "pricing.py").read_text().endswith(
                        "    return price - 5\n"
                    )
                    assert (tmp_path / "taxes.py").read_text().endswith(
                        "    return subtotal + 11\n"
                    )
                omit_indent = omit_initial_indentation and self.requests == 2
                tool_calls = [{
                    "id": f"fix-pricing-{self.requests}",
                    "name": "replace_file_content",
                    "arguments": {
                        "file_path": "pricing.py",
                        "target_content": (
                            "return price - 5" if omit_indent else "    return price - 5"
                        ),
                        "replacement_content": "    return price - 10",
                    },
                }, {
                    "id": f"fix-taxes-{self.requests}",
                    "name": "replace_file_content",
                    "arguments": {
                        "file_path": "taxes.py",
                        "target_content": (
                            "return subtotal + 11"
                            if omit_indent else "    return subtotal + 11"
                        ),
                        "replacement_content": "    return subtotal + 9",
                    },
                }]
            elif self.requests == (4 if omit_initial_indentation else 3):
                tool_calls = [{
                    "id": "recheck", "name": "run_command",
                    "arguments": {"command_line": command},
                }]
            else:
                assert '"pytest_passed": 2' in repr(messages)
                yield StreamChunk(
                    content="Both modules repaired and the checks passed.",
                    is_done=True,
                )
                return
            yield StreamChunk(is_done=True, native_tool_calls=tool_calls)

    async def approve(tool_name: str, arguments: dict[str, object]) -> bool:
        if tool_name == "run_command":
            assert arguments["command_line"] == command
        return True

    guard = SafetyGuard(tmp_path)
    store = SessionStore(tmp_path / "semantic-repair.db")
    provider = TestRepairProvider()
    loop = AshLoop(
        store, provider, guard,
        HeadlessUI(output_format="stream-json", stream=io.StringIO()),
        tmp_path,
        tools={
            "run_command": RunCommandTool(guard),
            "replace_file_content": ReplaceFileContentTool(guard),
        },
        on_tool_approval=approve,
    )
    try:
        session = await loop.start_session()
        response = await loop.run_turn(
            "Correct pricing and taxes, then run the authorized pytest check."
        )
        assert response == "Both modules repaired and the checks passed."
        assert provider.requests == (5 if omit_initial_indentation else 4)
        assert "return price - 10" in (tmp_path / "pricing.py").read_text()
        assert "return subtotal + 9" in (tmp_path / "taxes.py").read_text()
        calls = store.load_session(session.session_id).tool_calls
        expected_tools = [
            "run_command", "replace_file_content", "replace_file_content",
        ]
        expected_success = [False, not omit_initial_indentation, not omit_initial_indentation]
        if omit_initial_indentation:
            expected_tools.extend(("replace_file_content", "replace_file_content"))
            expected_success.extend((True, True))
        expected_tools.append("run_command")
        expected_success.append(True)
        assert [call.tool_name for call in calls] == expected_tools
        assert [call.success for call in calls] == expected_success
        assert [calls[0].exit_code, calls[-1].exit_code] == [1, 0]
        assert store.verify_audit_log(session.session_id) == []
    finally:
        await loop.aclose()


@pytest.mark.asyncio
async def test_redundant_atomic_edit_batch_fails_then_real_check_verifies_retry(
    tmp_path: Path,
) -> None:
    """Do not report a partly redundant batch as applied coding work."""

    source = tmp_path / "invoice.py"
    source.write_text(
        "def subtotal(price):\n    return price - 5\n\n"
        "def taxed(subtotal):\n    return subtotal + 11\n",
        encoding="utf-8",
    )
    (tmp_path / "verify.py").write_text(
        "from invoice import subtotal, taxed\n"
        "assert subtotal(100) == 90\n"
        "assert taxed(90) == 99\n"
        "print('CHECK-PASSED')\n",
        encoding="utf-8",
    )
    command = f"{shlex.quote(sys.executable)} -B verify.py"
    original = source.read_bytes()

    class Provider(ProviderABC):
        model_name = "scripted-atomic-retry"
        _ash_declared_capabilities = ProviderCapabilities(native_tools=True)

        def __init__(self) -> None:
            self.calls = 0

        def count_tokens(self, text: str) -> int:
            return max(1, len(str(text)) // 4)

        async def stream_chat(self, messages, temperature=0.0, tools=None):
            self.calls += 1
            if self.calls == 1:
                # First candidate would fix the tax but silently rewrite an
                # unchanged subtotal range if the tool allowed partial no-ops.
                assert source.read_bytes() == original
                edits = [
                    {"start_line": 5, "end_line": 5,
                     "target_content": "    return subtotal + 11",
                     "replacement_content": "    return subtotal + 9"},
                    {"start_line": 2, "end_line": 2,
                     "target_content": "    return price - 5",
                     "replacement_content": "    return price - 5"},
                ]
                calls = [{"id": "redundant-batch", "name": "replace_file_edits",
                          "arguments": {"file_path": "invoice.py", "edits": edits}}]
            elif self.calls == 2:
                assert source.read_bytes() == original
                assert "edit 2" in repr(messages)
                assert "no edit was made" in repr(messages)
                edits = [
                    {"start_line": 5, "end_line": 5,
                     "target_content": "    return subtotal + 11",
                     "replacement_content": "    return subtotal + 9"},
                    {"start_line": 2, "end_line": 2,
                     "target_content": "    return price - 5",
                     "replacement_content": "    return price - 10"},
                ]
                calls = [{"id": "actual-batch", "name": "replace_file_edits",
                          "arguments": {"file_path": "invoice.py", "edits": edits}}]
            elif self.calls == 3:
                assert b"return price - 10" in source.read_bytes()
                assert b"return subtotal + 9" in source.read_bytes()
                calls = [{"id": "verify-after-edit", "name": "run_command",
                          "arguments": {"command_line": command}}]
            else:
                assert self.calls == 4
                assert "CHECK-PASSED" in repr(messages)
                yield StreamChunk(content="Repaired and verified.", is_done=True)
                return
            yield StreamChunk(is_done=True, native_tool_calls=calls)

    async def approve(tool_name: str, arguments: dict[str, object]) -> bool:
        if tool_name == "run_command":
            assert arguments["command_line"] == command
        return True

    guard = SafetyGuard(tmp_path)
    store = SessionStore(tmp_path / "atomic-retry.db")
    provider = Provider()
    loop = AshLoop(
        store, provider, guard,
        HeadlessUI(output_format="stream-json", stream=io.StringIO()),
        tmp_path,
        tools={
            "replace_file_edits": ReplaceFileEditsTool(guard),
            "run_command": RunCommandTool(guard),
        },
        on_tool_approval=approve,
    )
    try:
        session = await loop.start_session()
        response = await loop.run_turn("Fix invoice.py and verify with verify.py")
        assert response == "Repaired and verified."
        assert provider.calls == 4
        recorded = store.load_session(session.session_id).tool_calls
        assert [call.tool_name for call in recorded] == [
            "replace_file_edits", "replace_file_edits", "run_command"
        ]
        assert [call.success for call in recorded] == [False, True, True]
        assert "edit 2" in (recorded[0].error or "")
        assert "CHECK-PASSED" in (recorded[2].result or "")
        assert store.verify_audit_log(session.session_id) == []
    finally:
        await loop.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("skip_tool", ["write_file", "run_command"])
async def test_skipped_coding_action_is_visible_as_failure_and_retried_deliberately(
    tmp_path: Path, skip_tool: str,
) -> None:
    """A skipped write/check cannot masquerade as a completed coding action."""

    (tmp_path / "verify.py").write_text(
        "from pathlib import Path\n"
        "from app import VALUE\n"
        "assert VALUE == 42\n"
        "Path('verified.txt').write_text('verified', encoding='utf-8')\n"
        "print('CHECK-PASSED')\n",
        encoding="utf-8",
    )
    command = f"{shlex.quote(sys.executable)} -B verify.py"

    class SkipOnce(ToolMiddleware):
        def __init__(self) -> None:
            self.skipped = False

        async def before_tool(self, name, arguments, tool) -> None:
            if name == skip_tool and not self.skipped:
                self.skipped = True
                raise ToolMiddlewareSkip()

    class Provider(ProviderABC):
        model_name = "scripted-skip-retry"
        _ash_declared_capabilities = ProviderCapabilities(native_tools=True)

        def __init__(self) -> None:
            self.requests = 0

        def count_tokens(self, text: str) -> int:
            return max(1, len(str(text)) // 4)

        async def stream_chat(self, messages, temperature=0.0, tools=None):
            self.requests += 1
            step = self.requests
            if step == 1 or (step == 2 and skip_tool == "write_file"):
                if step == 2:
                    assert not (tmp_path / "app.py").exists()
                    assert "Tool was not executed" in repr(messages)
                    assert '"success": false' in repr(messages).lower()
                calls = [{
                    "id": f"write-{step}", "name": "write_file",
                    "arguments": {
                        "file_path": "app.py", "content": "VALUE = 42\n",
                        "overwrite": False,
                    },
                }]
            elif step in (2, 3):
                assert (tmp_path / "app.py").read_text() == "VALUE = 42\n"
                if step == 3 and skip_tool == "run_command":
                    assert not (tmp_path / "verified.txt").exists()
                    assert "Tool was not executed" in repr(messages)
                    assert '"success": false' in repr(messages).lower()
                calls = [{
                    "id": f"check-{step}", "name": "run_command",
                    "arguments": {"command_line": command},
                }]
            else:
                assert step == 4
                assert "CHECK-PASSED" in repr(messages)
                yield StreamChunk(content="Verified after deliberate retry.", is_done=True)
                return
            yield StreamChunk(is_done=True, native_tool_calls=calls)

    async def approve(tool_name: str, arguments: dict[str, object]) -> bool:
        if tool_name == "run_command":
            assert arguments["command_line"] == command
        return True

    guard = SafetyGuard(tmp_path)
    store = SessionStore(tmp_path / f"skip-{skip_tool}.db")
    provider = Provider()
    loop = AshLoop(
        store, provider, guard,
        HeadlessUI(output_format="stream-json", stream=io.StringIO()),
        tmp_path,
        tools={
            "write_file": WriteFileTool(guard),
            "run_command": RunCommandTool(guard),
        },
        tool_middlewares=[SkipOnce()],
        on_tool_approval=approve,
    )
    try:
        session = await loop.start_session()
        response = await loop.run_turn("Write app.py and verify it using verify.py")
        assert response == "Verified after deliberate retry."
        assert provider.requests == 4
        assert (tmp_path / "verified.txt").read_text() == "verified"
        calls = store.load_session(session.session_id).tool_calls
        expected = (
            ["write_file", "write_file", "run_command"]
            if skip_tool == "write_file"
            else ["write_file", "run_command", "run_command"]
        )
        assert [item.tool_name for item in calls] == expected
        assert [item.executed for item in calls] == (
            [False, True, True] if skip_tool == "write_file" else [True, False, True]
        )
        skipped = [item for item in calls if not item.executed]
        assert len(skipped) == 1
        assert skipped[0].dispatched is True
        assert skipped[0].success is not True
        assert "Tool was not executed" in (skipped[0].error or "")
        assert store.verify_audit_log(session.session_id) == []
        assert any(
            entry.result == "FAILURE"
            and entry.details.get("call_id") == skipped[0].call_id
            for entry in store.list_audit_logs(session.session_id)
        )
    finally:
        await loop.aclose()


@pytest.mark.skipif(os.name != "posix", reason="POSIX parent-directory fsync")
@pytest.mark.asyncio
async def test_published_write_with_failed_sync_is_inspected_not_replayed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The real coding loop must preserve an ambiguous published write."""
    from ash.safety import scoped_io

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "verify.py").write_text(
        "from pathlib import Path\n"
        "assert Path('module.py').read_text() == 'VALUE = 42\\n'\n"
        "print('VISIBLE-CHECK-PASSED')\n",
        encoding="utf-8",
    )
    source = workspace / "module.py"
    command = f"{shlex.quote(sys.executable)} -B verify.py"
    expected_parent = workspace.stat()
    real_sync = scoped_io.os.fsync

    def fail_workspace_directory_sync(fd: int) -> None:
        metadata = os.fstat(fd)
        if stat.S_ISDIR(metadata.st_mode) and (
            metadata.st_dev, metadata.st_ino
        ) == (expected_parent.st_dev, expected_parent.st_ino):
            raise OSError("injected project-directory sync failure")
        real_sync(fd)

    class Provider(ProviderABC):
        model_name = "scripted-published-write-recovery"
        _ash_declared_capabilities = ProviderCapabilities(native_tools=True)

        def __init__(self) -> None:
            self.requests = 0

        def count_tokens(self, text: str) -> int:
            return max(1, len(str(text)) // 4)

        async def stream_chat(self, messages, temperature=0.0, tools=None):
            self.requests += 1
            history = repr(messages)
            if self.requests == 1:
                calls = [{"id": "one-write", "name": "write_file",
                          "arguments": {"file_path": "module.py", "content": "VALUE = 42\n"}}]
            elif self.requests == 2:
                assert source.read_text() == "VALUE = 42\n"
                assert "Tool outcome is ambiguous" in history
                assert "did not retry" in history
                calls = [{"id": "inspect-after-fault", "name": "read_file",
                          "arguments": {"file_path": "module.py"}}]
            elif self.requests == 3:
                assert "1: VALUE = 42" in history
                calls = [{"id": "check-after-inspection", "name": "run_command",
                          "arguments": {"command_line": command}}]
            else:
                assert self.requests == 4
                assert "VISIBLE-CHECK-PASSED" in history
                yield StreamChunk(
                    content="Workspace checked; write durability remains uncertain.",
                    is_done=True,
                )
                return
            yield StreamChunk(is_done=True, native_tool_calls=calls)

    async def approve(tool_name: str, arguments: dict[str, object]) -> bool:
        if tool_name == "run_command":
            assert arguments["command_line"] == command
        return True

    guard = SafetyGuard(workspace)
    store = SessionStore(tmp_path / "published-write.db")
    provider = Provider()
    loop = AshLoop(
        store, provider, guard,
        HeadlessUI(output_format="stream-json", stream=io.StringIO()),
        workspace,
        tools={
            "write_file": WriteFileTool(guard),
            "read_file": ReadFileTool(guard),
            "run_command": RunCommandTool(guard),
        },
        on_tool_approval=approve,
    )
    try:
        with monkeypatch.context() as filesystem_fault:
            filesystem_fault.setattr(
                scoped_io.os, "fsync", fail_workspace_directory_sync
            )
            session = await loop.start_session()
            response = await loop.run_turn("Create module and test it")
        assert response == "Workspace checked; write durability remains uncertain."
        assert provider.requests == 4
        assert source.read_text() == "VALUE = 42\n"
        calls = store.load_session(session.session_id).tool_calls
        assert [call.tool_name for call in calls] == [
            "write_file", "read_file", "run_command"
        ]
        assert calls[0].dispatched and calls[0].executed
        assert calls[0].success is None
        assert "ambiguous" in (calls[0].error or "").lower()
        assert calls[1].success and calls[2].success
        assert "VISIBLE-CHECK-PASSED" in (calls[2].result or "")
        assert store.verify_audit_log(session.session_id) == []
        assert any(
            entry.result == "FAILURE"
            and entry.details.get("call_id") == "one-write"
            and entry.details.get("ambiguous") is True
            for entry in store.list_audit_logs(session.session_id)
        )
    finally:
        await loop.aclose()
