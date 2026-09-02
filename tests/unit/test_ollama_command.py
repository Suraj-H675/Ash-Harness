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


def test_pull_drains_noisy_output_in_bounded_chunks(monkeypatch, capsys):
    from ash.commands import ollama

    class FakeStdout:
        def __init__(self):
            self.read_sizes = []
            self.calls = 0

        async def read(self, size):
            self.read_sizes.append(size)
            self.calls += 1
            return b"x" * 100_000 if self.calls == 1 else b""

    class FakeProcess:
        def __init__(self):
            self.stdout = FakeStdout()
            self.returncode = 0

        async def wait(self):
            return self.returncode

    process = FakeProcess()

    async def spawn(*args, **kwargs):
        return process

    monkeypatch.setattr(ollama.shutil, "which", lambda name: "/usr/bin/ollama")
    monkeypatch.setattr(ollama.asyncio, "create_subprocess_exec", spawn)

    assert asyncio.run(ollama.pull_model("test-model")) == 0
    assert process.stdout.read_sizes == [4096, 4096]
    output = capsys.readouterr().out
    assert output.startswith("x" * ollama.MAX_PULL_OUTPUT_CHARS)
    assert "Pulled test-model." in output


def test_pull_reports_spawn_failure_without_traceback(monkeypatch, capsys):
    from ash.commands import ollama

    async def spawn(*args, **kwargs):
        raise PermissionError("permission denied")

    monkeypatch.setattr(ollama.shutil, "which", lambda name: "/usr/bin/ollama")
    monkeypatch.setattr(ollama.asyncio, "create_subprocess_exec", spawn)

    assert asyncio.run(ollama.pull_model("test-model")) == 2
    assert "could not start ollama pull" in capsys.readouterr().err
