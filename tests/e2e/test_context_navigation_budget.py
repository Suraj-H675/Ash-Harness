"""Exercise source-navigation growth with immutable provider reasoning history."""

from __future__ import annotations

import io
import json
import shlex
import sys
from pathlib import Path
from typing import Any

import pytest

from ash.config import AshConfig
from ash.context.history import ContextBudgetExceededError, HistoryCompactor
from ash.core.loop import AshLoop
from ash.core.checkpoints import FileCheckpointMiddleware
from ash.core.secret_middleware import SecretRedactionMiddleware
from ash.core.session import SessionStore
from ash.providers.base import ProviderABC, StreamChunk
from ash.providers.capabilities import ProviderCapabilities
from ash.providers.replay_state import ProviderReplayCredentialIdentity
from ash.safety.guard import SafetyGuard
from ash.tools.command import RunCommandTool
from ash.tools.filesystem import ReadFileTool, ReadFilesTool, ReplaceFileContentTool
from ash.tools.search import SearchTextTool
from ash.ui.headless import HeadlessUI


@pytest.mark.asyncio
async def test_opaque_history_bounds_repeated_reads_and_keeps_edit_test_progress(
    tmp_path: Path,
) -> None:
    """Repeated navigation must leave room for an approved edit and test."""

    for index in range(24):
        (tmp_path / f"module_{index:02}.py").write_text(
            (f"source_{index} " * 700)[:4_096], encoding="utf-8"
        )
    target = tmp_path / "target.py"
    target.write_text("VALUE = 1\n", encoding="utf-8")

    command = (
        f"{shlex.quote(sys.executable)} -c "
        + shlex.quote(
            "from pathlib import Path; "
            "assert Path('target.py').read_text(encoding='utf-8') == 'VALUE = 2\\n'; "
            "print('TEST_OK')"
        )
    )

    class Provider(ProviderABC):
        model_name = "scripted-navigation-budget"
        _ash_declared_capabilities = ProviderCapabilities(native_tools=True)

        def __init__(self) -> None:
            self.requests: list[list[dict[str, Any]]] = []
            self.states: list[list[dict[str, Any]]] = []
            self.after_refusal_phase = 0
            self.request_input_tokens: list[int] = []
            self.tool_schema_tokens = 0

        def count_tokens(self, text: str) -> int:
            return max(1, (len(text) + 3) // 4)

        def replay_credential_identity(self) -> ProviderReplayCredentialIdentity:
            return ProviderReplayCredentialIdentity.from_secret(
                "fixture-only-replay-key",
                scope=("navigation-budget-test",),
            )

        async def stream_chat(self, messages, temperature=0.0, tools=None):
            request_number = len(self.requests) + 1
            self.requests.append(messages)
            self.request_input_tokens.append(
                HistoryCompactor._count(messages, self.count_tokens)
                + self.tool_schema_tokens
            )
            prior_states = [
                item["provider_state"]
                for item in messages
                if item.get("role") == "assistant" and item.get("provider_state")
            ]
            assert prior_states == self.states

            saw_budget_refusal = (
                "not dispatched because the remaining context budget"
                in json.dumps(messages).lower()
            )
            if saw_budget_refusal:
                self.after_refusal_phase += 1
                if self.after_refusal_phase == 1:
                    calls = [
                        {
                            "id": "edit-after-navigation",
                            "name": "replace_file_content",
                            "arguments": {
                                "file_path": "target.py",
                                "target_content": "VALUE = 1",
                                "replacement_content": "VALUE = 2",
                            },
                        }
                    ]
                elif self.after_refusal_phase == 2:
                    calls = [
                        {
                            "id": "test-after-edit",
                            "name": "run_command",
                            "arguments": {"command_line": command},
                        }
                    ]
                else:
                    assert self.after_refusal_phase == 3
                    assert "TEST_OK" in json.dumps(messages)
                    yield StreamChunk(content="Edited and tested.", is_done=True)
                    return
            elif request_number == 1:
                calls = [
                    {
                        "id": f"parallel-read-{index}",
                        "name": "read_file",
                        "arguments": {"file_path": f"module_{index:02}.py"},
                    }
                    for index in range(4)
                ]
            elif request_number == 2:
                calls = [
                    {
                        "id": f"search-{index}",
                        "name": "search_text",
                        "arguments": {
                            "pattern": f"source_{index}",
                            "directory_path": f"module_{index:02}.py",
                        },
                    }
                    for index in range(4)
                ]
            elif request_number <= 7:
                first = (request_number - 3) * 4 + 4
                calls = [
                    {
                        "id": f"read-{request_number}-batch",
                        "name": "read_files",
                        "arguments": {
                            "files": [
                                {
                                    "file_path": f"module_{index:02}.py",
                                    "start_line": 1,
                                    "max_chars": 4_096,
                                }
                                for index in range(first, first + 4)
                            ]
                        },
                    }
                ]
            else:
                raise AssertionError(
                    "scripted provider expected source-navigation refusal before "
                    f"request {request_number}"
                )

            state = [
                {
                    "type": "reasoning",
                    "id": f"opaque-{request_number}",
                    "summary": [],
                    "status": "completed",
                    "encrypted_content": f"fixture-state-{request_number}",
                }
            ]
            self.states.append(state)
            yield StreamChunk(
                is_done=True,
                native_tool_calls=calls,
                provider_state=state,
            )

    approvals: list[str] = []

    async def approve(tool_name: str, arguments: dict[str, object]) -> bool:
        approvals.append(tool_name)
        return True

    guard = SafetyGuard(tmp_path)
    store = SessionStore(tmp_path / "navigation-budget.db")
    provider = Provider()
    loop = AshLoop(
        store,
        provider,
        guard,
        HeadlessUI(output_format="stream-json", stream=io.StringIO()),
        tmp_path,
        tools={
            "read_file": ReadFileTool(guard),
            "read_files": ReadFilesTool(guard),
            "search_text": SearchTextTool(guard),
            "replace_file_content": ReplaceFileContentTool(guard),
            "run_command": RunCommandTool(guard, project_root=tmp_path),
        },
        config=AshConfig(
            model="custom/scripted-navigation-budget",
            workspace_root=tmp_path,
            db_directory=tmp_path / "db",
            memory_backend="off",
            max_context_tokens=27_000,
            max_completion_tokens=3_000,
        ),
        on_tool_approval=approve,
    )
    # The real CLI always attaches checkpointing and secret redaction. A
    # context budget that silently disables on these built-ins does not protect
    # the ordinary daily-driver path, even if bare AshLoop tests pass.
    def checkpoint_context() -> tuple[str, str, str] | None:
        if loop.current_session is None or loop.turn_context is None:
            return None
        return (
            loop.current_session.session_id,
            loop.turn_context.turn_id,
            str(loop.turn_context.get("tool_call_id", "")),
        )

    loop.tool_middlewares.extend([
        FileCheckpointMiddleware(store, guard, checkpoint_context),
        SecretRedactionMiddleware(),
    ])
    provider.tool_schema_tokens = loop._estimate_tool_schema_tokens(
        loop._tool_schema_payload(loop._provider_tools())
    )
    request_budget = loop._resolve_request_token_budget()
    assert request_budget is not None
    try:
        session = await loop.start_session()
        try:
            response = await loop.run_turn(
                "Inspect the modules, update target.py, and verify it."
            )
        except ContextBudgetExceededError as exc:
            persisted = store.load_session(session.session_id)
            read_results = [
                message.content
                for message in persisted.messages
                if message.role == "tool"
                and any(
                    name in message.content
                    for name in ("read_files", "read_file", "search_text")
                )
            ]
            pytest.fail(
                "opaque navigation overflowed after durable read batches: "
                f"provider_requests={len(provider.requests)}, "
                f"read_results={len(read_results)}, "
                f"read_result_chars={sum(map(len, read_results))}, "
                f"required_tokens={exc.required_tokens}, input_limit={exc.limit}, "
                f"opaque_state_preserved={exc.provider_replay_state_preserved}"
            )

        assert response == "Edited and tested."
        assert len(provider.requests) >= 5
        assert max(provider.request_input_tokens) <= request_budget.input_limit
        assert target.read_text(encoding="utf-8") == "VALUE = 2\n"
        persisted = store.load_session(session.session_id)
        read_results = [
            message.content
            for message in persisted.messages
            if message.role == "tool"
            and any(
                name in message.content
                for name in ("read_files", "read_file", "search_text")
            )
        ]
        assert len(read_results) >= 8
        assert sum(map(len, read_results)) < 89_739
        full_reads = [
            result
            for result in read_results
            if '<tool_response name="read_file"' in result
        ]
        assert len(full_reads) == 4
        assert all('"truncated": false' in result for result in full_reads)
        assert any(
            "context budget" in result.lower() and "truncated" in result.lower()
            for result in read_results
        )
        refused_calls = [
            call
            for call in persisted.tool_calls
            if call.tool_name == "read_files"
            and not call.dispatched
            and not call.approved
            and not call.executed
            and "not dispatched because the remaining context budget"
            in (call.error or "").lower()
        ]
        assert len(refused_calls) == 1, (
            provider.after_refusal_phase,
            len(provider.requests),
            [
                (
                    call.tool_name,
                    call.dispatched,
                    call.approved,
                    call.executed,
                    call.error,
                )
                for call in persisted.tool_calls
                if not call.dispatched
            ],
        )
        assert "sha256=" in "\n".join(read_results)
        assert "TEST_OK" in "\n".join(
            message.content for message in persisted.messages if message.role == "tool"
        )
        assert approvals.count("replace_file_content") == 1
        assert approvals.count("run_command") == 1
    finally:
        await loop.aclose()


@pytest.mark.asyncio
async def test_navigation_budget_keeps_native_tool_schemas_at_4k_and_larger_context(
    tmp_path: Path,
) -> None:
    schema_costs: list[int] = []
    for context_tokens in (4_000, 27_000):
        workspace = tmp_path / f"context-{context_tokens}"
        workspace.mkdir()
        (workspace / "module.py").write_text("VALUE = 1\n", encoding="utf-8")

        class Provider(ProviderABC):
            model_name = "scripted-schema-budget"
            _ash_declared_capabilities = ProviderCapabilities(native_tools=True)

            def count_tokens(self, text: str) -> int:
                return max(1, (len(text) + 3) // 4)

            async def stream_chat(self, messages, temperature=0.0, tools=None):
                raise AssertionError("schema admission must remain model-free")
                yield StreamChunk(is_done=True)

        guard = SafetyGuard(workspace)
        loop = AshLoop(
            SessionStore(workspace / "context.db"),
            Provider(),
            guard,
            HeadlessUI(output_format="stream-json", stream=io.StringIO()),
            workspace,
            tools={
                "read_file": ReadFileTool(guard),
                "read_files": ReadFilesTool(guard),
                "search_text": SearchTextTool(guard),
                "replace_file_content": ReplaceFileContentTool(guard),
                "run_command": RunCommandTool(guard, project_root=workspace),
            },
            config=AshConfig(
                model="custom/scripted-schema-budget",
                workspace_root=workspace,
                db_directory=workspace / "db",
                memory_backend="off",
                max_context_tokens=context_tokens,
                max_completion_tokens=512,
            ),
        )
        try:
            session = await loop.start_session()
            request_budget = loop._resolve_request_token_budget()
            assert request_budget is not None
            provider_tools = loop._provider_tools()
            schema = loop._tool_schema_payload(provider_tools)
            schema_tokens = loop._estimate_tool_schema_tokens(schema)
            messages = loop._build_messages(
                session,
                request_budget=request_budget,
                provider_tools=provider_tools,
                tool_schema_payload=schema,
            )
            navigation_budgets = loop._navigation_output_budgets(
                messages=messages,
                assistant_text="",
                tool_calls=[
                    {
                        "call_id": "schema-budget-read",
                        "name": "read_file",
                        "arguments": {"file_path": "module.py"},
                    }
                ],
                provider_state=[
                    {
                        "type": "reasoning",
                        "id": "schema-budget-state",
                        "summary": [],
                        "status": "completed",
                        "encrypted_content": "fixture-state",
                    }
                ],
                tool_schema=schema,
                request_budget=request_budget,
                tools_snapshot=provider_tools,
            )

            assert set(navigation_budgets) == {"schema-budget-read"}
            assert navigation_budgets["schema-budget-read"].can_dispatch
            assert loop._last_context_tokens <= request_budget.input_limit
            assert schema_tokens == loop.provider.count_tokens(
                loop._tool_schema_content(schema)
            )
            serialized_schema = json.dumps(schema)
            for name in (
                "read_file",
                "read_files",
                "search_text",
                "replace_file_content",
                "run_command",
            ):
                assert name in serialized_schema
            schema_costs.append(schema_tokens)
        finally:
            await loop.aclose()

    assert schema_costs[0] == schema_costs[1]


@pytest.mark.asyncio
async def test_parallel_navigation_budgets_project_their_own_call_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Independent regression: adjacent calls must not share closure identity."""
    import ash.core.loop as loop_module
    from ash.tools.base import ToolResult

    class Provider(ProviderABC):
        model_name = "scripted-budget-identity"
        _ash_declared_capabilities = ProviderCapabilities(native_tools=True)

        def count_tokens(self, text: str) -> int:
            return max(1, len(str(text)) // 4)

        async def stream_chat(self, messages, temperature=0.0, tools=None):
            raise AssertionError("no provider inference")
            yield StreamChunk(is_done=True)

    guard = SafetyGuard(tmp_path)
    provider = Provider()
    tools = {
        "read_file": ReadFileTool(guard),
        "search_text": SearchTextTool(guard),
    }
    loop = AshLoop(
        SessionStore(tmp_path / "identity.db"),
        provider,
        guard,
        HeadlessUI(output_format="stream-json", stream=io.StringIO()),
        tmp_path,
        tools=tools,
        config=AshConfig(
            model="custom/scripted-budget-identity",
            workspace_root=tmp_path,
            db_directory=tmp_path / "db",
            memory_backend="off",
            max_context_tokens=64_000,
            max_completion_tokens=4_000,
        ),
    )
    calls = [
        {"call_id": "first-read", "name": "read_file", "arguments": {"file_path": "f.py"}},
        {"call_id": "second-search", "name": "search_text", "arguments": {"pattern": "f"}},
    ]
    budgets = loop._navigation_output_budgets(
        messages=[
            {"role": "system", "content": "Instructions"},
            {"role": "user", "content": "Inspect these files"},
        ],
        assistant_text="Inspecting",
        tool_calls=calls,
        provider_state=[
            {
                "type": "reasoning",
                "id": "fake",
                "summary": [],
                "status": "completed",
                "encrypted_content": "fictional-opaque-state",
            }
        ],
        tool_schema=[],
        request_budget=loop._resolve_request_token_budget(),
        tools_snapshot=tools,
    )
    assert set(budgets) == {"first-read", "second-search"}

    real_render = loop_module._render_tool_response
    rendered: list[tuple[str, str]] = []

    def capture(call_id: str, tool_name: str, result: dict[str, Any]) -> str:
        rendered.append((call_id, tool_name))
        return real_render(call_id, tool_name, result)

    monkeypatch.setattr(loop_module, "_render_tool_response", capture)
    sample = ToolResult(success=True, output="source", truncated=False)
    budgets["first-read"].accepts(sample)
    assert rendered == [("first-read", "read_file")], rendered
    rendered.clear()
    budgets["second-search"].accepts(sample)
    assert rendered == [("second-search", "search_text")], rendered


@pytest.mark.asyncio
async def test_budget_projection_accounts_for_builtin_secret_redaction_growth(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The CLI's secret redactor can expand short URL query values."""
    import ash.core.loop as loop_module
    from ash.core.redaction import redact_text
    from ash.tools.base import ToolMiddleware, ToolResult

    class Provider(ProviderABC):
        model_name = "redaction-projection-test"
        _ash_declared_capabilities = ProviderCapabilities(native_tools=True)

        def count_tokens(self, value: str) -> int:
            return max(1, len(str(value)) // 4)

        async def stream_chat(self, messages, temperature=0.0, tools=None):
            raise AssertionError("projection needs no model")
            yield StreamChunk(is_done=True)

    guard = SafetyGuard(tmp_path)
    read = ReadFileTool(guard)
    loop = AshLoop(
        SessionStore(tmp_path / "projection.db"),
        Provider(),
        guard,
        HeadlessUI(output_format="stream-json", stream=io.StringIO()),
        tmp_path,
        tools={"read_file": read},
        config=AshConfig(
            model="custom/redaction-projection-test",
            workspace_root=tmp_path,
            db_directory=tmp_path / "db",
            memory_backend="off",
            max_context_tokens=32_768,
            max_completion_tokens=4_096,
        ),
    )
    loop.tool_middlewares.append(SecretRedactionMiddleware())
    tool_calls = [
        {"call_id": "source-1", "name": "read_file", "arguments": {"file_path": "source.py"}}
    ]
    budgets = loop._navigation_output_budgets(
        messages=[
            {"role": "system", "content": "Trusted policy"},
            {"role": "user", "content": "Inspect source"},
        ],
        assistant_text="Inspecting relevant source",
        tool_calls=tool_calls,
        provider_state=[
            {
                "type": "reasoning",
                "id": "state",
                "summary": [],
                "status": "completed",
                "encrypted_content": "fixture-opaque-replay",
            }
        ],
        tool_schema=[],
        request_budget=loop._resolve_request_token_budget(),
        tools_snapshot={"read_file": read},
    )
    assert set(budgets) == {"source-1"}
    assert budgets["source-1"].can_dispatch
    raw = "https://example.com/path?token=x"
    redacted = redact_text(raw)
    assert len(redacted) > len(raw)
    seen: list[str] = []
    original = loop_module._render_tool_response

    def capture(call_id: str, name: str, result: dict[str, Any]) -> str:
        assert call_id == "source-1" and name == "read_file"
        seen.append(str(result["output"]))
        return original(call_id, name, result)

    monkeypatch.setattr(loop_module, "_render_tool_response", capture)
    assert budgets["source-1"].accepts(
        ToolResult(success=True, output=raw, truncated=False)
    )
    assert seen == [redacted]

    class UnknownMiddleware(ToolMiddleware):
        async def before_tool(self, *args: object, **kwargs: object) -> None:
            return None

        async def after_tool(self, *args: object, **kwargs: object) -> None:
            return None

    # A third-party extension is not promised to preserve result size or
    # exactly-once outcomes. Never pretend its output can be pre-projected.
    loop.tool_middlewares.append(UnknownMiddleware())
    assert not loop._navigation_output_budgets(
        messages=[
            {"role": "system", "content": "Trusted policy"},
            {"role": "user", "content": "Inspect source"},
        ],
        assistant_text="Inspecting relevant source",
        tool_calls=tool_calls,
        provider_state=[
            {
                "type": "reasoning",
                "id": "state",
                "summary": [],
                "status": "completed",
                "encrypted_content": "fixture-opaque-replay",
            }
        ],
        tool_schema=[],
        request_budget=loop._resolve_request_token_budget(),
        tools_snapshot={"read_file": read},
    )


@pytest.mark.asyncio
async def test_read_files_budget_fits_long_missing_path_errors(tmp_path: Path) -> None:
    """Early read errors must not escape batch output admission."""
    from ash.tools.base import ToolOutputBudget

    reader = ReadFilesTool(SafetyGuard(tmp_path))
    # A nested, in-scope path that is absent can still have a lengthy
    # user-controlled name; each per-file error repeats the path label.
    requested = [
        {"file_path": f"missing_{i}/" + "/".join(f"segment_{j:02d}_" + "z" * 30 for j in range(12)) + "/file.py"}
        for i in range(4)
    ]
    budget = ToolOutputBudget(
        accepts=lambda result: len(result.output) <= 500 and len(result.error or "") <= 100,
        can_dispatch=True,
    )
    with reader.output_budget_context(budget):
        result = await reader.run(files=requested)
    assert budget.accepts(result), (len(result.output), result.error)
    assert not result.success  # These paths were never found or read.
    assert result.truncated
    assert "context budget" in (result.output + " " + (result.error or "")).lower()


@pytest.mark.asyncio
async def test_mixed_navigation_action_batch_budgets_read_without_double_edit(
    tmp_path: Path,
) -> None:
    """Bound navigation in a mixed response without changing action semantics."""
    from ash.tools.filesystem import WriteFileTool

    class Provider(ProviderABC):
        model_name = "mixed-navigation-fake"
        _ash_declared_capabilities = ProviderCapabilities(native_tools=True)

        def count_tokens(self, text: str) -> int:
            return max(1, len(str(text)) // 4)

        async def stream_chat(self, messages, temperature=0.0, tools=None):
            raise AssertionError("real model calls not required")
            yield StreamChunk(is_done=True)

    (tmp_path / "source.py").write_text("value = 1\n" * 1_000)
    guard = SafetyGuard(tmp_path)
    read = ReadFileTool(guard)
    write = WriteFileTool(guard)
    tools = {"read_file": read, "write_file": write}
    store = SessionStore(tmp_path / "mixed.db")
    approved_calls: list[str] = []

    async def approve(name: str, _: dict[str, Any]) -> bool:
        approved_calls.append(name)
        return True

    loop = AshLoop(
        store,
        Provider(),
        guard,
        HeadlessUI(output_format="stream-json", stream=io.StringIO()),
        tmp_path,
        tools=tools,
        on_tool_approval=approve,
        config=AshConfig(
            model="custom/mixed-navigation-fake",
            workspace_root=tmp_path,
            db_directory=tmp_path / "db",
            memory_backend="off",
            max_context_tokens=16_000,
            max_completion_tokens=2_000,
        ),
    )
    calls = [
        {"call_id": "one-read", "name": "read_file", "arguments": {"file_path": "source.py"}},
        {"call_id": "one-write", "name": "write_file", "arguments": {"file_path": "report.txt", "content": "done\n"}},
    ]
    budgets = loop._navigation_output_budgets(
        messages=[{"role": "system", "content": "Use tools safely"},
                  {"role": "user", "content": "Read and edit"}],
        assistant_text="Inspect source and write report",
        tool_calls=calls,
        provider_state=[{
            "type": "reasoning", "id": "mixed-state", "summary": [],
            "status": "completed", "encrypted_content": "fixture-replay",
        }],
        tool_schema=[],
        request_budget=loop._resolve_request_token_budget(),
        tools_snapshot=tools,
    )
    assert list(budgets) == ["one-read"]
    try:
        session = await loop.start_session()
        results = await loop._execute_tool_calls(
            calls, session, tools_snapshot=tools, navigation_output_budgets=budgets
        )
        assert len(results) == 2
        assert (tmp_path / "report.txt").read_text() == "done\n"
        assert approved_calls.count("write_file") == 1
        persisted = store.load_session(session.session_id)
        writes = [c for c in persisted.tool_calls if c.tool_name == "write_file"]
        assert len(writes) == 1 and writes[0].success and writes[0].executed
        assert store.verify_audit_log(session.session_id) == []
    finally:
        await loop.aclose()


@pytest.mark.asyncio
async def test_policy_denial_precedes_navigation_budget_refusal(tmp_path: Path) -> None:
    """An explicit deny rule must remain a safety-block, not budget masking."""
    from ash.safety.grants import PermissionRule, RuleEffect
    from ash.tools.base import ToolOutputBudget

    class Provider(ProviderABC):
        model_name = "policy-probe"

        def count_tokens(self, text: str) -> int:
            return len(str(text)) // 4

        async def stream_chat(self, messages, temperature=0.0, tools=None):
            raise AssertionError("no model calls")
            yield StreamChunk(is_done=True)

    guard = SafetyGuard(tmp_path)
    store = SessionStore(tmp_path / "safety.db")
    loop = AshLoop(
        store, Provider(), guard,
        HeadlessUI(output_format="stream-json", stream=io.StringIO()),
        tmp_path, tools={"read_file": ReadFileTool(guard)},
    )
    call = {"call_id": "denied-read", "name": "read_file", "arguments": {"file_path": "absent.txt"}}
    try:
        session = await loop.start_session()
        # start_session initializes persistent/session grants; install this
        # explicit simulated policy only after that normal setup finishes.
        loop.permission_policy.add_session_rule(
            PermissionRule.create(RuleEffect.DENY, "read_file")
        )
        result = await loop._execute_tool_calls(
            [call], session,
            navigation_output_budgets={
                "denied-read": ToolOutputBudget(accepts=lambda _: False, can_dispatch=False)
            },
        )
        assert len(result) == 1
        assert "matched deny rule" in str(result[0])
        assert "context budget" not in str(result[0]).lower()
        persisted = store.load_session(session.session_id)
        call_record = persisted.tool_calls[0]
        assert not call_record.approved and not call_record.dispatched
        assert not call_record.executed and not call_record.success
        assert any(record.action_type == "safety_block" for record in store.list_audit_logs(session.session_id))
        assert store.verify_audit_log(session.session_id) == []
    finally:
        await loop.aclose()


def test_multiple_budget_refusals_are_projected_at_real_rendered_size(tmp_path: Path) -> None:
    """Every refused call still needs a provider-visible tool result token budget."""
    import ash.core.loop as loop_module
    from ash.tools.base import ToolResult

    class Provider(ProviderABC):
        model_name = "refusal-projection"
        _ash_declared_capabilities = ProviderCapabilities(native_tools=True)

        def count_tokens(self, value: str) -> int:
            return max(1, len(str(value)) // 4)

        async def stream_chat(self, messages, temperature=0.0, tools=None):
            raise AssertionError("model inference prohibited")
            yield StreamChunk(is_done=True)

    guard = SafetyGuard(tmp_path)
    read = ReadFileTool(guard)
    loop = AshLoop(
        SessionStore(tmp_path / "refusals.db"), Provider(), guard,
        HeadlessUI(output_format="stream-json", stream=io.StringIO()),
        tmp_path, tools={"read_file": read},
        config=AshConfig(
            model="custom/refusal-projection", workspace_root=tmp_path,
            db_directory=tmp_path / "db", memory_backend="off",
            max_context_tokens=3_000, max_completion_tokens=500,
        ),
    )
    budget = loop._resolve_request_token_budget()
    assert budget is not None
    calls = [
        {"call_id": f"read-{i}", "name": "read_file", "arguments": {"file_path": "a.py"}}
        for i in range(4)
    ]
    opaque = [{
        "type": "reasoning", "id": "state", "summary": [],
        "status": "completed", "encrypted_content": "fixture-replay",
    }]
    refusal = (
        "Not dispatched because the remaining context budget cannot fit "
        "a useful source-navigation result. Continue from evidence already "
        "returned, make the requested edit or test, or use an explicitly "
        "enabled safe context continuation."
    )
    assistant = {
        "role": "assistant", "content": "Inspecting source",
        "tool_calls": loop._provider_history_tool_calls(calls),
        "provider_state": opaque,
    }
    refusals = [
        {
            "role": "tool", "tool_call_id": call["call_id"],
            "content": loop_module._render_tool_response(
                call["call_id"], "read_file",
                {"success": False, "output": "", "error": refusal,
                 "truncated": False, "token_count": 0},
            ),
        }
        for call in calls
    ]
    # Fill an otherwise valid small provider input near its limit, so ordinary
    # navigation is no longer admissible. The committed refusal text must
    # remain present in the budget instead of being an imaginary empty result.
    choice: list[dict[str, Any]] | None = None
    for padding in range(0, 12_000, 16):
        messages = [
            {"role": "system", "content": "System"},
            {"role": "user", "content": "X" * padding},
        ]
        projected = messages + [assistant, *refusals]
        tokens = HistoryCompactor._count(projected, loop.provider.count_tokens)
        navigation_limit = budget.input_limit - max(
            128, min(2_048, budget.input_limit // 12)
        )
        if navigation_limit - 90 <= tokens <= navigation_limit - 80:
            choice = messages
            break
    assert choice is not None
    budgets = loop._navigation_output_budgets(
        messages=choice, assistant_text="Inspecting source", tool_calls=calls,
        provider_state=opaque, tool_schema=[], request_budget=budget,
        tools_snapshot={"read_file": read},
    )
    assert len(budgets) == 4
    refused_result = ToolResult(success=False, output="", error=refusal)
    # A compact useful sample may itself be admissible near the limit, so
    # can_dispatch can be either value; failure feedback must still be fitted.
    assert all(guard.accepts(refused_result) for guard in budgets.values())
    assert HistoryCompactor._count(
        choice + [assistant, *refusals], loop.provider.count_tokens
    ) <= budget.input_limit


@pytest.mark.asyncio
async def test_single_read_early_error_fits_budget_before_tool_result_history(
    tmp_path: Path,
) -> None:
    """Errors returned before ReadFileTool's source clipping cannot escape."""
    import ash.core.loop as loop_module
    from ash.tools.base import ToolOutputBudget, ToolResult

    reader = ReadFileTool(SafetyGuard(tmp_path))
    missing = "missing/" + "/".join("nested_" + "a" * 24 for _ in range(16)) + "/absent.py"
    budget = ToolOutputBudget(
        accepts=lambda result: len(result.output) + len(result.error or "") <= 140,
        can_dispatch=True,
    )
    raw = await reader.run(file_path=missing)
    assert not raw.success and not budget.accepts(raw)
    payload = await loop_module._execute_tool_once(
        reader, {"file_path": missing}, output_budget=budget
    )
    result = ToolResult(
        success=payload["success"], output=payload["output"],
        error=payload["error"], truncated=payload["truncated"],
    )
    assert budget.accepts(result), (len(result.error or ""), len(result.output))
    assert not result.success and result.truncated
    assert "context budget" in (result.error or "").lower()
    assert not (tmp_path / missing).exists()
