"""Default-runtime command budgeting with real local SQLite memory."""

from __future__ import annotations

import asyncio
import io
import json
import shlex
import sys
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

import pytest

from ash.config import AshConfig
from ash.context.history import ContextBudgetExceededError, HistoryCompactor
from ash.providers.base import ProviderABC, StreamChunk
from ash.providers.capabilities import ProviderCapabilities
from ash.providers.replay_state import ProviderReplayCredentialIdentity
from ash.runtime import build_runtime
from ash.ui.headless import HeadlessUI


_VERBOSE_OUTPUT = "ordinary verification output\n" * 2_034
_MEMORY_WITNESS = "R52_MEMORY_WITNESS: preserve parser value 42 during verification."
_FAILURE_DIAGNOSTIC = "target.py:1: error: expected value 42"


class _DefaultMemoryCommandProvider(ProviderABC):
    model_name = "r52-default-memory-command"
    _ash_declared_capabilities = ProviderCapabilities(
        native_tools=True,
        context_window=16_000,
        max_output_tokens=2_000,
    )

    def __init__(
        self,
        *,
        scenario: str,
        verbose: bool,
        replay_words: int = 9_000,
    ) -> None:
        self.scenario = scenario
        self.verbose = verbose
        self.replay_words = replay_words
        self.requests: list[list[dict[str, Any]]] = []
        self.request_sizes: list[int] = []
        self.schema_counter = lambda _tools: 0
        self.failure_evidence_seen = False
        self.memory_witness_seen = False
        self.expected_memory_witness = _MEMORY_WITNESS
        self.memory_witness_per_request: list[bool] = []

    def count_tokens(self, text: str) -> int:
        return max(1, len(text.split()))

    def replay_credential_identity(self) -> ProviderReplayCredentialIdentity:
        return ProviderReplayCredentialIdentity.from_secret(
            "r52-default-memory-replay-key",
            scope=("default-memory-command",),
        )

    async def stream_chat(self, messages, temperature=0.0, tools=None):
        del temperature
        request = json.loads(json.dumps(messages))
        self.requests.append(request)
        self.request_sizes.append(
            HistoryCompactor._count(request, self.count_tokens)
            + self.schema_counter(tools)
        )
        has_memory_witness = self.expected_memory_witness in json.dumps(request)
        self.memory_witness_seen |= has_memory_witness
        self.memory_witness_per_request.append(has_memory_witness)
        call_number = len(self.requests)

        if call_number == 1:
            yield StreamChunk(
                is_done=True,
                native_tool_calls=[
                    {
                        "id": "default-memory-edit",
                        "name": "write_file",
                        "arguments": {
                            "file_path": "target.py",
                            "content": "value = "
                            + (
                                "41\n"
                                if self.scenario in {"repair", "mixed-repair", "navigation-repair"}
                                else "42\n"
                            ),
                        },
                    }
                ],
            )
            return

        sandbox_python = "/usr/bin/python3" if sys.platform.startswith("linux") else sys.executable
        command = f"{shlex.quote(sandbox_python)} -B verify.py"
        if self.verbose:
            command += " --verbose"

        if call_number == 2:
            native_tool_calls = []
            if self.scenario.startswith("mixed-"):
                native_tool_calls.append(
                    {
                        "id": "default-memory-mixed-write",
                        "name": "write_file",
                        "arguments": {
                            "file_path": "batch-side-effect.txt",
                            "content": "written once before verification\n",
                        },
                    }
                )
            if self.scenario == "navigation-repair":
                native_tool_calls.append({
                    "id": "default-memory-read",
                    "name": "read_file",
                    "arguments": {"file_path": "target.py"},
                })
            native_tool_calls.append(
                {
                    "id": "default-memory-command",
                    "name": "run_command",
                    "arguments": {"command_line": command},
                }
            )
            yield StreamChunk(
                is_done=True,
                native_tool_calls=native_tool_calls,
                provider_state=[
                    {
                        "type": "reasoning",
                        "id": "default-memory-opaque-state",
                        "summary": [],
                        "status": "completed",
                        "encrypted_content": "opaque " * self.replay_words,
                    }
                ],
            )
            return

        command_payload = _latest_command_payload(request)
        self.failure_evidence_seen |= (
            command_payload.get("exit_code") == 1
            and _FAILURE_DIAGNOSTIC in str(command_payload.get("error", ""))
        )
        if self.scenario in {"repair", "mixed-repair", "navigation-repair"} and call_number == 3:
            assert self.failure_evidence_seen
            yield StreamChunk(
                is_done=True,
                native_tool_calls=[
                    {
                        "id": "default-memory-repair",
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
        if self.scenario in {"repair", "mixed-repair", "navigation-repair"} and call_number == 4:
            yield StreamChunk(
                is_done=True,
                native_tool_calls=[
                    {
                        "id": "default-memory-retest",
                        "name": "run_command",
                        "arguments": {"command_line": command},
                    }
                ],
            )
            return
        expected_calls = 5 if self.scenario in {"repair", "mixed-repair", "navigation-repair"} else 3
        assert call_number == expected_calls
        yield StreamChunk(content="Verified and repaired locally.", is_done=True)


def _latest_command_payload(messages: list[dict[str, Any]]) -> dict[str, Any]:
    for message in reversed(messages):
        if message.get("role") == "tool" and message.get("tool_call_id") in {
            "default-memory-command",
            "default-memory-retest",
        }:
            root = ET.fromstring(str(message["content"]))
            return json.loads(root.text or "{}")
    raise AssertionError("provider request did not contain the command result")


def _write_verifier(workspace: Path, *, wait_for_memory_update: bool = False) -> None:
    wait_for_update = (
        "Path('command-started').write_text('started', encoding='utf-8')\n"
        "import time\n"
        "time.sleep(0.2)\n"
        if wait_for_memory_update
        else ""
    )
    verifier_source = (
        "from pathlib import Path\n"
        "import sys\n"
        "count_file = Path('check-count.txt')\n"
        "count = int(count_file.read_text() or '0') if count_file.exists() else 0\n"
        "count_file.write_text(str(count + 1))\n"
        + wait_for_update
        + "if '--verbose' in sys.argv:\n"
        + f"    sys.stdout.write({_VERBOSE_OUTPUT!r})\n"
        + "if Path('target.py').read_text(encoding='utf-8') != 'value = 42\\n':\n"
        + f"    print({(_FAILURE_DIAGNOSTIC)!r}, file=sys.stderr)\n"
        + "    raise SystemExit(1)\n"
        + "print('CHECK_OK')\n"
    )
    (workspace / "verify.py").write_text(
        verifier_source,
        encoding="utf-8",
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    (
        "scenario",
        "verbose",
        "expected_exits",
        "extra_memory_docs",
        "replay_words",
        "expect_budget_rejection",
    ),
    [
        ("success", False, [0], 0, 9_000, False),
        ("success", True, [0], 0, 9_000, False),
        ("repair", True, [1, 0], 0, 9_000, False),
        ("mixed-success", False, [0], 0, 9_000, False),
        ("mixed-repair", True, [1, 0], 0, 9_000, False),
        ("navigation-repair", True, [1, 0], 0, 9_000, False),
        ("success", True, [0], 160, 9_000, False),
        ("success", True, [0], 0, 14_000, True),
    ],
    ids=(
        "quiet-success",
        "verbose-success",
        "verbose-repair-retest",
        "mixed-quiet-success",
        "mixed-verbose-repair-retest",
        "navigation-map-memory-repair-retest",
        "large-memory-stress",
        "minimum-fit-rejects-honestly",
    ),
)
async def test_default_runtime_sqlite_memory_command_repair(
    tmp_path: Path,
    scenario: str,
    verbose: bool,
    expected_exits: list[int],
    extra_memory_docs: int,
    replay_words: int,
    expect_budget_rejection: bool,
) -> None:
    """The production default lexical memory path keeps repair requests usable."""

    _write_verifier(tmp_path)
    notes = tmp_path / "repair-notes.md"
    notes.write_text(_MEMORY_WITNESS, encoding="utf-8")
    provider = _DefaultMemoryCommandProvider(
        scenario=scenario,
        verbose=verbose,
        replay_words=replay_words,
    )
    config = AshConfig(
        model="custom/r52-default-memory-command",
        workspace_root=tmp_path,
        db_directory=tmp_path / "state",
        repo_map_enabled=scenario == "navigation-repair",
        automation_enabled=False,
        lsp_enabled=False,
        safety_tier="interactive",
    )
    approvals: list[str] = []

    async def approve(tool_name: str, _arguments: dict[str, Any]) -> bool:
        approvals.append(tool_name)
        return tool_name in {"read_file", "write_file", "run_command"}

    runtime = build_runtime(
        config,
        HeadlessUI(output_format="text", stream=io.StringIO()),
        provider=provider,
        workspace_trusted=False,
        approval_callback=approve,
        run_maintenance=False,
    )
    provider.schema_counter = runtime.loop._estimate_tool_schema_tokens
    assert config.memory_backend == "sqlite"
    assert config.embedding_provider == "none"
    assert runtime.loop.enable_project_memory is True
    assert runtime.loop._memory_pipeline is not None
    assert runtime.loop._memory_pipeline.adapter is None
    assert await runtime.loop.index_file_for_memory(notes) == 1
    assert any(
        hit.file_path == "repair-notes.md"
        for hit in await runtime.loop.search_memory("verify target.py value 42")
    )
    for index in range(extra_memory_docs):
        bulk_note = tmp_path / f"bulk-memory-{index:03}.md"
        bulk_note.write_text(
            f"Unrelated indexed record {index}: routine release notes and configuration.\n",
            encoding="utf-8",
        )
        assert await runtime.loop.index_file_for_memory(bulk_note) == 1
    assert len(runtime.loop._memory_pipeline.document_paths()) == extra_memory_docs + 1

    try:
        if scenario == "navigation-repair":
            repo_map = runtime.loop.repo_map
            assert repo_map is not None
            await asyncio.to_thread(lambda: repo_map.files)
        session = await runtime.loop.start_session()
        if expect_budget_rejection:
            with pytest.raises(ContextBudgetExceededError):
                await runtime.loop.run_turn(
                    "Edit target.py to value 42, verify it locally, and use the "
                    "relevant repair note."
                )
            assert len(provider.requests) == 2
        else:
            answer = await runtime.loop.run_turn(
                "Edit target.py to value 42, verify it locally, and use the relevant "
                "repair note."
            )
            assert answer == "Verified and repaired locally."
        assert provider.memory_witness_seen
        assert provider.request_sizes and all(size <= 14_000 for size in provider.request_sizes)
        assert (tmp_path / "target.py").read_text(encoding="utf-8") == "value = 42\n"
        assert (tmp_path / "check-count.txt").read_text(encoding="utf-8") == str(
            len(expected_exits)
        )

        saved = runtime.loop.session_store.load_session(session.session_id)
        command_records = [
            record for record in saved.tool_calls if record.tool_name == "run_command"
        ]
        assert [record.exit_code for record in command_records] == expected_exits
        assert all(record.approved and record.dispatched and record.executed for record in command_records)
        tool_payloads = {
            message.metadata.get("call_id"): json.loads(
                ET.fromstring(message.content).text or "{}"
            )
            for message in saved.messages
            if message.role == "tool"
            and message.metadata.get("call_id")
            in {record.call_id for record in command_records}
        }
        assert len(tool_payloads) == len(command_records)
        if scenario == "navigation-repair":
            assert runtime.loop.repo_map is not None and runtime.loop.repo_map.ready
            read = next(
                record for record in saved.tool_calls
                if record.call_id == "default-memory-read"
            )
            assert read.executed and read.success is True
            assert "value = 41" in (read.result or "")
            assert "## Repository Map" in json.dumps(provider.requests[2])
            batch_ids = [
                message.get("tool_call_id")
                for message in provider.requests[2]
                if message.get("role") == "tool"
                and message.get("tool_call_id")
                in {"default-memory-read", "default-memory-command"}
            ]
            assert batch_ids == ["default-memory-read", "default-memory-command"]
        if scenario.startswith("mixed-"):
            mixed_write = next(
                record
                for record in saved.tool_calls
                if record.call_id == "default-memory-mixed-write"
            )
            assert mixed_write.approved and mixed_write.dispatched and mixed_write.executed
            assert mixed_write.success is True
            assert (tmp_path / "batch-side-effect.txt").read_text(encoding="utf-8") == (
                "written once before verification\n"
            )
            batch_call_ids = [
                call.get("call_id")
                for message in saved.messages
                if message.role == "assistant"
                and message.metadata.get("tool_calls")
                for call in message.metadata["tool_calls"]
                if isinstance(call, dict)
                and call.get("call_id")
                in {"default-memory-mixed-write", "default-memory-command"}
            ]
            assert batch_call_ids == [
                "default-memory-mixed-write",
                "default-memory-command",
            ]
            request_call_ids = [
                message.get("tool_call_id")
                for message in provider.requests[2]
                if message.get("role") == "tool"
                and message.get("tool_call_id")
                in {"default-memory-mixed-write", "default-memory-command"}
            ]
            assert request_call_ids == batch_call_ids
        assert len(runtime.loop.session_store.provider_replay_state_bindings(
            session.session_id
        )) == 1
        assert all(
            message.metadata["provider_state"][0]["encrypted_content"]
            == "opaque " * replay_words
            for message in saved.messages
            if message.role == "assistant" and message.metadata.get("provider_state")
        )
        for request in provider.requests[2:]:
            replay_states = [
                message["provider_state"][0]["encrypted_content"]
                for message in request
                if message.get("role") == "assistant"
                and message.get("provider_state")
            ]
            assert replay_states == ["opaque " * replay_words]
        sandbox = runtime.sandbox_manager
        annotation = (
            f"[sandbox tier={sandbox.tier} backend={sandbox.backend_name}]\n"
            if sandbox is not None
            else ""
        )

        def command_stdout(result: str | None) -> str:
            assert result is not None
            return result.removeprefix(annotation) if annotation else result

        if expect_budget_rejection:
            assert command_stdout(command_records[0].result) == _VERBOSE_OUTPUT + "CHECK_OK\n"
            assert command_records[0].success is True
            assert (tmp_path / "check-count.txt").read_text(encoding="utf-8") == "1"
        else:
            assert len(provider.requests) == (
                5 if scenario in {"repair", "mixed-repair", "navigation-repair"} else 3
            )
        assert all(
            command_stdout(record.result) == (_VERBOSE_OUTPUT + "CHECK_OK\n")
            for record in command_records
            if record.exit_code == 0 and verbose
        )
        assert all(
            command_stdout(record.result) == "CHECK_OK\n"
            for record in command_records
            if record.exit_code == 0 and not verbose
        )
        if scenario in {"repair", "mixed-repair", "navigation-repair"}:
            assert provider.failure_evidence_seen
            assert "CHECK_OK" in tool_payloads[command_records[-1].call_id]["output"]
            assert _FAILURE_DIAGNOSTIC in (command_records[0].error or "")
            assert command_stdout(command_records[0].result) == _VERBOSE_OUTPUT
            assert _FAILURE_DIAGNOSTIC in json.dumps(provider.requests[2])
            failure_payload = tool_payloads[command_records[0].call_id]
            assert failure_payload["success"] is False
            assert failure_payload["exit_code"] == 1
            assert _FAILURE_DIAGNOSTIC in failure_payload["error"]
            assert failure_payload["provider_preview_truncated"] is True
        elif verbose and not expect_budget_rejection:
            result_payload = tool_payloads[command_records[0].call_id]
            assert result_payload["success"] is True
            assert result_payload["exit_code"] == 0
            assert result_payload["provider_preview_truncated"] is True
            assert "CHECK_OK" in result_payload["output"]
            assert result_payload["provider_preview_omitted"]["output_utf8_bytes"] > 0
        elif not expect_budget_rejection:
            assert "provider_preview_truncated" not in tool_payloads[
                command_records[0].call_id
            ]
        assert runtime.loop.session_store.verify_audit_log(session.session_id) == []
        assert "write_file" in approvals and "run_command" in approvals
    finally:
        await runtime.loop.aclose()


@pytest.mark.asyncio
async def test_default_sqlite_memory_refreshes_after_index_change_during_command(
    tmp_path: Path,
) -> None:
    _write_verifier(tmp_path, wait_for_memory_update=True)
    notes = tmp_path / "repair-notes.md"
    notes.write_text(_MEMORY_WITNESS, encoding="utf-8")
    updated_witness = (
        "R52_MEMORY_WITNESS_UPDATED: preserve parser value 42 during verification."
    )
    provider = _DefaultMemoryCommandProvider(scenario="success", verbose=True)
    runtime = build_runtime(
        AshConfig(
            model="custom/r52-default-memory-command",
            workspace_root=tmp_path,
            db_directory=tmp_path / "state",
            repo_map_enabled=False,
            automation_enabled=False,
            lsp_enabled=False,
            safety_tier="interactive",
        ),
        HeadlessUI(output_format="text", stream=io.StringIO()),
        provider=provider,
        workspace_trusted=False,
        approval_callback=_approve_command_tools,
        run_maintenance=False,
    )
    provider.schema_counter = runtime.loop._estimate_tool_schema_tokens
    assert await runtime.loop.index_file_for_memory(notes) == 1
    await runtime.loop.start_session()

    async def update_index_when_subprocess_starts() -> None:
        marker = tmp_path / "command-started"
        for _ in range(200):
            if marker.exists():
                notes.write_text(updated_witness, encoding="utf-8")
                provider.expected_memory_witness = updated_witness
                assert await runtime.loop.index_file_for_memory(notes) == 1
                return
            await asyncio.sleep(0.005)
        raise AssertionError("verification subprocess did not reach its start marker")

    updater = asyncio.create_task(update_index_when_subprocess_starts())
    try:
        answer = await runtime.loop.run_turn(
            "Edit target.py to value 42, verify locally, and use the current repair note."
        )
        await updater
        assert answer == "Verified and repaired locally."
        assert provider.memory_witness_per_request == [True, True, True]
        assert _MEMORY_WITNESS not in json.dumps(provider.requests[-1])
        assert updated_witness in json.dumps(provider.requests[-1])
        assert (tmp_path / "check-count.txt").read_text(encoding="utf-8") == "1"
        assert runtime.loop.session_store.verify_audit_log(
            runtime.loop.current_session.session_id
        ) == []
    finally:
        if not updater.done():
            updater.cancel()
            with pytest.raises(asyncio.CancelledError):
                await updater
        await runtime.loop.aclose()


async def _approve_command_tools(
    tool_name: str,
    _arguments: dict[str, Any],
) -> bool:
    return tool_name in {"write_file", "run_command"}
