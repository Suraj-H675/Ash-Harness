"""Cross-platform smoke checks for an installed minimal Ash wheel."""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
from importlib.metadata import distribution
from pathlib import Path

from packaging.requirements import Requirement


OPTIONAL_DISTRIBUTIONS = {"fastapi", "numpy", "uvicorn", "websockets"}


def run_ash(
    *arguments: str,
    cwd: Path,
    env: dict[str, str],
    expected: int = 0,
) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        [sys.executable, "-m", "ash", *arguments],
        cwd=cwd,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == expected, (
        arguments,
        result.returncode,
        result.stdout,
        result.stderr,
    )
    assert "Traceback (most recent call last)" not in result.stderr
    return result


def assert_distribution_metadata() -> None:
    installed = distribution("ash")
    requirements = [Requirement(value) for value in installed.requires or ()]
    base_names = {
        requirement.name.casefold()
        for requirement in requirements
        if requirement.marker is None
    }
    assert not (OPTIONAL_DISTRIBUTIONS & base_names)
    marked = {
        requirement.name.casefold(): str(requirement.marker)
        for requirement in requirements
    }
    assert 'extra == "server"' in marked["fastapi"]
    assert 'extra == "server"' in marked["uvicorn"]
    assert 'extra == "vector"' in marked["chromadb"]
    assert 'extra == "vector"' in marked["onnxruntime"]

    packaged = {str(path).replace("\\", "/") for path in installed.files or ()}
    assert not any(path.startswith("project/") for path in packaged)
    assert not any(path.startswith("tests/") for path in packaged)


def main() -> None:
    import ash
    from repo.repomap import calculate_personalized_pagerank
    from tools.symbols import FindSymbolTool
    from ui.turn_input import InteractiveTurnController

    assert Path(ash.__file__).resolve().is_relative_to(Path(sys.prefix).resolve())
    assert calculate_personalized_pagerank([[0, 1], [1, 0]], [0])
    assert FindSymbolTool and InteractiveTurnController
    assert_distribution_metadata()
    for module in OPTIONAL_DISTRIBUTIONS:
        assert importlib.util.find_spec(module) is None, module

    with tempfile.TemporaryDirectory(prefix="ash-wheel-smoke-") as temporary:
        root = Path(temporary)
        home = root / "home"
        workspace = root / "workspace"
        home.mkdir()
        workspace.mkdir()
        (workspace / ".git").mkdir()
        (workspace / ".git" / "HEAD").write_text(
            "ref: refs/heads/main\n", encoding="utf-8"
        )
        (workspace / ".ash").mkdir()
        (workspace / ".ash" / "config.toml").write_text(
            'model = "ollama/wheel-smoke"\nmax_context_tokens = 8192\n',
            encoding="utf-8",
        )
        env = {
            key: value
            for key, value in os.environ.items()
            if not key.startswith("ASH_")
            and key
            not in {
                "ANTHROPIC_API_KEY",
                "DEEPSEEK_API_KEY",
                "GROQ_API_KEY",
                "OPENAI_API_KEY",
            }
        }
        env.update({"HOME": str(home), "USERPROFILE": str(home)})

        version = run_ash("--version", cwd=workspace, env=env)
        assert version.stdout.strip().startswith("ash ")

        setup = run_ash(
            "setup",
            "--non-interactive",
            cwd=workspace,
            env=env,
            expected=2,
        )
        assert "requires an interactive terminal" in setup.stderr

        run_ash("trust", "add", str(workspace), cwd=workspace, env=env)
        explained = run_ash(
            "config",
            "explain",
            "--json",
            cwd=workspace,
            env=env,
        )
        entries = {
            item["field"]: item for item in json.loads(explained.stdout)["config"]
        }
        assert entries["model"]["value"] == "ollama/wheel-smoke"
        assert entries["model"]["source"] == "project"
        assert entries["workspace_root"]["value"] == str(workspace.resolve())

        server_env = dict(env)
        server_env["ASH_SERVER_TOKEN"] = "0123456789abcdef"
        server = run_ash("serve", cwd=workspace, env=server_env, expected=2)
        assert "ash[server]" in server.stderr

    print("minimal installed-wheel smoke passed")


if __name__ == "__main__":
    main()
