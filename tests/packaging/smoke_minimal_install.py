"""Cross-platform smoke checks for an installed minimal Ash wheel."""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
from importlib.resources import files
from importlib.metadata import distribution
from pathlib import Path

from packaging.requirements import Requirement


OPTIONAL_DISTRIBUTIONS = {
    "fastapi",
    "numpy",
    "playwright",
    "uvicorn",
    "websockets",
}


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
    installed = distribution("ash-ai")
    requirements = [Requirement(value) for value in installed.requires or ()]
    base_names = {
        requirement.name.casefold()
        for requirement in requirements
        if requirement.marker is None
    }
    assert not (OPTIONAL_DISTRIBUTIONS & base_names)
    marked: dict[str, list[str]] = {}
    for requirement in requirements:
        marked.setdefault(requirement.name.casefold(), []).append(
            str(requirement.marker)
        )
    assert any('extra == "server"' in marker for marker in marked["fastapi"])
    assert any('extra == "server"' in marker for marker in marked["uvicorn"])
    assert any('extra == "vector"' in marker for marker in marked["chromadb"])
    assert any('extra == "vector"' in marker for marker in marked["onnxruntime"])
    assert any('extra == "browser"' in marker for marker in marked["playwright"])

    packaged = {str(path).replace("\\", "/") for path in installed.files or ()}
    assert {
        "ash/commands/lsp.py",
        "ash/lsp/client.py",
        "ash/lsp/config.py",
        "ash/lsp/manager.py",
        "ash/lsp/middleware.py",
        "ash/sandbox/Dockerfile",
        "ash/tools/lsp.py",
    } <= packaged
    assert (installed.read_text("top_level.txt") or "").splitlines() == ["ash"]
    assert not any(path.startswith("project/") for path in packaged)
    assert not any(path.startswith("tests/") for path in packaged)


def main() -> None:
    import ash
    from ash.agents.shared_state import SharedState
    from ash.agents.subprocess_agent import SubprocessAgent, make_simple_text_task
    from ash.lsp.client import LSPClient
    from ash.lsp.config import load_lsp_server_configs
    from ash.lsp.manager import LanguageServerManager
    from ash.repo.repomap import calculate_personalized_pagerank
    from ash.tools.lsp import LSPTool
    from ash.tools.symbols import FindSymbolTool
    from ash.ui.turn_input import InteractiveTurnController

    assert Path(ash.__file__).resolve().is_relative_to(Path(sys.prefix).resolve())
    assert calculate_personalized_pagerank([[0, 1], [1, 0]], [0])
    assert FindSymbolTool and InteractiveTurnController
    assert LSPClient and LanguageServerManager and load_lsp_server_configs and LSPTool
    assert files("ash.sandbox").joinpath("Dockerfile").is_file()
    assert_distribution_metadata()
    for module in OPTIONAL_DISTRIBUTIONS:
        assert importlib.util.find_spec(module) is None, module
    for module in ("agents", "cli", "context", "providers", "sandbox", "tools"):
        assert importlib.util.find_spec(module) is None, module

    console_script = Path(sys.executable).with_name(
        "ash.exe" if os.name == "nt" else "ash"
    )
    console_version = subprocess.run(
        [str(console_script), "--version"],
        text=True,
        capture_output=True,
        check=False,
    )
    assert console_version.returncode == 0, console_version.stderr
    assert console_version.stdout.strip().startswith("ash ")

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

        shared_state = SharedState(root / "agents.db")
        try:
            agent = SubprocessAgent(
                agent_id="wheel-agent",
                role="general",
                task="installed wheel smoke",
                shared_state=shared_state,
                runner=make_simple_text_task("unused by child"),
            )
            process = agent.spawn_subprocess()
            stdout, stderr = process.communicate(timeout=15)
            assert process.returncode == 0, stderr or stdout
            status = shared_state.get_status("wheel-agent")
            assert status is not None and status.status == "completed"
        finally:
            shared_state.close()

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

        untrusted_lsp = json.loads(
            run_ash("lsp", "status", "--json", cwd=workspace, env=env).stdout
        )
        assert untrusted_lsp["enabled"] is True
        assert untrusted_lsp["trusted"] is False
        assert untrusted_lsp["servers"] == []

        lsp_config = home / ".ash" / "lsp.json"
        lsp_config.parent.mkdir(parents=True, exist_ok=True)
        lsp_config.write_text(
            json.dumps(
                {
                    "servers": {
                        "wheel-fake": {
                            "command": [sys.executable, "-c", "raise SystemExit(0)"],
                            "extensions": {".fake": "fake"},
                        }
                    }
                }
            ),
            encoding="utf-8",
        )

        run_ash("trust", "add", str(workspace), cwd=workspace, env=env)
        trusted_lsp = json.loads(
            run_ash("lsp", "status", "--json", cwd=workspace, env=env).stdout
        )
        assert trusted_lsp["trusted"] is True
        assert any(server["name"] == "wheel-fake" for server in trusted_lsp["servers"])
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
        assert entries["lsp_enabled"]["value"] is True

        server_env = dict(env)
        server_env["ASH_SERVER_TOKEN"] = "0123456789abcdef"
        server = run_ash("serve", cwd=workspace, env=server_env, expected=2)
        assert "ash[server]" in server.stderr

    print("minimal installed-wheel smoke passed")


if __name__ == "__main__":
    main()
