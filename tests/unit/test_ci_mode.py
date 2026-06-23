from __future__ import annotations

from ash.cli import main


def test_ci_mode_requires_noninteractive_work(capsys) -> None:
    assert main(["--ci"]) == 2
    assert "--ci requires" in capsys.readouterr().err


def test_ci_mode_does_not_intercept_subcommands() -> None:
    # Doctor may fail on credentials in an isolated test environment, but it must
    # run as a command instead of opening the REPL or setup wizard.
    assert main(["--ci", "doctor", "--json"]) in {0, 1}
