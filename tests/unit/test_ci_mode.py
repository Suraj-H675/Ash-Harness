from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from ash.cli import main
from ash.config import AshConfig


def test_ci_mode_requires_noninteractive_work(capsys) -> None:
    assert main(["--ci"]) == 2
    assert "--ci requires" in capsys.readouterr().err


def test_ci_mode_does_not_intercept_subcommands() -> None:
    # Doctor may fail on credentials in an isolated test environment, but it must
    # run as a command instead of opening the REPL or setup wizard.
    assert main(["--ci", "doctor", "--json"]) in {0, 1}


def test_empty_interactive_start_runs_setup_by_default_and_refuses_broken_repl(
    tmp_path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    config = AshConfig(model="anthropic/test-model", workspace_root=tmp_path)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setattr("ash.cli._load_config_or_report", lambda **_: (config, 0))
    monkeypatch.setattr("ash.safety.trust.is_workspace_trusted", lambda _: True)
    monkeypatch.setattr("ash.commands.setup.cmd_setup", lambda _: 0)
    monkeypatch.setattr("builtins.input", lambda _: "")
    terminal_input = MagicMock()
    terminal_input.isatty.return_value = True
    monkeypatch.setattr("ash.cli.sys.stdin", terminal_input)

    assert main([]) == 2

    captured = capsys.readouterr()
    assert "Ash is not configured yet" in captured.out
    assert "still not configured" in captured.err
