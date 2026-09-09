from __future__ import annotations

import builtins
from pathlib import Path
from types import SimpleNamespace

import pytest

from ash.cli import (
    PluginReloadResult,
    _repl,
    _mcp_reload_message,
    _print_mcp_reload_errors,
)
from ash.plugins.skills import (
    ActivateSkillTool,
    ListSkillsTool,
    ReadSkillResourceTool,
    SkillCatalog,
)
from ash.safety.guard import SafetyGuard


def test_targetless_mcp_reload_message_reflects_errors_and_preservation() -> None:
    assert _mcp_reload_message(
        PluginReloadResult("summary", {}, False)
    ) == "MCP configuration reloaded."
    assert _mcp_reload_message(
        PluginReloadResult("summary", {"broken": "connection failed"}, False)
    ) == "MCP configuration reloaded with errors."
    assert _mcp_reload_message(
        PluginReloadResult("summary", {"broken": "connection failed"}, True)
    ) == "MCP configuration reload failed; the previous runtime was preserved."


def test_mcp_reload_errors_are_redacted_and_bounded(capsys) -> None:
    marker = "synthetic diagnostic marker"
    _print_mcp_reload_errors(
        {"broken": f'password="{marker}" ' + "x" * 600}
    )

    rendered = capsys.readouterr().err
    assert marker not in rendered
    assert "broken: password=\"[REDACTED]\"" in rendered
    assert len(rendered.split(": ", 1)[1].rstrip("\n")) == 512


def test_mcp_reload_error_server_names_are_redacted(capsys) -> None:
    marker = "synthetic server-name marker"

    _print_mcp_reload_errors(
        {f'password="{marker}"': "connection failed"}
    )

    rendered = capsys.readouterr().err
    assert marker not in rendered
    assert 'password="[REDACTED]"' in rendered


@pytest.mark.asyncio
async def test_repl_reports_targetless_reload_errors_and_redacts_cancel_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys,
) -> None:
    marker = "synthetic repl diagnostic marker"
    commands = iter(("/mcp refresh", "/mcp cancel server task", "/exit"))

    class FakeTerminalUI:
        transcript = None

        def __init__(self, *args, **kwargs) -> None:
            self.viewport_mode = False

        def write_status(self, text: str, *, error: bool = False) -> None:
            builtins.print(text, end="", file=__import__("sys").stderr if error else None)

    class FakePromptInput:
        interactive = False
        uses_viewport = False

        def __init__(self, *args, **kwargs) -> None:
            pass

        async def read(self, prompt: str) -> str:
            del prompt
            return next(commands)

        def set_extra_commands(self, commands: list[str]) -> None:
            del commands

    class FakeStatusLine:
        def __init__(self, *args, **kwargs) -> None:
            pass

    class FakeNotifier:
        def __init__(self, *args, **kwargs) -> None:
            pass

    class FakeTurnController:
        def __init__(self, *args, **kwargs) -> None:
            pass

    class FakePrinter:
        def __init__(self, *args, **kwargs) -> None:
            pass

        def __call__(self, *values, sep=" ", end="\n", file=None, flush=False):
            builtins.print(*values, sep=sep, end=end, file=file, flush=flush)

    monkeypatch.setattr("ash.ui.terminal.TerminalUI", FakeTerminalUI)
    monkeypatch.setattr("ash.ui.prompt.PromptInput", FakePromptInput)
    monkeypatch.setattr("ash.ui.status.StatusLine", FakeStatusLine)
    monkeypatch.setattr("ash.ui.notifications.TerminalNotifier", FakeNotifier)
    monkeypatch.setattr(
        "ash.ui.turn_input.InteractiveTurnController", FakeTurnController
    )
    monkeypatch.setattr("ash.ui.output.ReplPrinter", FakePrinter)
    monkeypatch.setattr("ash.safety.trust.is_workspace_trusted", lambda root: False)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))

    class FakeRuntime:
        async def cancel_task(self, server: str, task_id: str) -> dict:
            raise RuntimeError(f'upstream password="{marker}"')

    runtime = FakeRuntime()
    reload_calls: list[dict] = []
    guard = SafetyGuard(tmp_path)
    skill_catalog = SkillCatalog(())
    loop = SimpleNamespace(
        ui=FakeTerminalUI(),
        project_root=tmp_path,
        repo_map=None,
        _mcp_runtime=runtime,
        _mcp_configs={},
        safety_guard=guard,
        tools={
            "list_skills": ListSkillsTool(guard, skill_catalog),
            "activate_skill": ActivateSkillTool(guard, skill_catalog),
            "read_skill_resource": ReadSkillResourceTool(guard, skill_catalog),
        },
        current_session=SimpleNamespace(session_id="session"),
        _emit_event=lambda event: None,
    )

    async def reload_mcp_servers(configs: dict) -> dict[str, str]:
        reload_calls.append(configs)
        return {
            f'password="{marker}"': f'upstream password="{marker}" ' + "x" * 700
        }

    async def reload_plugin_runtime_tools(tools) -> None:
        del tools

    loop.reload_mcp_servers = reload_mcp_servers
    loop.reload_plugin_runtime_tools = reload_plugin_runtime_tools

    config = SimpleNamespace(
        input_mode="emacs",
        keybindings={},
        tui_mode="inline",
        theme="default",
        screen_reader_mode=False,
        notification_method="off",
        notification_events=(),
        notification_include_preview=False,
        sandbox_backend="auto",
        sandbox_docker_image="ash-sandbox:latest",
        allow_unsafe_plugin_runtime=False,
    )

    assert await _repl(loop, config, SimpleNamespace()) == 0

    captured = capsys.readouterr()
    assert reload_calls == [{}]
    assert "MCP configuration reload failed; the previous runtime was preserved." in captured.out
    assert "MCP configuration reloaded." not in captured.out
    assert marker not in captured.out
    assert marker not in captured.err
    assert 'password="[REDACTED]": upstream password="[REDACTED]"' in captured.err
    assert 'Error: upstream password="[REDACTED]"' in captured.err
    assert 'password="[REDACTED]"' in captured.err
