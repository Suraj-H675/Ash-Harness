# tests/unit/test_terminal_ui.py
from ui.terminal import TerminalUI
from io import StringIO
from rich.console import Console
from types import SimpleNamespace


def test_terminal_ui_initializes_with_safety_tier():
    ui = TerminalUI(safety_tier="dry_run")
    assert ui.safety_tier == "dry_run"

    ui2 = TerminalUI(safety_tier="auto_approve")
    assert ui2.safety_tier == "auto_approve"


def test_terminal_ui_supports_no_color_and_reduced_motion():
    ui = TerminalUI(no_color=True, reduced_motion=True, show_token_meter=True)
    assert ui.console.no_color is True
    assert ui.reduced_motion is True
    assert ui.show_token_meter is True


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
    assert "reasoning: checking" in rendered
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


def test_terminal_ui_viewport_mode_uses_transcript_without_live_output() -> None:
    output = StringIO()
    ui = TerminalUI(console=Console(file=output, force_terminal=False))
    ui.viewport_mode = True

    with ui.begin_turn():
        ui.print_token("visible in viewport")
    ui.finalize_turn()
    ui.write_status("queued")

    assert output.getvalue() == ""
    assert [entry.content for entry in ui.transcript.snapshot()] == [
        "visible in viewport",
        "queued",
    ]


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


def test_terminal_ui_dry_run_denies_all():
    ui = TerminalUI(safety_tier="dry_run")
    approved = ui.request_tool_approval("write_file", {"file_path": "x"})
    assert approved is False


def test_terminal_ui_auto_approve_allows_all():
    ui = TerminalUI(safety_tier="auto_approve")
    approved = ui.request_tool_approval("write_file", {"file_path": "x"})
    assert approved is True


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
    assert "tool read_file [completed]" in output.getvalue()
    assert "secret" not in output.getvalue()
    assert ui.transcript.snapshot()[-1].content == "read_file [completed]"
    assert "arguments" not in (ui.transcript.snapshot()[-1].metadata or {})
