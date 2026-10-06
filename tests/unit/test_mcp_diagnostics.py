from __future__ import annotations

import builtins
from pathlib import Path
from types import SimpleNamespace

import pytest

from ash.cli import (
    PluginReloadResult,
    _mcp_task_cancel_message,
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
from ash.core.session import SessionStore
from ash.safety.guard import SafetyGuard
from ash.safety.policy import PermissionPolicy


def _install_fake_repl_frontend(
    monkeypatch: pytest.MonkeyPatch,
    commands,
    *,
    turn_inputs: list[str] | None = None,
):
    class FakeTerminalUI:
        transcript = None

        def __init__(self, *args, **kwargs) -> None:
            self.viewport_mode = False

        def write_status(self, text: str, *, error: bool = False) -> None:
            builtins.print(text, end="", file=__import__("sys").stderr if error else None)

        def load_session_transcript(self, session) -> None:
            del session

    class FakePromptInput:
        interactive = False
        uses_viewport = False
        supports_full_screen_ui = False

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

        def __call__(self) -> str:
            return ""

        def header(self) -> str:
            return ""

        def footer(self) -> str:
            return ""

    class FakeNotifier:
        def __init__(self, *args, **kwargs) -> None:
            pass

    class FakeTurnController:
        def __init__(self, *args, **kwargs) -> None:
            pass

        async def run(self, user_input: str, *, user_metadata=None) -> str:
            del user_metadata
            if turn_inputs is not None:
                turn_inputs.append(user_input)
            return "ok"

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
    return FakeTerminalUI


@pytest.mark.asyncio
async def test_repl_preserves_normal_prompt_indentation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    submitted = "    preserve this indentation    "
    commands = iter((submitted, "exit"))
    turns: list[str] = []
    FakeTerminalUI = _install_fake_repl_frontend(
        monkeypatch,
        commands,
        turn_inputs=turns,
    )
    monkeypatch.setattr("ash.safety.trust.is_workspace_trusted", lambda root: False)

    async def prepare_prompt(prompt: str, *args, **kwargs):
        del args, kwargs
        return _prepared_prompt(prompt)

    monkeypatch.setattr(
        "ash.commands.attachments.prepare_extended_mentions",
        prepare_prompt,
    )
    monkeypatch.setenv("HOME", str(tmp_path / "home"))

    loop = SimpleNamespace(
        ui=FakeTerminalUI(),
        project_root=tmp_path,
        repo_map=None,
        _mcp_runtime=None,
        _mcp_configs={},
        safety_guard=SafetyGuard(tmp_path),
        tools={},
        current_session=None,
        current_goal=None,
        provider=SimpleNamespace(
            capabilities=SimpleNamespace(vision=False),
            count_tokens=lambda text: len(text),
        ),
        permission_policy=PermissionPolicy("interactive"),
        safety_tier="interactive",
    )
    config = SimpleNamespace(
        custom_providers={},
        input_mode="emacs",
        keybindings={},
        tui_mode="inline",
        tui_mouse=True,
        theme="dark",
        no_color=True,
        screen_reader_mode=False,
        notification_method="off",
        notification_events=(),
        notification_include_preview=False,
        sandbox_backend="auto",
        sandbox_docker_image="ash-sandbox:latest",
        sandbox_docker_memory_mb=4096,
        sandbox_docker_cpus=2.0,
        allow_unsafe_plugin_runtime=False,
        allow_unsafe_auto_approve=False,
        safety_tier="interactive",
        attachment_token_budget=1024,
    )

    assert await _repl(loop, config, SimpleNamespace()) == 0
    assert turns == [submitted]


@pytest.mark.asyncio
async def test_repl_screen_reader_resume_uses_linear_session_list(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys,
) -> None:
    commands = iter(("/resume", "exit"))
    FakeTerminalUI = _install_fake_repl_frontend(monkeypatch, commands)
    monkeypatch.setattr("ash.safety.trust.is_workspace_trusted", lambda root: False)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))

    session_store = SessionStore(tmp_path / "sessions.db")
    session = session_store.create_session(str(tmp_path), model="ollama/test-model")
    session_store.rename_session(session.session_id, "Accessible Session")

    async def forbidden_picker(*args, **kwargs):
        del args, kwargs
        raise AssertionError("screen-reader mode must not open the session picker")

    monkeypatch.setattr("ash.commands.sessions.pick_session", forbidden_picker)

    loop = SimpleNamespace(
        ui=FakeTerminalUI(),
        project_root=tmp_path,
        repo_map=None,
        _mcp_runtime=None,
        _mcp_configs={},
        safety_guard=SafetyGuard(tmp_path),
        tools={},
        current_session=None,
        current_goal=None,
        session_store=session_store,
        provider=SimpleNamespace(
            capabilities=SimpleNamespace(vision=False),
            count_tokens=lambda text: len(text),
        ),
        permission_policy=PermissionPolicy("interactive"),
        safety_tier="interactive",
    )
    config = SimpleNamespace(
        custom_providers={},
        input_mode="emacs",
        keybindings={},
        tui_mode="inline",
        tui_mouse=True,
        theme="dark",
        no_color=True,
        screen_reader_mode=True,
        notification_method="off",
        notification_events=(),
        notification_include_preview=False,
        sandbox_backend="auto",
        sandbox_docker_image="ash-sandbox:latest",
        sandbox_docker_memory_mb=4096,
        sandbox_docker_cpus=2.0,
        allow_unsafe_plugin_runtime=False,
        allow_unsafe_auto_approve=False,
        safety_tier="interactive",
        attachment_token_budget=1024,
    )

    assert await _repl(loop, config, SimpleNamespace()) == 0
    output = capsys.readouterr().out
    assert session.session_id in output
    assert "Accessible Session" in output
    assert "Use /resume <session-id-or-title>." in output


@pytest.mark.asyncio
async def test_repl_status_uses_active_model_and_models_rejects_extra_arguments(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys,
) -> None:
    commands = iter(
        (
            "/status",
            "/models nonsense",
            "/status nonsense",
            "/recovery nonsense",
            "/cancel nonsense",
            "/new nonsense",
            "/undo nonsense",
            "/compact nonsense",
            "/reload-plugins nonsense",
            "/hooks nonsense",
            "/commands nonsense",
            "/sandbox nonsense",
            "/doctor nonsense",
            "/context nonsense",
            "/memory index-workspace nope",
            "/memory index-workspace 0",
            "/exit nonsense",
            "exit",
        )
    )
    FakeTerminalUI = _install_fake_repl_frontend(monkeypatch, commands)
    monkeypatch.setattr("ash.safety.trust.is_workspace_trusted", lambda root: False)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))

    loop = SimpleNamespace(
        ui=FakeTerminalUI(),
        project_root=tmp_path,
        repo_map=None,
        _mcp_runtime=None,
        _mcp_configs={},
        safety_guard=SafetyGuard(tmp_path),
        tools={},
        current_session=None,
        current_goal=None,
        active_model_id="groq/fallback-model",
        provider=SimpleNamespace(
            capabilities=SimpleNamespace(
                native_tools=True,
                vision=False,
                reasoning=True,
                local=False,
            ),
            count_tokens=lambda text: len(text),
        ),
        provider_circuit_breaker=SimpleNamespace(
            snapshot=lambda _key: {
                "open": False,
                "retry_after": 0.0,
                "failures": 0,
            }
        ),
        _provider_circuit_key="failover:openai/primary,groq/fallback-model",
        permission_policy=PermissionPolicy("interactive"),
        safety_tier="interactive",
        recovered_turns=0,
        recovery_summary=None,
        _config=None,
        _memory_pipeline=None,
    )
    config = SimpleNamespace(
        model="openai/primary",
        fallback_models=[],
        custom_providers={},
        workspace_root=tmp_path,
        input_mode="emacs",
        keybindings={},
        tui_mode="inline",
        tui_mouse=True,
        theme="dark",
        no_color=True,
        screen_reader_mode=False,
        notification_method="off",
        notification_events=(),
        notification_include_preview=False,
        sandbox_backend="auto",
        sandbox_docker_image="ash-sandbox:latest",
        sandbox_docker_memory_mb=4096,
        sandbox_docker_cpus=2.0,
        allow_unsafe_plugin_runtime=False,
        allow_unsafe_auto_approve=False,
        safety_tier="interactive",
        attachment_token_budget=1024,
    )

    assert await _repl(loop, config, SimpleNamespace()) == 0
    captured = capsys.readouterr()
    assert "Model: groq/fallback-model" in captured.out
    assert "Configured route: openai/primary" in captured.out
    assert "Usage: /models [--refresh]" in captured.err
    for usage in (
        "/status",
        "/recovery",
        "/cancel",
        "/new",
        "/undo",
        "/compact",
        "/reload-plugins",
        "/hooks",
        "/commands",
        "/sandbox",
        "/doctor",
        "/context [--provenance]",
        "/exit",
    ):
        assert f"Usage: {usage}" in captured.err
    assert "memory index-workspace LIMIT must be an integer" in captured.err
    assert "memory index-workspace LIMIT must be between 1 and 10000" in captured.err


def _prepared_prompt(prompt: str):
    return SimpleNamespace(prompt=prompt, message_metadata=lambda: None)


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


def test_mcp_task_cancel_message_distinguishes_modern_ack_from_legacy_status() -> None:
    assert _mcp_task_cancel_message(
        {"server": "modern", "taskId": "task-1", "acknowledged": True}
    ) == "modern: task-1 cancellation acknowledged"
    assert _mcp_task_cancel_message(
        {
            "server": "legacy",
            "taskId": "task-2",
            "status": "cancelled",
            "statusMessage": "stopped",
        }
    ) == "legacy: task-2 cancelled: stopped"


@pytest.mark.asyncio
async def test_repl_permission_mode_audit_records_actual_previous_mode(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    commands = iter(("/permissions plan", "/exit"))
    FakeTerminalUI = _install_fake_repl_frontend(monkeypatch, commands)
    monkeypatch.setattr("ash.safety.trust.is_workspace_trusted", lambda root: False)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))

    store = SessionStore(tmp_path / "sessions.db")
    session = store.create_session(str(tmp_path), model="test")
    loop = SimpleNamespace(
        ui=FakeTerminalUI(),
        project_root=tmp_path,
        repo_map=None,
        _mcp_runtime=None,
        _mcp_configs={},
        safety_guard=SafetyGuard(tmp_path),
        tools={},
        current_session=session,
        permission_policy=PermissionPolicy("interactive"),
        safety_tier="interactive",
        session_store=store,
        notify_permission_rules_changed=lambda **kwargs: None,
        _emit_event=lambda event: None,
    )
    config = SimpleNamespace(
        custom_providers={},
        input_mode="emacs",
        keybindings={},
        tui_mode="inline",
        tui_mouse=True,
        theme="default",
        no_color=False,
        screen_reader_mode=False,
        notification_method="off",
        notification_events=(),
        notification_include_preview=False,
        sandbox_backend="auto",
        sandbox_docker_image="ash-sandbox:latest",
        sandbox_docker_memory_mb=4096,
        sandbox_docker_cpus=2.0,
        allow_unsafe_plugin_runtime=False,
        allow_unsafe_auto_approve=False,
        safety_tier="interactive",
    )

    assert await _repl(loop, config, SimpleNamespace()) == 0

    audit = store.list_audit_logs(session.session_id)
    mode_events = [item for item in audit if item.action_type == "permission_mode"]
    assert len(mode_events) == 1
    assert mode_events[0].details == {
        "previous_mode": "interactive",
        "mode": "plan",
    }
    assert loop.permission_policy.mode.value == "plan"
    assert config.safety_tier == "plan"


@pytest.mark.asyncio
async def test_repl_reports_targetless_reload_errors_and_redacts_cancel_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys,
) -> None:
    marker = "synthetic repl diagnostic marker"
    commands = iter(
        (
            "/mcp watch server file:///watched.txt",
            "/mcp watches",
            "/mcp unwatch server file:///watched.txt",
            "/mcp refresh",
            "/mcp cancel server task",
            "/exit",
        )
    )

    FakeTerminalUI = _install_fake_repl_frontend(monkeypatch, commands)
    monkeypatch.setattr("ash.safety.trust.is_workspace_trusted", lambda root: False)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))

    class FakeRuntime:
        def __init__(self) -> None:
            self.watches: list[dict[str, str]] = []

        async def watch_resource(self, server: str, uri: str) -> None:
            self.watches.append({"server": server, "uri": uri})

        async def unwatch_resource(self, server: str, uri: str) -> None:
            self.watches = [
                watch
                for watch in self.watches
                if watch != {"server": server, "uri": uri}
            ]

        def resource_watches(self, server: str | None = None) -> list[dict[str, str]]:
            if server is None:
                return list(self.watches)
            return [watch for watch in self.watches if watch["server"] == server]

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
        custom_providers={},
        input_mode="emacs",
        keybindings={},
        tui_mode="inline",
        tui_mouse=True,
        theme="default",
        no_color=False,
        screen_reader_mode=False,
        notification_method="off",
        notification_events=(),
        notification_include_preview=False,
        sandbox_backend="auto",
        sandbox_docker_image="ash-sandbox:latest",
        sandbox_docker_memory_mb=4096,
        sandbox_docker_cpus=2.0,
        allow_unsafe_plugin_runtime=False,
    )

    assert await _repl(loop, config, SimpleNamespace()) == 0

    captured = capsys.readouterr()
    assert reload_calls == [{}]
    assert "MCP configuration reload failed; the previous runtime was preserved." in captured.out
    assert "MCP configuration reloaded." not in captured.out
    assert "server: watching file:///watched.txt" in captured.out
    assert "server: file:///watched.txt" in captured.out
    assert "server: stopped watching file:///watched.txt" in captured.out
    assert runtime.watches == []
    assert marker not in captured.out
    assert marker not in captured.err
    assert 'password="[REDACTED]": upstream password="[REDACTED]"' in captured.err
    assert 'Error: upstream password="[REDACTED]"' in captured.err
    assert 'password="[REDACTED]"' in captured.err


@pytest.mark.asyncio
async def test_repl_plugin_reload_failure_preserves_simple_live_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys,
) -> None:
    commands = iter(("/reload-plugins", "/exit"))
    FakeTerminalUI = _install_fake_repl_frontend(monkeypatch, commands)
    monkeypatch.setattr("ash.safety.trust.is_workspace_trusted", lambda root: False)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))

    guard = SafetyGuard(tmp_path)
    old_catalog = SkillCatalog(())
    list_skills = ListSkillsTool(guard, old_catalog)
    activate_skill = ActivateSkillTool(guard, old_catalog)
    read_skill = ReadSkillResourceTool(guard, old_catalog)
    old_hooks = object()
    loop = SimpleNamespace(
        ui=FakeTerminalUI(),
        project_root=tmp_path,
        repo_map=None,
        _mcp_runtime=None,
        _mcp_configs={},
        safety_guard=guard,
        tools={
            "list_skills": list_skills,
            "activate_skill": activate_skill,
            "read_skill_resource": read_skill,
        },
        hooks=old_hooks,
        current_session=SimpleNamespace(session_id="session"),
        _emit_event=lambda event: None,
    )

    async def fail_plugin_tool_reload(tools) -> None:
        del tools
        raise RuntimeError("injected plugin tool reload failure")

    async def unexpected_mcp_reload(configs):
        del configs
        pytest.fail("MCP reload must not run after plugin-tool reload failure")

    loop.reload_plugin_runtime_tools = fail_plugin_tool_reload
    loop.reload_mcp_servers = unexpected_mcp_reload

    config = SimpleNamespace(
        custom_providers={},
        input_mode="emacs",
        keybindings={},
        tui_mode="inline",
        tui_mouse=True,
        theme="default",
        no_color=False,
        screen_reader_mode=False,
        notification_method="off",
        notification_events=(),
        notification_include_preview=False,
        sandbox_backend="auto",
        sandbox_docker_image="ash-sandbox:latest",
        sandbox_docker_memory_mb=4096,
        sandbox_docker_cpus=2.0,
        allow_unsafe_plugin_runtime=False,
    )

    assert await _repl(loop, config, SimpleNamespace()) == 0

    captured = capsys.readouterr()
    assert "injected plugin tool reload failure" in captured.err
    assert list_skills.catalog is old_catalog
    assert activate_skill.catalog is old_catalog
    assert read_skill.catalog is old_catalog
    assert loop.hooks is old_hooks


@pytest.mark.asyncio
async def test_repl_plugin_reload_refuses_replaced_workspace_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys,
) -> None:
    commands = iter(("/reload-plugins", "/exit"))
    FakeTerminalUI = _install_fake_repl_frontend(monkeypatch, commands)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setattr("ash.safety.trust.is_workspace_trusted", lambda root: True)

    workspace = tmp_path / "workspace"
    saved = tmp_path / "workspace-original"
    replacement = tmp_path / "workspace-replacement"
    workspace.mkdir()
    replacement.mkdir()
    guard = SafetyGuard(workspace)

    old_catalog = SkillCatalog(())
    list_skills = ListSkillsTool(guard, old_catalog)
    activate_skill = ActivateSkillTool(guard, old_catalog)
    read_skill = ReadSkillResourceTool(guard, old_catalog)
    reload_calls: list[object] = []

    loop = SimpleNamespace(
        ui=FakeTerminalUI(),
        project_root=workspace,
        repo_map=None,
        _mcp_runtime=None,
        _mcp_configs={},
        safety_guard=guard,
        tools={
            "list_skills": list_skills,
            "activate_skill": activate_skill,
            "read_skill_resource": read_skill,
        },
        hooks=object(),
        current_session=SimpleNamespace(session_id="session"),
        _emit_event=lambda event: None,
    )

    def verify_project_root_identity() -> None:
        try:
            guard.ensure_project_root_current()
        except Exception as exc:
            raise RuntimeError(
                "workspace root changed after runtime startup; refusing to continue"
            ) from exc

    async def reload_plugin_runtime_tools(tools) -> None:
        reload_calls.append(tools)

    async def reload_mcp_servers(configs):
        reload_calls.append(configs)
        return {}

    loop._verify_project_root_identity = verify_project_root_identity
    loop.reload_plugin_runtime_tools = reload_plugin_runtime_tools
    loop.reload_mcp_servers = reload_mcp_servers

    config = SimpleNamespace(
        custom_providers={},
        input_mode="emacs",
        keybindings={},
        tui_mode="inline",
        tui_mouse=True,
        theme="default",
        no_color=False,
        screen_reader_mode=False,
        notification_method="off",
        notification_events=(),
        notification_include_preview=False,
        sandbox_backend="auto",
        sandbox_docker_image="ash-sandbox:latest",
        sandbox_docker_memory_mb=4096,
        sandbox_docker_cpus=2.0,
        allow_unsafe_plugin_runtime=False,
    )

    workspace.rename(saved)
    replacement.rename(workspace)

    assert await _repl(loop, config, SimpleNamespace()) == 0

    captured = capsys.readouterr()
    assert reload_calls == []
    assert "workspace root changed after runtime startup" in captured.err
    assert list_skills.catalog is old_catalog
    assert activate_skill.catalog is old_catalog
    assert read_skill.catalog is old_catalog


@pytest.mark.asyncio
async def test_repl_mcp_reload_exception_commits_other_plugin_state_and_reports_partial(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys,
) -> None:
    commands = iter(("/reload-plugins", "/exit"))
    FakeTerminalUI = _install_fake_repl_frontend(monkeypatch, commands)
    monkeypatch.setattr("ash.safety.trust.is_workspace_trusted", lambda root: False)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))

    guard = SafetyGuard(tmp_path)
    old_catalog = SkillCatalog(())
    list_skills = ListSkillsTool(guard, old_catalog)
    activate_skill = ActivateSkillTool(guard, old_catalog)
    read_skill = ReadSkillResourceTool(guard, old_catalog)
    old_hooks = object()
    old_mcp_runtime = object()
    loop = SimpleNamespace(
        ui=FakeTerminalUI(),
        project_root=tmp_path,
        repo_map=None,
        _mcp_runtime=old_mcp_runtime,
        _mcp_configs={},
        safety_guard=guard,
        tools={
            "list_skills": list_skills,
            "activate_skill": activate_skill,
            "read_skill_resource": read_skill,
        },
        hooks=old_hooks,
        current_session=SimpleNamespace(session_id="session"),
        _emit_event=lambda event: None,
    )

    async def successful_plugin_tool_reload(tools) -> None:
        del tools

    async def fail_mcp_reload(configs):
        del configs
        raise RuntimeError("injected MCP reload failure")

    loop.reload_plugin_runtime_tools = successful_plugin_tool_reload
    loop.reload_mcp_servers = fail_mcp_reload

    config = SimpleNamespace(
        custom_providers={},
        input_mode="emacs",
        keybindings={},
        tui_mode="inline",
        tui_mouse=True,
        theme="default",
        no_color=False,
        screen_reader_mode=False,
        notification_method="off",
        notification_events=(),
        notification_include_preview=False,
        sandbox_backend="auto",
        sandbox_docker_image="ash-sandbox:latest",
        sandbox_docker_memory_mb=4096,
        sandbox_docker_cpus=2.0,
        allow_unsafe_plugin_runtime=False,
    )

    assert await _repl(loop, config, SimpleNamespace()) == 0

    captured = capsys.readouterr()
    assert "Reloaded 0 plugin(s)" in captured.out
    assert "injected MCP reload failure" in captured.err
    assert list_skills.catalog is not old_catalog
    assert activate_skill.catalog is list_skills.catalog
    assert read_skill.catalog is list_skills.catalog
    assert loop.hooks is not old_hooks
    assert loop._mcp_runtime is old_mcp_runtime


@pytest.mark.asyncio
async def test_repl_plugin_action_reports_persisted_state_when_reload_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys,
) -> None:
    commands = iter(("/plugins disable demo", "/exit"))
    FakeTerminalUI = _install_fake_repl_frontend(monkeypatch, commands)
    monkeypatch.setattr("ash.safety.trust.is_workspace_trusted", lambda root: False)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))

    def fake_manage(action, target, **kwargs):
        del kwargs
        assert action == "disable"
        assert target == "demo"
        return {
            "action": "disable",
            "name": "demo",
            "version": "1.0.0",
            "root": str(tmp_path / "home" / ".ash" / "plugins" / "demo"),
            "enabled": False,
        }

    monkeypatch.setattr("ash.commands.extensions.manage_local_plugin", fake_manage)

    guard = SafetyGuard(tmp_path)
    old_catalog = SkillCatalog(())
    loop = SimpleNamespace(
        ui=FakeTerminalUI(),
        project_root=tmp_path,
        repo_map=None,
        _mcp_runtime=None,
        _mcp_configs={},
        safety_guard=guard,
        tools={
            "list_skills": ListSkillsTool(guard, old_catalog),
            "activate_skill": ActivateSkillTool(guard, old_catalog),
            "read_skill_resource": ReadSkillResourceTool(guard, old_catalog),
        },
        hooks=object(),
        current_session=SimpleNamespace(session_id="session"),
        _emit_event=lambda event: None,
    )

    async def fail_plugin_tool_reload(tools) -> None:
        del tools
        raise RuntimeError("injected live reload failure")

    async def unexpected_mcp_reload(configs):
        del configs
        pytest.fail("MCP reload must not run after plugin-tool reload failure")

    loop.reload_plugin_runtime_tools = fail_plugin_tool_reload
    loop.reload_mcp_servers = unexpected_mcp_reload

    config = SimpleNamespace(
        custom_providers={},
        input_mode="emacs",
        keybindings={},
        tui_mode="inline",
        tui_mouse=True,
        theme="default",
        no_color=False,
        screen_reader_mode=False,
        notification_method="off",
        notification_events=(),
        notification_include_preview=False,
        sandbox_backend="auto",
        sandbox_docker_image="ash-sandbox:latest",
        sandbox_docker_memory_mb=4096,
        sandbox_docker_cpus=2.0,
        allow_unsafe_plugin_runtime=False,
    )

    assert await _repl(loop, config, SimpleNamespace()) == 0

    captured = capsys.readouterr()
    assert "Disabled demo" in captured.out
    assert "plugin state was persisted, but live reload failed" in captured.err
    assert "injected live reload failure" in captured.err
    assert "/reload-plugins" in captured.err
