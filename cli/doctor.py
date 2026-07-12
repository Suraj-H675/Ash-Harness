"""Local installation and runtime diagnostics for Ash."""

from __future__ import annotations

import json
import importlib.util
import os
import platform
import shutil
import sqlite3
import subprocess
import sys
from dataclasses import asdict, dataclass
from typing import Any

import httpx

from config import AshConfig
from mcp.server import load_mcp_servers
from sandbox import SandboxManager


@dataclass(frozen=True)
class DoctorCheck:
    name: str
    status: str
    message: str
    remedy: str = ""


def _check_credentials(config: AshConfig) -> DoctorCheck:
    provider = config.provider
    key_names = {
        "anthropic": "ANTHROPIC_API_KEY",
        "openai": "OPENAI_API_KEY",
        "deepseek": "DEEPSEEK_API_KEY",
        "groq": "GROQ_API_KEY",
    }
    if provider == "ollama":
        return DoctorCheck("credentials", "pass", "Ollama requires no API key")
    if provider in config.custom_providers:
        key_name = str(config.custom_providers[provider].get("key_env", ""))
        if not key_name and config.custom_providers[provider].get("api_key"):
            return DoctorCheck(
                "credentials",
                "warn",
                "legacy inline custom-provider key is configured",
                "Run ash setup to migrate the key into ~/.ash/.env.",
            )
    else:
        key_name = key_names.get(provider, "")
    if key_name and os.environ.get(key_name):
        return DoctorCheck("credentials", "pass", f"{key_name} is configured")
    return DoctorCheck(
        "credentials",
        "fail",
        f"No API key is configured for provider {provider!r}",
        "Run ash setup.",
    )


def _check_web_search(config: AshConfig) -> DoctorCheck:
    keys = {
        "brave": "BRAVE_SEARCH_API_KEY",
        "tavily": "TAVILY_API_KEY",
    }
    selected = config.web_search_provider
    if selected == "auto":
        available = [name for name, key in keys.items() if os.environ.get(key)]
        if available:
            return DoctorCheck(
                "web-search",
                "pass",
                "auto mode; available providers: " + ", ".join(available),
            )
    else:
        key = keys[selected]
        if os.environ.get(key):
            return DoctorCheck(
                "web-search", "pass", f"{selected} selected; {key} is configured"
            )
        return DoctorCheck(
            "web-search",
            "warn",
            f"{selected} selected but {key} is not configured",
            "Run `ash setup web`.",
        )
    return DoctorCheck(
        "web-search",
        "warn",
        "optional live search is not configured",
        "Run `ash setup web` to configure Brave Search or Tavily.",
    )


def _check_browser() -> DoctorCheck:
    if importlib.util.find_spec("playwright") is None:
        return DoctorCheck(
            "browser",
            "warn",
            "optional Playwright package is not installed",
            "Install `ash-ai[browser]`, then run `ash setup browser`.",
        )
    try:
        completed = subprocess.run(
            [sys.executable, "-m", "playwright", "install", "--list"],
            check=False,
            capture_output=True,
            text=True,
            timeout=15,
        )
    except (OSError, subprocess.TimeoutExpired):
        completed = None
    output = (
        (completed.stdout + completed.stderr).casefold()
        if completed is not None
        else ""
    )
    if completed is not None and completed.returncode == 0 and "chromium" in output:
        return DoctorCheck("browser", "pass", "Playwright Chromium is installed")
    return DoctorCheck(
        "browser",
        "warn",
        "Playwright is installed but Chromium is missing",
        "Run `ash setup browser`.",
    )


def _check_storage(config: AshConfig) -> DoctorCheck:
    try:
        config.db_directory.mkdir(parents=True, exist_ok=True)
        db = config.db_directory / ".doctor.sqlite3"
        connection = sqlite3.connect(db)
        try:
            connection.execute("PRAGMA quick_check").fetchone()
        finally:
            connection.close()
            db.unlink(missing_ok=True)
    except (OSError, sqlite3.Error) as exc:
        return DoctorCheck(
            "storage", "fail", f"Database directory is not writable: {exc}"
        )
    return DoctorCheck("storage", "pass", str(config.db_directory))


def _check_workspace(config: AshConfig) -> DoctorCheck:
    root = config.workspace_root.expanduser().resolve()
    if not root.is_dir():
        return DoctorCheck("workspace", "fail", f"Not a directory: {root}")
    if not os.access(root, os.R_OK | os.W_OK):
        return DoctorCheck("workspace", "fail", f"Not readable and writable: {root}")
    return DoctorCheck("workspace", "pass", str(root))


def _check_mcp(config: AshConfig) -> DoctorCheck:
    path = config.workspace_root / ".mcp.json"
    if not path.exists():
        return DoctorCheck("mcp", "pass", "No project MCP configuration")
    try:
        servers = load_mcp_servers(path)
    except (OSError, ValueError, KeyError, json.JSONDecodeError) as exc:
        return DoctorCheck("mcp", "fail", f"Invalid {path.name}: {exc}")
    return DoctorCheck("mcp", "pass", f"{len(servers)} server(s) configured")


def _check_extensions(config: AshConfig) -> DoctorCheck:
    from cli.extensions import discover_extensions

    inventory = discover_extensions(config.workspace_root)
    if inventory.errors:
        return DoctorCheck(
            "extensions",
            "fail",
            "; ".join(inventory.errors),
            "Run `ash extensions --json`, then fix or remove invalid plugin/hook files.",
        )
    return DoctorCheck(
        "extensions",
        "pass",
        f"{len(inventory.skills)} skill(s), {len(inventory.plugins)} plugin(s), "
        f"{len(inventory.hooks)} hook config(s)",
    )


def _check_a2a(config: AshConfig) -> DoctorCheck:
    from agents.a2a_remote import load_remote_agent_configs
    from safety.trust import is_workspace_trusted

    try:
        agents = load_remote_agent_configs(
            config.workspace_root,
            include_project=is_workspace_trusted(config.workspace_root),
        )
    except (OSError, ValueError) as exc:
        return DoctorCheck(
            "a2a",
            "fail",
            f"Invalid remote-agent configuration: {exc}",
            "Repair ~/.ash/a2a.json or the trusted project's .ash/a2a.json.",
        )
    if not agents:
        return DoctorCheck("a2a", "pass", "No remote A2A agents configured")
    if importlib.util.find_spec("a2a") is None:
        return DoctorCheck(
            "a2a",
            "fail",
            f"{len(agents)} remote agent(s) configured but A2A support is missing",
            "Install `ash-ai[a2a]`.",
        )
    missing = sorted(
        {
            agent.token_env
            for agent in agents.values()
            if not os.environ.get(agent.token_env)
        }
    )
    if missing:
        return DoctorCheck(
            "a2a",
            "warn",
            f"{len(agents)} remote agent(s); unset credentials: {', '.join(missing)}",
            "Set each credential environment variable before delegating.",
        )
    return DoctorCheck("a2a", "pass", f"{len(agents)} remote agent(s) ready")


def _check_lsp(config: AshConfig) -> DoctorCheck:
    from lsp.config import lsp_command_available, load_lsp_server_configs
    from safety.trust import is_workspace_trusted

    if not config.lsp_enabled:
        return DoctorCheck("lsp", "pass", "Managed LSP is disabled by configuration")
    if not is_workspace_trusted(config.workspace_root):
        return DoctorCheck(
            "lsp",
            "warn",
            "Managed LSP is disabled for this untrusted workspace",
            "Run `ash trust add` after reviewing project-controlled executable configuration.",
        )
    try:
        servers = load_lsp_server_configs(
            config.workspace_root, include_project=True
        )
    except (OSError, ValueError) as exc:
        return DoctorCheck(
            "lsp",
            "fail",
            f"Invalid language-server configuration: {exc}",
            "Repair ~/.ash/lsp.json or the trusted project's .ash/lsp.json.",
        )
    if not servers:
        return DoctorCheck(
            "lsp",
            "pass",
            "No supported language servers are installed or configured",
        )
    missing = [
        name
        for name, server in servers.items()
        if not lsp_command_available(server, config.workspace_root)
    ]
    if missing:
        return DoctorCheck(
            "lsp",
            "fail",
            "configured server executables are missing: " + ", ".join(sorted(missing)),
            "Install the server or repair its command in lsp.json.",
        )
    return DoctorCheck(
        "lsp",
        "pass",
        "detected/configured servers (not started): " + ", ".join(sorted(servers)),
        "Run `ash lsp diagnostics FILE` to verify a real server handshake.",
    )


async def _check_connectivity(config: AshConfig) -> DoctorCheck:
    provider = config.provider
    if provider == "ollama":
        url = os.environ.get("OLLAMA_API_BASE", "http://localhost:11434").rstrip("/")
        endpoint = f"{url}/api/tags"
        headers: dict[str, str] = {}
    else:
        defaults = {
            "openai": "https://api.openai.com/v1",
            "groq": "https://api.groq.com/openai/v1",
            "deepseek": "https://api.deepseek.com/v1",
        }
        if provider == "anthropic":
            return DoctorCheck(
                "connectivity",
                "warn",
                "Anthropic has no inexpensive model-list health endpoint",
                "Run a one-shot prompt to verify the selected model.",
            )
        custom = config.custom_providers.get(provider, {})
        base = str(custom.get("base_url") or defaults.get(provider, "")).rstrip("/")
        endpoint = f"{base}/models"
        key_env = str(custom.get("key_env", ""))
        key = (
            os.environ.get(key_env, "")
            if key_env
            else os.environ.get(
                {
                    "openai": "OPENAI_API_KEY",
                    "groq": "GROQ_API_KEY",
                    "deepseek": "DEEPSEEK_API_KEY",
                }.get(provider, ""),
                "",
            )
        )
        headers = {"Authorization": f"Bearer {key}"} if key else {}
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.get(endpoint, headers=headers)
        response.raise_for_status()
    except (httpx.HTTPError, ValueError) as exc:
        return DoctorCheck("connectivity", "fail", f"{endpoint}: {exc}")
    return DoctorCheck("connectivity", "pass", endpoint)


async def run_doctor(*, connect: bool = False) -> list[DoctorCheck]:
    checks: list[DoctorCheck] = [
        DoctorCheck(
            "runtime",
            "pass" if sys.version_info >= (3, 11) else "fail",
            f"Python {platform.python_version()} on {platform.system()} {platform.machine()}",
            "Install Python 3.11 or newer." if sys.version_info < (3, 11) else "",
        )
    ]
    try:
        config = AshConfig.load()
    except Exception as exc:  # noqa: BLE001
        checks.append(
            DoctorCheck(
                "config",
                "fail",
                str(exc),
                "Repair ~/.ash/ash.toml or environment settings.",
            )
        )
        return checks
    checks.extend(
        [
            DoctorCheck(
                "config", "pass", f"model={config.model}; mode={config.safety_tier}"
            ),
            _check_credentials(config),
            _check_web_search(config),
            _check_browser(),
            _check_workspace(config),
            _check_storage(config),
            DoctorCheck(
                "git",
                "pass" if shutil.which("git") else "warn",
                shutil.which("git") or "git is not installed",
            ),
            DoctorCheck(
                "ripgrep",
                "pass" if shutil.which("rg") else "warn",
                shutil.which("rg")
                or "rg is unavailable; Python search fallback will be used",
            ),
        ]
    )
    sandbox = SandboxManager(
        workspace_root=config.workspace_root,
        network=config.sandbox_network,
        backend_preference=config.sandbox_backend,
        docker_image=config.sandbox_docker_image,
    )
    sandbox_status = sandbox.status()
    checks.append(
        DoctorCheck(
            "sandbox",
            "pass" if sandbox_status["isolated"] else "warn",
            (
                f"{sandbox_status['backend']} (tier {sandbox_status['tier']}); "
                f"filesystem={sandbox_status['filesystem']}; "
                f"network={sandbox_status['network']}; "
                f"fail_closed={str(sandbox_status['fail_closed']).lower()}"
            ),
            str(sandbox_status["remediation"]),
        )
    )
    checks.append(_check_mcp(config))
    checks.append(_check_extensions(config))
    checks.append(_check_a2a(config))
    checks.append(_check_lsp(config))
    if connect:
        checks.append(await _check_connectivity(config))
    return checks


def render_doctor(checks: list[DoctorCheck], *, json_output: bool = False) -> str:
    if json_output:
        payload: dict[str, Any] = {
            "schema_version": 1,
            "ok": not any(check.status == "fail" for check in checks),
            "checks": [asdict(check) for check in checks],
        }
        return json.dumps(payload, indent=2)
    lines = ["Ash doctor"]
    for check in checks:
        lines.append(f"[{check.status.upper():4}] {check.name}: {check.message}")
        if check.remedy:
            lines.append(f"       remedy: {check.remedy}")
    return "\n".join(lines)
