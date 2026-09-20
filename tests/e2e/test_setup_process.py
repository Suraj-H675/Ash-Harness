from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]
PROVIDER_ENV_KEYS = (
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_API_BASE",
    "OPENAI_API_KEY",
    "OPENAI_API_BASE",
    "GOOGLE_API_KEY",
    "GEMINI_API_KEY",
    "GOOGLE_API_BASE",
    "OPENROUTER_API_KEY",
    "OPENROUTER_API_BASE",
    "DEEPSEEK_API_KEY",
    "DEEPSEEK_API_BASE",
    "GROQ_API_KEY",
    "GROQ_API_BASE",
    "MISTRAL_API_KEY",
    "MISTRAL_API_BASE",
    "XAI_API_KEY",
    "XAI_API_BASE",
    "TOGETHER_API_KEY",
    "TOGETHER_API_BASE",
    "FIREWORKS_API_KEY",
    "FIREWORKS_API_BASE",
    "CEREBRAS_API_KEY",
    "CEREBRAS_API_BASE",
    "NVIDIA_API_KEY",
    "NVIDIA_API_BASE",
    "OLLAMA_API_BASE",
    "LMSTUDIO_API_BASE",
    "VLLM_API_BASE",
)


def _setup_process(
    tmp_path: Path, extra_env: dict[str, str]
) -> subprocess.CompletedProcess[str]:
    environment = os.environ.copy()
    for key in (*PROVIDER_ENV_KEYS, "ASH_MODEL", "ASH_PROVIDER"):
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
            {"ASH_MODEL": "anthropic/test-model", "ANTHROPIC_API_KEY": "test-key"},
            "anthropic/test-model",
        ),
        (
            {"ASH_MODEL": "openai/test-model", "OPENAI_API_KEY": "test-key"},
            "openai/test-model",
        ),
        (
            {"ASH_MODEL": "google/gemini-test", "GOOGLE_API_KEY": "test-key"},
            "google/gemini-test",
        ),
        (
            {"ASH_MODEL": "google/gemini-alias-test", "GEMINI_API_KEY": "test-key"},
            "google/gemini-alias-test",
        ),
        (
            {"ASH_MODEL": "openrouter/vendor/test-model", "OPENROUTER_API_KEY": "test-key"},
            "openrouter/vendor/test-model",
        ),
        (
            {"ASH_MODEL": "deepseek/test-model", "DEEPSEEK_API_KEY": "test-key"},
            "deepseek/test-model",
        ),
        (
            {"ASH_MODEL": "groq/test-model", "GROQ_API_KEY": "test-key"},
            "groq/test-model",
        ),
        (
            {"ASH_MODEL": "mistral/test-model", "MISTRAL_API_KEY": "test-key"},
            "mistral/test-model",
        ),
        (
            {"ASH_MODEL": "xai/test-model", "XAI_API_KEY": "test-key"},
            "xai/test-model",
        ),
        (
            {"ASH_MODEL": "together/vendor/test-model", "TOGETHER_API_KEY": "test-key"},
            "together/vendor/test-model",
        ),
        (
            {
                "ASH_MODEL": "fireworks/accounts/test/models/test-model",
                "FIREWORKS_API_KEY": "test-key",
            },
            "fireworks/accounts/test/models/test-model",
        ),
        (
            {"ASH_MODEL": "cerebras/test-model", "CEREBRAS_API_KEY": "test-key"},
            "cerebras/test-model",
        ),
        (
            {"ASH_MODEL": "nvidia/vendor/test-model", "NVIDIA_API_KEY": "test-key"},
            "nvidia/vendor/test-model",
        ),
        ({"ASH_MODEL": "ollama/local-model"}, "ollama/local-model"),
        ({"ASH_MODEL": "lmstudio/local-model"}, "lmstudio/local-model"),
        ({"ASH_MODEL": "vllm/local-model"}, "vllm/local-model"),
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
