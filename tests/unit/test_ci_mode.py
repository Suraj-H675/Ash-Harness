from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from ash.cli import main
from ash.config import AshConfig
from ash.logging import configure_logging, get_logger


def test_ci_mode_requires_noninteractive_work(capsys) -> None:
    assert main(["--ci"]) == 2
    assert "--ci requires" in capsys.readouterr().err


def test_ci_mode_does_not_intercept_subcommands() -> None:
    # Doctor may fail on credentials in an isolated test environment, but it must
    # run as a command instead of opening the REPL or setup wizard.
    assert main(["--ci", "doctor", "--json"]) in {0, 1}


def test_ci_mode_reconfigures_real_log_emission_without_ansi(
    monkeypatch: pytest.MonkeyPatch,
    capsys,
) -> None:
    async def no_checks(*, connect: bool):
        del connect
        return []

    monkeypatch.setattr("ash.commands.doctor.run_doctor", no_checks)
    configure_logging(no_color=False)
    assert main(["--ci", "doctor", "--json"]) == 0
    get_logger("ci-test").warning("plain warning")
    captured = capsys.readouterr()
    assert "\x1b[" not in captured.out
    assert "\x1b[" not in captured.err

    configure_logging(no_color=False)
    get_logger("color-test").warning("colored warning")
    assert "\x1b[" in capsys.readouterr().err


def test_false_ash_no_color_value_does_not_disable_color(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
    capsys,
) -> None:
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.setenv("ASH_NO_COLOR", "false")
    monkeypatch.setenv("ASH_DB_DIRECTORY", str(tmp_path / "db"))
    assert main(["storage", "check", "--json"]) == 1
    capsys.readouterr()
    get_logger("false-no-color-test").warning("color remains enabled")
    assert "\x1b[" in capsys.readouterr().err


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
