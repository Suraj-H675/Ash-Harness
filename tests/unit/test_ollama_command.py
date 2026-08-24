import asyncio

import pytest

from ash.commands.ollama import validate_ollama_model


def test_ollama_model_names_are_validated_without_shell_interpretation():
    assert validate_ollama_model(" qwen3-coder:7b ") == "qwen3-coder:7b"
    assert validate_ollama_model("library/model") == "library/model"

    for invalid in (
        "",
        "model; rm -rf /",
        "model && echo bad",
        "../escape",
        "a" * 200,
    ):
        with pytest.raises(ValueError, match="model"):
            validate_ollama_model(invalid)


def test_pull_requires_executable(monkeypatch):
    from ash.commands import ollama

    monkeypatch.setattr(ollama.shutil, "which", lambda name: None)

    assert asyncio.run(ollama.pull_model("test-model")) == 2
