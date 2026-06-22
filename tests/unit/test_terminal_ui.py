# tests/unit/test_terminal_ui.py
from ui.terminal import TerminalUI
from io import StringIO


def test_terminal_ui_initializes_with_safety_tier():
    ui = TerminalUI(safety_tier="dry_run")
    assert ui.safety_tier == "dry_run"

    ui2 = TerminalUI(safety_tier="auto_approve")
    assert ui2.safety_tier == "auto_approve"


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
