from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]
PROVIDER_KEYS = (
    "ANTHROPIC_API_KEY",
    "OPENAI_API_KEY",
    "DEEPSEEK_API_KEY",
    "GROQ_API_KEY",
)


def _setup_process(
    tmp_path: Path, extra_env: dict[str, str]
) -> subprocess.CompletedProcess[str]:
    environment = os.environ.copy()
    for key in (*PROVIDER_KEYS, "ASH_MODEL", "ASH_PROVIDER"):
        environment.pop(key, None)
    environment.update(
        {
            "HOME": str(tmp_path / "home"),
            "USERPROFILE": str(tmp_path / "home"),
            "PYTHONPATH": os.pathsep.join(
                filter(None, (str(ROOT), environment.get("PYTHONPATH", "")))
            ),
            **extra_env,
        }
    )
    return subprocess.run(
        [sys.executable, "-m", "ash", "setup", "--non-interactive"],
        cwd=tmp_path,
        env=environment,
        text=True,
        capture_output=True,
        timeout=15,
        check=False,
    )


@pytest.mark.parametrize(
    "environment, expected_model",
    [
        (
            {"ASH_MODEL": "openai/test-model", "OPENAI_API_KEY": "test-key"},
            "openai/test-model",
        ),
        ({"ASH_MODEL": "ollama/local-model"}, "ollama/local-model"),
    ],
)
def test_noninteractive_setup_accepts_preconfigured_fresh_process(
    tmp_path: Path,
    environment: dict[str, str],
    expected_model: str,
) -> None:
    result = _setup_process(tmp_path, environment)

    assert result.returncode == 0, result.stderr
    assert f"Ash is configured for {expected_model}." in result.stdout
    assert "doctor --connect" in result.stdout


def test_noninteractive_setup_fails_cleanly_without_credentials(tmp_path: Path) -> None:
    result = _setup_process(tmp_path, {})

    assert result.returncode == 2
    assert result.stdout == ""
    assert "requires an interactive terminal" in result.stderr
    assert "Set ASH_MODEL" in result.stderr
    assert "Traceback" not in result.stderr
