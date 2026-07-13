"""Canonical runtime assembly shared by the CLI, SDK, and HTTP server."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable

from ash.config import AshConfig
from ash.context.instructions import (
    InstructionDiagnostic,
    discover_instructions,
    render_instructions,
)
from ash.core.checkpoints import FileCheckpointMiddleware
from ash.core.loop import AshLoop, LoopUI
from ash.core.planner import Planner
from ash.core.secret_middleware import SecretRedactionMiddleware
from ash.core.session import SessionStore
from ash.hooks.config import HookConfigSource, load_command_hooks
from ash.mcp.server import MCPConfigSource, MCPServerConfig, load_mcp_server_sources
from ash.plugins.lifecycle import load_extension_state
from ash.plugins.registry import DiscoveredPlugin, PluginCatalog
from ash.providers.base import ProviderABC
from ash.providers.registry import get_provider_registry
from ash.safety.grants import PermissionRule, load_permission_rules
from ash.safety.guard import SafetyGuard
from ash.safety.trust import is_workspace_trusted
from ash.sandbox import (
    SandboxBackendUnavailable,
    SandboxManager,
    auto_approve_safety_error,
)


ApprovalCallback = Callable[[str, dict[str, Any]], Awaitable[bool]]


@dataclass(frozen=True)
class RuntimeComponents:
    """Objects owned by one fully assembled Ash runtime."""

    loop: AshLoop
    provider: ProviderABC
    session_store: SessionStore
    safety_guard: SafetyGuard
    sandbox_manager: SandboxManager
    workspace_trusted: bool
    plugins: tuple[DiscoveredPlugin, ...]


def discover_active_plugins(
    workspace: Path,
    *,
    include_project: bool,
) -> list[DiscoveredPlugin]:
    roots = [(Path.home() / ".ash" / "plugins", "user")]
    if include_project:
        roots.append((workspace / ".ash" / "plugins", "project"))
    return PluginCatalog(
        tuple(roots),
        disabled_plugins=load_extension_state().disabled_plugins,
    ).discover()


def build_tools(
    safety_guard: SafetyGuard,
    project_root: Path | None = None,
    *,
    sandbox_manager: Any | None = None,
    allow_project_extensions: bool = False,
    provider_factory: Any | None = None,
    agent_db_path: Path | None = None,
    allowed_web_domains: list[str] | tuple[str, ...] | None = None,
    repo_map: Any | None = None,
    runtime_config: AshConfig | None = None,
    active_plugins: list[DiscoveredPlugin] | None = None,
    lsp_manager: Any | None = None,
) -> dict[str, Any]:
    """Build the standard tool set and its trusted declarative extensions."""

    from ash.agents.shared_state import SharedState
    from ash.plugins.agents import AgentCatalog, AgentSource
    from ash.plugins.skills import (
        ActivateSkillTool,
        ListSkillsTool,
        ReadSkillResourceTool,
        SkillCatalog,
        SkillSource,
    )
    from ash.plugins.runtime import build_plugin_runtime_tools
    from ash.tools.agent import SpawnAgentTool
    from ash.tools.ask_user import AskUserTool
    from ash.tools.automation import ListAutomationsTool, ManageAutomationTool
    from ash.tools.base import BaseTool
    from ash.tools.browser import build_browser_tools
    from ash.tools.command import RunCommandTool
    from ash.tools.delegate import DelegateAgentsTool
    from ash.tools.filesystem import (
        ReadFileTool,
        ReplaceFileContentTool,
        ReplaceFileEditsTool,
        WholeEditTool,
        WriteFileTool,
    )
    from ash.tools.git import AutoCommitTool, GitDiffTool, GitLogTool, GitStatusTool
    from ash.tools.patch import ApplyPatchTool
    from ash.tools.process import BackgroundProcessTool
    from ash.tools.search import GlobFilesTool, ListDirectoryTool, SearchTextTool
    from ash.tools.symbols import FindReferencesTool, FindSymbolTool
    from ash.tools.tool_search import SearchToolsTool
    from ash.tools.web import WebFetchTool
    from ash.tools.web_search import WebSearchTool
    from ash.tools.lsp import LSPTool

    root = project_root if project_root is not None else safety_guard.project_root
    plugins = active_plugins
    if plugins is None:
        plugins = discover_active_plugins(
            root, include_project=allow_project_extensions
        )

    skill_roots: list[Path | SkillSource] = [Path.home() / ".ash" / "skills"]
    if allow_project_extensions:
        skill_roots.append(root / ".ash" / "skills")
    skill_roots.extend(
        SkillSource(paths=plugin.skill_paths(), namespace=plugin.manifest.name)
        for plugin in plugins
    )

    agent_sources: list[Path | AgentSource] = [Path.home() / ".ash" / "agents"]
    if allow_project_extensions:
        agent_sources.append(root / ".ash" / "agents")
    agent_sources.extend(
        AgentSource(paths=plugin.agent_paths(), namespace=plugin.manifest.name)
        for plugin in plugins
    )
    agent_definitions = {
        definition.name: definition
        for definition in AgentCatalog(tuple(agent_sources)).discover()
    }
    catalog = SkillCatalog(tuple(skill_roots))
    environment_allowlist = (
        runtime_config.command_env_allowlist if runtime_config else ()
    )
    tools: list[BaseTool] = [
        ReadFileTool(safety_guard),
        WriteFileTool(safety_guard),
        ReplaceFileContentTool(safety_guard),
        ReplaceFileEditsTool(safety_guard),
        WholeEditTool(safety_guard),
        RunCommandTool(
            safety_guard,
            project_root=root,
            sandbox_manager=sandbox_manager,
            environment_allowlist=environment_allowlist,
        ),
        AutoCommitTool(
            safety_guard,
            environment_allowlist=environment_allowlist,
        ),
        GitStatusTool(safety_guard),
        GitDiffTool(safety_guard),
        GitLogTool(safety_guard),
        ApplyPatchTool(safety_guard),
        BackgroundProcessTool(
            safety_guard,
            sandbox_manager=sandbox_manager,
            environment_allowlist=environment_allowlist,
        ),
        AskUserTool(safety_guard),
        ListDirectoryTool(safety_guard),
        GlobFilesTool(safety_guard),
        SearchTextTool(safety_guard),
        WebFetchTool(safety_guard, allowed_domains=allowed_web_domains),
        WebSearchTool(
            safety_guard,
            provider=(runtime_config.web_search_provider if runtime_config else "auto"),
            timeout=(
                runtime_config.web_search_timeout_seconds if runtime_config else 20.0
            ),
            allowed_domains=allowed_web_domains,
        ),
        ListSkillsTool(safety_guard, catalog),
        ActivateSkillTool(safety_guard, catalog),
        ReadSkillResourceTool(safety_guard, catalog),
    ]
    if runtime_config is not None and runtime_config.automation_enabled:
        automation_store_path = runtime_config.db_directory / "automation.db"
        tools.extend(
            [
                ListAutomationsTool(safety_guard, automation_store_path),
                ManageAutomationTool(safety_guard, automation_store_path),
            ]
        )
    if lsp_manager is not None:
        tools.append(LSPTool(safety_guard, lsp_manager))
    tools.extend(
        build_browser_tools(
            safety_guard,
            headless=(runtime_config.browser_headless if runtime_config else True),
            timeout_seconds=(
                runtime_config.browser_timeout_seconds if runtime_config else 30.0
            ),
            allowed_domains=allowed_web_domains,
        )
    )
    from ash.agents.a2a_remote import (
        DelegateRemoteAgentTool,
        ListRemoteAgentsTool,
        load_remote_agent_configs,
    )

    remote_agents = load_remote_agent_configs(
        root, include_project=allow_project_extensions
    )
    if remote_agents:
        tools.extend(
            [
                ListRemoteAgentsTool(safety_guard, remote_agents),
                DelegateRemoteAgentTool(safety_guard, remote_agents),
            ]
        )
    if provider_factory is not None and agent_db_path is not None:
        spawn_tool = SpawnAgentTool(
            safety_guard,
            SharedState(agent_db_path),
            provider_factory,
            config=runtime_config,
            custom_agents=agent_definitions,
        )
        tools.append(spawn_tool)
        if runtime_config is not None:
            tools.append(
                DelegateAgentsTool(
                    safety_guard,
                    SharedState(agent_db_path),
                    spawn_tool,
                    runtime_config,
                )
            )
    if repo_map is not None:
        tools.extend(
            [
                FindSymbolTool(safety_guard, repo_map),
                FindReferencesTool(safety_guard, repo_map),
            ]
        )
    by_name = {tool.name: tool for tool in tools}
    tool_search = SearchToolsTool(
        safety_guard,
        lambda: by_name,
        threshold=(runtime_config.tool_search_threshold if runtime_config else 32),
    )
    if tool_search.name in by_name:
        raise ValueError(f"tool collides with an existing tool: {tool_search.name}")
    by_name[tool_search.name] = tool_search
    plugin_tools = build_plugin_runtime_tools(
        plugins,
        safety_guard,
        backend_preference=(
            runtime_config.sandbox_backend if runtime_config else "auto"
        ),
        docker_image=(
            runtime_config.sandbox_docker_image
            if runtime_config
            else "ash-sandbox:latest"
        ),
        allow_unisolated=(
            runtime_config.allow_unsafe_plugin_runtime if runtime_config else False
        ),
    )
    for tool in plugin_tools:
        if tool.name in by_name:
            raise ValueError(f"plugin tool collides with an existing tool: {tool.name}")
        by_name[tool.name] = tool
    return by_name


def build_repo_map(config: AshConfig) -> Any | None:
    """Build the optional repository map without making startup depend on it."""

    if not config.repo_map_enabled:
        return None
    from ash.logging import get_logger
    from ash.repo.repomap import RepoMap

    try:
        return RepoMap(
            config.workspace_root,
            max_files=config.repo_map_max_files,
            exclude_patterns=config.repo_map_exclude_patterns,
        )
    except OSError as exc:
        get_logger(__name__).warning("repository map unavailable: {}", exc)
        return None


def build_runtime(
    config: AshConfig,
    ui: LoopUI,
    *,
    provider: ProviderABC | None = None,
    agent_provider_factory: Callable[[], ProviderABC] | None = None,
    session_store: SessionStore | None = None,
    permission_rules: list[PermissionRule] | None = None,
    workspace_trusted: bool | None = None,
    approval_callback: ApprovalCallback | None = None,
    additional_mcp_configs: dict[str, MCPServerConfig] | None = None,
    run_maintenance: bool = True,
) -> RuntimeComponents:
    """Assemble one runtime with identical extension and safety semantics."""

    trusted = (
        is_workspace_trusted(config.workspace_root)
        if workspace_trusted is None
        else workspace_trusted
    )
    sandbox = SandboxManager(
        workspace_root=config.workspace_root,
        network=config.sandbox_network,
        backend_preference=config.sandbox_backend,
        docker_image=config.sandbox_docker_image,
    )
    safety_error = auto_approve_safety_error(
        sandbox,
        allow_unsafe=config.allow_unsafe_auto_approve,
    )
    if config.safety_tier == "auto_approve" and safety_error:
        raise SandboxBackendUnavailable(safety_error)

    store = session_store or SessionStore(config.db_directory / "sessions.db")
    if run_maintenance and config.session_retention_days > 0:
        store.cleanup_sessions(
            config.session_retention_days,
            project_path=str(config.workspace_root.resolve()),
        )
    rules = (
        load_permission_rules(config.workspace_root)
        if permission_rules is None
        else permission_rules
    )
    guard = SafetyGuard(
        config.workspace_root,
        blocklist_commands=config.command_blocklist,
    )
    active_provider = provider or get_provider_registry().build(config)
    plugins = discover_active_plugins(config.workspace_root, include_project=trusted)
    repo_map = build_repo_map(config)
    lsp_manager = None
    if trusted and config.lsp_enabled:
        from ash.lsp.config import load_lsp_server_configs
        from ash.lsp.manager import LanguageServerManager

        lsp_configs = load_lsp_server_configs(
            config.workspace_root,
            include_project=True,
        )
        if lsp_configs:
            lsp_manager = LanguageServerManager(config.workspace_root, lsp_configs)
    tools = build_tools(
        guard,
        config.workspace_root,
        sandbox_manager=sandbox,
        allow_project_extensions=trusted,
        provider_factory=(
            agent_provider_factory
            if agent_provider_factory is not None
            else lambda: get_provider_registry().build(config)
        ),
        agent_db_path=config.db_directory / "agents.db",
        allowed_web_domains=config.allowed_web_domains,
        repo_map=repo_map,
        runtime_config=config,
        active_plugins=plugins,
        lsp_manager=lsp_manager,
    )

    instruction_diagnostics: list[InstructionDiagnostic] = []
    instructions = render_instructions(
        discover_instructions(
            config.workspace_root,
            include_project=trusted,
            diagnostics=instruction_diagnostics,
        ),
        instruction_diagnostics,
    )
    project_hook_environment = (("ASH_PROJECT_ROOT", str(config.workspace_root)),)
    hook_sources: list[Path | HookConfigSource] = [
        HookConfigSource(
            Path.home() / ".ash" / "hooks.json",
            cwd=config.workspace_root,
            environment=project_hook_environment,
        )
    ]
    if trusted:
        hook_sources.append(
            HookConfigSource(
                config.workspace_root / ".ash" / "hooks.json",
                cwd=config.workspace_root,
                environment=project_hook_environment,
            )
        )
    hook_sources.extend(
        HookConfigSource(
            path=path,
            cwd=plugin.root,
            environment=(("ASH_PLUGIN_ROOT", str(plugin.root)),),
        )
        for plugin in plugins
        for path in plugin.hook_paths()
    )
    hooks = load_command_hooks(hook_sources)

    mcp_sources: list[MCPConfigSource] = []
    if trusted:
        mcp_sources.append(MCPConfigSource(config.workspace_root / ".mcp.json"))
    mcp_sources.extend(
        MCPConfigSource(
            path=path,
            namespace=plugin.manifest.name,
            cwd=plugin.root,
            environment=(("ASH_PLUGIN_ROOT", str(plugin.root)),),
        )
        for plugin in plugins
        for path in plugin.mcp_paths()
    )
    mcp_configs = load_mcp_server_sources(mcp_sources)
    for name, mcp_config in (additional_mcp_configs or {}).items():
        if name in mcp_configs:
            raise ValueError(f"duplicate MCP server name: {name}")
        mcp_configs[name] = mcp_config

    loop = AshLoop(
        session_store=store,
        provider=active_provider,
        safety_guard=guard,
        ui=ui,
        project_root=config.workspace_root,
        repo_map=repo_map,
        tools=tools,
        hooks=hooks,
        additional_instructions=instructions,
        config=config,
        max_steering_messages=config.steering_queue_limit,
        planner=Planner(active_provider) if config.enable_sprint_planning else None,
        enable_sprint_planning=config.enable_sprint_planning,
        safety_tier=config.safety_tier,
        on_tool_approval=approval_callback,
        mcp_configs=mcp_configs,
        enable_semantic_memory=config.memory_backend != "off",
        memory_backend=config.memory_backend,
        embedding_provider=config.embedding_provider,
        openai_api_key=config.openai_api_key,
        onnx_model_path=config.onnx_model_path,
        chroma_persist_dir=config.chroma_persist_dir,
    )
    loop.permission_policy.set_persistent_rules(rules)
    hooks.set_event_sink(loop._emit_event)

    def checkpoint_context() -> tuple[str, str, str] | None:
        if loop.current_session is None or loop.turn_context is None:
            return None
        return (
            loop.current_session.session_id,
            loop.turn_context.turn_id,
            str(loop.turn_context.get("tool_call_id", "")),
        )

    loop.tool_middlewares.append(
        FileCheckpointMiddleware(store, guard, checkpoint_context)
    )
    if lsp_manager is not None:
        from ash.lsp.middleware import LSPDiagnosticsMiddleware

        loop.tool_middlewares.append(LSPDiagnosticsMiddleware(lsp_manager, guard))
    loop.tool_middlewares.append(SecretRedactionMiddleware())
    return RuntimeComponents(
        loop=loop,
        provider=active_provider,
        session_store=store,
        safety_guard=guard,
        sandbox_manager=sandbox,
        workspace_trusted=trusted,
        plugins=tuple(plugins),
    )
