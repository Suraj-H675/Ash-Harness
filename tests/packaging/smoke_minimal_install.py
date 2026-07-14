"""Cross-platform smoke checks for an installed minimal Ash wheel."""

from __future__ import annotations

import asyncio
import importlib.util
import io
import json
import os
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
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
    assert {"croniter", "referencing", "tzdata"} <= base_names
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
        "ash/automation/models.py",
        "ash/automation/maintenance.py",
        "ash/automation/runner.py",
        "ash/automation/schedules.py",
        "ash/automation/store.py",
        "ash/automation/worker.py",
        "ash/commands/automation.py",
        "ash/commands/lsp.py",
        "ash/lsp/client.py",
        "ash/lsp/config.py",
        "ash/lsp/manager.py",
        "ash/lsp/middleware.py",
        "ash/mcp/schema_worker.py",
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
    from ash.core.loop import AshLoop
    from ash.core.session import SessionStore
    from ash.lsp.client import LSPClient
    from ash.lsp.config import load_lsp_server_configs
    from ash.lsp.manager import LanguageServerManager
    from ash.mcp.runtime import MCPTool
    from ash.repo.repomap import calculate_personalized_pagerank
    from ash.providers.base import ProviderABC, StreamChunk
    from ash.providers.capabilities import ProviderCapabilities
    from ash.safety.guard import SafetyGuard
    from ash.sdk import AshClient
    from ash.tools.lsp import LSPTool
    from ash.tools.symbols import FindSymbolTool
    from ash.ui.turn_input import InteractiveTurnController
    from ash.ui.headless import HeadlessUI

    assert Path(ash.__file__).resolve().is_relative_to(Path(sys.prefix).resolve())
    assert calculate_personalized_pagerank([[0, 1], [1, 0]], [0])
    assert AshClient and FindSymbolTool and InteractiveTurnController
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
        env.update(
            {
                "HOME": str(home),
                "USERPROFILE": str(home),
                "OLLAMA_API_BASE": "http://127.0.0.1:1",
            }
        )

        class InstalledNativeProvider(ProviderABC):
            model_name = "installed-native-smoke"
            _ash_declared_capabilities = ProviderCapabilities(native_tools=True)

            def count_tokens(self, text: str) -> int:
                return len(text)

            async def stream_chat(self, messages, temperature=0.0, tools=None):
                yield StreamChunk(
                    content=(
                        "comparison: 1 < 2; literal "
                        '<call_tool name="never_execute"></call_tool>'
                    ),
                    is_done=True,
                    stop_reason="stop",
                )

        async def verify_native_protocol() -> None:
            loop = AshLoop(
                SessionStore(root / "native-protocol.db"),
                InstalledNativeProvider(),
                SafetyGuard(project_root=workspace),
                HeadlessUI(output_format="text", stream=io.StringIO()),
                workspace,
            )
            try:
                result = await loop.run_turn("preserve native text")
                assert result == (
                    "comparison: 1 < 2; literal "
                    '<call_tool name="never_execute"></call_tool>'
                )
            finally:
                await loop.aclose()

        asyncio.run(verify_native_protocol())

        class InstalledMCPClient:
            def __init__(self) -> None:
                self.calls = 0

            async def call_tool(
                self, name: str, arguments: dict[str, object]
            ) -> dict[str, object]:
                self.calls += 1
                assert name == "inspect"
                assert arguments == {"mode": "safe"}
                return {
                    "content": [{"type": "text", "text": "value: 2"}],
                    "structuredContent": {"value": 2},
                    "_meta": {"source": "installed-wheel"},
                }

        async def verify_mcp_boundary() -> None:
            input_schema = {
                "type": "object",
                "properties": {"mode": {"enum": ["safe"]}},
                "required": ["mode"],
                "additionalProperties": False,
            }
            client = InstalledMCPClient()
            tool = MCPTool(
                SafetyGuard(project_root=workspace),
                client=client,  # type: ignore[arg-type]
                server_name="wheel",
                definition={
                    "name": "inspect",
                    "description": "Inspect the installed MCP boundary.",
                    "inputSchema": input_schema,
                    "outputSchema": {
                        "type": "object",
                        "properties": {"value": {"type": "integer"}},
                        "required": ["value"],
                        "additionalProperties": False,
                    },
                },
            )
            assert tool.json_schema() == input_schema
            invalid = await tool.run(mode="unsafe")
            assert invalid.success is False
            assert client.calls == 0
            valid = await tool.run(mode="safe")
            assert valid.success is True
            envelope = json.loads(valid.output)
            assert envelope["structuredContent"] == {"value": 2}
            assert envelope["_meta"] == {"source": "installed-wheel"}
            assert client.calls == 1

        asyncio.run(verify_mcp_boundary())

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

        created_automation = json.loads(
            run_ash(
                "cron",
                "add",
                "wheel review",
                "--prompt",
                "Review installed automation",
                "--every",
                "1h",
                "--json",
                cwd=workspace,
                env=env,
            ).stdout
        )
        assert created_automation["schedule"]["kind"] == "every"
        named_timezone = json.loads(
            run_ash(
                "cron",
                "add",
                "wheel weekday",
                "--prompt",
                "Review the weekday build",
                "--cron",
                "30 9 * * mon-fri",
                "--timezone",
                "Asia/Kolkata",
                "--json",
                cwd=workspace,
                env=env,
            ).stdout
        )
        assert named_timezone["schedule"]["timezone"] == "Asia/Kolkata"

        due_at = datetime.now(timezone.utc) + timedelta(seconds=3)
        due = json.loads(
            run_ash(
                "cron",
                "add",
                "wheel due failure",
                "--prompt",
                "Exercise the installed automation runtime",
                "--at",
                due_at.isoformat(),
                "--timeout",
                "8",
                "--json",
                cwd=workspace,
                env=env,
            ).stdout
        )
        time.sleep(max(0.0, due_at.timestamp() - time.time()) + 0.2)
        automation_status = json.loads(
            run_ash("cron", "status", "--json", cwd=workspace, env=env).stdout
        )
        assert automation_status["active_jobs"] == 3
        assert automation_status["workers"] == []
        worker_once = json.loads(
            run_ash(
                "cron",
                "worker",
                "--once",
                "--json",
                cwd=workspace,
                env=env,
                expected=1,
            ).stdout
        )
        assert worker_once == {
            "cancelled": 0,
            "completed": 1,
            "failed": 1,
            "interrupted": 0,
            "ok": False,
            "skipped": 0,
            "stopped": False,
            "succeeded": 0,
        }
        history = json.loads(
            run_ash(
                "cron",
                "history",
                due["job_id"],
                "--json",
                cwd=workspace,
                env=env,
            ).stdout
        )
        assert len(history) == 1
        assert history[0]["status"] == "failed"
        assert history[0]["finished_at"] is not None

        doctor = json.loads(
            run_ash("doctor", "--json", cwd=workspace, env=env).stdout
        )
        automation_check = next(
            check for check in doctor["checks"] if check["name"] == "automation"
        )
        assert automation_check["status"] == "warn"
        assert "no live worker" in automation_check["message"]

        server_env = dict(env)
        server_env["ASH_SERVER_TOKEN"] = "0123456789abcdef"
        server = run_ash("serve", cwd=workspace, env=server_env, expected=2)
        assert "ash-ai[server]" in server.stderr

    print("minimal installed-wheel smoke passed")


if __name__ == "__main__":
    main()
