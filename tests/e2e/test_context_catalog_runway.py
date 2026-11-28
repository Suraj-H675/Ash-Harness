"""A deferred catalog must not be the sole reason protected coding stops."""

from __future__ import annotations

import io
import shlex
import sys
from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest

from ash.config import AshConfig
from ash.context.history import ContextBudgetExceededError
from ash.core.checkpoints import FileCheckpointMiddleware
from ash.core.loop import AshLoop
from ash.core.secret_middleware import SecretRedactionMiddleware
from ash.core.session import SessionStore
from ash.providers.base import ProviderABC, StreamChunk
from ash.providers.capabilities import ProviderCapabilities
from ash.providers.replay_state import ProviderReplayCredentialIdentity
from ash.safety.guard import SafetyGuard
from ash.tools.base import BaseTool, ToolResult
from ash.tools.command import RunCommandTool
from ash.tools.filesystem import ReplaceFileContentTool
from ash.tools.tool_search import SearchToolsTool
from ash.ui.headless import HeadlessUI


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "case", ["normal", "recover", "too-large", "fresh-search", "omitted-call", "chatgpt-edit"]
)
async def test_native_deferred_catalog_pressure_preserves_coding_and_replay(
    tmp_path: Path, case: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ash.providers.openai_chatgpt import OpenAIChatGPTProvider
    from ash.providers.openai_chatgpt_auth import ChatGPTAuthSession, ChatGPTCredentialStore

    monkeypatch.setenv("ASH_ENABLE_TIKTOKEN_DOWNLOAD", "0")
    source = tmp_path / "value.py"
    source.write_text("VALUE = 1\n")
    marker = tmp_path / "once.marker"
    command = shlex.quote(sys.executable) + " -c " + shlex.quote(
        "from pathlib import Path; assert Path('value.py').read_text() == 'VALUE = 2\\n'; "
        "p=Path('once.marker'); assert not p.exists(); p.write_text('once'); print('OK')"
    )
    guard = SafetyGuard(tmp_path)
    store = SessionStore(tmp_path / "session.db")
    catalog: dict[str, BaseTool] = {}
    search = SearchToolsTool(guard, lambda: catalog, threshold=1)

    class OptionalTool(BaseTool):
        name = "optional_inspection"
        description = "Inspect an optional structured record."

        def json_schema(self) -> dict[str, Any]:
            return {
                "type": "object",
                "properties": {
                    f"field_{i}": {"type": "string", "description": "Structured record field. " * 4}
                    for i in range(70)
                },
            }

        async def run(self, **kwargs: Any) -> ToolResult:
            pytest.fail("The optional schema is never needed for this coding task")

    optional = OptionalTool(guard)
    catalog.update({
        search.name: search, optional.name: optional,
        "replace_file_content": ReplaceFileContentTool(guard),
        "run_command": RunCommandTool(guard, project_root=tmp_path),
    })

    class Scripted(ProviderABC):
        model_name = "catalog-runway"
        _ash_declared_capabilities = ProviderCapabilities(
            native_tools=True, context_window=16_384,
            max_input_tokens=14_336, max_output_tokens=2_048,
        )

        def __init__(self) -> None:
            self.states: list[list[dict[str, Any]]] = []
            self.names: list[set[str]] = []
            self.history: list[dict[str, Any]] = []

        def count_tokens(self, text: str) -> int:
            return max(1, (len(text) + 3) // 4)

        def replay_credential_identity(self) -> ProviderReplayCredentialIdentity:
            return ProviderReplayCredentialIdentity.from_secret(
                "synthetic-catalog-replay", scope=("catalog-runway",),
            )

        async def stream_chat(self, messages, temperature=0.0, tools=None):
            assert [m["provider_state"] for m in messages
                    if m.get("provider_state")] == self.states
            # All prior non-system messages, including tool arguments/results,
            # must survive the smaller declaration set byte-for-byte in content.
            assert messages[1:1 + len(self.history)] == self.history
            self.history = deepcopy(messages[1:])
            names = {item["function"]["name"] for item in tools or []}
            self.names.append(names)
            index = len(self.names)
            assert {"search_tools", "replace_file_content", "run_command"} <= names
            if index == 1:
                assert optional.name not in names
                tool, args = "search_tools", {"query": optional.name, "limit": 1}
            elif index == 2:
                assert case != "fresh-search"
                assert optional.name in names
                tool, args = "replace_file_content", {
                    "file_path": "value.py", "target_content": "VALUE = 1",
                    "replacement_content": "VALUE = 2",
                }
            elif index == 3:
                assert (optional.name in names) is (case != "chatgpt-edit")
                assert source.read_text() == "VALUE = 2\n"
                tool, args = "run_command", {"command_line": command}
            else:
                assert case in ("normal", "recover", "omitted-call", "chatgpt-edit")
                assert (optional.name in names) is (case == "normal")
                assert marker.read_text() == "once"
                result = next(m["content"] for m in messages if m.get("tool_call_id") == "call-3")
                assert '"success": true' in result and '"exit_code": 0' in result
                if case == "normal":
                    assert "OK" in result
                elif "OK" not in result:
                    # The pre-existing preview guard can omit output. It must
                    # retain the successful exit and explicitly report omission.
                    assert '"provider_preview_truncated": true' in result
                if case == "omitted-call" and index == 4:
                    yield StreamChunk(native_tool_calls=[{
                        "id": "stale-call", "name": optional.name, "arguments": {},
                    }], is_done=True)
                    return
                assert index == (5 if case == "omitted-call" else 4)
                yield StreamChunk(content="Verified.", is_done=True)
                return
            size = 800
            if case == "fresh-search" and index == 1:
                size = 42_000
            elif case == "chatgpt-edit" and index == 2:
                size = 37_000
            elif index == 3:
                size = {"normal": 800, "recover": 42_000, "omitted-call": 42_000,
                        "too-large": 65_000, "chatgpt-edit": 800}[case]
            state = [{"type": "reasoning", "id": f"r{index}", "summary": [],
                      "status": "completed", "encrypted_content": "Ab0_" * (size // 4)}]
            self.states.append(state)
            yield StreamChunk(
                native_tool_calls=[{"id": f"call-{index}", "name": tool, "arguments": args}],
                provider_state=state, is_done=True,
            )

    class ChatGPTScripted(Scripted, OpenAIChatGPTProvider):
        # Use the production adapter's actual transport estimate, with only
        # the inference stream and credential identity replaced by fixtures.
        count_tokens = OpenAIChatGPTProvider.count_tokens

        def __init__(self) -> None:
            OpenAIChatGPTProvider.__init__(
                self, "catalog-runway", auth_session=ChatGPTAuthSession(
                    store=ChatGPTCredentialStore(tmp_path / "fixture-auth")
                ),
            )
            Scripted.__init__(self)

    provider = ChatGPTScripted() if case == "chatgpt-edit" else Scripted()
    approvals = []

    async def approve(name, arguments):
        approvals.append(name)
        return name in {"search_tools", "replace_file_content"} or (
            name == "run_command" and arguments == {"command_line": command}
        )

    loop = AshLoop(
        store, provider, guard, HeadlessUI(output_format="text", stream=io.StringIO()),
        tmp_path, tools=catalog, on_tool_approval=approve,
        config=AshConfig(
            model=("openai/catalog-runway" if case == "chatgpt-edit" else "custom/catalog-runway"),
            workspace_root=tmp_path,
            db_directory=tmp_path / "db", memory_backend="off",
            max_context_tokens=16_384, max_completion_tokens=2_048,
        ),
    )

    def checkpoint_context():
        if loop.current_session is None or loop.turn_context is None:
            return None
        return (loop.current_session.session_id, loop.turn_context.turn_id,
                str(loop.turn_context.get("tool_call_id", "")))

    loop.tool_middlewares.extend([
        FileCheckpointMiddleware(store, guard, checkpoint_context),
        SecretRedactionMiddleware(),
    ])
    try:
        session = await loop.start_session()
        task = "Change VALUE to 2, verify exactly once, then report."
        if case in ("too-large", "fresh-search"):
            with pytest.raises(ContextBudgetExceededError):
                await loop.run_turn(task)
        else:
            assert await loop.run_turn(task) == "Verified."
        saved = store.load_session(session.session_id)
        assert [m.metadata["provider_state"] for m in saved.messages
                if m.metadata.get("provider_state")] == provider.states
        assert not store.verify_audit_log(session.session_id)
        assert len(provider.names) == {"normal": 4, "recover": 4, "too-large": 3,
                                       "fresh-search": 1, "omitted-call": 5, "chatgpt-edit": 4}[case]
        assert optional.name not in approvals
        assert approvals.count("run_command") == (0 if case == "fresh-search" else 1)
        assert marker.exists() is (case != "fresh-search")
        assert source.read_text() == ("VALUE = 1\n" if case == "fresh-search" else "VALUE = 2\n")
        assert [c.tool_name for c in saved.tool_calls if c.success] == (
            ["search_tools"] if case == "fresh-search" else
            ["search_tools", "replace_file_content", "run_command"]
        )
        if case != "fresh-search":
            command_record = next(c for c in saved.tool_calls if c.tool_name == "run_command")
            assert command_record.success and command_record.exit_code == 0
            assert command_record.result is not None and "OK" in command_record.result
        if case == "omitted-call":
            refused = next(c for c in saved.tool_calls if c.call_id == "stale-call")
            assert not refused.success and not refused.dispatched and not refused.executed
            assert not refused.approved
        if case in ("recover", "omitted-call", "chatgpt-edit"):
            from ash.core.activity import summarize_operational_events

            runtime_events = store.list_runtime_events(session.session_id)
            events = [r.event for r in runtime_events]
            admitted = [e for e in events if e["type"] == "context.usage"
                        and e.get("assembly_purpose") == "request"]
            reduced = [e for e in admitted if e.get("deferred_catalog_omitted_count")]
            assert len(reduced) == 1
            assert reduced[0]["deferred_catalog_omitted_count"] == 1
            assert reduced[0]["original_tool_schema_tokens"] > reduced[0]["native_tool_schema_tokens"]
            assert reduced[0]["current"] <= reduced[0]["maximum"]
            assert optional.name not in search.activated_names
            metrics = summarize_operational_events(runtime_events)
            projected = [e for e in metrics["timeline"] if e.get("deferred_catalog_omitted_count")]
            assert len(projected) == 1
            assert projected[0]["original_tool_schema_tokens"] == reduced[0]["original_tool_schema_tokens"]
            if case == "chatgpt-edit":
                assert reduced[0] is admitted[2]
                assert reduced[0]["opaque_transport_estimate_enabled"] is True
                assert reduced[0]["estimator_count_mode"] == "offline_text_heuristic"
            else:
                assert reduced[0] is admitted[3]
        else:
            assert optional.name in search.activated_names
    finally:
        await loop.aclose()
