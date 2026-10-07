# tests/unit/test_terminal_ui.py
import asyncio
import os
import re
from pathlib import Path

from ash.ui import terminal as terminal_module
from ash.ui.terminal import TerminalUI, _user_message_text
from ash.ui.safe_text import terminal_safe_text
from io import StringIO
import pytest
from prompt_toolkit.formatted_text import to_formatted_text
from prompt_toolkit.formatted_text.utils import fragment_list_to_text
from rich.console import Console
from rich.text import Text
from types import SimpleNamespace


@pytest.mark.skipif(os.name == "nt", reason="POSIX executable fixture")
def test_editor_launch_skips_workspace_shadowed_bare_editor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    host_bin = tmp_path / "host-bin"
    workspace.mkdir()
    host_bin.mkdir()
    for directory, marker in ((workspace, "workspace"), (host_bin, "host")):
        executable = directory / "editorprobe"
        executable.write_text(f"#!/bin/sh\necho {marker}\n", encoding="utf-8")
        executable.chmod(0o755)
    monkeypatch.setenv("PATH", f"{workspace}{os.pathsep}{host_bin}")

    command, _environment = terminal_module._editor_launch(
        "editorprobe --wait",
        workspace=workspace,
    )

    assert Path(command[0]).resolve() == (host_bin / "editorprobe").resolve()
    assert command[1:] == ["--wait"]


def test_editor_launch_scrubs_parent_credentials(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    editor = tmp_path / "editor"
    editor.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    editor.chmod(0o755)
    monkeypatch.setenv("OPENAI_API_KEY", "synthetic-openai-secret")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "synthetic-anthropic-secret")
    monkeypatch.setenv("DISPLAY", ":77")

    _command, environment = terminal_module._editor_launch(
        str(editor),
        workspace=tmp_path,
    )

    assert "OPENAI_API_KEY" not in environment
    assert "ANTHROPIC_API_KEY" not in environment
    assert environment["DISPLAY"] == ":77"
    assert "PATH" in environment
    assert "HOME" in environment


def test_editor_launch_allows_explicit_workspace_editor(tmp_path: Path) -> None:
    editor = tmp_path / "local-editor"
    editor.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    editor.chmod(0o755)

    command, _environment = terminal_module._editor_launch(
        "./local-editor --flag",
        workspace=tmp_path,
    )

    assert Path(command[0]).resolve() == editor.resolve()
    assert command[1:] == ["--flag"]


def test_edit_plan_passes_scrubbed_environment_to_editor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    editor = tmp_path / "editor"
    editor.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    editor.chmod(0o755)
    monkeypatch.setenv("EDITOR", str(editor))
    monkeypatch.delenv("VISUAL", raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "must-not-cross")
    monkeypatch.setattr(
        "ash.core.planner.render_sprint_markdown",
        lambda execution: "plan text\n",
    )
    observed: dict[str, object] = {}

    def fake_run(command, *, check, env):
        observed["command"] = command
        observed["check"] = check
        observed["env"] = env
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(terminal_module.subprocess, "run", fake_run)
    monkeypatch.setattr(
        "ash.core.planner.apply_sprint_markdown_edit",
        lambda execution, markdown: observed.update(markdown=markdown),
    )
    ui = TerminalUI(workspace_root=tmp_path)

    ui._edit_plan(SimpleNamespace())

    environment = observed["env"]
    assert isinstance(environment, dict)
    assert "OPENAI_API_KEY" not in environment
    assert observed["check"] is False
    assert observed["markdown"] == "plan text\n"


def test_terminal_ui_initializes_with_safety_tier():
    ui = TerminalUI(safety_tier="dry_run")
    assert ui.safety_tier == "dry_run"

    ui2 = TerminalUI(safety_tier="auto_approve")
    assert ui2.safety_tier == "auto_approve"


def test_single_line_terminal_text_escapes_unicode_line_separators() -> None:
    rendered = terminal_safe_text("left\u2028middle\u2029right", single_line=True)

    assert rendered == "left\\u2028middle\\u2029right"


def test_terminal_ui_supports_no_color_and_reduced_motion():
    ui = TerminalUI(no_color=True, reduced_motion=True, show_token_meter=True)
    assert ui.console.no_color is True
    assert ui.reduced_motion is True
    assert ui.show_token_meter is True


def test_terminal_ui_selects_theme_palette() -> None:
    dark = TerminalUI(theme="dark")
    light = TerminalUI(theme="light")

    assert dark.theme.name == "dark"
    assert light.theme.name == "light"
    assert light.theme.border_primary == "#005faf"


def test_screen_reader_mode_emits_linear_non_rewriting_output() -> None:
    output = StringIO()
    ui = TerminalUI(
        console=Console(file=output, force_terminal=False, width=80),
        screen_reader_mode=True,
        show_token_meter=True,
    )

    with ui.begin_turn():
        ui.print_thought("checking")
        ui.print_token("**done**")
    ui.finalize_turn()
    ui.show_tool_approval("write_file", {"file_path": "x.py"}, auto=False)

    rendered = output.getvalue()
    assert "Reasoning: checking" in rendered
    assert "done" in rendered
    assert "Approval:" in rendered
    assert "write_file" in rendered
    assert "\x1b" not in rendered
    assert "╭" not in rendered
    assert ui.reduced_motion is True
    assert ui.show_token_meter is False


def test_terminal_ui_renders_markdown_and_reasoning() -> None:
    output = StringIO()
    ui = TerminalUI(
        console=Console(file=output, force_terminal=False, width=80),
    )
    with ui.begin_turn():
        ui.print_thought("checking")
        ui.print_token("**bold**\n\n```python\nprint('ok')\n```")
    ui.finalize_turn()

    rendered = output.getvalue()
    assert "THINK  checking" in rendered
    assert "ASH" in rendered
    assert "bold" in rendered
    assert "print('ok')" in rendered
    assert "**bold**" not in rendered
    transcript = ui.transcript.snapshot()
    assert [(entry.kind, entry.finalized) for entry in transcript] == [
        ("reasoning", True),
        ("assistant", True),
    ]
    assert transcript[0].content == "checking"
    assert transcript[1].content.startswith("**bold**")


def test_terminal_ui_does_not_commit_empty_assistant_entry() -> None:
    ui = TerminalUI(console=Console(file=StringIO(), force_terminal=False))

    with ui.begin_turn():
        pass
    ui.finalize_turn()

    assert ui.transcript.snapshot() == ()


def test_prompt_thinking_view_does_not_read_committed_history(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ui = TerminalUI(console=Console(file=StringIO(), force_terminal=False, width=80))
    for index in range(1_000):
        ui.transcript.append("assistant", f"old answer {index}", title="ash")
    ui.bind_prompt_surface(lambda: None)
    with ui.begin_turn():
        ui.print_thought("current reasoning")

        def fail_snapshot():
            raise AssertionError("live rendering must not scan committed history")

        monkeypatch.setattr(ui.transcript, "snapshot", fail_snapshot)
        _revision, rendered = ui.prompt_thinking_view(80)
        dock_view = ui.prompt_dock_view()

    plain = fragment_list_to_text(to_formatted_text(rendered))
    assert "current reasoning" in plain
    assert dock_view.reasoning == "current reasoning"
    assert "old answer" not in plain


def test_prompt_surface_streams_to_app_owned_transcript_without_console_output() -> None:
    output = StringIO()
    ui = TerminalUI(console=Console(file=output, force_terminal=False, width=80))
    ui.bind_prompt_surface(lambda: None)
    ui.record_user_input("question")

    with ui.begin_turn():
        ui.print_thought("checking")
        ui.print_token("first ")
        ui.print_token("answer")
    ui.finalize_turn()
    ui.commit_completed_turn()
    rendered = output.getvalue()
    assert rendered == ""
    entries = ui.transcript.snapshot()
    assert [(entry.kind, entry.finalized) for entry in entries] == [
        ("user", True),
        ("reasoning", True),
        ("assistant", True),
    ]
    assert entries[-1].content == "first answer"
    assert "THINK  checking" in fragment_list_to_text(
        to_formatted_text(ui.prompt_thinking_view(80)[1])
    )

    ui.commit_completed_turn()
    assert output.getvalue() == rendered


def test_app_owned_conversation_keeps_gray_user_band_in_semantic_transcript() -> None:
    output = StringIO()
    ui = TerminalUI(
        console=Console(
            file=output,
            force_terminal=True,
            color_system="truecolor",
            no_color=False,
            width=80,
        )
    )
    ui.bind_prompt_surface(lambda: None)

    ui.record_user_input("hello from user")
    with ui.begin_turn():
        ui.print_thought("checking")
        ui.print_token("hello from ash")
    reasoning_text = fragment_list_to_text(
        to_formatted_text(ui.prompt_thinking_view(80)[1])
    )
    ui.finalize_turn()
    ui.commit_completed_turn()

    rendered = output.getvalue()
    assert rendered == ""
    entries = ui.transcript.snapshot()
    assert [entry.kind for entry in entries] == ["user", "reasoning", "assistant"]
    assert entries[0].content == "hello from user"
    assert entries[-1].content == "hello from ash"
    assert "THINK  " not in rendered
    assert "THINK  checking" in reasoning_text
    assert "╭" not in rendered
    assert "╰" not in rendered


def test_user_message_band_wraps_by_terminal_cells_with_composer_gray() -> None:
    console = Console(
        file=StringIO(),
        force_terminal=True,
        color_system="truecolor",
        no_color=False,
        width=12,
    )
    band = _user_message_text(
        "alpha🙂beta\nsecond",
        theme_name="dark",
        console=console,
        width=12,
    )

    rows = band.plain.splitlines()
    assert len(rows) == 3
    assert all(Text(row).cell_len == 12 for row in rows)
    assert rows[0].startswith("> alpha🙂be")
    assert rows[1].startswith("  a")
    assert rows[2].startswith("> second")
    console.print(band, soft_wrap=True, highlight=False)
    assert "48;2;48;48;48" in console.file.getvalue()


def test_user_band_uses_light_composer_gray() -> None:
    output = StringIO()
    console = Console(
        file=output,
        force_terminal=True,
        color_system="truecolor",
        no_color=False,
        width=24,
    )
    band = _user_message_text(
        "light text",
        theme_name="light",
        console=console,
        width=24,
    )
    console.print(band, soft_wrap=True, highlight=False)

    assert "48;2;234;234;234" in output.getvalue()
    assert "38;2;0;95;175" in output.getvalue()


def test_no_color_user_band_keeps_markers_without_invisible_fill() -> None:
    console = Console(file=StringIO(), force_terminal=False, no_color=True, width=24)
    band = _user_message_text(
        "plain text",
        theme_name="dark",
        console=console,
        width=24,
    )

    assert band.plain == "> plain text"


def test_live_user_band_is_preserved_for_the_app_owned_renderer() -> None:
    output = StringIO()
    console = Console(file=output, force_terminal=False, width=80)
    ui = TerminalUI(console=console)
    ui.record_user_input("🙂 this wraps at the live width")
    entry = ui.transcript.snapshot()[0]

    assert entry.kind == "user"
    assert entry.content == "🙂 this wraps at the live width"
    assert output.getvalue() == ""


def test_inter_turn_spacing_adds_one_blank_line_before_the_next_user() -> None:
    ui = TerminalUI(console=Console(file=StringIO(), force_terminal=False, width=40))
    ui.record_user_input("first question")
    with ui.begin_turn():
        ui.print_token("first answer")
    ui.finalize_turn()

    ui.record_user_input("second question")
    from ash.ui.transcript_view import TranscriptView

    view = TranscriptView(ui.transcript)
    content = view.create_content(40, 20)
    rows = [
        "".join(text for _style, text in content.get_line(index)).rstrip()
        for index in range(content.line_count)
    ]
    assert rows[:6] == [
        "> first question",
        "",
        "ASH",
        "· first answer",
        "",
        "> second question",
    ]
    view.close()


def test_prompt_surface_streams_long_response_once_without_preview_truncation() -> None:
    ui = TerminalUI(console=Console(file=StringIO(), force_terminal=False, width=80))
    response = "START-" + ("x" * 5_000) + "-END"

    with ui.begin_turn():
        for offset in range(0, len(response), 37):
            ui.print_token(response[offset : offset + 37])

    ui.finalize_turn()
    ui.commit_completed_turn()
    entry = ui.transcript.snapshot()[0]
    assert entry.content == response
    assert entry.finalized


def test_suspend_live_render_pauses_and_resumes_same_live_instance() -> None:
    ui = TerminalUI(console=Console(file=StringIO(), force_terminal=False))
    calls: list[tuple[str, bool | None]] = []

    class FakeLive:
        def stop(self) -> None:
            calls.append(("stop", None))

        def start(self, refresh: bool = False) -> None:
            calls.append(("start", refresh))

    live = FakeLive()
    ui._active_live = live  # type: ignore[assignment]

    with ui.suspend_live_render():
        assert calls == [("stop", None)]
        assert ui._active_live is live

    assert calls == [("stop", None), ("start", True)]


def test_inline_long_tool_activity_remains_visible_after_reasoning() -> None:
    output = StringIO()
    ui = TerminalUI(console=Console(file=output, force_terminal=False))

    with ui.begin_turn():
        ui.print_thought("I should inspect the repository first.")
        ui.emit_event(
            {
                "type": "tool.started",
                "tool": "read_file",
                "call_id": "c1",
            }
        )

        rendered = ui._render_active_turn()
        ui.console.print(rendered)

    assert "Inspecting…" in output.getvalue()


def test_inline_tool_lifecycle_uses_shared_semantic_label() -> None:
    output = StringIO()
    ui = TerminalUI(console=Console(file=output, force_terminal=False))

    ui.emit_event(
        {
            "type": "tool.completed",
            "tool": "read_file",
            "call_id": "c1",
            "success": True,
        }
    )

    assert "TOOL  read_file [completed]" in output.getvalue()


@pytest.mark.parametrize(
    "terminal_event", ["turn.completed", "turn.cancelled", "turn.error"]
)
def test_terminal_activity_clears_on_every_turn_terminal_event(
    terminal_event: str,
) -> None:
    ui = TerminalUI(console=Console(file=StringIO(), force_terminal=False))

    ui.emit_event(
        {
            "type": "provider.retrying",
            "attempt": 2,
            "max_attempts": 3,
        }
    )
    assert ui._activity_status == "Retrying…"

    ui.emit_event({"type": terminal_event})

    assert ui._activity_status == ""


@pytest.mark.parametrize(
    "terminal_event", ["turn.completed", "turn.cancelled", "turn.error"]
)
def test_terminal_events_hide_busy_and_reasoning_dock(terminal_event: str) -> None:
    ui = TerminalUI(console=Console(file=StringIO(), force_terminal=False))
    ui.bind_prompt_surface(lambda: None)
    ui.emit_event({"type": "turn.started"})
    ui.emit_event({"type": "model.request.started", "operation_id": "r1"})
    with ui.begin_turn():
        ui.print_thought("reasoning that must not look busy")
        assert ui.prompt_dock_view().activity == "Thinking"
        assert ui.prompt_dock_view().reasoning
        ui.emit_event({"type": terminal_event})

    view = ui.prompt_dock_view()
    assert view.activity is None
    assert view.reasoning == ""


def test_tool_output_preserves_activity_while_rendering_output() -> None:
    ui = TerminalUI(console=Console(file=StringIO(), force_terminal=False))

    ui.emit_event({"type": "tool.started", "tool": "run_command", "call_id": "c1"})
    assert ui._activity_status == "Running…"

    ui.emit_event(
        {
            "type": "tool.output",
            "tool": "run_command",
            "call_id": "c1",
            "stream": "stdout",
            "delta": "building…\n",
        }
    )

    entries = ui.transcript.snapshot()
    assert ui._activity_status == "Running…"
    assert [entry.content for entry in entries] == ["building…\n"]


def test_activity_dock_tracks_model_tools_retries_and_terminal_state() -> None:
    ui = TerminalUI(console=Console(file=StringIO(), force_terminal=False))
    ui.bind_prompt_surface(lambda: None)

    ui.emit_event({"type": "turn.started"})
    assert ui.prompt_dock_view().activity is None

    ui.emit_event({"type": "model.request.started", "operation_id": "request-1"})
    assert ui.prompt_dock_view().activity == "Thinking"
    ui.emit_event({"type": "assistant.delta", "text": "hello"})
    assert ui.prompt_dock_view().activity == "Responding"
    ui.emit_event({"type": "model.request.completed", "operation_id": "stale-request"})
    assert ui.prompt_dock_view().activity == "Responding"
    ui.emit_event({"type": "model.request.completed", "operation_id": "request-1"})
    assert ui.prompt_dock_view().activity is None

    ui.emit_event({"type": "tool.requested", "tool": "read_file"})
    assert ui.prompt_dock_view().activity is None
    ui.emit_event({"type": "tool.started", "tool": "read_file", "call_id": "read-1"})
    assert ui.prompt_dock_view().activity == "Inspecting"
    ui.emit_event({"type": "tool.started", "tool": "web_search", "call_id": "search-1"})
    assert ui.prompt_dock_view().activity == "Working"
    ui.emit_event(
        {
            "type": "tool.output",
            "tool": "web_search",
            "call_id": "search-1",
            "delta": "still working",
        }
    )
    assert ui.prompt_dock_view().activity == "Working"
    ui.emit_event(
        {
            "type": "tool.completed",
            "tool": "web_search",
            "call_id": "search-1",
            "success": True,
        }
    )
    assert ui.prompt_dock_view().activity == "Inspecting"
    ui.emit_event({"type": "tool.error", "tool": "read_file", "call_id": "read-1"})
    assert ui.prompt_dock_view().activity is None

    ui.emit_event({"type": "provider.retrying", "attempt": 2})
    assert ui.prompt_dock_view().activity == "Retrying"
    ui.emit_event({"type": "model.request.started", "operation_id": "request-2"})
    assert ui.prompt_dock_view().activity == "Thinking"
    with ui.begin_turn():
        ui.print_thought("bounded provider reasoning")
        assert ui.prompt_dock_view().reasoning == "bounded provider reasoning"
        ui.emit_event({"type": "turn.cancelled"})

    view = ui.prompt_dock_view()
    assert view.activity is None
    assert view.reasoning == ""
    ui.emit_event({"type": "tool.started", "tool": "run_command", "call_id": "late"})
    assert ui.prompt_dock_view().activity is None


@pytest.mark.parametrize(
    ("tool", "expected"),
    [
        ("read_file", "Inspecting"),
        ("search_text", "Inspecting"),
        ("web_search", "Researching"),
        ("browser_snapshot", "Browsing"),
        ("replace_file_content", "Coding"),
        ("run_command", "Running"),
        ("auto_commit", "Committing"),
        ("activate_skill", "Loading"),
        ("manage_automation", "Scheduling"),
        ("update_goal", "Planning"),
        ("ask_user", "Waiting"),
        ("brave", "Researching"),
        ("delegate_remote_agent", "Delegating"),
        ("mcp__untrusted_server__magic", "Working"),
    ],
)
def test_tool_activity_taxonomy_is_exact_and_safe(tool: str, expected: str) -> None:
    ui = TerminalUI(console=Console(file=StringIO(), force_terminal=False))
    ui.emit_event({"type": "tool.started", "tool": tool, "call_id": "call-1"})

    view = ui.prompt_dock_view()
    assert view.activity == expected
    assert tool not in view.activity


def test_reduced_motion_keeps_semantic_activity_invalidation() -> None:
    invalidations = 0

    def invalidate() -> None:
        nonlocal invalidations
        invalidations += 1

    ui = TerminalUI(
        console=Console(file=StringIO(), force_terminal=False),
        reduced_motion=True,
    )
    ui.bind_prompt_surface(invalidate)
    ui.emit_event({"type": "model.request.started", "operation_id": "r1"})

    assert invalidations == 1
    assert ui.prompt_dock_view().activity == "Thinking"


def test_multiple_tools_keep_only_terminal_lifecycle_rows() -> None:
    ui = TerminalUI(console=Console(file=StringIO(), force_terminal=False))

    for call_id, tool in (("c1", "read_file"), ("c2", "search_text")):
        ui.emit_event({"type": "tool.started", "tool": tool, "call_id": call_id})
        ui.emit_event(
            {
                "type": "tool.completed",
                "tool": tool,
                "call_id": call_id,
                "success": True,
            }
        )

    assert [entry.content for entry in ui.transcript.snapshot()] == [
        "read_file [completed]",
        "search_text [completed]",
    ]
    assert ui._activity_status == ""


def test_screen_reader_activity_is_linear_and_deduplicated() -> None:
    output = StringIO()
    ui = TerminalUI(
        console=Console(file=output, force_terminal=False, width=80),
        screen_reader_mode=True,
    )

    ui.emit_event({"type": "turn.started"})
    ui.emit_event(
        {
            "type": "model.request.started",
            "attempt": 1,
            "max_attempts": 3,
        }
    )
    ui.emit_event(
        {
            "type": "provider.retrying",
            "attempt": 2,
            "max_attempts": 3,
        }
    )
    ui.emit_event(
        {"type": "tool.started", "tool": "read_file", "call_id": "reader-1"}
    )
    ui.emit_event(
        {
            "type": "tool.output",
            "tool": "read_file",
            "call_id": "reader-1",
            "delta": "file bytes",
        }
    )

    rendered = output.getvalue()
    assert rendered.count("Status: Thinking") == 1
    assert "Status: Retrying" in rendered
    assert rendered.count("Status: Inspecting") == 1
    assert "Status: read_file" not in rendered


def test_terminal_ui_hydrates_bounded_durable_session_transcript() -> None:
    ui = TerminalUI(console=Console(file=StringIO(), force_terminal=False))
    session = SimpleNamespace(
        messages=[
            SimpleNamespace(role="system", content="hidden", metadata={}),
            SimpleNamespace(role="user", content="question", metadata={}),
            SimpleNamespace(role="assistant", content="answer", metadata={}),
            SimpleNamespace(
                role="tool",
                content="x" * 5000,
                metadata={"call_id": "c1"},
            ),
            SimpleNamespace(role="assistant", content="", metadata={}),
        ]
    )

    ui.load_session_transcript(session)

    entries = ui.transcript.snapshot()
    assert [entry.kind for entry in entries] == ["user", "assistant", "tool"]
    assert entries[0].content == "question"
    assert entries[1].content == "answer"
    assert len(entries[2].content) < 4100
    assert entries[2].metadata == {"call_id": "c1"}




@pytest.mark.asyncio
async def test_prompt_notice_expires_without_replacing_status_indefinitely(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    invalidations = 0

    def invalidate() -> None:
        nonlocal invalidations
        invalidations += 1

    monkeypatch.setattr(terminal_module, "PROMPT_NOTICE_SECONDS", 0.01)
    ui = TerminalUI(console=Console(file=StringIO(), force_terminal=False))
    ui.bind_prompt_surface(invalidate)

    ui.set_prompt_notice("Resumed chat.")
    assert ui.prompt_status_notice() == "Resumed chat."

    await asyncio.sleep(0.03)

    assert ui.prompt_status_notice() is None
    assert invalidations >= 2

def test_session_view_replacement_drops_old_and_ephemeral_transcript_entries() -> None:
    from ash.ui.transcript_view import TranscriptView

    ui = TerminalUI(console=Console(file=StringIO(), force_terminal=False))
    ui.bind_prompt_surface(lambda: None)
    ui.transcript.append("user", "old session question", title="you")
    ui.transcript.append("status", "old transient output", title="output")
    view = TranscriptView(ui.transcript)
    ui.set_prompt_notice("Forked branch")
    assert ui.prompt_status_notice() == "Forked branch"

    selected = SimpleNamespace(
        resident_message_offset=3,
        messages=[
            SimpleNamespace(role="user", content="selected question", metadata={}),
            SimpleNamespace(role="assistant", content="selected answer", metadata={}),
        ],
    )
    ui.load_session_transcript(selected)
    selected_entries = ui.transcript.snapshot()
    assert [entry.content for entry in selected_entries] == [
        "Earlier session history is outside the resident snapshot: 3 message(s).",
        "selected question",
        "selected answer",
    ]
    assert all("old " not in entry.content for entry in selected_entries)
    assert view.follow_latest is True
    assert ui.prompt_status_notice() is None

    ui.set_prompt_notice("Rewound chat")
    ui.record_user_input("continue here")
    assert ui.prompt_status_notice() is None

    ui.load_session_transcript(None)
    empty = view.create_content(40, 8)
    visible = "\n".join(
        "".join(text for _style, text in empty.get_line(index))
        for index in range(empty.line_count)
    )
    assert ui.transcript.snapshot() == ()
    assert "Ready" in visible
    assert "selected" not in visible
    view.close()


def test_inline_resume_renders_bounded_recent_conversation() -> None:
    output = StringIO()
    ui = TerminalUI(
        console=Console(file=output, force_terminal=False, width=80),
    )
    session = SimpleNamespace(
        messages=[
            SimpleNamespace(
                role="user",
                content=f"question {index}",
                metadata={},
            )
            for index in range(14)
        ]
        + [
            SimpleNamespace(
                role="assistant",
                content="latest answer",
                metadata={},
            )
        ]
    )

    ui.load_session_transcript(session)

    rendered = output.getvalue()
    assert "Recent conversation" not in rendered
    assert "… 3 earlier entries omitted" in rendered
    assert "question 0" not in rendered
    assert re.search(r"> question 3 *\n\n> question 4", rendered)
    assert "YOU" not in rendered
    assert "\nASH\n· latest answer" in rendered
    assert "╭" not in rendered
    assert "╰" not in rendered


def test_terminal_ui_dry_run_denies_all():
    ui = TerminalUI(safety_tier="dry_run")
    approved = ui.request_tool_approval("write_file", {"file_path": "x"})
    assert approved is False


def test_terminal_ui_auto_approve_allows_all():
    ui = TerminalUI(safety_tier="auto_approve")
    approved = ui.request_tool_approval("write_file", {"file_path": "x"})
    assert approved is True


def test_terminal_ui_approval_redacts_signed_url_arguments() -> None:
    output = StringIO()
    marker = "approval-signature-marker"
    ui = TerminalUI(
        safety_tier="auto_approve",
        console=Console(file=output, force_terminal=False, width=120),
    )

    approved = ui.request_tool_approval(
        "browser_navigate",
        {
            "url": (
                "https://storage.example/object?"
                f"X-Amz-Signature={marker}&view=complete"
            )
        },
    )

    rendered = output.getvalue()
    assert approved is True
    assert marker not in rendered
    assert "[REDACTED]" in rendered
    assert "view=complete" in rendered


def test_terminal_ui_can_approve_tool_for_session():
    stream = StringIO("a\n")
    ui = TerminalUI(safety_tier="interactive", input_stream=stream)
    assert ui.request_tool_approval("write_file", {"file_path": "x"}) is True
    assert ui.request_tool_approval("write_file", {"file_path": "y"}) is True
    assert stream.tell() == 2


def test_terminal_ui_builds_workspace_edit_preview(tmp_path):
    target = tmp_path / "example.txt"
    target.write_text("old\n")
    ui = TerminalUI(workspace_root=tmp_path)
    preview = ui._edit_preview(
        "whole_edit", {"file_path": "example.txt", "content": "new\n"}
    )
    assert "-old" in preview
    assert "+new" in preview


def test_side_by_side_approval_preview_is_bounded_and_labeled(tmp_path):
    target = tmp_path / "example.txt"
    target.write_text("\n".join(f"old {index}" for index in range(250)) + "\n")
    ui = TerminalUI(workspace_root=tmp_path)

    preview = ui._edit_preview(
        "whole_edit",
        {
            "file_path": "example.txt",
            "content": "\n".join(
                f"new {index}" if index % 2 == 0 else f"old {index}"
                for index in range(250)
            )
            + "\n",
        },
        side_by_side=True,
    )
    lines = preview.splitlines()

    assert lines[0].startswith("--- a/example.txt")
    assert lines[1].startswith("+++ b/example.txt")
    assert " | " in preview
    assert any(line.startswith("- ") and " | + " in line for line in lines[2:])
    assert len(lines) <= 201
    assert lines[-1] == "[diff preview truncated]"

    ui.show_tool_approval(
        "whole_edit",
        {"file_path": "example.txt", "content": "new"},
        auto=False,
        diff_mode="side-by-side",
    )
    approval = ui.transcript.snapshot()[-1]
    assert approval.kind == "approval"
    assert "Diff preview (side-by-side):" in approval.content
    assert "old 0" in approval.content
    assert " | " in approval.content


def test_side_by_side_rich_diff_styles_each_changed_half() -> None:
    body = Text()

    terminal_module._append_styled_diff(
        body,
        "- old                                  | + new",
        theme_name="dark",
        side_by_side=True,
    )

    styled = [
        (str(span.style), body.plain[span.start : span.end]) for span in body.spans
    ]
    assert ("#c8c8c8 on #6b252e", "- old                                 ") in styled
    assert ("#c8c8c8 on #14532d", "+ new") in styled


def test_terminal_ui_does_not_read_oversized_existing_file(tmp_path, monkeypatch):
    target = tmp_path / "large.txt"
    target.write_text("x" * 33)
    monkeypatch.setattr("ash.ui.terminal.MAX_EDIT_PREVIEW_FILE_BYTES", 32)
    ui = TerminalUI(workspace_root=tmp_path)

    preview = ui._edit_preview(
        "whole_edit", {"file_path": "large.txt", "content": "replacement"}
    )

    assert preview == "[preview unavailable: existing file exceeds 32 bytes]"


def test_terminal_ui_does_not_follow_symlinked_existing_file(tmp_path):
    target = tmp_path / "target.txt"
    target.write_text("private content", encoding="utf-8")
    link = tmp_path / "link.txt"
    try:
        link.symlink_to(target)
    except OSError:
        pytest.skip("symlinks are unavailable")
    ui = TerminalUI(workspace_root=tmp_path)

    preview = ui._edit_preview(
        "whole_edit", {"file_path": "link.txt", "content": "replacement"}
    )

    assert preview == "[preview unavailable: existing file is not readable text]"


def test_terminal_ui_bounds_inline_diff_inputs():
    ui = TerminalUI()
    huge = "line\n" * 1_000

    preview = ui._edit_preview(
        "replace_file_content",
        {"target_content": huge, "replacement_content": "replacement\n"},
    )

    assert preview.endswith("[diff preview truncated]")
    assert len(preview.splitlines()) <= 201


def test_show_tool_approval_rejects_unknown_diff_mode():
    ui = TerminalUI(safety_tier="dry_run")
    with pytest.raises(ValueError, match="diff_mode"):
        ui.show_tool_approval("whole_edit", {}, auto=False, diff_mode="split")


def test_terminal_ui_renders_tool_lifecycle_without_arguments() -> None:
    output = StringIO()
    ui = TerminalUI(console=Console(file=output, force_terminal=False))
    ui.emit_event(
        {
            "type": "tool.completed",
            "tool": "read_file",
            "success": True,
            "arguments": {"file_path": "secret"},
        }
    )
    assert "TOOL  read_file [completed]" in output.getvalue()
    assert "secret" not in output.getvalue()
    assert ui.transcript.snapshot()[-1].content == "read_file [completed]"
    assert "arguments" not in (ui.transcript.snapshot()[-1].metadata or {})


def test_terminal_ui_streams_and_finalizes_command_output() -> None:
    output = StringIO()
    ui = TerminalUI(console=Console(file=output, force_terminal=False))

    ui.emit_event({"type": "tool.started", "tool": "run_command", "call_id": "c1"})
    ui.emit_event(
        {
            "type": "tool.output",
            "tool": "run_command",
            "call_id": "c1",
            "stream": "stdout",
            "delta": "one\n",
        }
    )
    ui.emit_event(
        {
            "type": "tool.output",
            "tool": "run_command",
            "call_id": "c1",
            "stream": "stderr",
            "delta": "warning\n",
        }
    )
    ui.emit_event(
        {
            "type": "tool.completed",
            "tool": "run_command",
            "call_id": "c1",
            "success": True,
        }
    )

    entries = ui.transcript.snapshot()
    streamed = next(entry for entry in entries if entry.title == "run_command output")
    assert streamed.content == "one\nwarning\n"
    assert streamed.finalized is True
    assert "one" in output.getvalue()
    assert "warning" in output.getvalue()


def test_terminal_ui_neutralizes_terminal_controls_in_untrusted_output() -> None:
    output = StringIO()
    ui = TerminalUI(
        console=Console(
            file=output,
            force_terminal=True,
            color_system="standard",
            width=80,
        )
    )
    malicious = "safe\x1b[2Jafter\x1b]52;c;SEVMTE8=\x07done\n"

    ui.emit_event(
        {
            "type": "tool.output",
            "tool": "run_command",
            "call_id": "c1",
            "stream": "stdout",
            "delta": malicious,
        }
    )
    rendered = output.getvalue()

    assert "\x1b[2J" not in rendered
    assert "\x1b]52;" not in rendered
    assert "\x07" not in rendered
    assert r"\x1b[2J" in rendered
    assert r"\x1b]52;c;SEVMTE8=\x07" in rendered
    assert ui.transcript.snapshot()[0].content == (
        r"safe\x1b[2Jafter\x1b]52;c;SEVMTE8=\x07done" + "\n"
    )


def test_terminal_ui_neutralizes_controls_in_live_assistant_and_tool_output() -> None:
    output = StringIO()
    ui = TerminalUI(
        console=Console(
            file=output,
            force_terminal=True,
            color_system="standard",
            width=80,
        )
    )

    with ui.begin_turn():
        ui.print_token("answer\x1b[2J")
        ui.print_thought("reason\x1b]0;title\x07")
        ui.emit_event(
            {
                "type": "tool.output",
                "tool": "run_command",
                "call_id": "c1",
                "stream": "stdout",
                "delta": "tool\x1b[3J",
            }
        )
    ui.finalize_turn()

    rendered = output.getvalue()
    assert "\x1b[2J" not in rendered
    assert "\x1b]0;" not in rendered
    assert "\x1b[3J" not in rendered
    assert all("\x1b" not in entry.content for entry in ui.transcript.snapshot())


def test_terminal_ui_renders_bidi_and_single_line_identifier_controls() -> None:
    output = StringIO()
    ui = TerminalUI(
        console=Console(
            file=output,
            force_terminal=True,
            color_system="standard",
            width=80,
        )
    )

    ui.emit_event(
        {
            "type": "tool.completed",
            "tool": "remote\nname\u202ehidden\u202c",
            "call_id": "call-1",
            "success": True,
        }
    )

    rendered = output.getvalue()
    assert "remote\\x0aname\\u202ehidden\\u202c" in rendered
    assert "remote\nname" not in rendered
    assert "\u202e" not in rendered
    assert "\u202c" not in rendered


def test_terminal_ui_sanitizes_approval_labels_and_arguments() -> None:
    output = StringIO()
    ui = TerminalUI(
        console=Console(file=output, force_terminal=True, color_system="standard")
    )

    ui.show_tool_approval(
        "[bold red]tool\nname\u202e",
        {"arg\tname": "value\x1b[2J\u202e"},
        auto=False,
    )

    rendered = output.getvalue()
    assert "\x1b[2J" not in rendered
    assert "\u202e" not in rendered
    assert "tool\\x0aname\\u202e" in rendered
    assert "arg\\x09name" in rendered
    assert "value\\x1b[2J\\u202e" in rendered


def test_terminal_ui_status_does_not_interpret_rich_markup() -> None:
    output = StringIO()
    ui = TerminalUI(
        console=Console(
            file=output,
            force_terminal=True,
            color_system="standard",
            width=80,
        )
    )

    ui.write_status("[link=https://evil.test]click[/link]")

    rendered = output.getvalue()
    assert "\x1b]8;" not in rendered
    assert "[link=https://evil.test]click[/link]" in rendered


def test_mcp_sampling_review_never_inherits_auto_approve() -> None:
    output = StringIO()
    ui = TerminalUI(
        safety_tier="auto_approve",
        input_stream=StringIO("n\n"),
        console=Console(file=output, force_terminal=False, width=100),
    )

    approved = ui.review_mcp_sampling(
        "docs\x1b[31m",
        "request",
        {
            "maxTokens": 100,
            "messages": [
                {"role": "user", "content": [{"type": "text", "text": "hello"}]}
            ],
        },
    )

    assert approved is False
    rendered = output.getvalue()
    assert "docs" in rendered
    assert "MCP sampling request" in rendered
    assert "\x1b[31m" not in rendered


def test_mcp_sampling_review_displays_entire_bounded_payload() -> None:
    output = StringIO()
    ui = TerminalUI(
        input_stream=StringIO("n\n"),
        console=Console(file=output, force_terminal=False, width=120),
    )
    end_marker = "END_OF_REVIEW_PAYLOAD"
    payload = {"response": "x" * 13_000 + end_marker}

    approved = ui.review_mcp_sampling("docs", "response", payload)

    assert approved is False
    assert end_marker in output.getvalue()


def test_mcp_form_elicitation_collects_reviews_and_submits_typed_values() -> None:
    output = StringIO()
    ui = TerminalUI(
        input_stream=StringIO("Suraj\n3\ny\ny\n"),
        console=Console(file=output, force_terminal=False, width=100),
    )
    schema = {
        "type": "object",
        "properties": {
            "name": {"type": "string", "title": "Name"},
            "count": {"type": "integer", "title": "Count"},
            "confirm": {"type": "boolean", "title": "Confirm"},
        },
        "required": ["name", "count", "confirm"],
        "additionalProperties": False,
    }

    response = ui.request_mcp_elicitation("docs", "Provide values", schema)

    assert response == {
        "action": "accept",
        "content": {"name": "Suraj", "count": 3, "confirm": True},
    }
    rendered = output.getvalue()
    assert "Provide values" in rendered
    assert "Review MCP form response" in rendered


def test_mcp_form_elicitation_can_edit_before_submit() -> None:
    ui = TerminalUI(
        input_stream=StringIO("first\ne\nsecond\ny\n"),
        console=Console(file=StringIO(), force_terminal=False),
    )
    schema = {
        "type": "object",
        "properties": {"value": {"type": "string"}},
        "required": ["value"],
        "additionalProperties": False,
    }

    response = ui.request_mcp_elicitation("docs", "Value", schema)

    assert response == {"action": "accept", "content": {"value": "second"}}
