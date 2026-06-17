# tests/unit/test_terminal_ui.py
import pytest
from ui.terminal import TerminalUI
from pathlib import Path

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