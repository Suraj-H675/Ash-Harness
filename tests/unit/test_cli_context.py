import asyncio

import pytest

from ash.cli import (
    _repl,
    _context_diagnostics_for_session,
    _render_context_budget,
    _render_context_diagnostics,
    _render_context_provenance,
)
from ash.core.activity import summarize_context_diagnostics
from ash.context.history import (
    ContextBudgetReport,
    ContextBudgetSlice,
    ContextFragment,
    ContextFragmentKind,
    ContextTrust,
)
from ash.core.session import StoredRuntimeEvent
from ash.config import AshConfig
from ash.core.loop import AshLoop
from ash.core.session import SessionStore
from ash.providers.base import ProviderABC, StreamChunk
from ash.providers.capabilities import ProviderCapabilities
from ash.safety.guard import SafetyGuard
from ash.ui.terminal import TerminalUI


def test_context_diagnostics_join_exact_request_and_keep_rejection_separate() -> None:
    session_id = "session-one"
    context_id = "11111111-1111-4111-8111-111111111111"
    rejected_context_id = "22222222-2222-4222-8222-222222222222"
    first_operation_id = "33333333-3333-4333-8333-333333333333"
    second_operation_id = "44444444-4444-4444-8444-444444444444"
    preview_context_id = "55555555-5555-4555-8555-555555555555"

    def record(sequence: int, event: dict[str, object], *, owner: str = session_id):
        return StoredRuntimeEvent(
            sequence=sequence,
            event={
                "session_id": owner,
                "timestamp": f"2026-11-21T00:00:{sequence:02d}+00:00",
                **event,
            },
        )

    diagnostics = summarize_context_diagnostics(
        [
            record(
                1,
                {
                    "type": "context.usage",
                    "context_id": context_id,
                    "assembly_purpose": "request",
                    "estimate_source": "local",
                    "current": 74_000,
                    "maximum": 80_000,
                    "estimator_route": "unknown",
                    "estimator_count_mode": "unknown",
                },
            ),
            record(
                2,
                {
                    "type": "model.request.started",
                    "context_id": context_id,
                    "operation_id": first_operation_id,
                    "attempt": 1,
                    "requested_route": "provider-a/model-a",
                    "effective_route": "unknown",
                    "turn_id": "turn-one",
                },
            ),
            record(
                3,
                {
                    "type": "model.request.error",
                    "context_id": context_id,
                    "operation_id": first_operation_id,
                    "requested_route": "provider-a/model-a",
                    "effective_route": "unknown",
                },
            ),
            record(
                4,
                {
                    "type": "model.request.started",
                    "context_id": context_id,
                    "operation_id": second_operation_id,
                    "attempt": 2,
                    "requested_route": "provider-a/model-a",
                    "effective_route": "unknown",
                    "turn_id": "turn-one",
                },
            ),
            record(
                5,
                {
                    "type": "model.request.completed",
                    "context_id": context_id,
                    "operation_id": second_operation_id,
                    "requested_route": "provider-a/model-a",
                    "effective_route": "provider-b/model-b",
                    "usage_source": "provider",
                    "prompt_tokens": 900,
                    "completion_tokens": 30,
                    "cache_read_tokens": 200,
                    "cache_write_tokens": 50,
                },
            ),
            record(
                6,
                {
                    "type": "context.usage",
                    "context_id": rejected_context_id,
                    "assembly_purpose": "request",
                    "estimate_source": "local",
                    "current": 1500,
                    "maximum": 80_000,
                    "estimator_route": "provider-a/model-a",
                    "estimator_count_mode": "unknown",
                },
            ),
            record(
                7,
                {
                    "type": "context.request.rejected",
                    "context_id": rejected_context_id,
                    "cause": "turn_budget",
                    "scope": "request_attempt",
                    "dispatch_status": "not_dispatched",
                    "required_input_tokens": 1500,
                    "input_limit": 80_000,
                    "protected_current_turn": True,
                    "opaque_provider_replay_state_preserved": False,
                    "required_history_summary": False,
                    "turn_tokens_used": 7900,
                    "turn_token_limit": 9000,
                },
            ),
            record(
                8,
                {
                    "type": "turn.usage",
                    "turn_id": "turn-one",
                    "usage_source": "provider",
                    "prompt_tokens": 1200,
                    "completion_tokens": 80,
                    "cache_read_tokens": 400,
                    "cache_write_tokens": 90,
                    "estimated_prompt_tokens": 0,
                    "estimated_completion_tokens": 0,
                },
            ),
            record(
                9,
                {
                    "type": "context.usage",
                    "context_id": context_id,
                    "assembly_purpose": "request",
                    "current": 1,
                    "maximum": 2,
                    "estimator_route": "secret/profile/token",
                },
                owner="another-session",
            ),
            record(
                10,
                {
                    "type": "context.usage",
                    "context_id": preview_context_id,
                    "assembly_purpose": "preview",
                    "estimate_source": "local",
                    "current": 42,
                    "maximum": 100,
                    "estimator_route": "provider-a/model-a",
                },
            ),
        ],
        session_id=session_id,
    )

    assert diagnostics["assembly"]["context_id"] == rejected_context_id
    assert diagnostics["assembly"]["current"] == 1500
    request = diagnostics["request"]
    assert request["context_id"] == context_id
    assert request["assembly"]["context_id"] == context_id
    assert request["assembly"]["context_id"] != preview_context_id
    assert request["assembly"]["current"] == 74_000
    assert request["operation_id"] == second_operation_id
    assert request["attempt"] == 2
    assert request["state"] == "completed"
    assert request["route_parity"] == "mismatch"
    assert request["usage_source"] == "provider"
    assert request["cache_read_tokens"] == 200
    assert diagnostics["rejection"]["context_id"] == rejected_context_id
    assert diagnostics["rejection"]["dispatch_status"] == "not_dispatched"
    assert diagnostics["turn_usage"]["prompt_tokens"] == 1200

    rendered = _render_context_diagnostics(diagnostics)
    assert "~1,500/80,000 estimated input tokens" in rendered
    assert "matched local estimate: ~74,000/80,000 input tokens" in rendered
    assert "route: provider-a/model-a -> provider-b/model-b (parity: mismatch)" in rendered
    assert "cumulative turn budget 7,900/9,000" in rendered
    assert "prompt cache subsets: 400 read, 90 written" in rendered
    assert "secret/profile/token" not in rendered


def test_context_diagnostics_leave_legacy_records_unavailable() -> None:
    diagnostics = summarize_context_diagnostics(
        [
            StoredRuntimeEvent(
                sequence=1,
                event={
                    "session_id": "legacy",
                    "type": "context.usage",
                    "current": 42,
                    "maximum": 100,
                },
            ),
            StoredRuntimeEvent(
                sequence=2,
                event={
                    "session_id": "legacy",
                    "type": "model.request.completed",
                    "prompt_tokens": 20,
                    "usage_source": "provider",
                },
            ),
        ],
        session_id="legacy",
    )

    assert diagnostics["assembly"] is None
    assert diagnostics["request"] is None
    assert diagnostics["rejection"] is None
    rendered = _render_context_diagnostics(diagnostics)
    assert "Last recorded request assembly: unavailable" in rendered
    assert "Last dispatched request attempt: unavailable" in rendered


@pytest.mark.parametrize(
    ("terminal_type", "expected_state"),
    [("model.request.cancelled", "cancelled"), ("turn.error", "missing")],
)
def test_context_diagnostics_distinguish_cancelled_and_missing_usage(
    terminal_type: str, expected_state: str
) -> None:
    context_id = "55555555-5555-4555-8555-555555555555"
    operation_id = "66666666-6666-4666-8666-666666666666"

    def record(sequence: int, event: dict[str, object]) -> StoredRuntimeEvent:
        return StoredRuntimeEvent(
            sequence=sequence,
            event={
                "session_id": "session",
                "turn_id": "turn",
                "timestamp": f"2026-11-21T00:00:0{sequence}+00:00",
                **event,
            },
        )

    events = [
        record(
            1,
            {
                "type": "model.request.started",
                "context_id": context_id,
                "operation_id": operation_id,
                "attempt": 1,
                "requested_route": "provider/model",
                "effective_route": "unknown",
            },
        ),
        record(
            2,
            {
                "type": terminal_type,
                **(
                    {"operation_id": operation_id, "context_id": context_id}
                    if terminal_type == "model.request.cancelled"
                    else {}
                ),
            },
        ),
    ]

    diagnostics = summarize_context_diagnostics(events, session_id="session")

    assert diagnostics["request"]["state"] == expected_state
    assert diagnostics["request"]["usage_source"] == "unavailable"
    assert diagnostics["request"]["prompt_tokens"] is None
    assert diagnostics["request"]["route_parity"] == "unknown"


@pytest.mark.asyncio
async def test_context_command_reads_current_session_events_after_session_switch(
    tmp_path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    class Provider(ProviderABC):
        model_name = "context-cli"
        provider_family = "custom"
        _ash_declared_capabilities = ProviderCapabilities(native_tools=True)

        def count_tokens(self, text):
            return len(str(text).split())

        async def stream_chat(self, messages, temperature=0.0, tools=None):
            yield StreamChunk(
                content="done",
                is_done=True,
                stop_reason="stop",
                prompt_tokens=11,
                completion_tokens=1,
                cache_read_tokens=2,
                cache_write_tokens=1,
                usage_source="provider",
            )

    commands = iter(["measure this", "/context", "/new", "/context", "/exit"])

    class Prompt:
        supports_live_surface = False
        supports_full_screen_ui = False
        supports_choice_ui = False
        linear_mode = False

        def __init__(self, *args, **kwargs):
            self.supports_choice_ui = False

        async def read(self, prompt: str) -> str:
            if prompt == "steer> ":
                await asyncio.Event().wait()
            return next(commands)

        def set_extra_commands(self, commands) -> None:
            del commands

    class Status:
        def __init__(self, *args, **kwargs):
            pass

        def left(self) -> str:
            return ""

        def context_usage(self) -> tuple[int, int]:
            return (0, 1)

    class Notifier:
        def __init__(self, *args, **kwargs):
            pass

    monkeypatch.setattr("ash.ui.prompt.PromptInput", Prompt)
    monkeypatch.setattr("ash.ui.status.StatusLine", Status)
    monkeypatch.setattr("ash.ui.notifications.TerminalNotifier", Notifier)
    monkeypatch.setattr("ash.safety.trust.is_workspace_trusted", lambda _root: False)
    isolated_home = tmp_path / "home"
    isolated_home.mkdir()
    monkeypatch.setenv("HOME", str(isolated_home))
    project = tmp_path / "project"
    project.mkdir()
    store = SessionStore(tmp_path / "db" / "sessions.db")
    config = AshConfig(
        model="custom/context-cli",
        workspace_root=project,
        db_directory=tmp_path / "db",
        memory_backend="off",
        notification_method="off",
    )
    loop = AshLoop(
        store,
        Provider(),
        SafetyGuard(project_root=project),
        TerminalUI(safety_tier="auto_approve"),
        project,
        tools={},
        config=config,
        safety_tier="auto_approve",
    )
    first_session = await loop.start_session()
    try:
        assert await _repl(loop, config, object()) == 0
    finally:
        await loop.aclose()

    rendered = capsys.readouterr().out
    context_positions = [
        position
        for position in range(len(rendered))
        if rendered.startswith("Context: ~", position)
    ]
    assert len(context_positions) == 2
    first_view = rendered[context_positions[0] : context_positions[1]]
    second_view = rendered[context_positions[1] :]
    first_context_id = next(
        event.event["context_id"]
        for event in store.list_recent_runtime_events(first_session.session_id)
        if event.event.get("type") == "context.usage"
    )
    assert "Last recorded request assembly: ~" in first_view
    assert "Last dispatched request attempt: completed; usage=provider" in first_view
    assert "Last persisted turn cumulative usage (provider)" in first_view
    assert "prompt cache subsets: 2 read, 1 written" in first_view
    assert first_context_id in first_view
    assert "Last recorded request assembly: unavailable" in second_view
    assert "Last dispatched request attempt: unavailable" in second_view
    assert first_context_id not in second_view


@pytest.mark.asyncio
async def test_context_reports_crash_interrupted_request_after_session_recovery(
    tmp_path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    class Provider(ProviderABC):
        model_name = "context-recovery"
        provider_family = "custom"
        _ash_declared_capabilities = ProviderCapabilities(native_tools=True)

        def __init__(self) -> None:
            self.request_calls = 0

        def count_tokens(self, text):
            return len(str(text).split())

        async def stream_chat(self, messages, temperature=0.0, tools=None):
            self.request_calls += 1
            yield StreamChunk(
                content="unexpected provider call",
                is_done=True,
                stop_reason="stop",
                prompt_tokens=1,
                completion_tokens=1,
                usage_source="provider",
            )

    commands = iter(["/context", "/exit", "/context", "/exit"])

    class Prompt:
        supports_live_surface = False
        supports_full_screen_ui = False
        supports_choice_ui = False
        linear_mode = False

        def __init__(self, *args, **kwargs):
            pass

        async def read(self, prompt: str) -> str:
            if prompt == "steer> ":
                await asyncio.Event().wait()
            return next(commands)

        def set_extra_commands(self, commands) -> None:
            del commands

    class Status:
        def __init__(self, *args, **kwargs):
            pass

        def left(self) -> str:
            return ""

        def context_usage(self) -> tuple[int, int]:
            return (0, 1)

    class Notifier:
        def __init__(self, *args, **kwargs):
            pass

    monkeypatch.setattr("ash.ui.prompt.PromptInput", Prompt)
    monkeypatch.setattr("ash.ui.status.StatusLine", Status)
    monkeypatch.setattr("ash.ui.notifications.TerminalNotifier", Notifier)
    monkeypatch.setattr("ash.safety.trust.is_workspace_trusted", lambda _root: False)
    isolated_home = tmp_path / "home"
    isolated_home.mkdir()
    monkeypatch.setenv("HOME", str(isolated_home))
    project = tmp_path / "project"
    project.mkdir()
    store = SessionStore(tmp_path / "db" / "sessions.db")
    config = AshConfig(
        model="custom/context-recovery",
        workspace_root=project,
        db_directory=tmp_path / "db",
        memory_backend="off",
        notification_method="off",
    )

    def build_loop(provider: Provider) -> AshLoop:
        return AshLoop(
            store,
            provider,
            SafetyGuard(project_root=project),
            TerminalUI(safety_tier="auto_approve"),
            project,
            tools={},
            config=config,
            safety_tier="auto_approve",
        )

    provider = Provider()
    live_loop = build_loop(provider)
    session = await live_loop.start_session()
    turn_id = "11111111-1111-4111-8111-111111111111"
    context_id = "22222222-2222-4222-8222-222222222222"
    operation_id = "33333333-3333-4333-8333-333333333333"
    store.start_turn(session.session_id, turn_id, "")
    live_loop._emit_event(
        {
            "type": "model.request.started",
            "turn_id": turn_id,
            "context_id": context_id,
            "operation_id": operation_id,
            "attempt": 1,
            "provider": "custom",
            "model": "context-recovery",
            "requested_route": "custom/context-recovery",
            "effective_route": "unknown",
        }
    )
    live_loop._flush_runtime_events()
    assert [row["turn_id"] for row in store.started_turns(session.session_id)] == [
        turn_id
    ]
    assert await _repl(live_loop, config, object()) == 0
    live_output = capsys.readouterr().out
    assert "Last dispatched request attempt: pending; usage=unavailable" in live_output
    await live_loop.aclose()

    recovered_loop = build_loop(provider)
    try:
        recovered_session = await recovered_loop.start_session(session.session_id)
        assert recovered_session.session_id == session.session_id
        assert recovered_loop.recovered_turns == 1
        assert recovered_loop.recovery_summary.interrupted_turns == 1
        assert store.turn_journal_status(session.session_id, turn_id) == "interrupted"
        persisted = store.list_recent_runtime_events(session.session_id, limit=300)
        assert any(
            item.event.get("type") == "model.request.started"
            and item.event.get("turn_id") == turn_id
            and item.event.get("context_id") == context_id
            and item.event.get("operation_id") == operation_id
            for item in persisted
        )
        assert not any(
            item.event.get("type") == "session.recovery" for item in persisted
        )
        assert not any(
            item.event.get("type") == "turn.usage"
            and item.event.get("turn_id") == turn_id
            for item in persisted
        )
        assert await _repl(recovered_loop, config, object()) == 0
        output = capsys.readouterr().out
        assert "Last dispatched request attempt: turn interrupted; " \
            "provider outcome=unknown; usage=unavailable" in output

        foreign_session = store.create_session(str(project))
        foreign_turn_id = "44444444-4444-4444-8444-444444444444"
        store.start_turn(foreign_session.session_id, foreign_turn_id, "")
        recovered_loop._emit_event(
            {
                "type": "model.request.started",
                "turn_id": foreign_turn_id,
                "context_id": "55555555-5555-4555-8555-555555555555",
                "operation_id": "66666666-6666-4666-8666-666666666666",
                "attempt": 1,
                "provider": "custom",
                "model": "context-recovery",
                "requested_route": "custom/context-recovery",
                "effective_route": "unknown",
            }
        )
        recovered_loop._flush_runtime_events()
        assert (
            store.turn_journal_status(foreign_session.session_id, foreign_turn_id)
            == "started"
        )
        assert store.turn_journal_status(session.session_id, foreign_turn_id) is None
        diagnostics = _context_diagnostics_for_session(store, session.session_id)
        assert diagnostics["request"]["state"] == "unknown"
        assert "provider outcome=unknown; usage=unavailable" in (
            _render_context_diagnostics(diagnostics)
        )
        store.save_runtime_events(
            [
                {
                    "type": "model.request.started",
                    "session_id": session.session_id,
                    "context_id": "77777777-7777-4777-8777-777777777777",
                    "operation_id": "88888888-8888-4888-8888-888888888888",
                    "attempt": 1,
                    "requested_route": "custom/context-recovery",
                    "effective_route": "unknown",
                }
            ]
        )
        diagnostics = _context_diagnostics_for_session(store, session.session_id)
        assert diagnostics["request"]["turn_id"] is None
        assert diagnostics["request"]["state"] == "unknown"
    finally:
        await recovered_loop.aclose()

    assert provider.request_calls == 0


def test_render_context_budget_shows_bucket_usage() -> None:
    rendered = _render_context_budget(
        ContextBudgetReport(
            maximum=100,
            completion_reserve=10,
            input_limit=90,
            slices={
                "system": ContextBudgetSlice("system", limit=20, used=12),
                "memory": ContextBudgetSlice(
                    "memory", limit=10, used=10, truncated=True
                ),
            },
            fragments=(
                ContextFragment(
                    kind=ContextFragmentKind.SYSTEM,
                    source="assembled_system_prompt",
                    trust=ContextTrust.MIXED,
                    tokens=12,
                    limit=20,
                    truncated=False,
                    content_sha256="a" * 64,
                ),
            ),
        )
    )

    assert "input=90; response_reserve=10; combined=100" in rendered
    assert "system: ~12/20" in rendered
    assert "memory: ~10/10 truncated" in rendered
    assert "system: assembled_system_prompt [mixed] sha256=aaaaaaaaaaaa" in rendered


def test_render_context_budget_without_report_is_empty() -> None:
    assert _render_context_budget(None) == ""


def test_context_provenance_escapes_provider_visible_tool_names() -> None:
    report = ContextBudgetReport(
        maximum=100,
        completion_reserve=10,
        input_limit=90,
        slices={},
        fragments=(
            ContextFragment(
                kind=ContextFragmentKind.TOOL_SCHEMA,
                source="runtime_tool_registry",
                trust=ContextTrust.MIXED,
                tokens=12,
                limit=20,
                truncated=False,
                content_sha256="a" * 64,
                metadata=(("provider_visible_tool_names", "good,evil\x1b[2J\nfake"),),
            ),
        ),
    )

    rendered = _render_context_provenance(report)
    assert "provider_visible_tool_names=good,evil\\x1b[2J\\x0afake" in rendered
    assert "\x1b" not in rendered
    assert "\nfake" not in rendered
