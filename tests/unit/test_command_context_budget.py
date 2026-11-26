from __future__ import annotations

import json
import xml.etree.ElementTree as ET
from copy import deepcopy
from pathlib import Path
import shlex
import sys

import pytest

from ash.config import AshConfig
from ash.context.history import ContextBudgetExceededError, HistoryCompactor
from ash.context.instructions import (
    InstructionDiagnostic,
    discover_instructions_for_directories,
    render_instructions,
)
import ash.core.loop as loop_module
from ash.core.loop import AshLoop
from ash.core.redaction import redact_text
from ash.core.secret_middleware import SecretRedactionMiddleware
from ash.core.session import Message, SessionStore
from ash.memory.pipeline import MemoryHit
from ash.providers.base import ProviderABC, StreamChunk
from ash.providers.capabilities import ProviderCapabilities
from ash.safety.guard import SafetyGuard
from ash.tools.command import RunCommandTool
from ash.tools.base import ToolMiddleware, ToolResult
from ash.tools.filesystem import WriteFileTool
from ash.ui.terminal import TerminalUI


_VERBOSE_OUTPUT = "ordinary verification output\n" * 2_034
_FAILURE_DIAGNOSTIC = "target.py:1: error: expected value 42"


class _EventUI(TerminalUI):
    def __init__(self) -> None:
        super().__init__(safety_tier="auto_approve")
        self.events: list[dict[str, object]] = []

    def emit_event(self, payload: dict[str, object]) -> None:
        self.events.append(payload)


class _OpaqueCommandProvider(ProviderABC):
    model_name = "r52-command-context"
    _ash_declared_capabilities = ProviderCapabilities(
        native_tools=True,
        context_window=16_000,
        max_output_tokens=2_000,
    )

    def __init__(
        self,
        *,
        repair_after_failure: bool,
        verbose: bool,
        replay_words: int = 10_000,
        byte_sensitive_counting: bool = False,
        timeout_after_output: bool = False,
        mixed_batch: bool = False,
    ) -> None:
        self.repair_after_failure = repair_after_failure
        self.verbose = verbose
        self.replay_words = replay_words
        self.byte_sensitive_counting = byte_sensitive_counting
        self.timeout_after_output = timeout_after_output
        self.mixed_batch = mixed_batch
        self.calls = 0
        self.requests: list[list[dict[str, object]]] = []
        self.schemas: list[list[dict[str, object]] | None] = []
        self.repair_saw_failure_evidence = False
        self.unknown_result_was_truthful = False
        self.unknown_result_payload: dict[str, object] = {}

    def count_tokens(self, text: str) -> int:
        token_count = len(text.split())
        if self.byte_sensitive_counting:
            token_count += len(text.encode("utf-8")) // 128
        return token_count

    def replay_credential_identity(self):
        from ash.providers.replay_state import ProviderReplayCredentialIdentity

        return ProviderReplayCredentialIdentity.from_secret(
            "r52-synthetic-replay-key",
            scope=("command-context-test",),
        )

    async def stream_chat(self, messages, temperature=0.0, tools=None):
        del temperature
        self.requests.append(json.loads(json.dumps(messages)))
        self.schemas.append(json.loads(json.dumps(tools)) if tools is not None else None)
        self.calls += 1

        if self.calls == 1:
            yield StreamChunk(
                is_done=True,
                native_tool_calls=[
                    {
                        "id": "r52-edit-initial",
                        "name": "write_file",
                        "arguments": {
                            "file_path": "target.py",
                            "content": "value = 41\n" if self.repair_after_failure else "value = 42\n",
                        },
                    }
                ],
            )
            return

        if self.calls == 2:
            executable = shlex.quote(sys.executable)
            command = f"{executable} -B timeout_verify.py" if self.timeout_after_output else (
                f"{executable} -B verify.py --verbose"
                if self.verbose
                else f"{executable} -B verify.py"
            )
            command_arguments: dict[str, object] = {"command_line": command}
            if self.timeout_after_output:
                command_arguments["timeout_seconds"] = 1
            native_tool_calls = [
                {
                    "id": "r52-command-initial",
                    "name": "run_command",
                    "arguments": command_arguments,
                }
            ]
            if self.mixed_batch:
                native_tool_calls.append(
                    {
                        "id": "r52-mixed-write",
                        "name": "write_file",
                        "arguments": {
                            "file_path": "mixed-batch-side-effect.txt",
                            "content": "write occurred once\n",
                        },
                    }
                )
            yield StreamChunk(
                is_done=True,
                native_tool_calls=native_tool_calls,
                provider_state=[
                    {
                        "type": "reasoning",
                        "id": "r52-opaque-state",
                        "summary": [],
                        "status": "completed",
                        "encrypted_content": "opaque " * self.replay_words,
                    }
                ],
            )
            return

        if self.timeout_after_output and self.calls == 3:
            result = next(
                message
                for message in reversed(self.requests[-1])
                if message.get("role") == "tool"
            )
            content = str(result.get("content", ""))
            try:
                payload = json.loads(ET.fromstring(content).text or "")
            except (ET.ParseError, json.JSONDecodeError):
                payload = {}
            self.unknown_result_payload = payload
            self.unknown_result_was_truthful = (
                payload.get("success") is False
                and payload.get("outcome") == "unknown"
                and "exit_code" not in payload
                and "Tool outcome is ambiguous" in str(payload.get("error", ""))
            )
            yield StreamChunk(
                content="Ambiguous command outcome preserved",
                is_done=True,
            )
            return

        if self.repair_after_failure and self.calls == 3:
            result = next(
                message
                for message in reversed(self.requests[-1])
                if message.get("role") == "tool"
            )
            content = str(result.get("content", ""))
            self.repair_saw_failure_evidence = (
                '"success": false' in content
                and '"exit_code": 1' in content
                and _FAILURE_DIAGNOSTIC in content
            )
            yield StreamChunk(
                is_done=True,
                native_tool_calls=[
                    {
                        "id": "r52-edit-repair",
                        "name": "write_file",
                        "arguments": {
                            "file_path": "target.py",
                            "content": "value = 42\n",
                            "overwrite": True,
                        },
                    }
                ],
            )
            return

        if self.repair_after_failure and self.calls == 4:
            yield StreamChunk(
                is_done=True,
                native_tool_calls=[
                    {
                        "id": "r52-command-retest",
                        "name": "run_command",
                        "arguments": {
                            "command_line": f"{shlex.quote(sys.executable)} -B verify.py"
                        },
                    }
                ],
            )
            return

        yield StreamChunk(content="Repair and verification evidence complete", is_done=True)


def _write_verifier(
    root: Path,
    *,
    verbose_output: str = _VERBOSE_OUTPUT,
    instruction_update: str | None = None,
) -> None:
    instruction_write = (
        f"Path('AGENTS.md').write_text({instruction_update!r}, encoding='utf-8')\n"
        if instruction_update is not None
        else ""
    )
    verifier = (
        "from pathlib import Path\n"
        "import sys\n"
        "counter = Path('check-count.txt')\n"
        "count = int(counter.read_text() if counter.exists() else '0') + 1\n"
        "counter.write_text(str(count))\n"
        f"{instruction_write}"
        "if '--verbose' in sys.argv:\n"
        f"    sys.stdout.write({verbose_output!r})\n"
        "namespace = {}\n"
        "exec(Path('target.py').read_text(encoding='utf-8'), namespace)\n"
        "if namespace.get('value') != 42:\n"
        "    print('target.py:1: error: expected value 42', file=sys.stderr)\n"
        "    raise SystemExit(1)\n"
        "print('VERIFICATION_OK')\n"
    )
    (root / "verify.py").write_text(verifier, encoding="utf-8")


def _write_timeout_verifier(root: Path) -> None:
    verifier = (
        "from pathlib import Path\n"
        "import sys\n"
        "import time\n"
        "counter = Path('check-count.txt')\n"
        "count = int(counter.read_text() if counter.exists() else '0') + 1\n"
        "counter.write_text(str(count))\n"
        f"sys.stdout.write({_VERBOSE_OUTPUT!r})\n"
        "sys.stdout.flush()\n"
        "time.sleep(3)\n"
    )
    (root / "timeout_verify.py").write_text(verifier, encoding="utf-8")


class _UnlistedMiddleware(ToolMiddleware):
    """Test extension whose result semantics are not eligible for projection."""


class _ChangingMemoryPipeline:
    def __init__(self) -> None:
        self.searches = 0

    async def search(
        self, query_text: str, *, top_k: int = 5
    ) -> tuple[list[MemoryHit], str]:
        del query_text
        self.searches += 1
        if self.searches == 1:
            return [], "lexical"
        return (
            [
                MemoryHit(
                    chunk_key=f"r52-memory-{index}",
                    file_path=f"notes/{index}.md",
                    content="memory evidence " * 32,
                    score=1.0,
                    metadata={},
                )
                for index in range(min(3, top_k))
            ],
            "lexical",
        )

    async def aclose(self) -> None:
        return None


def _make_loop(
    tmp_path: Path,
    *,
    repair_after_failure: bool,
    verbose: bool,
    replay_words: int = 10_000,
    byte_sensitive_counting: bool = False,
    timeout_after_output: bool = False,
    mixed_batch: bool = False,
) -> tuple[AshLoop, SessionStore, _EventUI, _OpaqueCommandProvider]:
    guard = SafetyGuard(project_root=tmp_path)
    store = SessionStore(tmp_path / "sessions.db")
    ui = _EventUI()
    provider = _OpaqueCommandProvider(
        repair_after_failure=repair_after_failure,
        verbose=verbose,
        replay_words=replay_words,
        byte_sensitive_counting=byte_sensitive_counting,
        timeout_after_output=timeout_after_output,
        mixed_batch=mixed_batch,
    )
    loop = AshLoop(
        store,
        provider,
        guard,
        ui,
        tmp_path,
        tools={
            "write_file": WriteFileTool(guard),
            "run_command": RunCommandTool(guard, project_root=tmp_path),
        },
        tool_middlewares=[SecretRedactionMiddleware()],
        safety_tier="auto_approve",
        system_prompt="Synthetic command context test.",
        config=AshConfig(
            model="custom/r52-command-context",
            workspace_root=tmp_path,
            db_directory=tmp_path / "db",
            memory_backend="off",
            max_context_tokens=16_000,
            max_completion_tokens=2_000,
        ),
    )
    return loop, store, ui, provider


def test_canonical_size_fallback_keeps_provider_preview_outcome_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(loop_module, "MAX_CANONICAL_CONTENT_BYTES", 1_024)
    rendered = loop_module._render_tool_response(
        "call-unknown",
        "run_command",
        {
            "success": False,
            "output": "partial diagnostic context " * 100,
            "error": "Tool outcome is ambiguous; Ash did not retry the operation.",
            "truncated": False,
            "token_count": 10,
            "outcome": "unknown",
            "provider_preview_truncated": True,
            "provider_preview_note": "provider-history preview omitted output bytes.",
            "provider_preview_omitted": {"output_utf8_bytes": 2_500},
        },
    )

    payload = json.loads(ET.fromstring(rendered).text or "")
    assert payload["outcome"] == "unknown"
    assert payload["success"] is False
    assert "exit_code" not in payload
    assert payload["provider_preview_truncated"] is True
    assert payload["provider_preview_note"] == (
        "provider-history preview omitted output bytes."
    )
    assert payload["provider_preview_omitted"]["output_utf8_bytes"] == 2_500


def test_full_result_renderer_ignores_non_preview_execution_outcome() -> None:
    common = {
        "success": False,
        "output": "",
        "error": "check failed",
        "truncated": False,
        "token_count": 1,
    }

    rendered = loop_module._render_tool_response(
        "full-result",
        "run_command",
        {**common, "outcome": "completed"},
    )

    assert rendered == loop_module._render_tool_response(
        "full-result",
        "run_command",
        common,
    )


def test_command_preview_excerpts_mark_gap_and_count_omitted_utf8_source() -> None:
    omitted_lines = "omitted π line\n" * 5
    line_source = "HEAD_TOKEN α\n" + omitted_lines + "TAIL_TOKEN 雪\n"

    line_excerpt = loop_module._head_tail_lines(line_source, 2)

    assert "HEAD_TOKEN α\n" in line_excerpt.text
    assert "TAIL_TOKEN 雪\n" in line_excerpt.text
    assert "omitted π line" not in line_excerpt.text
    assert "[..." in line_excerpt.text
    assert line_excerpt.omitted_utf8_bytes == len(omitted_lines.encode("utf-8"))
    assert line_excerpt.omitted_lines == 5

    char_source = "false-diagnostic-prefix " + ("middle π token " * 40) + "suffix-PASS"
    char_excerpt = loop_module._head_tail_characters(char_source, 20)

    assert char_excerpt.text.startswith(char_source[:10] + "\n[...")
    assert char_excerpt.text.endswith(char_source[-10:])
    assert "middle π token" not in char_excerpt.text
    assert "[..." in char_excerpt.text
    assert char_excerpt.omitted_utf8_bytes == len(
        char_source[10:-10].encode("utf-8")
    )
    assert char_excerpt.omitted_lines == 0
    empty_excerpt = loop_module._head_tail_lines(line_source, 0)
    assert empty_excerpt.text == ""
    assert empty_excerpt.selected_characters == 0
    assert empty_excerpt.omitted_utf8_bytes == len(line_source.encode("utf-8"))
    assert empty_excerpt.omitted_lines == len(line_source.splitlines())


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("repair_after_failure", "verbose"),
    [(False, False), (False, True), (True, True)],
)
async def test_command_result_keeps_repair_workflow_inside_opaque_context(
    tmp_path: Path,
    repair_after_failure: bool,
    verbose: bool,
) -> None:
    _write_verifier(tmp_path)
    loop, store, ui, provider = _make_loop(
        tmp_path,
        repair_after_failure=repair_after_failure,
        verbose=verbose,
    )
    try:
        session = await loop.start_session()
        response = await loop.run_turn("Repair target.py and verify it locally")

        assert response == "Repair and verification evidence complete"
        assert (tmp_path / "target.py").read_bytes() == b"value = 42\n"
        expected_commands = 2 if repair_after_failure else 1
        assert (tmp_path / "check-count.txt").read_text(encoding="utf-8") == str(
            expected_commands
        )
        assert provider.calls == (5 if repair_after_failure else 3)
        assert len(provider.requests) == provider.calls
        assert provider.repair_saw_failure_evidence is repair_after_failure

        saved = store.load_session(session.session_id)
        command_records = [
            record for record in saved.tool_calls if record.tool_name == "run_command"
        ]
        assert len(command_records) == expected_commands
        assert all(record.approved and record.dispatched and record.executed for record in command_records)
        assert [record.exit_code for record in command_records] == (
            [1, 0] if repair_after_failure else [0]
        )
        assert [record.success for record in command_records] == (
            [False, True] if repair_after_failure else [True]
        )
        assert store.verify_audit_log(session.session_id) == []

        first_command = command_records[0]
        expected_output = (
            _VERBOSE_OUTPUT
            if repair_after_failure
            else _VERBOSE_OUTPUT + "VERIFICATION_OK\n"
            if verbose
            else "VERIFICATION_OK\n"
        )
        assert first_command.result == expected_output
        if repair_after_failure:
            assert first_command.error == _FAILURE_DIAGNOSTIC + "\n"
        else:
            assert first_command.error is None

        command_messages = [
            message for message in saved.messages
            if message.role == "tool" and message.metadata.get("call_id") in {
                "r52-command-initial", "r52-command-retest"
            }
        ]
        assert len(command_messages) == expected_commands
        first_message = command_messages[0].content
        if verbose:
            assert "provider-history preview" in first_message
            assert "VERIFICATION" in first_message or _FAILURE_DIAGNOSTIC in first_message
            assert len(first_message.encode("utf-8")) < len(expected_output.encode("utf-8"))
        else:
            assert "provider-history preview" not in first_message
            assert "VERIFICATION_OK" in first_message

        initial_terminal = [
            event for event in ui.events
            if event.get("type") == "tool.completed"
            and event.get("tool") == "run_command"
        ]
        assert len(initial_terminal) == expected_commands
        assert [event.get("exit_code") for event in initial_terminal] == (
            [1, 0] if repair_after_failure else [0]
        )
        assert [event.get("success") for event in initial_terminal] == (
            [False, True] if repair_after_failure else [True]
        )

        request_usage = [
            event for event in ui.events
            if event.get("type") == "context.usage"
            and event.get("assembly_purpose") == "request"
        ]
        assert len(request_usage) == provider.calls
        assert all(
            isinstance(event.get("current"), int)
            and isinstance(event.get("maximum"), int)
            and event["current"] <= event["maximum"]
            for event in request_usage
        )
        opaque_requests = [
            message
            for request in provider.requests
            for message in request
            if message.get("role") == "assistant" and message.get("provider_state")
        ]
        assert opaque_requests
        assert opaque_requests[-1]["provider_state"][0]["encrypted_content"] == (
            "opaque " * provider.replay_words
        )
    finally:
        await loop.aclose()


@pytest.mark.asyncio
async def test_success_preview_keeps_verification_when_followup_reserve_is_tight(
    tmp_path: Path,
) -> None:
    _write_verifier(tmp_path)
    loop, store, _, provider = _make_loop(
        tmp_path, repair_after_failure=False, verbose=True, replay_words=13_000,
    )
    try:
        session = await loop.start_session()
        assert await loop.run_turn("Edit target.py and verify it locally") == (
            "Repair and verification evidence complete"
        )
        assert provider.calls == 3
        saved = store.load_session(session.session_id)
        command = next(c for c in saved.tool_calls if c.tool_name == "run_command")
        assert command.success is True and command.exit_code == 0
        assert command.result == _VERBOSE_OUTPUT + "VERIFICATION_OK\n"
        message = next(
            m for m in saved.messages
            if m.role == "tool" and m.metadata.get("call_id") == command.call_id
        )
        payload = json.loads(ET.fromstring(message.content).text or "{}")
        assert payload["provider_preview_truncated"] is True
        assert "VERIFICATION_OK" in payload["output"]
        assert (tmp_path / "check-count.txt").read_text() == "1"
        assert not store.verify_audit_log(session.session_id)
    finally:
        await loop.aclose()


@pytest.mark.asyncio
async def test_command_result_that_cannot_fit_still_fails_closed_with_full_evidence(
    tmp_path: Path,
) -> None:
    _write_verifier(tmp_path)
    loop, store, ui, provider = _make_loop(
        tmp_path,
        repair_after_failure=False,
        verbose=True,
        replay_words=13_950,
    )
    try:
        session = await loop.start_session()
        with pytest.raises(ContextBudgetExceededError):
            await loop.run_turn("Edit target.py and verify it locally")

        assert provider.calls == 2
        assert (tmp_path / "check-count.txt").read_text(encoding="utf-8") == "1"
        saved = store.load_session(session.session_id)
        command = next(record for record in saved.tool_calls if record.tool_name == "run_command")
        assert command.approved and command.dispatched and command.executed
        assert command.success is True and command.exit_code == 0
        assert command.result == _VERBOSE_OUTPUT + "VERIFICATION_OK\n"
        preview_message = next(
            message
            for message in saved.messages
            if message.role == "tool"
            and message.metadata.get("call_id") == "r52-command-initial"
        )
        assert "provider-history preview" in preview_message.content
        assert '"success": true' in preview_message.content
        assert '"exit_code": 0' in preview_message.content
        rejection = next(
            event for event in ui.events if event.get("type") == "context.request.rejected"
        )
        assert rejection["cause"] == "input_limit"
        assert rejection["dispatch_status"] == "not_dispatched"
        assert rejection["opaque_provider_replay_state_preserved"] is True
    finally:
        await loop.aclose()


@pytest.mark.asyncio
async def test_preview_failure_keeps_completed_command_result_atomic(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_verifier(tmp_path)
    loop, store, ui, provider = _make_loop(
        tmp_path,
        repair_after_failure=False,
        verbose=True,
    )

    def fail_preview(*args: object, **kwargs: object) -> None:
        del args, kwargs
        raise RuntimeError("synthetic preview failure")

    monkeypatch.setattr(loop, "_command_result_provider_preview", fail_preview)
    try:
        session = await loop.start_session()
        with pytest.raises(ContextBudgetExceededError):
            await loop.run_turn("Edit target.py and verify it locally")

        assert provider.calls == 2
        assert (tmp_path / "check-count.txt").read_text(encoding="utf-8") == "1"
        saved = store.load_session(session.session_id)
        command = next(record for record in saved.tool_calls if record.tool_name == "run_command")
        assert command.approved and command.dispatched and command.executed
        assert command.success is True and command.exit_code == 0
        assert command.result == _VERBOSE_OUTPUT + "VERIFICATION_OK\n"
        assert command.error is None
        command_message = next(
            message
            for message in saved.messages
            if message.role == "tool"
            and message.metadata.get("call_id") == "r52-command-initial"
        )
        payload = json.loads(ET.fromstring(command_message.content).text or "")
        assert payload["output"] == _VERBOSE_OUTPUT + "VERIFICATION_OK\n"
        assert "provider_preview_truncated" not in payload
        completed = [
            event for event in ui.events
            if event.get("type") == "tool.completed"
            and event.get("tool") == "run_command"
        ]
        assert len(completed) == 1
        assert completed[0].get("output") == _VERBOSE_OUTPUT + "VERIFICATION_OK\n"
        assert store.verify_audit_log(session.session_id) == []
    finally:
        await loop.aclose()


@pytest.mark.asyncio
async def test_command_preview_defers_instruction_refresh_until_after_persistence(
    tmp_path: Path,
) -> None:
    instruction_text = "evidence. " * 3_000
    _write_verifier(tmp_path, instruction_update=instruction_text)
    loop, store, ui, provider = _make_loop(
        tmp_path,
        repair_after_failure=True,
        verbose=True,
    )
    guard = loop.safety_guard
    instruction_loads: list[tuple[str, ...]] = []
    terminal_calls_seen_during_refresh: list[list[str]] = []

    def load_runtime_instructions(current_directories: tuple[Path, ...]) -> str:
        instruction_loads.append(tuple(path.as_posix() for path in current_directories))
        if loop.current_session is not None:
            persisted = loop.session_store.load_session(
                loop.current_session.session_id
            )
            terminal_calls_seen_during_refresh.append(
                [
                    record.call_id
                    for record in persisted.tool_calls
                    if record.tool_name == "run_command" and record.executed
                ]
            )
        diagnostics: list[InstructionDiagnostic] = []
        files = discover_instructions_for_directories(
            tmp_path,
            include_project=True,
            current_directories=current_directories,
            diagnostics=diagnostics,
            project_guard=guard,
        )
        return render_instructions(files, diagnostics)

    loop._additional_instructions_loader = load_runtime_instructions
    loop._instruction_scope_directories = (tmp_path,)
    loop._base_instruction_scope_directories = (tmp_path,)
    try:
        session = await loop.start_session()
        with pytest.raises(ContextBudgetExceededError):
            await loop.run_turn("Repair target.py and verify it locally")

        assert (tmp_path / "AGENTS.md").read_text(encoding="utf-8") == instruction_text
        assert (tmp_path / "target.py").read_bytes() == b"value = 41\n"
        assert (tmp_path / "check-count.txt").read_text(encoding="utf-8") == "1"
        assert provider.calls == 2
        assert len(instruction_loads) == 3
        assert terminal_calls_seen_during_refresh[-1] == ["r52-command-initial"]
        assert not provider.repair_saw_failure_evidence

        saved = store.load_session(session.session_id)
        failed = next(
            record
            for record in saved.tool_calls
            if record.tool_name == "run_command" and record.exit_code == 1
        )
        assert failed.approved and failed.dispatched and failed.executed
        assert failed.success is False
        assert failed.result == _VERBOSE_OUTPUT
        assert failed.error == _FAILURE_DIAGNOSTIC + "\n"
        assert failed.exit_code == 1
        first_message = next(
            message
            for message in saved.messages
            if message.role == "tool"
            and message.metadata.get("call_id") == "r52-command-initial"
        )
        payload = json.loads(ET.fromstring(first_message.content).text or "")
        assert payload["provider_preview_truncated"] is True
        assert payload["exit_code"] == 1
        assert _FAILURE_DIAGNOSTIC in payload["error"]
        assert payload["output"] != _VERBOSE_OUTPUT
        assert len(payload["output"]) < len(_VERBOSE_OUTPUT)
        request_usage = [
            event
            for event in ui.events
            if event.get("type") == "context.usage"
            and event.get("assembly_purpose") == "request"
        ]
        assert len(request_usage) == 2
        assert store.verify_audit_log(session.session_id) == []
    finally:
        await loop.aclose()


@pytest.mark.asyncio
async def test_command_preview_rebuilds_exact_transient_tool_result_images(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    loop, _store, _ui, provider = _make_loop(
        tmp_path,
        repair_after_failure=False,
        verbose=True,
    )
    provider._ash_declared_capabilities = ProviderCapabilities(
        native_tools=True,
        context_window=16_000,
        max_output_tokens=2_000,
        vision=True,
    )
    images = loop_module._TurnToolResultImages(enabled=True)
    images.capture(
        "prior-image-result",
        [{"type": "image", "media_type": "image/png", "data": "YWJj"}],
    )
    try:
        session = await loop.start_session()
        user_message = Message(
            role="user",
            content="Inspect the image and verify the edited target.",
            timestamp=loop_module._utc_now(),
        )
        session.messages.extend(
            [
                user_message,
                Message(
                    role="assistant",
                    content="",
                    timestamp=loop_module._utc_now(),
                    metadata={
                        "provider_state": [
                            {
                                "type": "reasoning",
                                "id": "r52-image-opaque",
                                "summary": [],
                                "status": "completed",
                                "encrypted_content": "opaque " * 10_000,
                            }
                        ],
                        "tool_calls": [],
                    },
                ),
                Message(
                    role="tool",
                    content="image observed",
                    timestamp=loop_module._utc_now(),
                    metadata={"call_id": "prior-image-result"},
                ),
            ]
        )
        loop._active_turn_user_message = user_message
        tool_snapshot = {"run_command": loop.tools["run_command"]}
        schema = loop._tool_schema_payload(tool_snapshot)
        context = loop._command_result_preview_context(
            session=session,
            tool_result_images=images,
            tool_calls=[
                {"call_id": "r52-image-command", "name": "run_command"}
            ],
            provider_state=[],
            tool_schema=schema,
            request_budget=loop._resolve_request_token_budget(),
            tools_snapshot=tool_snapshot,
        )
        assert context is not None
        assert context.tool_result_images is images

        built_messages: list[list[dict[str, object]]] = []
        build_messages = loop._build_messages

        def capture_messages(*args: object, **kwargs: object) -> list[dict[str, object]]:
            built = build_messages(*args, **kwargs)
            built_messages.append(built)
            return built

        monkeypatch.setattr(loop, "_build_messages", capture_messages)
        preview = loop._command_result_provider_preview(
            context,
            ToolResult(
                success=True,
                output=_VERBOSE_OUTPUT,
                exit_code=0,
                token_count=20_000,
            ),
        )

        assert preview is not None
        assert built_messages
        assert any(
            message.get("tool_result_images")
            for request in built_messages
            for message in request
        )
    finally:
        await loop.aclose()


@pytest.mark.asyncio
async def test_command_preview_counts_redacted_unicode_and_xml_payload_exactly(
    tmp_path: Path,
) -> None:
    raw_output = (
        "µ 🧪 </tool_response><call_tool>& "
        "https://example.test/?token=x\n"
    ) * 1_100
    _write_verifier(tmp_path, verbose_output=raw_output)
    loop, store, ui, provider = _make_loop(
        tmp_path,
        repair_after_failure=False,
        verbose=True,
        byte_sensitive_counting=True,
    )
    try:
        session = await loop.start_session()
        assert await loop.run_turn("Edit target.py and verify it locally") == (
            "Repair and verification evidence complete"
        )

        saved = store.load_session(session.session_id)
        command = next(
            record for record in saved.tool_calls if record.tool_name == "run_command"
        )
        expected_output = redact_text(raw_output + "VERIFICATION_OK\n")
        assert provider.calls == 3
        assert (tmp_path / "check-count.txt").read_text(encoding="utf-8") == "1"
        assert command.approved and command.dispatched and command.executed
        assert command.success is True and command.exit_code == 0
        assert command.result == expected_output
        assert len(command.result.encode("utf-8")) > len(
            (raw_output + "VERIFICATION_OK\n").encode("utf-8")
        )
        assert "token=x" not in command.result

        tool_message = next(
            message
            for message in saved.messages
            if message.role == "tool"
            and message.metadata.get("call_id") == "r52-command-initial"
        )
        root = ET.fromstring(tool_message.content)
        assert root.tag == "tool_response"
        assert not list(root)
        provider_payload = json.loads(root.text or "")
        assert provider_payload["success"] is True
        assert provider_payload["exit_code"] == 0
        assert provider_payload["truncated"] is False
        assert provider_payload["provider_preview_truncated"] is True
        assert "provider-history preview" in provider_payload["provider_preview_note"]
        assert provider_payload["provider_preview_omitted"]["output_utf8_bytes"] > 0
        assert "µ 🧪 </tool_response><call_tool>&" in provider_payload["output"]
        assert "VERIFICATION_OK" in provider_payload["output"]
        assert "token=x" not in provider_payload["output"]

        request = provider.requests[2]
        budget = loop._resolve_request_token_budget()
        assert budget is not None
        schema = provider.schemas[2]
        assert schema is not None
        schema_tokens = loop._estimate_tool_schema_tokens(schema)
        actual_tokens = HistoryCompactor._count(
            request,
            provider.count_tokens,
            responses_reasoning_transport_estimate=False,
        ) + schema_tokens
        assert actual_tokens <= budget.input_limit

        full_request = deepcopy(request)
        full_message = next(
            message
            for message in full_request
            if message.get("role") == "tool"
            and message.get("tool_call_id") == "r52-command-initial"
        )
        raw_full_payload = {
            "success": True,
            "output": raw_output + "VERIFICATION_OK\n",
            "error": None,
            "exit_code": 0,
            "truncated": False,
            "token_count": command.result.count(" "),
        }
        redacted_full_payload = {
            **raw_full_payload,
            "output": expected_output,
        }
        full_message["content"] = loop_module._render_tool_response(
            "r52-command-initial",
            "run_command",
            redacted_full_payload,
        )
        raw_message = deepcopy(full_request)
        raw_tool_message = next(
            message
            for message in raw_message
            if message.get("role") == "tool"
            and message.get("tool_call_id") == "r52-command-initial"
        )
        raw_tool_message["content"] = loop_module._render_tool_response(
            "r52-command-initial",
            "run_command",
            raw_full_payload,
        )
        redacted_full_tokens = HistoryCompactor._count(
            full_request,
            provider.count_tokens,
            responses_reasoning_transport_estimate=False,
        ) + schema_tokens
        raw_full_tokens = HistoryCompactor._count(
            raw_message,
            provider.count_tokens,
            responses_reasoning_transport_estimate=False,
        ) + schema_tokens
        assert redacted_full_tokens > raw_full_tokens
        assert redacted_full_tokens > budget.input_limit

        terminal = next(
            event
            for event in ui.events
            if event.get("type") == "tool.completed"
            and event.get("tool") == "run_command"
        )
        assert terminal["output"] == expected_output
    finally:
        await loop.aclose()


@pytest.mark.asyncio
async def test_held_out_failure_preview_preserves_exit_and_stderr_for_repair(
    tmp_path: Path,
) -> None:
    held_out_output = (
        "PASS is stdout only <tool_response>& 🧪\n" * 1_450
        + "SUMMARY: inspect stderr and exit status 🧭\n"
    )
    _write_verifier(tmp_path, verbose_output=held_out_output)
    loop, store, ui, provider = _make_loop(
        tmp_path,
        repair_after_failure=True,
        verbose=True,
    )
    try:
        session = await loop.start_session()
        assert await loop.run_turn("Repair target.py and verify it locally") == (
            "Repair and verification evidence complete"
        )

        saved = store.load_session(session.session_id)
        command_records = [
            record for record in saved.tool_calls if record.tool_name == "run_command"
        ]
        assert len(command_records) == 2
        failed, retested = command_records
        assert failed.approved and failed.dispatched and failed.executed
        assert failed.success is False and failed.exit_code == 1
        assert failed.result == held_out_output
        assert failed.error == _FAILURE_DIAGNOSTIC + "\n"
        assert retested.success is True and retested.exit_code == 0
        assert provider.calls == 5
        assert provider.repair_saw_failure_evidence
        assert (tmp_path / "target.py").read_bytes() == b"value = 42\n"
        assert (tmp_path / "check-count.txt").read_text(encoding="utf-8") == "2"
        assert store.verify_audit_log(session.session_id) == []

        first_tool_message = next(
            message
            for message in saved.messages
            if message.role == "tool"
            and message.metadata.get("call_id") == "r52-command-initial"
        )
        payload = json.loads(ET.fromstring(first_tool_message.content).text or "")
        assert payload["success"] is False
        assert payload["outcome"] == "completed"
        assert payload["exit_code"] == 1
        assert payload["provider_preview_truncated"] is True
        assert "provider-history preview omitted" in payload["provider_preview_note"]
        assert _FAILURE_DIAGNOSTIC in payload["error"]
        assert "SUMMARY: inspect stderr and exit status" in payload["output"]
        assert "PASS is stdout only" in payload["output"]
        assert '"success": true' not in first_tool_message.content

        assert provider.requests[2]
        request_budget = loop._resolve_request_token_budget()
        assert request_budget is not None
        schema = provider.schemas[2]
        assert schema is not None
        request_tokens = HistoryCompactor._count(
            provider.requests[2],
            provider.count_tokens,
            responses_reasoning_transport_estimate=False,
        ) + loop._estimate_tool_schema_tokens(schema)
        assert request_tokens <= request_budget.input_limit

        command_events = [
            event
            for event in ui.events
            if event.get("type") == "tool.completed"
            and event.get("tool") == "run_command"
        ]
        assert [event.get("exit_code") for event in command_events] == [1, 0]
        assert [event.get("success") for event in command_events] == [False, True]
        assert command_events[0].get("output") == held_out_output
    finally:
        await loop.aclose()


@pytest.mark.asyncio
async def test_unknown_command_preview_never_invents_completion_or_retries(
    tmp_path: Path,
) -> None:
    _write_timeout_verifier(tmp_path)
    loop, store, ui, provider = _make_loop(
        tmp_path,
        repair_after_failure=False,
        verbose=True,
        timeout_after_output=True,
        replay_words=13_000,
    )
    try:
        session = await loop.start_session()
        assert await loop.run_turn("Edit target.py and check the outcome") == (
            "Ambiguous command outcome preserved"
        )

        saved = store.load_session(session.session_id)
        command = next(record for record in saved.tool_calls if record.tool_name == "run_command")
        assert command.approved and command.dispatched and command.executed
        assert command.success is None and command.exit_code is None
        assert command.result and "ordinary verification output" in command.result
        assert (tmp_path / "check-count.txt").read_text(encoding="utf-8") == "1"
        assert provider.calls == 3
        assert provider.unknown_result_was_truthful, (
            provider.unknown_result_payload.get("success"),
            provider.unknown_result_payload.get("outcome"),
            "exit_code" in provider.unknown_result_payload,
            provider.unknown_result_payload.get("error"),
            provider.unknown_result_payload.get("provider_preview_truncated"),
        )
        assert store.verify_audit_log(session.session_id) == []

        tool_message = next(
            message
            for message in saved.messages
            if message.role == "tool"
            and message.metadata.get("call_id") == "r52-command-initial"
        )
        payload = json.loads(ET.fromstring(tool_message.content).text or "")
        assert payload["outcome"] == "unknown"
        assert payload["success"] is False
        assert "exit_code" not in payload
        assert payload["provider_preview_truncated"] is True
        assert "Tool outcome is ambiguous" in payload["error"]

        unknown_events = [
            event
            for event in ui.events
            if event.get("type") == "tool.error"
            and event.get("tool") == "run_command"
        ]
        assert len(unknown_events) == 1
        assert unknown_events[0].get("ambiguous") is True
        assert "exit_code" not in unknown_events[0]
    finally:
        await loop.aclose()


@pytest.mark.asyncio
async def test_denied_command_keeps_approval_boundary_and_never_dispatches(
    tmp_path: Path,
) -> None:
    _write_verifier(tmp_path)
    loop, store, ui, provider = _make_loop(
        tmp_path,
        repair_after_failure=False,
        verbose=True,
    )

    async def approve_edits_and_deny_commands(
        tool_name: str,
        arguments: dict[str, object],
    ) -> bool:
        del arguments
        return tool_name != "run_command"

    loop.on_tool_approval = approve_edits_and_deny_commands
    try:
        session = await loop.start_session()
        assert await loop.run_turn("Edit target.py and verify it locally") == (
            "Repair and verification evidence complete"
        )

        saved = store.load_session(session.session_id)
        command = next(record for record in saved.tool_calls if record.tool_name == "run_command")
        assert not command.approved and not command.dispatched and not command.executed
        assert command.result is None and command.exit_code is None
        assert command.error == "Denied by user"
        assert not (tmp_path / "check-count.txt").exists()
        assert provider.calls == 3
        command_message = next(
            message
            for message in saved.messages
            if message.role == "tool"
            and message.metadata.get("call_id") == "r52-command-initial"
        )
        payload = json.loads(ET.fromstring(command_message.content).text or "")
        assert payload["success"] is False
        assert "provider_preview_truncated" not in payload
        denied_events = [event for event in ui.events if event.get("type") == "tool.denied"]
        assert len(denied_events) == 1
        assert denied_events[0].get("tool") == "run_command"
        assert store.verify_audit_log(session.session_id) == []
    finally:
        await loop.aclose()


@pytest.mark.asyncio
async def test_mixed_command_and_write_calls_keep_full_results_and_dispatch(
    tmp_path: Path,
) -> None:
    _write_verifier(tmp_path)
    loop, store, _, provider = _make_loop(
        tmp_path,
        repair_after_failure=False,
        verbose=True,
        mixed_batch=True,
    )
    try:
        session = await loop.start_session()
        with pytest.raises(ContextBudgetExceededError):
            await loop.run_turn("Edit and verify the workspace")

        assert provider.calls == 2
        assert (tmp_path / "check-count.txt").read_text(encoding="utf-8") == "1"
        assert (tmp_path / "mixed-batch-side-effect.txt").read_bytes() == (
            b"write occurred once\n"
        )
        saved = store.load_session(session.session_id)
        command = next(record for record in saved.tool_calls if record.tool_name == "run_command")
        mixed_write = next(
            record
            for record in saved.tool_calls
            if record.call_id == "r52-mixed-write"
        )
        assert command.approved and command.dispatched and command.executed
        assert command.success is True and command.exit_code == 0
        assert command.result == _VERBOSE_OUTPUT + "VERIFICATION_OK\n"
        assert mixed_write.approved and mixed_write.dispatched and mixed_write.executed
        assert mixed_write.success is True
        command_message = next(
            message
            for message in saved.messages
            if message.role == "tool"
            and message.metadata.get("call_id") == "r52-command-initial"
        )
        assert "provider-history preview" not in command_message.content
        command_payload = json.loads(ET.fromstring(command_message.content).text or "")
        assert command_payload["output"] == _VERBOSE_OUTPUT + "VERIFICATION_OK\n"
        assert store.verify_audit_log(session.session_id) == []
    finally:
        await loop.aclose()


@pytest.mark.asyncio
async def test_custom_middleware_keeps_command_result_unprojected(
    tmp_path: Path,
) -> None:
    _write_verifier(tmp_path)
    loop, store, _, provider = _make_loop(
        tmp_path,
        repair_after_failure=False,
        verbose=True,
    )
    loop.tool_middlewares.append(_UnlistedMiddleware())
    try:
        session = await loop.start_session()
        with pytest.raises(ContextBudgetExceededError):
            await loop.run_turn("Edit and verify the workspace")

        assert provider.calls == 2
        assert (tmp_path / "check-count.txt").read_text(encoding="utf-8") == "1"
        saved = store.load_session(session.session_id)
        command = next(record for record in saved.tool_calls if record.tool_name == "run_command")
        assert command.approved and command.dispatched and command.executed
        assert command.success is True and command.exit_code == 0
        command_message = next(
            message
            for message in saved.messages
            if message.role == "tool"
            and message.metadata.get("call_id") == "r52-command-initial"
        )
        assert "provider-history preview" not in command_message.content
        command_payload = json.loads(ET.fromstring(command_message.content).text or "")
        assert command_payload["output"] == _VERBOSE_OUTPUT + "VERIFICATION_OK\n"
        assert store.verify_audit_log(session.session_id) == []
    finally:
        await loop.aclose()


@pytest.mark.asyncio
async def test_project_memory_refresh_disables_stale_command_preview(
    tmp_path: Path,
) -> None:
    _write_verifier(tmp_path)
    loop, store, _, provider = _make_loop(
        tmp_path,
        repair_after_failure=False,
        verbose=True,
        replay_words=12_300,
    )
    memory = _ChangingMemoryPipeline()
    loop.enable_project_memory = True
    loop._memory_pipeline = memory
    try:
        session = await loop.start_session()
        with pytest.raises(ContextBudgetExceededError) as rejection:
            await loop.run_turn("Edit target.py and verify it locally")

        assert rejection.value.required_tokens > rejection.value.limit
        assert rejection.value.provider_replay_state_preserved
        assert memory.searches >= 2
        assert provider.calls == 2
        assert (tmp_path / "check-count.txt").read_text(encoding="utf-8") == "1"
        saved = store.load_session(session.session_id)
        command = next(
            record for record in saved.tool_calls if record.tool_name == "run_command"
        )
        assert command.approved and command.dispatched and command.executed
        assert command.success is True and command.exit_code == 0
        assert command.result == _VERBOSE_OUTPUT + "VERIFICATION_OK\n"
        message = next(
            item
            for item in saved.messages
            if item.role == "tool"
            and item.metadata.get("call_id") == "r52-command-initial"
        )
        payload = json.loads(ET.fromstring(message.content).text or "")
        assert payload["output"] == command.result
        assert "provider_preview_truncated" not in payload
        assert store.verify_audit_log(session.session_id) == []
    finally:
        await loop.aclose()
