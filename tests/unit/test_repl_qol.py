from __future__ import annotations

import builtins
import hashlib
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from ash.cli import _repl, _session_usage_lines
from ash.core.session import Message, SessionStore, SessionUsage
from ash.safety.guard import SafetyGuard
from ash.safety.policy import PermissionPolicy
from ash.tools.base import ToolResult


def _install_frontend(
    monkeypatch: pytest.MonkeyPatch,
    commands,
    *,
    turn_inputs: list[str] | None = None,
    turn_metadata: list[dict | None] | None = None,
):
    class FakeTerminalUI:
        transcript = None

        def __init__(self, *args, **kwargs) -> None:
            pass

        def write_status(self, text: str, *, error: bool = False) -> None:
            builtins.print(
                text,
                end="",
                file=__import__("sys").stderr if error else None,
            )

        def load_session_transcript(self, session) -> None:
            del session

    class FakePromptInput:
        interactive = False
        supports_full_screen_ui = False

        def __init__(self, *args, **kwargs) -> None:
            del args, kwargs
            self.supports_choice_ui = True

        async def read(self, prompt: str) -> str:
            del prompt
            return next(commands)

        def set_extra_commands(self, commands) -> None:
            del commands

    class FakeStatusLine:
        def __init__(self, *args, **kwargs) -> None:
            pass

        def __call__(self) -> str:
            return ""

        def header(self) -> str:
            return ""

        def footer(self) -> str:
            return ""

    class FakeNotifier:
        def __init__(self, *args, **kwargs) -> None:
            pass

    class FakeTurnController:
        def __init__(self, *args, **kwargs) -> None:
            pass

        async def run(self, user_input: str, *, user_metadata=None) -> str:
            if turn_inputs is not None:
                turn_inputs.append(user_input)
            if turn_metadata is not None:
                turn_metadata.append(user_metadata)
            return "ok"

    class FakePrinter:
        def __init__(self, *args, **kwargs) -> None:
            pass

        def __call__(self, *values, sep=" ", end="\n", file=None, flush=False):
            builtins.print(*values, sep=sep, end=end, file=file, flush=flush)

    monkeypatch.setattr("ash.ui.terminal.TerminalUI", FakeTerminalUI)
    monkeypatch.setattr("ash.ui.prompt.PromptInput", FakePromptInput)
    monkeypatch.setattr("ash.ui.status.StatusLine", FakeStatusLine)
    monkeypatch.setattr("ash.ui.notifications.TerminalNotifier", FakeNotifier)
    monkeypatch.setattr(
        "ash.ui.turn_input.InteractiveTurnController", FakeTurnController
    )
    monkeypatch.setattr("ash.ui.output.ReplPrinter", FakePrinter)
    return FakeTerminalUI


def _config(tmp_path: Path, **overrides):
    values = {
        "model": "ollama/test-model",
        "fallback_models": [],
        "custom_providers": {},
        "workspace_root": tmp_path,
        "input_mode": "emacs",
        "keybindings": {},
        "theme": "dark",
        "no_color": True,
        "reduced_motion": False,
        "screen_reader_mode": False,
        "show_token_meter": False,
        "notification_method": "off",
        "notification_events": (),
        "notification_include_preview": False,
        "sandbox_backend": "auto",
        "memory_backend": "off",
        "sandbox_docker_image": "ash-sandbox:latest",
        "sandbox_docker_memory_mb": 4096,
        "sandbox_docker_cpus": 2.0,
        "allow_unsafe_plugin_runtime": False,
        "allow_unsafe_auto_approve": False,
        "safety_tier": "interactive",
        "attachment_token_budget": 1024,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


async def _run_repl(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    commands,
    *,
    session_store: SessionStore | None = None,
    current_session=None,
    tools: dict | None = None,
    safety_tier: str = "interactive",
    active_model_id: str = "ollama/test-model",
    vision: bool = False,
    config_overrides: dict | None = None,
    turn_inputs: list[str] | None = None,
    turn_metadata: list[dict | None] | None = None,
) -> int:
    ui_type = _install_frontend(
        monkeypatch,
        commands,
        turn_inputs=turn_inputs,
        turn_metadata=turn_metadata,
    )
    monkeypatch.setattr("ash.safety.trust.is_workspace_trusted", lambda root: False)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    store = session_store or SessionStore(tmp_path / "repl-test-sessions.db")
    loop = SimpleNamespace(
        ui=ui_type(),
        project_root=tmp_path,
        repo_map=None,
        _mcp_runtime=None,
        _mcp_configs={},
        safety_guard=SafetyGuard(tmp_path),
        tools=tools or {},
        current_session=current_session,
        current_goal=None,
        session_store=store,
        active_model_id=active_model_id,
        provider=SimpleNamespace(
            capabilities=SimpleNamespace(vision=vision),
            count_tokens=lambda text: len(text),
        ),
        permission_policy=PermissionPolicy(safety_tier),
        safety_tier=safety_tier,
    )
    config = _config(tmp_path, **(config_overrides or {}))
    return await _repl(loop, config, SimpleNamespace())


def _persist_turn(
    store: SessionStore,
    session,
    turn_id: str,
    messages: tuple[Message, ...],
) -> None:
    for message in messages:
        store.save_message(session.session_id, message, turn_id=turn_id)
        session.messages.append(message)


@pytest.mark.asyncio
async def test_retry_rewinds_old_answer_and_replays_primary_user_metadata(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys,
) -> None:
    store = SessionStore(tmp_path / "sessions.db")
    session = store.create_session(str(tmp_path), model="ollama/test-model")
    now = datetime.now(timezone.utc)
    _persist_turn(
        store,
        session,
        "retry-turn",
        (
            Message(
                role="user",
                content="retry this request",
                timestamp=now,
                metadata={"source": "test"},
            ),
            Message(
                role="user",
                content="mid-turn steer",
                timestamp=now,
                metadata={"steering": True},
            ),
            Message(role="assistant", content="old answer", timestamp=now),
        ),
    )
    turns: list[str] = []
    metadata: list[dict | None] = []

    assert (
        await _run_repl(
            tmp_path,
            monkeypatch,
            iter(("/retry", "exit")),
            session_store=store,
            current_session=session,
            turn_inputs=turns,
            turn_metadata=metadata,
        )
        == 0
    )
    assert turns == ["retry this request"]
    assert metadata == [{"source": "test"}]
    assert "ok" in capsys.readouterr().out
    assert store.load_session(session.session_id).messages == []


@pytest.mark.asyncio
async def test_retry_refuses_resumed_image_without_image_bytes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys,
) -> None:
    store = SessionStore(tmp_path / "sessions.db")
    session = store.create_session(str(tmp_path), model="ollama/test-model")
    message = Message(
        role="user",
        content="describe @image.png",
        timestamp=datetime.now(timezone.utc),
        metadata={
            "images": [
                {
                    "path": "image.png",
                    "media_type": "image/png",
                    "sha256": "abc",
                }
            ]
        },
    )
    _persist_turn(store, session, "image-turn", (message,))
    turns: list[str] = []

    assert (
        await _run_repl(
            tmp_path,
            monkeypatch,
            iter(("/retry", "exit")),
            session_store=store,
            current_session=session,
            vision=True,
            turn_inputs=turns,
        )
        == 0
    )
    captured = capsys.readouterr()
    assert turns == []
    assert "Cannot retry the last image turn" in captured.err
    assert "Reattach the image" in captured.err


@pytest.mark.asyncio
async def test_retry_refuses_non_reversible_tool_turn(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys,
) -> None:
    store = SessionStore(tmp_path / "sessions.db")
    session = store.create_session(str(tmp_path), model="ollama/test-model")
    now = datetime.now(timezone.utc)
    _persist_turn(
        store,
        session,
        "side-effect-turn",
        (
            Message(role="user", content="run the deploy", timestamp=now),
            Message(
                role="assistant",
                content="running it",
                timestamp=now,
                metadata={
                    "tool_calls": [
                        {
                            "call_id": "call-1",
                            "name": "run_command",
                            "arguments": {"command_line": "deploy"},
                        }
                    ]
                },
            ),
        ),
    )
    turns: list[str] = []

    assert (
        await _run_repl(
            tmp_path,
            monkeypatch,
            iter(("/retry", "exit")),
            session_store=store,
            current_session=session,
            turn_inputs=turns,
        )
        == 0
    )
    assert turns == []
    assert "non-reversible tool(s): run_command" in capsys.readouterr().err
    assert len(store.load_session(session.session_id).messages) == 2


@pytest.mark.asyncio
async def test_retry_restores_direct_file_edit_before_replay(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "target.txt"
    target.write_text("before\n", encoding="utf-8")
    store = SessionStore(tmp_path / "sessions.db")
    session = store.create_session(str(tmp_path), model="ollama/test-model")
    now = datetime.now(timezone.utc)
    _persist_turn(
        store,
        session,
        "edit-turn",
        (
            Message(role="user", content="update target.txt", timestamp=now),
            Message(
                role="assistant",
                content="updated",
                timestamp=now,
                metadata={
                    "tool_calls": [
                        {
                            "call_id": "write-1",
                            "name": "write_file",
                            "arguments": {
                                "file_path": "target.txt",
                                "content": "after\n",
                            },
                        }
                    ]
                },
            ),
        ),
    )
    store.save_file_checkpoint(
        session.session_id,
        "edit-turn",
        "write_file",
        str(target),
        existed=True,
        before_content=b"before\n",
        before_mode=target.stat().st_mode & 0o777,
        call_id="write-1",
    )
    target.write_text("after\n", encoding="utf-8")
    store.finish_file_checkpoint(
        session.session_id,
        "edit-turn",
        str(target),
        hashlib.sha256(b"after\n").hexdigest(),
        call_id="write-1",
    )
    turns: list[str] = []

    assert (
        await _run_repl(
            tmp_path,
            monkeypatch,
            iter(("/retry", "exit")),
            session_store=store,
            current_session=session,
            turn_inputs=turns,
        )
        == 0
    )
    assert target.read_text(encoding="utf-8") == "before\n"
    assert turns == ["update target.txt"]
    assert store.load_session(session.session_id).messages == []


def test_usage_lines_distinguish_unknown_price_from_known_subtotal() -> None:
    rendered = _session_usage_lines(
        SessionUsage(
            prompt_tokens=120,
            completion_tokens=30,
            cache_read_tokens=40,
            cache_write_tokens=5,
            cost_usd=0.0123,
            estimated_prompt_tokens=20,
            estimated_completion_tokens=10,
            estimated_cost_usd=0.002,
            pricing_unknown_turns=1,
        )
    )

    assert rendered == (
        "Tokens: 120 prompt, 30 completion (20 prompt, 10 completion estimated)",
        "Prompt cache: 40 read, 5 written",
        "Cost: unknown (known subtotal $0.012300); estimated subtotal $0.002000",
    )


def test_usage_lines_render_complete_known_cost() -> None:
    assert _session_usage_lines(
        SessionUsage(prompt_tokens=10, completion_tokens=5, cost_usd=0.0042)
    ) == (
        "Tokens: 10 prompt, 5 completion",
        "Prompt cache: 0 read, 0 written",
        "Cost: $0.004200",
    )


@pytest.mark.asyncio
async def test_copy_uses_latest_assistant_response(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys,
) -> None:
    copied: list[str] = []
    monkeypatch.setattr(
        "ash.ui.clipboard.copy_to_clipboard",
        lambda text, *, workspace_root: copied.append(text) or "test-clipboard",
    )
    store = SessionStore(tmp_path / "sessions.db")
    session = store.create_session(str(tmp_path), model="ollama/test-model")
    now = datetime.now(timezone.utc)
    session.messages.extend(
        (
            Message(role="assistant", content="older answer", timestamp=now),
            Message(role="user", content="follow-up", timestamp=now),
            Message(role="assistant", content="latest answer", timestamp=now),
        )
    )

    assert (
        await _run_repl(
            tmp_path,
            monkeypatch,
            iter(("/copy", "exit")),
            session_store=store,
            current_session=session,
        )
        == 0
    )
    assert copied == ["latest answer"]
    assert "Copied latest assistant response via test-clipboard." in capsys.readouterr().out


@pytest.mark.asyncio
async def test_processes_lists_and_stops_managed_jobs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys,
) -> None:
    class FakeProcessTool:
        def __init__(self) -> None:
            self.calls: list[dict[str, str]] = []

        async def run(self, **kwargs):
            self.calls.append(dict(kwargs))
            if kwargs["action"] == "list":
                return ToolResult(success=True, output="job-1 running")
            return ToolResult(success=True, output="Stopped job-1.")

    process_tool = FakeProcessTool()
    assert (
        await _run_repl(
            tmp_path,
            monkeypatch,
            iter(("/ps", "/jobs stop job-1", "exit")),
            tools={"background_process": process_tool},
        )
        == 0
    )
    assert process_tool.calls == [
        {"action": "list"},
        {"action": "stop", "job_id": "job-1"},
    ]
    output = capsys.readouterr().out
    assert "job-1 running" in output
    assert "Stopped job-1." in output


@pytest.mark.asyncio
async def test_settings_shows_runtime_terminal_and_safety_preferences(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys,
) -> None:
    assert (
        await _run_repl(
            tmp_path,
            monkeypatch,
            iter(("/settings", "exit")),
            safety_tier="auto_edit",
            active_model_id="openai/runtime-model",
            config_overrides={
                "model": "openai/configured-model",
                "fallback_models": ["groq/fallback"],
                "input_mode": "vi",
                "no_color": False,
                "reduced_motion": True,
                "show_token_meter": True,
                "notification_method": "osc9",
                "sandbox_backend": "native",
                "memory_backend": "sqlite",
                "safety_tier": "auto_edit",
            },
        )
        == 0
    )
    output = capsys.readouterr().out
    for expected in (
        "Model: openai/runtime-model",
        "Fallbacks: groq/fallback",
        "Permission mode: auto_edit",
        "Input: vi",
        "Interface: terminal-native",
        "Reduced motion: on",
        "Token meter: on",
        "Sandbox: native",
        "Memory: sqlite",
        "Run `ash config`",
    ):
        assert expected in output
