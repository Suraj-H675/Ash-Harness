# tests/unit/test_mcp.py
import asyncio
import json
import io
import os
import sys
import time
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, Mock, patch

import pytest
import httpx
from ash.core.loop import AshLoop
from ash.core.session import SessionStore, ToolCallRecord
from ash.core.secret_middleware import SecretRedactionMiddleware
from ash.providers.base import ProviderABC, StreamChunk
from ash.safety.guard import SafetyGuard
from ash.sandbox.process_utils import ProcessTreeTerminationError
from ash.mcp.client import (
    MCPClient,
    MCPProtocolError,
    MCPTaskTimeout,
)
from ash.mcp import client as mcp_client_module
from ash.mcp.oauth import MCPOAuthSession
from ash.mcp.runtime import (
    CURRENT_SCHEMA_DIALECT,
    MCPListResourcesTool,
    MCPRuntime,
    MCPTool,
    _extract_mcp_header_annotations,
    _validate_schema_instance,
)
from ash.mcp.server import (
    MCPServerConfig,
    MCPServerInstance,
    MCPServerLifecycleError,
    MCPServerManager,
    MCPConfigSource,
    MAX_MCP_CONFIG_BYTES,
    load_mcp_servers,
    load_mcp_server_sources,
    mcp_server_fingerprint,
    save_mcp_servers,
    expand_env_vars,
)
from pathlib import Path
from ash.ui.headless import HeadlessUI


def test_load_mcp_servers_from_file(tmp_path: Path) -> None:
    mcp_file = tmp_path / ".mcp.json"
    mcp_file.write_text(
        json.dumps(
            {
                "github": {
                    "command": "npx",
                    "args": ["-y", "@modelcontextprotocol/server-github"],
                    "env": {"GITHUB_TOKEN": "test"},
                }
            }
        )
    )
    servers = load_mcp_servers(mcp_file)
    assert "github" in servers
    assert servers["github"].command == "npx"
    assert servers["github"].args == ["-y", "@modelcontextprotocol/server-github"]


def test_load_mcp_sources_namespaces_plugin_servers(tmp_path: Path) -> None:
    plugin = tmp_path / "plugin"
    plugin.mkdir()
    path = plugin / ".mcp.json"
    path.write_text(
        json.dumps(
            {"mcpServers": {"local": {"command": "server", "env": {"TOKEN": "value"}}}}
        )
    )

    servers = load_mcp_server_sources(
        [
            MCPConfigSource(
                path,
                namespace="example",
                cwd=plugin,
                environment=(("ASH_PLUGIN_ROOT", str(plugin)),),
            )
        ]
    )

    config = servers["example__local"]
    assert config.cwd == str(plugin)
    assert config.env == {"TOKEN": "value", "ASH_PLUGIN_ROOT": str(plugin)}


def test_load_mcp_sources_rejects_duplicate_names(tmp_path: Path) -> None:
    paths = []
    for name in ("first", "second"):
        path = tmp_path / f"{name}.json"
        path.write_text(json.dumps({"same": {"command": "server"}}))
        paths.append(MCPConfigSource(path))

    with pytest.raises(ValueError, match="duplicate MCP server"):
        load_mcp_server_sources(paths)


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        ([], "must be an object"),
        ({"mcpServers": []}, "mcpServers must be an object"),
        ({"bad name": {"command": "server"}}, "invalid MCP server name"),
        ({"server": "invalid"}, "must be an object"),
        ({"server": {"args": []}}, "requires a command"),
        (
            {"server": {"command": "server", "args": [1]}},
            "args must be a list of strings",
        ),
        (
            {"server": {"transport": "http", "url": ""}},
            "requires a url",
        ),
    ],
)
def test_load_mcp_servers_rejects_invalid_config(
    tmp_path: Path, payload, message: str
) -> None:
    path = tmp_path / ".mcp.json"
    path.write_text(json.dumps(payload))

    with pytest.raises(ValueError, match=message):
        load_mcp_servers(path)


def test_load_mcp_servers_rejects_oversized_config(tmp_path: Path) -> None:
    path = tmp_path / ".mcp.json"
    path.write_bytes(b" " * (MAX_MCP_CONFIG_BYTES + 1))

    with pytest.raises(ValueError, match="exceeds 256 KiB"):
        load_mcp_servers(path)


def test_load_mcp_servers_does_not_follow_symlinked_config(tmp_path: Path) -> None:
    target = tmp_path / "outside.json"
    target.write_text('{"server": {"command": "server"}}', encoding="utf-8")
    path = tmp_path / ".mcp.json"
    try:
        path.symlink_to(target)
    except OSError:
        pytest.skip("symlinks are unavailable")

    with pytest.raises(ValueError, match="symlinked MCP config"):
        load_mcp_servers(path)


def test_load_mcp_servers_rejects_parent_swapped_before_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import ash.mcp.server as mcp_module

    project = tmp_path / "project"
    project.mkdir()
    path = project / ".mcp.json"
    path.write_text(
        '{"safe":{"command":"echo","args":["safe"]}}\n',
        encoding="utf-8",
    )
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / ".mcp.json").write_text(
        '{"evil":{"command":"echo","args":["attacker"]}}\n',
        encoding="utf-8",
    )
    real_read = mcp_module.read_bounded_open_file
    swapped = False

    def read_then_swap(*args, **kwargs):
        nonlocal swapped
        if not swapped:
            swapped = True
            project.rename(tmp_path / "project-real")
            try:
                project.symlink_to(outside, target_is_directory=True)
            except OSError as exc:
                pytest.skip(f"symlink creation is unavailable: {exc}")
        return real_read(*args, **kwargs)

    monkeypatch.setattr(mcp_module, "read_bounded_open_file", read_then_swap)

    with pytest.raises(ValueError, match="symlink or junction"):
        load_mcp_servers(path)
    assert swapped is True


def test_load_mcp_servers_rejects_duplicate_json_keys(tmp_path: Path) -> None:
    path = tmp_path / ".mcp.json"
    path.write_text(
        '{"mcpServers":{"server":{"command":"first"},'
        '"server":{"command":"second"}}}',
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="duplicate JSON object key"):
        load_mcp_servers(path)


def test_expand_env_vars() -> None:
    import os

    os.environ["TEST_VAR"] = "hello"
    assert expand_env_vars("${TEST_VAR}/path") == "hello/path"
    assert expand_env_vars("(no var)") == "(no var)"


def test_save_mcp_servers_round_trip(tmp_path: Path) -> None:
    path = tmp_path / ".mcp.json"
    config = MCPServerConfig(
        name="local",
        command="server",
        args=["--flag"],
        env={"TOKEN": "${TOKEN}"},
    )
    save_mcp_servers({"local": config}, path)
    loaded = load_mcp_servers(path)
    assert loaded["local"].command == "server"
    assert loaded["local"].args == ["--flag"]
    assert loaded["local"].env == {"TOKEN": "${TOKEN}"}


def test_save_mcp_servers_rejects_parent_swapped_before_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import ash.mcp.server as mcp_module

    project = tmp_path / "project"
    project.mkdir()
    path = project / ".mcp.json"
    outside = tmp_path / "outside"
    outside.mkdir()
    victim = outside / ".mcp.json"
    victim.write_text('{"marker":"do-not-touch"}\n', encoding="utf-8")
    config = MCPServerConfig(
        name="local",
        command="echo",
        args=["ok"],
        env={"DUMMY_TOKEN": "dummy-value"},
    )
    real_write = mcp_module.atomic_write_unlinked_bytes
    swapped = False

    def write_then_swap(*args, **kwargs):
        nonlocal swapped
        if not swapped:
            swapped = True
            project.rename(tmp_path / "project-real")
            try:
                project.symlink_to(outside, target_is_directory=True)
            except OSError as exc:
                pytest.skip(f"symlink creation is unavailable: {exc}")
        return real_write(*args, **kwargs)

    monkeypatch.setattr(mcp_module, "atomic_write_unlinked_bytes", write_then_swap)

    with pytest.raises(ValueError, match="symlink or junction"):
        save_mcp_servers({"local": config}, path)
    assert swapped is True
    assert victim.read_text(encoding="utf-8") == '{"marker":"do-not-touch"}\n'


def test_save_mcp_oauth_server_round_trip_and_resolves_secret(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MCP_OAUTH_SECRET", "runtime-secret")
    path = tmp_path / ".mcp.json"
    config = MCPServerConfig(
        name="protected",
        command="",
        args=[],
        env={},
        transport="http",
        url="https://mcp.example.test/rpc",
        auth="oauth",
        oauth={
            "client_id": "registered-client",
            "client_secret": "${MCP_OAUTH_SECRET}",
            "scope": "files:read",
            "redirect_port": 43123,
        },
    )

    save_mcp_servers({"protected": config}, path)
    loaded = load_mcp_servers(path)["protected"]

    assert loaded.auth == "oauth"
    assert loaded.oauth == config.oauth
    assert loaded.resolved_oauth["client_secret"] == "runtime-secret"
    assert "runtime-secret" not in path.read_text(encoding="utf-8")


def test_mcp_oauth_reports_missing_client_secret_environment() -> None:
    config = MCPServerConfig(
        name="protected",
        command="",
        args=[],
        env={},
        transport="http",
        url="https://mcp.example.test/rpc",
        auth="oauth",
        oauth={
            "client_id": "registered-client",
            "client_secret": "${ASH_TEST_MISSING_MCP_SECRET}",
        },
    )

    with pytest.raises(ValueError, match="environment variable is not set"):
        _ = config.resolved_oauth


def test_mcp_oauth_constructor_rejects_invalid_options_before_save() -> None:
    with pytest.raises(ValueError, match="options require auth mode oauth"):
        MCPServerConfig(
            name="remote",
            command="",
            args=[],
            env={},
            transport="http",
            url="https://mcp.example.test/rpc",
            oauth={"scope": "files:read"},
        )
    with pytest.raises(ValueError, match="redirect_port is invalid"):
        MCPServerConfig(
            name="remote",
            command="",
            args=[],
            env={},
            transport="http",
            url="https://mcp.example.test/rpc",
            auth="oauth",
            oauth={"redirect_port": -1},
        )
    with pytest.raises(ValueError, match="redirect_port is invalid"):
        MCPServerConfig(
            name="remote",
            command="",
            args=[],
            env={},
            transport="http",
            url="https://mcp.example.test/rpc",
            auth="oauth",
            oauth={"redirect_port": True},
        )
    with pytest.raises(ValueError, match="issuer must be an HTTPS URL"):
        MCPServerConfig(
            name="remote",
            command="",
            args=[],
            env={},
            transport="http",
            url="https://mcp.example.test/rpc",
            auth="oauth",
            oauth={"issuer": "http://auth.example.test"},
        )
    with pytest.raises(ValueError, match="client metadata URL"):
        MCPServerConfig(
            name="remote",
            command="",
            args=[],
            env={},
            transport="http",
            url="https://mcp.example.test/rpc",
            auth="oauth",
            oauth={"client_metadata_url": "https://client.example.test/"},
        )


def test_mcp_oauth_rejects_stdio_transport() -> None:
    with pytest.raises(ValueError, match="requires the http or sse transport"):
        MCPServerConfig(
            name="protected",
            command="server",
            args=[],
            env={},
            transport="stdio",
            auth="oauth",
        )


def test_mcp_oauth_rejects_remote_plaintext_transport() -> None:
    with pytest.raises(ValueError, match="OAuth URLs must use HTTPS, except localhost"):
        MCPServerConfig(
            name="protected",
            command="",
            args=[],
            env={},
            transport="http",
            url="http://mcp.example.test/rpc",
            auth="oauth",
        )


def test_mcp_oauth_allows_loopback_http_transport() -> None:
    config = MCPServerConfig(
        name="protected",
        command="",
        args=[],
        env={},
        transport="http",
        url="http://127.0.0.1:43123/rpc",
        auth="oauth",
    )

    assert config.resolved_url == "http://127.0.0.1:43123/rpc"


def test_mcp_authorization_header_rejects_remote_plaintext_transport() -> None:
    with pytest.raises(ValueError, match="OAuth URLs must use HTTPS, except localhost"):
        MCPServerConfig(
            name="protected",
            command="",
            args=[],
            env={},
            transport="http",
            url="http://mcp.example.test/rpc",
            headers={"Authorization": "Bearer ${MCP_TOKEN}"},
        )


def test_mcp_authorization_header_allows_loopback_http_transport() -> None:
    config = MCPServerConfig(
        name="protected",
        command="",
        args=[],
        env={},
        transport="http",
        url="http://localhost:43123/rpc",
        headers={"Authorization": "Bearer ${MCP_TOKEN}"},
    )

    assert config.resolved_url == "http://localhost:43123/rpc"


def test_mcp_config_rejects_case_variant_duplicate_headers() -> None:
    with pytest.raises(ValueError, match="duplicate header names"):
        MCPServerConfig.from_dict(
            "remote",
            {
                "transport": "http",
                "url": "https://mcp.example.test/rpc",
                "headers": {
                    "Authorization": "Bearer first",
                    "authorization": "Bearer second",
                },
            },
        )


@pytest.mark.parametrize(
    "url",
    [
        "ftp://mcp.example.test/rpc",
        "https:///missing-host",
        "https://user:password@mcp.example.test/rpc",
        "https://mcp.example.test/rpc#fragment",
        "https://mcp.example.test:not-a-port/rpc",
        "https://mcp.example.test:99999/rpc",
        "https://mcp.example.test:0/rpc",
    ],
)
def test_mcp_http_transport_rejects_malformed_or_embedded_credential_urls(
    url: str,
) -> None:
    with pytest.raises(ValueError):
        MCPServerConfig(
            name="remote",
            command="",
            args=[],
            env={},
            transport="http",
            url=url,
        )


@pytest.mark.parametrize("timeout", [float("nan"), float("inf"), float("-inf")])
def test_mcp_client_rejects_non_finite_timeout(timeout: float) -> None:
    config = MCPServerConfig(name="fake", command="fake", args=[], env={})

    with pytest.raises(ValueError, match="timeout must be positive"):
        MCPClient(config, timeout=timeout)


@pytest.mark.parametrize("timeout", [float("nan"), float("inf"), float("-inf")])
@pytest.mark.asyncio
async def test_mcp_task_call_rejects_non_finite_timeout(timeout: float) -> None:
    client = MCPClient(MCPServerConfig(name="fake", command="fake", args=[], env={}))

    with pytest.raises(ValueError, match="task timeout must be positive"):
        await client.call_tool("slow", {}, as_task=True, task_timeout=timeout)


def test_mcp_config_rejects_unimplemented_websocket_transport() -> None:
    with pytest.raises(ValueError, match="Unknown MCP transport"):
        MCPServerConfig(
            name="remote",
            command="",
            args=[],
            env={},
            transport="websocket",
            url="wss://mcp.example.test/rpc",
        )


def test_load_mcp_oauth_rejects_plaintext_client_secret(tmp_path: Path) -> None:
    path = tmp_path / ".mcp.json"
    path.write_text(
        json.dumps(
            {
                "mcpServers": {
                    "protected": {
                        "transport": "http",
                        "url": "https://mcp.example.test/rpc",
                        "auth": "oauth",
                        "oauth": {"client_secret": "plaintext-secret"},
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="must reference an environment variable"):
        load_mcp_servers(path)


def test_mcp_secrets_are_resolved_only_at_runtime(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SECRET_TOKEN", "resolved-secret")
    config = MCPServerConfig(
        name="local",
        command="${MCP_COMMAND}",
        args=["--token=${SECRET_TOKEN}"],
        env={"TOKEN": "${SECRET_TOKEN}"},
    )
    monkeypatch.setenv("MCP_COMMAND", "server")
    assert config.command == "${MCP_COMMAND}"
    assert config.resolved_command == "server"
    assert config.resolved_env == {"TOKEN": "resolved-secret"}

    path = tmp_path / ".mcp.json"
    save_mcp_servers({"local": config}, path)
    assert "resolved-secret" not in path.read_text()


def test_manager_starts_and_stops_server() -> None:
    manager = MCPServerManager()
    config = MCPServerConfig(
        name="test-server",
        command=sys.executable,
        args=["-c", "import time; time.sleep(60)"],
        env={},
        transport="stdio",
    )
    instance = manager.start_server(config)
    assert isinstance(instance, MCPServerInstance)
    assert instance.name == "test-server"
    assert instance.process.poll() is None
    manager.stop_server("test-server")
    assert manager.get_server("test-server") is None


def test_mcp_config_snapshots_existing_cwd_identity(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    metadata = workspace.stat()

    existing = MCPServerConfig(
        name="existing-cwd",
        command="server",
        args=[],
        env={},
        cwd=str(workspace),
    )
    missing = MCPServerConfig(
        name="missing-cwd",
        command="server",
        args=[],
        env={},
        cwd=str(tmp_path / "missing"),
    )

    assert existing.cwd_identity == (metadata.st_dev, metadata.st_ino)
    assert missing.cwd_identity is None


@pytest.mark.skipif(os.name == "nt", reason="POSIX cwd identity regression")
def test_stdio_manager_refuses_cwd_replaced_after_config_creation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    saved = tmp_path / "workspace-saved"
    workspace.mkdir()
    config = MCPServerConfig(
        name="config-cwd-race",
        command=sys.executable,
        args=["-c", "pass"],
        env={},
        transport="stdio",
        cwd=str(workspace),
    )
    workspace.rename(saved)
    workspace.mkdir()
    popen = Mock(side_effect=AssertionError("MCP stdio must not launch"))
    monkeypatch.setattr("ash.mcp.server.subprocess.Popen", popen)

    with pytest.raises(MCPServerLifecycleError, match="working directory identity changed"):
        MCPServerManager().start_server(config)

    popen.assert_not_called()


@pytest.mark.skipif(os.name == "nt", reason="POSIX cwd race regression")
def test_stdio_manager_cwd_swap_cannot_escape_workspace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ash.sandbox import process_utils as process_utils_module

    workspace = tmp_path / "workspace"
    outside = tmp_path / "outside"
    saved = tmp_path / "workspace-saved"
    workspace.mkdir()
    outside.mkdir()
    cwd_log = tmp_path / "manager-cwd.txt"
    config = MCPServerConfig(
        name="cwd-race",
        command=sys.executable,
        args=[
            "-c",
            (
                "import os,time; from pathlib import Path; "
                f"Path({str(cwd_log)!r}).write_text(os.getcwd(), encoding='utf-8'); "
                "time.sleep(60)"
            ),
        ],
        env={},
        transport="stdio",
        cwd=str(workspace),
    )
    real_prepare = process_utils_module.prepare_process_tree
    swapped = False

    def prepare_then_swap(*args, **kwargs):
        nonlocal swapped
        plan = real_prepare(*args, **kwargs)
        if not swapped:
            swapped = True
            workspace.rename(saved)
            try:
                workspace.symlink_to(outside, target_is_directory=True)
            except OSError as exc:
                pytest.skip(f"symlink creation is unavailable: {exc}")
        return plan

    monkeypatch.setattr("ash.mcp.server.prepare_process_tree", prepare_then_swap)
    manager = MCPServerManager()
    manager.start_server(config)
    recorded_cwd = ""
    try:
        for _ in range(50):
            try:
                recorded_cwd = cwd_log.read_text(encoding="utf-8")
            except FileNotFoundError:
                recorded_cwd = ""
            if recorded_cwd:
                break
            time.sleep(0.02)
    finally:
        manager.stop_server("cwd-race")

    assert swapped is True
    assert recorded_cwd
    assert Path(recorded_cwd).resolve() == saved.resolve()


def test_stdio_manager_fails_closed_when_stable_cwd_is_unavailable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ash.sandbox.process_utils import ProcessTreeUnavailable

    config = MCPServerConfig(
        name="stable-cwd",
        command=sys.executable,
        args=["-c", "pass"],
        env={},
        transport="stdio",
        cwd=str(tmp_path),
    )

    def unavailable(*args, **kwargs):
        raise ProcessTreeUnavailable("stable cwd unavailable")

    popen = Mock(side_effect=AssertionError("MCP stdio must not launch"))
    monkeypatch.setattr("ash.mcp.server.prepare_scoped_process_launch", unavailable)
    monkeypatch.setattr("ash.mcp.server.subprocess.Popen", popen)

    with pytest.raises(MCPServerLifecycleError, match="stable cwd unavailable"):
        MCPServerManager().start_server(config)

    popen.assert_not_called()


def test_stdio_manager_preflights_tree_cleanup_before_spawn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = MCPServerManager()
    config = MCPServerConfig(
        name="preflight-server",
        command="server",
        args=[],
        env={},
        transport="stdio",
    )

    def unavailable(*args: object, **kwargs: object) -> object:
        from ash.sandbox.process_utils import ProcessTreeUnavailable

        raise ProcessTreeUnavailable("taskkill unavailable")

    monkeypatch.setattr("ash.mcp.server.prepare_process_tree", unavailable)
    monkeypatch.setattr(
        "ash.mcp.server.subprocess.Popen",
        lambda *args, **kwargs: pytest.fail("MCP stdio must not launch"),
    )

    with pytest.raises(MCPServerLifecycleError, match="not started"):
        manager.start_server(config)
    assert manager.list_servers() == []


def test_http_manager_does_not_require_local_tree_cleanup_backend(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = MCPServerManager()
    config = MCPServerConfig(
        name="http-server",
        command="",
        args=[],
        env={},
        transport="http",
        url="https://mcp.example.test/rpc",
    )
    preflight = Mock(side_effect=AssertionError("HTTP must not preflight taskkill"))
    monkeypatch.setattr("ash.mcp.server.prepare_process_tree", preflight)

    instance = manager.start_server(config)

    assert instance.process is None
    preflight.assert_not_called()
    manager.stop_server("http-server")


def test_manager_stop_server_terminates_descendants(tmp_path: Path) -> None:
    if os.name == "nt":
        pytest.skip("descendant survival probe is POSIX-only")
    marker = tmp_path / "child-survived"
    child_code = (
        "import time; "
        "time.sleep(0.4); "
        f"open({str(marker)!r}, 'w').write('survived'); "
        "time.sleep(5)"
    )
    parent_code = (
        "import subprocess, sys, time; "
        f"subprocess.Popen([sys.executable, '-c', {child_code!r}]); "
        "time.sleep(60)"
    )
    manager = MCPServerManager()
    config = MCPServerConfig(
        name="tree-server",
        command=sys.executable,
        args=["-c", parent_code],
        env={},
        transport="stdio",
    )

    manager.start_server(config)
    time.sleep(0.15)
    manager.stop_server("tree-server")
    time.sleep(0.5)

    assert not marker.exists()


def test_manager_rejects_duplicate_server_without_leaking_original() -> None:
    manager = MCPServerManager()
    config = MCPServerConfig(
        name="duplicate-server",
        command=sys.executable,
        args=["-c", "import time; time.sleep(60)"],
        env={},
        transport="stdio",
    )

    original = manager.start_server(config)
    try:
        with pytest.raises(ValueError, match="already registered"):
            manager.start_server(config)
        assert manager.get_server("duplicate-server") is original
        assert original.process is not None
        assert original.process.poll() is None
    finally:
        manager.stop_server("duplicate-server")


def test_manager_scrubs_host_secrets_and_keeps_explicit_server_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("UNRELATED_SECRET", "must-not-leak")
    manager = MCPServerManager()
    config = MCPServerConfig(
        name="test-server",
        command="server",
        args=[],
        env={"SERVER_TOKEN": "explicit-value"},
    )
    process = Mock()

    with patch("ash.mcp.server.subprocess.Popen", return_value=process) as popen:
        manager.start_server(config)

    environment = popen.call_args.kwargs["env"]
    assert environment["SERVER_TOKEN"] == "explicit-value"
    assert "UNRELATED_SECRET" not in environment
    assert environment["PATH"]


def test_manager_stop_all() -> None:
    manager = MCPServerManager()
    for i in range(3):
        config = MCPServerConfig(
            name=f"server-{i}",
            command=sys.executable,
            args=["-c", "import time; time.sleep(60)"],
            env={},
            transport="stdio",
        )
        manager.start_server(config)
    assert len(manager.list_servers()) == 3
    manager.stop_all()
    assert len(manager.list_servers()) == 0


def test_manager_retains_failed_tree_owner_and_continues_stop_all(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = MCPServerManager()
    config = MCPServerConfig(name="unused", command="server", args=[], env={})
    first_process = Mock()
    second_process = Mock()
    manager._servers = {
        "first": MCPServerInstance("first", config, first_process),
        "second": MCPServerInstance("second", config, second_process),
    }
    calls: list[Mock] = []

    def stop(process: Mock, *, plan: object = None) -> None:
        calls.append(process)
        if process is first_process:
            raise ProcessTreeTerminationError("cleanup unconfirmed")

    monkeypatch.setattr("ash.mcp.server._terminate_server_process", stop)

    with pytest.raises(MCPServerLifecycleError, match="process cleanup failed"):
        manager.stop_all()

    assert calls == [first_process, second_process]
    assert manager.get_server("first") is not None
    assert manager.get_server("second") is None


FAKE_MCP_SERVER = r"""
import json, os, sys
for line in sys.stdin:
    message = json.loads(line)
    if "id" not in message:
        continue
    method = message["method"]
    if method == "server/discover":
        print(json.dumps({"jsonrpc": "2.0", "id": message["id"], "error": {"code": -32601, "message": "legacy"}}), flush=True)
        continue
    if method == "initialize":
        result = {"protocolVersion": "2025-06-18", "capabilities": {"tools": {}, "resources": {}, "prompts": {}}, "serverInfo": {"name": "fake", "version": "1"}}
    elif method == "tools/list":
        result = {"tools": [{"name": "echo", "description": "Echo text", "inputSchema": {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]}}]}
    elif method == "tools/call":
        text = message["params"]["arguments"]["text"]
        if text == "__environment__":
            text = f"{os.getenv('SERVER_TOKEN', 'missing')}|{os.getenv('UNRELATED_SECRET', 'missing')}"
        result = {"content": [{"type": "text", "text": text}], "isError": False}
    elif method == "resources/list":
        result = {"resources": [{"uri": "file:///example", "name": "example"}]}
    elif method == "resources/templates/list":
        result = {"resourceTemplates": [{"uriTemplate": "file:///{path}", "name": "file"}]}
    elif method == "prompts/list":
        result = {"prompts": [{"name": "review", "description": "Review"}]}
    elif method == "resources/read":
        result = {"contents": [{"uri": "file:///example", "text": "resource text"}]}
    elif method == "prompts/get":
        result = {"messages": [{"role": "user", "content": {"type": "text", "text": "review it"}}]}
    else:
        result = {}
    print(json.dumps({"jsonrpc": "2.0", "id": message["id"], "result": result}), flush=True)
"""


class IdleProvider(ProviderABC):
    model_name = "idle"

    async def stream_chat(self, messages, temperature=0.0, tools=None):
        yield StreamChunk(content="idle", is_done=True)

    def count_tokens(self, text: str) -> int:
        return len(text.split())


COMPLEX_MCP_INPUT_SCHEMA = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$defs": {
        "identifier": {"type": "string", "pattern": "^[a-z]+$"},
    },
    "type": "object",
    "properties": {
        "mode": {"type": "string", "enum": ["safe", "fast"]},
        "target": {
            "oneOf": [
                {
                    "type": "object",
                    "properties": {
                        "kind": {"const": "path"},
                        "path": {"$ref": "#/$defs/identifier"},
                    },
                    "required": ["kind", "path"],
                    "additionalProperties": False,
                },
                {
                    "type": "object",
                    "properties": {
                        "kind": {"const": "id"},
                        "id": {"type": "integer", "minimum": 1},
                    },
                    "required": ["kind", "id"],
                    "additionalProperties": False,
                },
            ]
        },
        "options": {
            "anyOf": [
                {"type": "null"},
                {
                    "type": "object",
                    "properties": {"enabled": {"type": "boolean"}},
                    "required": ["enabled"],
                    "additionalProperties": False,
                },
            ]
        },
        "tags": {
            "type": "array",
            "items": {"type": "string", "minLength": 1},
            "uniqueItems": True,
        },
        "limit": {"type": "integer", "minimum": 1},
    },
    "required": ["mode", "target", "options", "tags", "limit"],
    "additionalProperties": False,
}


class StubMCPClient:
    def __init__(self, result: dict) -> None:
        self.result = result
        self.calls: list[tuple[str, dict]] = []
        self.config = MCPServerConfig(name="stub", command="fake", args=[], env={})
        self.server_info: dict[str, Any] = {}

    async def call_tool(
        self,
        name: str,
        arguments: dict,
        *,
        expected_contract: str | None = None,
        as_task: bool = False,
        header_annotations: list | None = None,
    ) -> dict:
        del expected_contract, as_task, header_annotations
        self.calls.append((name, arguments))
        return self.result


def _mcp_tool(
    tmp_path: Path,
    client: StubMCPClient,
    *,
    input_schema: dict | None = None,
    output_schema: dict | None = None,
    protocol_version: str = "2025-11-25",
) -> MCPTool:
    definition = {
        "name": "complex",
        "description": "Exercise the complete MCP schema boundary.",
        "inputSchema": input_schema or COMPLEX_MCP_INPUT_SCHEMA,
    }
    if output_schema is not None:
        definition["outputSchema"] = output_schema
    return MCPTool(
        SafetyGuard(tmp_path),
        client=client,  # type: ignore[arg-type]
        server_name="test",
        definition=definition,
        protocol_version=protocol_version,
    )


@pytest.mark.asyncio
async def test_modern_mcp_tool_binds_task_state_to_ash_call_identity(
    tmp_path: Path,
) -> None:
    persisted: list[dict[str, Any]] = []
    client = StubMCPClient({"content": [{"type": "text", "text": "done"}]})

    async def call_tool(
        name: str,
        arguments: dict,
        **kwargs: Any,
    ) -> dict:
        assert name == "durable"
        assert arguments == {}
        callback = kwargs.get("modern_task_state_callback")
        assert callback is not None
        await callback(
            {
                "taskId": "task-1",
                "status": "working",
                "createdAt": "2026-09-20T00:00:00Z",
                "lastUpdatedAt": "2026-09-20T00:00:01Z",
                "ttlMs": None,
            },
            {"approve": "fingerprint"},
        )
        return {"content": [{"type": "text", "text": "done"}]}

    client.call_tool = call_tool  # type: ignore[method-assign]

    async def persist(payload: dict[str, Any]) -> None:
        persisted.append(payload)

    tool = MCPTool(
        SafetyGuard(tmp_path),
        client=client,  # type: ignore[arg-type]
        server_name="durable-server",
        definition={
            "name": "durable",
            "description": "durable task",
            "inputSchema": {"type": "object"},
        },
        protocol_version="2026-07-28",
        task_state_handler=persist,
    )

    with tool.event_context({"call_id": "call-123"}):
        result = await tool.run()

    assert result.success is True
    assert len(persisted) == 1
    payload = persisted[0]
    assert payload["call_id"] == "call-123"
    assert payload["server_name"] == "durable-server"
    assert payload["remote_tool_name"] == "durable"
    assert payload["protocol_version"] == "2026-07-28"
    assert isinstance(payload["contract_fingerprint"], str)
    assert payload["task"]["taskId"] == "task-1"
    assert payload["answered_inputs"] == {"approve": "fingerprint"}


@pytest.mark.asyncio
async def test_mcp_tool_preserves_and_enforces_complete_input_schema(
    tmp_path: Path,
) -> None:
    client = StubMCPClient({"content": [{"type": "text", "text": "ok"}]})
    tool = _mcp_tool(tmp_path, client)
    exposed = tool.json_schema()

    assert exposed == COMPLEX_MCP_INPUT_SCHEMA
    exposed["properties"]["mode"]["enum"].append("mutated")
    assert tool.json_schema() == COMPLEX_MCP_INPUT_SCHEMA

    arguments = {
        "mode": "safe",
        "target": {"kind": "path", "path": "alpha"},
        "options": None,
        "tags": ["one", "two"],
        "limit": 2,
    }
    result = await tool.run(**arguments)

    assert result.success is True
    assert result.output == "ok"
    assert client.calls == [("complex", arguments)]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "overrides",
    [
        {"mode": "unknown"},
        {"target": {"kind": "path", "path": "INVALID"}},
        {"tags": ["same", "same"]},
        {"limit": "2"},
        {"extra": True},
    ],
)
async def test_mcp_tool_rejects_invalid_arguments_without_remote_call(
    tmp_path: Path,
    overrides: dict,
) -> None:
    client = StubMCPClient({"content": []})
    tool = _mcp_tool(tmp_path, client)
    arguments = {
        "mode": "safe",
        "target": {"kind": "id", "id": 4},
        "options": {"enabled": True},
        "tags": ["one"],
        "limit": 2,
        **overrides,
    }

    result = await tool.run(**arguments)

    assert result.success is False
    assert result.error is not None and "invalid MCP tool arguments" in result.error
    assert client.calls == []


def test_mcp_tool_rejects_invalid_and_unknown_schema_dialects(tmp_path: Path) -> None:
    client = StubMCPClient({"content": []})
    with pytest.raises(ValueError, match="not valid JSON Schema"):
        _mcp_tool(
            tmp_path,
            client,
            input_schema={"type": "object", "required": "not-an-array"},
        )
    with pytest.raises(ValueError, match="unsupported JSON Schema dialect"):
        _mcp_tool(
            tmp_path,
            client,
            input_schema={
                "$schema": "https://example.invalid/unknown-dialect",
                "type": "object",
            },
        )

    draft7 = _mcp_tool(
        tmp_path,
        client,
        input_schema={
            "$schema": "http://json-schema.org/draft-07/schema#",
            "type": "object",
            "properties": {"value": {"type": "string"}},
        },
    )
    assert draft7.json_schema()["$schema"].endswith("draft-07/schema#")


def test_mcp_tool_rejects_non_object_roots_and_remote_references(
    tmp_path: Path,
) -> None:
    client = StubMCPClient({"content": []})
    for schema in ({}, {"type": "string"}):
        with pytest.raises(ValueError, match="root type must be object"):
            MCPTool(
                SafetyGuard(tmp_path),
                client=client,  # type: ignore[arg-type]
                server_name="test",
                definition={"name": "invalid", "inputSchema": schema},
            )
    with pytest.raises(ValueError, match="non-local reference"):
        _mcp_tool(
            tmp_path,
            client,
            input_schema={
                "type": "object",
                "properties": {
                    "value": {"$ref": "http://169.254.169.254/latest/meta-data/"}
                },
            },
        )
    with pytest.raises(ValueError, match="root type must be object"):
        _mcp_tool(
            tmp_path,
            client,
            output_schema={"type": "array"},
        )


def test_mcp_tool_preserves_task_execution_support(tmp_path: Path) -> None:
    client = StubMCPClient({"content": []})
    required = MCPTool(
        SafetyGuard(tmp_path),
        client=client,  # type: ignore[arg-type]
        server_name="test",
        definition={
            "name": "task-required",
            "inputSchema": {"type": "object"},
            "execution": {"taskSupport": "required"},
        },
    )

    assert required.name == "mcp__test__task-required"
    assert required._task_support == "required"
    optional = MCPTool(
        SafetyGuard(tmp_path),
        client=client,  # type: ignore[arg-type]
        server_name="test",
        definition={
            "name": "ordinary-or-task",
            "inputSchema": {"type": "object"},
            "execution": {"taskSupport": "optional"},
        },
    )
    assert optional.name == "mcp__test__ordinary-or-task"
    default_forbidden = MCPTool(
        SafetyGuard(tmp_path),
        client=client,  # type: ignore[arg-type]
        server_name="test",
        definition={
            "name": "ordinary",
            "inputSchema": {"type": "object"},
            "execution": {},
        },
    )
    assert default_forbidden.name == "mcp__test__ordinary"


@pytest.mark.asyncio
async def test_mcp_schema_regex_cannot_block_runtime_or_reach_server(
    tmp_path: Path,
) -> None:
    client = StubMCPClient({"content": []})
    tool = _mcp_tool(
        tmp_path,
        client,
        input_schema={
            "type": "object",
            "properties": {"value": {"type": "string", "pattern": "^(a+)+$"}},
            "required": ["value"],
        },
    )

    result = await asyncio.wait_for(tool.run(value="a" * 32 + "!"), timeout=3)

    assert result.success is False
    assert result.error is not None
    assert "deadline" in result.error or "resource limit" in result.error
    assert client.calls == []


@pytest.mark.asyncio
async def test_mcp_schema_worker_ignores_workspace_shadow_package(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    package = workspace / "ash" / "mcp"
    package.mkdir(parents=True)
    (workspace / "ash" / "__init__.py").write_text("", encoding="utf-8")
    (package / "__init__.py").write_text("", encoding="utf-8")
    marker = workspace / "shadow-executed.txt"
    (package / "schema_worker.py").write_text(
        "from pathlib import Path\n"
        f"Path({str(marker)!r}).write_text('executed', encoding='utf-8')\n"
        "print('{\"valid\": true}')\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(workspace)

    result = await _validate_schema_instance(
        {"type": "object", "properties": {"name": {"type": "string"}}},
        {"name": "ash"},
        default_dialect=CURRENT_SCHEMA_DIALECT,
    )

    assert result["valid"] is True
    assert not marker.exists()


@pytest.mark.asyncio
async def test_mcp_legacy_protocol_uses_draft7_for_implicit_schema(
    tmp_path: Path,
) -> None:
    client = StubMCPClient({"content": [{"type": "text", "text": "ok"}]})
    tool = _mcp_tool(
        tmp_path,
        client,
        protocol_version="2025-03-26",
        input_schema={
            "type": "object",
            "properties": {
                "pair": {
                    "type": "array",
                    "items": [{"type": "string"}, {"type": "integer"}],
                    "additionalItems": False,
                }
            },
            "required": ["pair"],
        },
        output_schema={
            "type": "object",
            "required": ["ignored-for-legacy-protocol"],
        },
    )

    result = await tool.run(pair=["one", 2])

    assert result.success is True
    assert result.output == "ok"


@pytest.mark.asyncio
async def test_mcp_schema_allows_properties_named_like_schema_keywords(
    tmp_path: Path,
) -> None:
    client = StubMCPClient({"content": [{"type": "text", "text": "ok"}]})
    tool = _mcp_tool(
        tmp_path,
        client,
        input_schema={
            "type": "object",
            "properties": {
                "$ref": {"type": "string"},
                "patternProperties": {"type": "string"},
            },
            "required": ["$ref", "patternProperties"],
            "additionalProperties": False,
        },
    )

    result = await tool.run(**{"$ref": "literal", "patternProperties": "literal"})

    assert result.success is True


@pytest.mark.asyncio
async def test_mcp_tool_preserves_rich_result_and_validates_output_schema(
    tmp_path: Path,
) -> None:
    output_schema = {
        "type": "object",
        "properties": {"count": {"type": "integer", "minimum": 1}},
        "required": ["count"],
        "additionalProperties": False,
    }
    remote_result = {
        "content": [
            {"type": "text", "text": "two results"},
            {"type": "image", "mimeType": "image/png", "data": "AAAA"},
        ],
        "structuredContent": {"count": 2},
        "isError": False,
        "_meta": {"cacheKey": "stable"},
        "vendorExtension": {"trace": "abc"},
    }
    client = StubMCPClient(remote_result)
    tool = _mcp_tool(
        tmp_path,
        client,
        input_schema={"type": "object", "additionalProperties": False},
        output_schema=output_schema,
    )

    result = await tool.run()

    assert result.success is True
    assert json.loads(result.output) == remote_result


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("output_schema", "structured_content"),
    [
        ({"type": "array", "items": {"type": "integer"}}, [1, 2]),
        ({"type": "string"}, "value"),
        ({"type": "number"}, 1.5),
        ({"type": "boolean"}, True),
        ({"type": "null"}, None),
    ],
)
async def test_modern_mcp_tool_accepts_arbitrary_json_structured_content(
    tmp_path: Path,
    output_schema: dict,
    structured_content: object,
) -> None:
    remote_result = {
        "content": [{"type": "text", "text": "modern structured result"}],
        "structuredContent": structured_content,
    }
    tool = _mcp_tool(
        tmp_path,
        StubMCPClient(remote_result),
        input_schema={"type": "object"},
        output_schema=output_schema,
        protocol_version="2026-07-28",
    )

    result = await tool.run()

    assert result.success is True
    assert json.loads(result.output)["structuredContent"] == structured_content


@pytest.mark.asyncio
async def test_modern_mcp_tool_validates_non_object_structured_content_schema(
    tmp_path: Path,
) -> None:
    remote_result = {
        "content": [{"type": "text", "text": "invalid array"}],
        "structuredContent": [1, "two"],
    }
    tool = _mcp_tool(
        tmp_path,
        StubMCPClient(remote_result),
        input_schema={"type": "object"},
        output_schema={"type": "array", "items": {"type": "integer"}},
        protocol_version="2026-07-28",
    )

    result = await tool.run()

    assert result.success is False
    assert result.error is not None and "invalid MCP structured result" in result.error
    assert json.loads(result.output)["structuredContent"] == [1, "two"]


def test_legacy_mcp_tool_still_requires_object_output_schema(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="outputSchema root type must be object"):
        _mcp_tool(
            tmp_path,
            StubMCPClient({"content": []}),
            input_schema={"type": "object"},
            output_schema={"type": "array", "items": {"type": "integer"}},
            protocol_version="2025-11-25",
        )


@pytest.mark.asyncio
async def test_mcp_tool_preserves_annotated_text_block_envelope(tmp_path: Path) -> None:
    remote_result = {
        "content": [
            {
                "type": "text",
                "text": "annotated",
                "annotations": {"audience": ["assistant"], "priority": 0.8},
                "_meta": {"trace": "one"},
                "vendor": "retained",
            }
        ]
    }
    tool = _mcp_tool(
        tmp_path,
        StubMCPClient(remote_result),
        input_schema={"type": "object"},
    )

    result = await tool.run()

    assert result.success is True
    assert json.loads(result.output)["content"] == remote_result["content"]


@pytest.mark.asyncio
async def test_mcp_tool_keeps_structured_only_and_application_error_envelopes(
    tmp_path: Path,
) -> None:
    structured_client = StubMCPClient(
        {"content": [], "structuredContent": {"items": [1, 2]}}
    )
    structured_tool = _mcp_tool(
        tmp_path,
        structured_client,
        input_schema={"type": "object"},
    )
    structured = await structured_tool.run()
    assert json.loads(structured.output) == {
        "content": [],
        "structuredContent": {"items": [1, 2]},
        "isError": False,
    }

    error_client = StubMCPClient(
        {
            "content": [{"type": "text", "text": "retry with another date"}],
            "structuredContent": {"code": "invalid_date"},
            "isError": True,
            "_meta": {"request": "one"},
        }
    )
    error_tool = _mcp_tool(
        tmp_path,
        error_client,
        input_schema={"type": "object"},
    )
    failed = await error_tool.run()
    assert failed.success is False
    assert failed.error == "retry with another date"
    assert json.loads(failed.output) == {
        "content": [{"type": "text", "text": "retry with another date"}],
        "structuredContent": {"code": "invalid_date"},
        "isError": True,
        "_meta": {"request": "one"},
    }


@pytest.mark.asyncio
async def test_mcp_application_error_output_is_redacted_and_bounded(
    tmp_path: Path,
) -> None:
    marker = "synthetic application error marker"
    remote_result = {
        "content": [
            {
                "type": "text",
                "text": f'password="{marker}" ' + "x" * 5000,
            }
        ],
        "isError": True,
    }
    tool = _mcp_tool(
        tmp_path,
        StubMCPClient(remote_result),
        input_schema={"type": "object"},
    )

    result = await tool.run()

    assert result.success is False
    assert marker not in result.output
    assert marker not in (result.error or "")
    assert len(result.output) <= 512
    assert len(result.error or "") <= 512


@pytest.mark.asyncio
async def test_mcp_capability_tool_redacts_and_bounds_direct_errors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    marker = "synthetic capability error marker"
    runtime = MCPRuntime({}, SafetyGuard(tmp_path))

    async def fail_list_capability(method: str, *, server: str | None = None):
        del method, server
        raise ValueError(f'password="{marker}" ' + "x" * 5000)

    monkeypatch.setattr(runtime, "list_capability", fail_list_capability)
    tool = MCPListResourcesTool(SafetyGuard(tmp_path), runtime)

    result = await tool.run()

    assert result.success is False
    assert marker not in (result.error or "")
    assert len(result.error or "") <= 512


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("remote_result", "message"),
    [
        ({"structuredContent": {"value": 1}}, "content is required"),
        ({"content": None}, "content must be an array"),
        ({"content": [], "structuredContent": None}, "must be an object"),
        ({"content": [], "isError": None}, "isError must be a boolean"),
        ({"content": [], "_meta": None}, "_meta must be an object"),
    ],
)
async def test_mcp_tool_rejects_malformed_results_without_losing_wire_payload(
    tmp_path: Path,
    remote_result: dict,
    message: str,
) -> None:
    tool = _mcp_tool(
        tmp_path,
        StubMCPClient(remote_result),
        input_schema={"type": "object"},
    )

    result = await tool.run()

    assert result.success is False
    assert result.error is not None and message in result.error
    assert json.loads(result.output) == remote_result


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "content",
    [
        {"type": "text", "text": 7},
        {"type": "image", "data": "not base64", "mimeType": "image/png"},
        {"type": "resource", "resource": {"text": "missing uri"}},
        {"type": "unknown", "value": "extension"},
    ],
)
async def test_mcp_tool_rejects_malformed_content_blocks(
    tmp_path: Path,
    content: dict,
) -> None:
    remote_result = {"content": [content]}
    tool = _mcp_tool(
        tmp_path,
        StubMCPClient(remote_result),
        input_schema={"type": "object"},
    )

    result = await tool.run()

    assert result.success is False
    assert result.error is not None and "content[0]" in result.error
    assert json.loads(result.output) == remote_result


@pytest.mark.asyncio
async def test_mcp_tool_preserves_invalid_structured_output_for_recovery(
    tmp_path: Path,
) -> None:
    remote_result = {
        "content": [{"type": "text", "text": "server summary"}],
        "structuredContent": {"count": "two"},
    }
    tool = _mcp_tool(
        tmp_path,
        StubMCPClient(remote_result),
        input_schema={"type": "object"},
        output_schema={
            "type": "object",
            "properties": {"count": {"type": "integer"}},
            "required": ["count"],
        },
    )

    result = await tool.run()

    assert result.success is False
    assert result.error is not None and "invalid MCP structured result" in result.error
    assert json.loads(result.output)["structuredContent"] == {"count": "two"}


@pytest.mark.asyncio
async def test_mcp_tool_requires_structured_content_for_declared_output_schema(
    tmp_path: Path,
) -> None:
    tool = _mcp_tool(
        tmp_path,
        StubMCPClient({"content": [{"type": "text", "text": "summary"}]}),
        input_schema={"type": "object"},
        output_schema={"type": "object"},
    )

    result = await tool.run()

    assert result.success is False
    assert result.output == "summary"
    assert result.error is not None and "requires structuredContent" in result.error


@pytest.mark.asyncio
async def test_mcp_tool_rejects_non_json_wire_values(tmp_path: Path) -> None:
    tool = _mcp_tool(
        tmp_path,
        StubMCPClient({"content": [], "structuredContent": {"value": float("nan")}}),
        input_schema={"type": "object"},
    )

    result = await tool.run()

    assert result.success is False
    assert result.output == ""
    assert result.error is not None and "not JSON-serializable" in result.error


@pytest.mark.asyncio
async def test_mcp_tool_preserves_protocol_error_data_without_replay(
    tmp_path: Path,
) -> None:
    class ErrorClient:
        def __init__(self) -> None:
            self.calls = 0

        async def call_tool(
            self,
            name: str,
            arguments: dict,
            *,
            expected_contract: str | None = None,
            as_task: bool = False,
            header_annotations: list | None = None,
        ) -> dict:
            del expected_contract
            del as_task
            self.calls += 1
            raise MCPProtocolError(
                "tools/call failed (-32602): invalid mode",
                code=-32602,
                data={"field": "mode", "expected": ["safe", "fast"]},
            )

    client = ErrorClient()
    tool = MCPTool(
        SafetyGuard(tmp_path),
        client=client,  # type: ignore[arg-type]
        server_name="test",
        definition={"name": "fails", "inputSchema": {"type": "object"}},
    )

    result = await tool.run()

    assert result.success is False
    assert client.calls == 1
    assert result.outcome == "unknown"
    assert json.loads(result.output) == {
        "error": {
            "type": "mcp_protocol_error",
            "message": "tools/call failed (-32602): invalid mode",
            "code": -32602,
            "data": {"field": "mode", "expected": ["safe", "fast"]},
        }
    }


@pytest.mark.asyncio
async def test_mcp_tool_redacts_secret_values_in_protocol_errors(
    tmp_path: Path,
) -> None:
    marker = "synthetic protocol marker"

    class ErrorClient:
        async def call_tool(
            self,
            name: str,
            arguments: dict,
            *,
            expected_contract: str | None = None,
            as_task: bool = False,
            header_annotations: list | None = None,
        ) -> dict:
            del name, arguments, expected_contract, as_task, header_annotations
            raise MCPProtocolError(
                f'upstream payload={{\\"password\\": \\"{marker}\\"}} '
                f'password="line one\n{marker}"',
                code=-32602,
                data={"password": marker, "detail": "not secret"},
            )

    tool = MCPTool(
        SafetyGuard(tmp_path),
        client=ErrorClient(),  # type: ignore[arg-type]
        server_name="test",
        definition={"name": "fails", "inputSchema": {"type": "object"}},
    )

    result = await tool.run()
    await SecretRedactionMiddleware().after_tool("mcp__test__fails", {}, result)

    assert marker not in result.output
    assert marker not in (result.error or "")
    payload = json.loads(result.output)
    assert payload["error"]["data"] == {
        "password": "[REDACTED]",
        "detail": "not secret",
    }


@pytest.mark.asyncio
async def test_mcp_tool_preserves_explicit_null_protocol_error_data(
    tmp_path: Path,
) -> None:
    class ErrorClient:
        async def call_tool(
            self,
            name: str,
            arguments: dict,
            *,
            expected_contract: str | None = None,
            as_task: bool = False,
            header_annotations: list | None = None,
        ) -> dict:
            del expected_contract
            del as_task
            raise MCPProtocolError("explicit null", code=-32000, data=None)

    tool = MCPTool(
        SafetyGuard(tmp_path),
        client=ErrorClient(),  # type: ignore[arg-type]
        server_name="test",
        definition={"name": "fails", "inputSchema": {"type": "object"}},
    )

    result = await tool.run()

    assert json.loads(result.output)["error"]["data"] is None


@pytest.mark.asyncio
async def test_mcp_output_schema_applies_to_application_errors(tmp_path: Path) -> None:
    remote_result = {
        "content": [{"type": "text", "text": "failed"}],
        "structuredContent": {"code": 7},
        "isError": True,
    }
    tool = _mcp_tool(
        tmp_path,
        StubMCPClient(remote_result),
        input_schema={"type": "object"},
        output_schema={
            "type": "object",
            "properties": {"code": {"type": "string"}},
            "required": ["code"],
        },
    )

    result = await tool.run()

    assert result.success is False
    assert result.error is not None and "invalid MCP structured result" in result.error
    assert json.loads(result.output) == remote_result


@pytest.mark.asyncio
async def test_mcp_client_retains_jsonrpc_error_code_and_data() -> None:
    client = MCPClient(MCPServerConfig(name="fake", command="fake", args=[], env={}))
    client._request_stdio = AsyncMock(
        return_value={
            "jsonrpc": "2.0",
            "id": 1,
            "error": {
                "code": -32602,
                "message": "invalid arguments",
                "data": {"field": "query"},
            },
        }
    )

    with pytest.raises(MCPProtocolError) as caught:
        await client.request("tools/call", {"name": "search", "arguments": {}})

    assert caught.value.code == -32602
    assert caught.value.data == {"field": "query"}


@pytest.mark.asyncio
async def test_mcp_client_rejects_boolean_error_code_and_distinguishes_data() -> None:
    client = MCPClient(MCPServerConfig(name="fake", command="fake", args=[], env={}))
    client._request_stdio = AsyncMock(
        return_value={
            "jsonrpc": "2.0",
            "id": 1,
            "error": {"code": True, "message": "invalid", "data": None},
        }
    )

    with pytest.raises(MCPProtocolError) as caught:
        await client.request("tools/call")

    assert caught.value.code is None
    assert "invalid error code" in str(caught.value)
    assert caught.value.has_data is True
    assert caught.value.data is None


def _task_client(
    responses: list[dict | Exception],
    *,
    timeout: float = 30.0,
) -> tuple[MCPClient, AsyncMock]:
    client = MCPClient(
        MCPServerConfig(name="fake", command="fake", args=[], env={}),
        timeout=timeout,
    )
    client.protocol_version = "2025-11-25"
    client.server_capabilities = {
        "tasks": {
            "cancel": {},
            "list": {},
            "requests": {"tools": {"call": {}}},
        }
    }
    request = AsyncMock(side_effect=responses)
    client.request = request  # type: ignore[method-assign]
    return client, request


@pytest.mark.asyncio
async def test_mcp_required_task_tool_polls_and_fetches_result() -> None:
    client, request = _task_client(
        [
            {
                "task": {
                    "taskId": "one",
                    "status": "working",
                    "ttl": None,
                    "pollInterval": 0,
                }
            },
            {"task": {"taskId": "one", "status": "completed", "ttl": None}},
            {"content": [{"type": "text", "text": "done"}]},
        ]
    )
    result = await client.call_tool("long", {}, as_task=True)

    assert result["content"][0]["text"] == "done"
    assert [call.args[0] for call in request.await_args_list] == [
        "tools/call",
        "tasks/get",
        "tasks/result",
    ]
    assert request.await_args_list[0].args[1]["task"] == {}
    assert request.await_args_list[1].args[1] == {"taskId": "one"}


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["failed", "cancelled"])
async def test_mcp_task_terminal_failure_is_not_fetched(status: str) -> None:
    client, request = _task_client(
        [
            {
                "task": {
                    "taskId": "bad",
                    "status": status,
                    "ttl": None,
                    "statusMessage": "no",
                }
            },
        ]
    )

    with pytest.raises(MCPProtocolError, match=f"MCP tool task {status}: no"):
        await client.call_tool("long", {}, as_task=True)

    assert request.await_count == 1


@pytest.mark.asyncio
async def test_mcp_task_status_notification_wakes_without_polling() -> None:
    client, request = _task_client(
        [
            {
                "task": {
                    "taskId": "fast",
                    "status": "working",
                    "ttl": None,
                    "pollInterval": 100000,
                }
            },
            {"content": [{"type": "text", "text": "notified"}]},
        ]
    )

    call = asyncio.create_task(client.call_tool("long", {}, as_task=True))
    await asyncio.sleep(0.01)
    await client._handle_incoming(
        {
            "jsonrpc": "2.0",
            "method": "notifications/tasks/status",
            "params": {
                "taskId": "fast",
                "status": "completed",
                "createdAt": "2025-11-25T10:30:00Z",
                "lastUpdatedAt": "2025-11-25T10:31:00Z",
                "ttl": None,
            },
        }
    )

    result = await asyncio.wait_for(call, 1)

    assert result["content"][0]["text"] == "notified"
    assert [sent.args[0] for sent in request.await_args_list] == [
        "tools/call",
        "tasks/result",
    ]


@pytest.mark.asyncio
async def test_mcp_task_invalid_notifications_are_ignored_and_fallback_polls() -> None:
    client, request = _task_client(
        [
            {
                "task": {
                    "taskId": "safe",
                    "status": "working",
                    "ttl": None,
                    "pollInterval": 10,
                }
            },
            {
                "task": {
                    "taskId": "safe",
                    "status": "completed",
                    "ttl": None,
                }
            },
            {"content": [{"type": "text", "text": "polled"}]},
        ]
    )

    call = asyncio.create_task(client.call_tool("long", {}, as_task=True))
    await asyncio.sleep(0.01)
    await client._handle_incoming(
        {
            "jsonrpc": "2.0",
            "method": "notifications/tasks/status",
            "params": {"taskId": "other", "status": "completed", "ttl": None},
        }
    )
    await client._handle_incoming(
        {
            "jsonrpc": "2.0",
            "method": "notifications/tasks/status",
            "params": {"taskId": "safe", "status": "exploded", "ttl": None},
        }
    )

    result = await asyncio.wait_for(call, 1)

    assert result["content"][0]["text"] == "polled"
    assert [sent.args[0] for sent in request.await_args_list] == [
        "tools/call",
        "tasks/get",
        "tasks/result",
    ]


@pytest.mark.asyncio
async def test_mcp_task_status_notification_can_update_before_terminal() -> None:
    client, request = _task_client(
        [
            {
                "task": {
                    "taskId": "ordered",
                    "status": "input_required",
                    "ttl": None,
                    "pollInterval": 100000,
                    "statusMessage": "need input",
                }
            },
            {
                "task": {
                    "taskId": "ordered",
                    "status": "completed",
                    "ttl": None,
                }
            },
            {"content": [{"type": "text", "text": "resumed"}]},
        ]
    )

    call = asyncio.create_task(client.call_tool("long", {}, as_task=True))
    await asyncio.sleep(0.01)
    await client._handle_incoming(
        {
            "jsonrpc": "2.0",
            "method": "notifications/tasks/status",
            "params": {
                "taskId": "ordered",
                "status": "working",
                "createdAt": "2025-11-25T10:30:00Z",
                "lastUpdatedAt": "2025-11-25T10:31:00Z",
                "ttl": None,
                "pollInterval": 10,
            },
        }
    )

    result = await asyncio.wait_for(call, 1)

    assert result["content"][0]["text"] == "resumed"
    assert [sent.args[0] for sent in request.await_args_list] == [
        "tools/call",
        "tasks/get",
        "tasks/result",
    ]

    assert request.await_args_list[1].args[1] == {"taskId": "ordered"}


@pytest.mark.asyncio
async def test_mcp_input_required_opens_result_then_resumes_polling() -> None:
    client, request = _task_client([])
    states = iter(["working", "input_required", "working", "completed"])
    result_calls = 0

    def task_state():
        status = next(states)
        return {
            "taskId": "input",
            "status": status,
            "createdAt": "2025-11-25T10:00:00Z",
            "lastUpdatedAt": "2025-11-25T10:01:00Z",
            "ttl": None,
            "pollInterval": 0,
        }

    async def respond(method, params, **_):
        nonlocal result_calls
        del params
        if method == "tools/call":
            return {"task": task_state()}
        if method == "tasks/get":
            return {"task": task_state()}
        if method == "tasks/result":
            result_calls += 1
            if result_calls < 2:
                return {"value": {}}
            return {"content": [{"type": "text", "text": "answered"}]}
        raise AssertionError(f"unexpected MCP method {method}")

    request.side_effect = respond

    result = await client.call_tool("long", {}, as_task=True)

    assert result["content"][0]["text"] == "answered"
    assert [sent.args[0] for sent in request.await_args_list] == [
        "tools/call",
        "tasks/get",
        "tasks/result",
        "tasks/get",
        "tasks/get",
        "tasks/result",
    ]
    assert request.await_args_list[3].args[1] == {"taskId": "input"}


@pytest.mark.asyncio
async def test_mcp_task_timeout_cancels_remote_task() -> None:
    client, request = _task_client([], timeout=0.01)
    responses = iter(
        [
            {
                "task": {
                    "taskId": "slow",
                    "status": "working",
                    "ttl": None,
                    "pollInterval": 0,
                }
            },
        ]
    )

    async def request_side_effect(method, params, **_):
        del params
        if method == "tools/call":
            return next(responses)
        return {"task": {"taskId": "slow", "status": "working", "ttl": None}}

    request.side_effect = request_side_effect
    client._task_poll_delay = lambda task: 1.0  # type: ignore[method-assign]

    with pytest.raises(MCPTaskTimeout):
        await client.call_tool("slow", {}, as_task=True)

    assert request.await_args_list[-1].args[:2] == ("tasks/cancel", {"taskId": "slow"})


@pytest.mark.asyncio
async def test_mcp_list_tasks_paginates_and_validates_states() -> None:
    client, request = _task_client([])
    responses = [
        {
            "tasks": [
                {
                    "taskId": "working",
                    "status": "working",
                    "createdAt": "2025-11-25T10:00:00Z",
                    "lastUpdatedAt": "2025-11-25T10:01:00Z",
                    "ttl": None,
                }
            ],
            "nextCursor": "page-two",
        },
        {"tasks": []},
    ]

    async def request_side_effect(method, params, **_):
        del params
        return responses.pop(0)

    request.side_effect = request_side_effect
    tasks = await client.list_mcp_tasks()

    assert [task["taskId"] for task in tasks] == ["working"]
    assert [sent.args[0] for sent in request.await_args_list] == [
        "tasks/list",
        "tasks/list",
    ]
    assert request.await_args_list[1].args[1] == {"cursor": "page-two"}


@pytest.mark.asyncio
async def test_mcp_list_tasks_rejects_oversized_aggregate_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, request = _task_client([])
    task = {
        "taskId": "working",
        "status": "working",
        "createdAt": "2025-11-25T10:00:00Z",
        "lastUpdatedAt": "2025-11-25T10:01:00Z",
        "ttl": None,
    }
    request.side_effect = [
        {"tasks": [task], "nextCursor": "page-two"},
        {"tasks": [{**task, "taskId": "second"}]},
    ]
    monkeypatch.setattr(mcp_client_module, "MAX_PAGINATION_RESULT_BYTES", 200)

    with pytest.raises(MCPProtocolError, match="aggregate result exceeds"):
        await client.list_mcp_tasks()


@pytest.mark.asyncio
async def test_mcp_list_tasks_requires_capability_and_fails_closed() -> None:
    client, request = _task_client([])
    request.side_effect = AssertionError("must not call unadvertised method")
    del client.server_capabilities["tasks"]["list"]

    with pytest.raises(MCPProtocolError, match="tasks/list"):
        await client.list_mcp_tasks()

    request.assert_not_awaited()


@pytest.mark.asyncio
async def test_mcp_cancel_task_validates_response_and_capability() -> None:
    client, request = _task_client(
        [
            {"task": {}},
            {"task": {}},
        ]
    )
    cancelled = {
        "taskId": "stop",
        "status": "cancelled",
        "createdAt": "2025-11-25T10:00:00Z",
        "lastUpdatedAt": "2025-11-25T10:01:00Z",
        "ttl": None,
        "statusMessage": "stopped by user",
    }
    request.side_effect = [
        {"task": cancelled},
        {"task": {**cancelled, "taskId": "different"}},
    ]
    task = await client.cancel_mcp_task("stop")

    assert task == cancelled
    assert request.await_args.args == ("tasks/cancel", {"taskId": "stop"})

    with pytest.raises(MCPProtocolError, match="another taskId"):
        await client.cancel_mcp_task("stop")

    del client.server_capabilities["tasks"]["cancel"]
    request.reset_mock()
    with pytest.raises(MCPProtocolError, match="tasks/cancel"):
        await client.cancel_mcp_task("stop")
    request.assert_not_awaited()


@pytest.mark.asyncio
async def test_runtime_cancel_task_binds_server_and_records_errors(
    tmp_path: Path,
) -> None:
    class RuntimeClient:
        def __init__(self) -> None:
            self.calls = []

        async def cancel_mcp_task(self, task_id):
            self.calls.append(task_id)
            return {"taskId": task_id, "status": "cancelled"}

    config = MCPServerConfig(name="unused", command="unused", args=[], env={})
    runtime = MCPRuntime({"fake": config}, SafetyGuard(tmp_path))
    client = RuntimeClient()
    runtime.clients["fake"] = client

    task = await runtime.cancel_task("fake", "stop")

    assert task == {"server": "fake", "taskId": "stop", "status": "cancelled"}
    assert client.calls == ["stop"]
    assert not runtime.errors

    with pytest.raises(ValueError, match="unknown MCP server"):
        await runtime.cancel_task("missing", "stop")


@pytest.mark.asyncio
async def test_mcp_task_cancellation_sends_tasks_cancel() -> None:
    client, request = _task_client(
        [
            {
                "task": {
                    "taskId": "stop",
                    "status": "working",
                    "ttl": None,
                    "pollInterval": 100000,
                }
            },
        ]
    )

    async def cancel_side_effect(method, params, **_):
        if method != "tools/call":
            return {"task": {"taskId": "stop", "status": "cancelled", "ttl": None}}
        return {
            "task": {
                "taskId": "stop",
                "status": "working",
                "ttl": None,
                "pollInterval": 100000,
            }
        }

    request.side_effect = cancel_side_effect
    call = asyncio.create_task(client.call_tool("stop", {}, as_task=True))
    await asyncio.sleep(0)
    call.cancel()
    with pytest.raises(asyncio.CancelledError):
        await call

    await asyncio.sleep(0)
    assert request.await_args_list[-1].args[:2] == ("tasks/cancel", {"taskId": "stop"})


@pytest.mark.asyncio
async def test_mcp_request_cancellation_owns_one_notification_until_settled() -> None:
    client = MCPClient(MCPServerConfig(name="fake", command="fake", args=[], env={}))
    request_started = asyncio.Event()
    cancel_started = asyncio.Event()
    cancel_release = asyncio.Event()
    cancel_finished = asyncio.Event()
    cancel_calls = 0

    async def blocked_request(*args: object, **kwargs: object) -> dict[str, object]:
        del args, kwargs
        request_started.set()
        await asyncio.Event().wait()
        return {}

    async def blocked_cancel(request_id: int, reason: str) -> None:
        nonlocal cancel_calls
        assert request_id == 1
        assert reason == "tools/call was cancelled"
        cancel_calls += 1
        cancel_started.set()
        await cancel_release.wait()
        cancel_finished.set()

    client._request_stdio = blocked_request  # type: ignore[method-assign]
    client._cancel_request = blocked_cancel  # type: ignore[method-assign]
    request_task = asyncio.create_task(client.request("tools/call"))
    await request_started.wait()

    request_task.cancel()
    await cancel_started.wait()
    request_task.cancel()
    await asyncio.sleep(0)
    assert not request_task.done()
    assert cancel_calls == 1
    assert not cancel_finished.is_set()

    cancel_release.set()
    with pytest.raises(asyncio.CancelledError):
        await request_task
    assert cancel_finished.is_set()
    assert not any(
        task.get_name().startswith("ash-mcp-cancel-request-")
        and not task.done()
        for task in asyncio.all_tasks()
    )


@pytest.mark.asyncio
async def test_mcp_task_cancellation_owns_one_cancel_request_until_settled() -> None:
    client, request = _task_client(
        [
            {
                "task": {
                    "taskId": "stop-once",
                    "status": "working",
                    "ttl": None,
                    "pollInterval": 100000,
                }
            }
        ]
    )
    cancel_started = asyncio.Event()
    cancel_release = asyncio.Event()
    cancel_finished = asyncio.Event()
    cancel_calls = 0

    async def blocked_cancel(task_id: str) -> None:
        nonlocal cancel_calls
        assert task_id == "stop-once"
        cancel_calls += 1
        cancel_started.set()
        await cancel_release.wait()
        cancel_finished.set()

    client._cancel_mcp_task = blocked_cancel  # type: ignore[method-assign]
    call = asyncio.create_task(client.call_tool("slow", {}, as_task=True))
    await asyncio.sleep(0)
    call.cancel()
    await cancel_started.wait()
    call.cancel()
    await asyncio.sleep(0)
    assert not call.done()
    assert cancel_calls == 1
    assert not cancel_finished.is_set()

    cancel_release.set()
    with pytest.raises(asyncio.CancelledError):
        await call
    assert cancel_finished.is_set()
    assert request.await_count == 1


@pytest.mark.asyncio
async def test_mcp_runtime_isolates_invalid_tool_schema(
    tmp_path: Path, monkeypatch
) -> None:
    class CatalogClient:
        server_capabilities = {"tools": {}}

        def __init__(self, config, *, roots=()) -> None:
            self.config = config

        async def connect(self) -> None:
            return None

        def supports_server_capability(self, name: str) -> bool:
            return name == "tools"

        async def list_tools(self) -> list[dict]:
            return [
                {
                    "name": "broken",
                    "inputSchema": {"type": "object", "required": "invalid"},
                },
                {
                    "name": "task-only",
                    "inputSchema": {"type": "object"},
                    "execution": {"taskSupport": "required"},
                },
                {"name": "healthy", "inputSchema": {"type": "object"}},
            ]

        async def disconnect(self) -> None:
            return None

    monkeypatch.setattr("ash.mcp.runtime.MCPClient", CatalogClient)
    config = MCPServerConfig(name="catalog", command="unused", args=[], env={})
    runtime = MCPRuntime({"catalog": config}, SafetyGuard(tmp_path))

    tools = await runtime.start()
    try:
        assert "mcp__catalog__healthy" in tools
        assert "mcp__catalog__broken" not in tools
        assert "mcp__catalog__task-only" in tools
        assert "not valid JSON Schema" in runtime.errors["catalog:tool:broken"]
        assert "catalog:tool:task-only" not in runtime.errors
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_async_client_initializes_lists_and_calls_tools() -> None:
    config = MCPServerConfig(
        name="fake",
        command=sys.executable,
        args=["-u", "-c", FAKE_MCP_SERVER],
        env={},
    )
    client = MCPClient(config)
    await asyncio.wait_for(client.connect(), timeout=1)
    try:
        assert client.protocol_version == "2025-06-18"
        assert client.server_info == {"name": "fake", "version": "1"}
        assert client.supports_server_capability("tools") is True
        tools = await client.list_tools()
        assert tools[0]["name"] == "echo"
        result = await client.call_tool("echo", {"text": "hello"})
        assert result["content"][0]["text"] == "hello"
    finally:
        await client.disconnect()


@pytest.mark.skipif(os.name == "nt", reason="POSIX cwd identity regression")
@pytest.mark.asyncio
async def test_stdio_client_refuses_cwd_replaced_after_config_creation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    saved = tmp_path / "workspace-saved"
    workspace.mkdir()
    config = MCPServerConfig(
        name="config-cwd-race",
        command=sys.executable,
        args=["-c", "pass"],
        env={},
        cwd=str(workspace),
    )
    workspace.rename(saved)
    workspace.mkdir()
    create = AsyncMock(side_effect=AssertionError("MCP stdio must not launch"))
    monkeypatch.setattr("ash.mcp.client.asyncio.create_subprocess_exec", create)
    client = MCPClient(config)

    with pytest.raises(MCPProtocolError, match="working directory identity changed"):
        await client.connect()

    create.assert_not_awaited()


@pytest.mark.skipif(os.name == "nt", reason="POSIX cwd race regression")
@pytest.mark.asyncio
async def test_stdio_client_cwd_swap_cannot_escape_workspace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ash.sandbox import process_utils as process_utils_module

    workspace = tmp_path / "workspace"
    outside = tmp_path / "outside"
    saved = tmp_path / "workspace-saved"
    workspace.mkdir()
    outside.mkdir()
    cwd_log = tmp_path / "client-cwd.txt"
    server = (
        "import os\n"
        "from pathlib import Path\n"
        f"Path({str(cwd_log)!r}).write_text(os.getcwd(), encoding='utf-8')\n"
        + FAKE_MCP_SERVER
    )
    config = MCPServerConfig(
        name="cwd-race",
        command=sys.executable,
        args=["-u", "-c", server],
        env={},
        cwd=str(workspace),
    )
    real_prepare = process_utils_module.prepare_process_tree
    swapped = False

    def prepare_then_swap(*args, **kwargs):
        nonlocal swapped
        plan = real_prepare(*args, **kwargs)
        if not swapped:
            swapped = True
            workspace.rename(saved)
            try:
                workspace.symlink_to(outside, target_is_directory=True)
            except OSError as exc:
                pytest.skip(f"symlink creation is unavailable: {exc}")
        return plan

    monkeypatch.setattr("ash.mcp.client.prepare_process_tree", prepare_then_swap)
    client = MCPClient(config)
    try:
        await asyncio.wait_for(client.connect(), timeout=1)
    finally:
        await client.disconnect()

    assert swapped is True
    assert Path(cwd_log.read_text(encoding="utf-8")).resolve() == saved.resolve()


@pytest.mark.asyncio
async def test_stdio_client_fails_closed_when_stable_cwd_is_unavailable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ash.sandbox.process_utils import ProcessTreeUnavailable

    config = MCPServerConfig(
        name="stable-cwd",
        command=sys.executable,
        args=["-c", "pass"],
        env={},
        cwd=str(tmp_path),
    )

    def unavailable(*args, **kwargs):
        raise ProcessTreeUnavailable("stable cwd unavailable")

    create = AsyncMock(side_effect=AssertionError("MCP stdio must not launch"))
    monkeypatch.setattr("ash.mcp.client.prepare_scoped_process_launch", unavailable)
    monkeypatch.setattr("ash.mcp.client.asyncio.create_subprocess_exec", create)
    client = MCPClient(config)

    with pytest.raises(MCPProtocolError, match="stable cwd unavailable"):
        await client.connect()

    create.assert_not_awaited()


@pytest.mark.asyncio
async def test_runtime_list_tasks_aggregates_and_records_errors(
    tmp_path: Path,
) -> None:
    class RuntimeClient:
        def __init__(self, *, fail: bool = False) -> None:
            self.fail = fail

        async def list_mcp_tasks(self) -> list[dict[str, str]]:
            if self.fail:
                raise RuntimeError("unavailable")
            return [
                {
                    "taskId": "one",
                    "status": "working",
                    "statusMessage": "running",
                },
                {"taskId": "two", "status": "completed"},
            ]

    config = MCPServerConfig(name="unused", command="unused", args=[], env={})
    runtime = MCPRuntime(
        {"healthy": config, "broken": config},
        SafetyGuard(tmp_path),
    )
    runtime.clients = {
        "healthy": RuntimeClient(),
        "broken": RuntimeClient(fail=True),
    }

    tasks = await runtime.list_tasks()

    assert [(task["server"], task["taskId"]) for task in tasks] == [
        ("healthy", "one"),
        ("healthy", "two"),
    ]
    assert runtime.errors["broken:list_mcp_tasks"] == "unavailable"


@pytest.mark.asyncio
async def test_stdio_client_accepts_bounded_rich_results_above_64_kib() -> None:
    config = MCPServerConfig(
        name="fake",
        command=sys.executable,
        args=["-u", "-c", FAKE_MCP_SERVER],
        env={},
    )
    client = MCPClient(config)
    await asyncio.wait_for(client.connect(), timeout=1)
    try:
        text = "x" * 70_000
        result = await client.call_tool("echo", {"text": text})
        assert result["content"] == [{"type": "text", "text": text}]
    finally:
        await client.disconnect()


@pytest.mark.asyncio
async def test_async_client_scrubs_host_secrets_and_keeps_explicit_server_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("UNRELATED_SECRET", "must-not-leak")
    config = MCPServerConfig(
        name="fake",
        command=sys.executable,
        args=["-u", "-c", FAKE_MCP_SERVER],
        env={"SERVER_TOKEN": "explicit-value"},
    )
    client = MCPClient(config)
    await client.connect()
    try:
        result = await client.call_tool("echo", {"text": "__environment__"})
        assert result["content"][0]["text"] == "explicit-value|missing"
    finally:
        await client.disconnect()


@pytest.mark.asyncio
async def test_runtime_registers_namespaced_tool(tmp_path: Path) -> None:
    config = MCPServerConfig(
        name="fake",
        command=sys.executable,
        args=["-u", "-c", FAKE_MCP_SERVER],
        env={},
    )
    runtime = MCPRuntime({"fake": config}, SafetyGuard(tmp_path))
    tools = await runtime.start()
    try:
        tool = tools["mcp__fake__echo"]
        result = await tool.run(text="hello")
        assert result.success is True
        assert result.output == "hello"
        assert (await runtime.list_resources())[0]["uri"] == "file:///example"
        assert (await runtime.list_prompts())[0]["name"] == "review"
        listed_resources = await tools["mcp_list_resources"].run(server="fake")
        listed_templates = await tools["mcp_list_resource_templates"].run()
        listed_prompts = await tools["mcp_list_prompts"].run()
        assert "file:///example" in listed_resources.output
        assert "file:///{path}" in listed_templates.output
        assert "review" in listed_prompts.output
        resource = await tools["mcp_read_resource"].run(
            server="fake", uri="file:///example"
        )
        assert "resource text" in resource.output
        prompt = await tools["mcp_get_prompt"].run(server="fake", name="review")
        assert "review it" in prompt.output
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_runtime_applies_plugin_mcp_working_directory_and_environment(
    tmp_path: Path,
) -> None:
    plugin = tmp_path / "plugin"
    plugin.mkdir()
    server = FAKE_MCP_SERVER.replace(
        'message["params"]["arguments"]["text"]',
        "__import__('os').getcwd() + '|' + __import__('os').environ['ASH_PLUGIN_ROOT']",
    )
    config = MCPServerConfig(
        name="example__fake",
        command=sys.executable,
        args=["-u", "-c", server],
        env={"ASH_PLUGIN_ROOT": str(plugin)},
        cwd=str(plugin),
    )
    runtime = MCPRuntime({"example__fake": config}, SafetyGuard(tmp_path))
    tools = await runtime.start()
    try:
        result = await tools["mcp__example__fake__echo"].run(text="ignored")
        assert result.output == f"{plugin}|{plugin}"
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_loop_reloads_mcp_tools_without_restarting_session(
    tmp_path: Path,
) -> None:
    config = MCPServerConfig(
        name="fake",
        command=sys.executable,
        args=["-u", "-c", FAKE_MCP_SERVER],
        env={},
    )
    loop = AshLoop(
        session_store=SessionStore(tmp_path / "sessions.db"),
        provider=IdleProvider(),
        safety_guard=SafetyGuard(tmp_path),
        ui=HeadlessUI(output_format="text", stream=io.StringIO()),
        project_root=tmp_path,
        mcp_configs={"fake": config},
    )
    await loop.start_session()
    assert "mcp__fake__echo" in loop.tools

    errors = await loop.reload_mcp_servers({})

    assert errors == {}
    assert "mcp__fake__echo" not in loop.tools
    await loop.aclose()


@pytest.mark.asyncio
async def test_loop_persists_mcp_task_only_for_active_tool_call(
    tmp_path: Path,
) -> None:
    from ash.context.turn import TurnContext

    store = SessionStore(tmp_path / "sessions.db")
    loop = AshLoop(
        session_store=store,
        provider=IdleProvider(),
        safety_guard=SafetyGuard(tmp_path),
        ui=HeadlessUI(output_format="text", stream=io.StringIO()),
        project_root=tmp_path,
    )
    session = await loop.start_session()
    loop.turn_context = TurnContext(session.session_id, "turn-1")
    loop.turn_context.set("tool_call_id", "call-1")
    payload = {
        "call_id": "call-1",
        "server_name": "server",
        "remote_tool_name": "slow",
        "contract_fingerprint": "contract",
        "server_fingerprint": "server-fingerprint",
        "protocol_version": "2026-07-28",
        "task": {
            "taskId": "task-1",
            "status": "working",
            "createdAt": "2026-09-20T00:00:00Z",
            "lastUpdatedAt": "2026-09-20T00:00:01Z",
            "ttlMs": None,
        },
        "answered_inputs": {},
    }
    try:
        await loop._persist_mcp_task_state(payload)
        rows = store.list_mcp_tasks(session.session_id)
        assert len(rows) == 1
        assert rows[0]["task_id"] == "task-1"
        assert rows[0]["call_id"] == "call-1"
        assert rows[0]["turn_id"] == "turn-1"

        loop.turn_context.set("tool_call_id", "another-call")
        with pytest.raises(RuntimeError, match="does not match active tool"):
            await loop._persist_mcp_task_state(payload)
        assert len(store.list_mcp_tasks(session.session_id)) == 1
    finally:
        loop.turn_context = None
        await loop.aclose()


@pytest.mark.asyncio
async def test_resume_modern_task_refuses_new_input_during_recovery() -> None:
    client = MCPClient(MCPServerConfig(name="server", command="fake", args=[], env={}))
    client.protocol_version = "2026-07-28"
    client.server_capabilities = {
        "extensions": {"io.modelcontextprotocol/tasks": {}}
    }
    methods: list[str] = []

    async def request(method: str, params: dict, **kwargs: Any) -> dict:
        del params, kwargs
        methods.append(method)
        assert method == "tasks/get"
        return {
            "taskId": "task-recovery-input",
            "status": "input_required",
            "createdAt": "2026-09-20T00:00:00Z",
            "lastUpdatedAt": "2026-09-20T00:00:01Z",
            "ttlMs": 60_000,
            "pollIntervalMs": 0,
            "inputRequests": {"request": {}},
        }

    client.request = request  # type: ignore[method-assign]
    client._fulfill_modern_input_requests = AsyncMock(return_value={"request": {}})  # type: ignore[method-assign]
    client._start_modern_task_subscription = Mock()  # type: ignore[method-assign]
    client._stop_modern_task_subscription = AsyncMock()  # type: ignore[method-assign]

    with pytest.raises(MCPProtocolError, match="requires new input during recovery"):
        await client.resume_modern_task(
            {
                "taskId": "task-recovery-input",
                "status": "working",
                "createdAt": "2026-09-20T00:00:00Z",
                "lastUpdatedAt": "2026-09-20T00:00:00Z",
                "ttlMs": 60_000,
                "pollIntervalMs": 0,
            },
            {},
        )

    assert methods == ["tasks/get"]
    client._fulfill_modern_input_requests.assert_not_awaited()  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_loop_resumes_persisted_mcp_task_without_replaying_tool_call(
    tmp_path: Path,
) -> None:
    store = SessionStore(tmp_path / "sessions.db")
    session = store.create_session(str(tmp_path))
    turn_id = "turn-crash"
    call_id = "call-crash"
    store.start_turn(session.session_id, turn_id, "run long MCP task")
    store.save_tool_call(
        session.session_id,
        ToolCallRecord(
            call_id=call_id,
            tool_name="mcp__server__long",
            arguments={},
            approved=True,
            executed=False,
            dispatched=True,
            timestamp=datetime.now(timezone.utc),
        ),
        turn_id=turn_id,
    )
    task_id = "durable-task"
    methods: list[str] = []
    definition = {
        "name": "long",
        "description": "Long-running durable task",
        "inputSchema": {"type": "object", "additionalProperties": False},
    }

    first_client = MCPClient(
        MCPServerConfig(name="server", command="fake", args=[], env={})
    )
    first_client.protocol_version = "2026-07-28"
    first_client.server_capabilities = {
        "extensions": {"io.modelcontextprotocol/tasks": {}}
    }

    async def first_request(method: str, params: dict, **kwargs: Any) -> dict:
        del kwargs
        methods.append(method)
        assert method == "tools/call"
        assert params == {"name": "long", "arguments": {}}
        return {
            "resultType": "task",
            "taskId": task_id,
            "status": "working",
            "createdAt": "2026-09-20T00:00:00Z",
            "lastUpdatedAt": "2026-09-20T00:00:01Z",
            "ttlMs": 60_000,
            "pollIntervalMs": 0,
        }

    first_client.request = first_request  # type: ignore[method-assign]

    async def persist_then_crash(payload: dict[str, Any]) -> None:
        task = payload["task"]
        store.save_mcp_task(
            task_id=task["taskId"],
            session_id=session.session_id,
            turn_id=turn_id,
            call_id=payload["call_id"],
            server_name=payload["server_name"],
            remote_tool_name=payload["remote_tool_name"],
            contract_fingerprint=payload["contract_fingerprint"],
            server_fingerprint=payload["server_fingerprint"],
            protocol_version=payload["protocol_version"],
            task=task,
            answered_inputs=payload["answered_inputs"],
        )
        raise SystemExit("simulated process crash")

    first_tool = MCPTool(
        SafetyGuard(tmp_path),
        client=first_client,
        server_name="server",
        definition=definition,
        protocol_version="2026-07-28",
        task_state_handler=persist_then_crash,
    )
    with first_tool.event_context({"call_id": call_id}):
        with pytest.raises(SystemExit, match="simulated process crash"):
            await first_tool.run()

    rows = store.list_mcp_tasks(session.session_id)
    assert len(rows) == 1
    assert rows[0]["task_id"] == task_id
    assert methods == ["tools/call"]

    recovery_client = MCPClient(
        MCPServerConfig(name="server", command="fake", args=[], env={})
    )
    recovery_client.protocol_version = "2026-07-28"
    recovery_client.server_capabilities = {
        "extensions": {"io.modelcontextprotocol/tasks": {}}
    }

    async def recovery_request(method: str, params: dict, **kwargs: Any) -> dict:
        del kwargs
        methods.append(method)
        assert method == "tasks/get"
        assert params == {"taskId": task_id}
        return {
            "resultType": "complete",
            "taskId": task_id,
            "status": "completed",
            "createdAt": "2026-09-20T00:00:00Z",
            "lastUpdatedAt": "2026-09-20T00:00:02Z",
            "ttlMs": 60_000,
            "result": {
                "content": [{"type": "text", "text": "recovered result"}],
                "isError": False,
            },
        }

    recovery_client.request = recovery_request  # type: ignore[method-assign]
    guard = SafetyGuard(tmp_path)
    recovery_tool = MCPTool(
        guard,
        client=recovery_client,
        server_name="server",
        definition=definition,
        protocol_version="2026-07-28",
    )
    runtime = MCPRuntime({}, guard)
    runtime.clients["server"] = recovery_client
    loop = AshLoop(
        session_store=store,
        provider=IdleProvider(),
        safety_guard=guard,
        ui=HeadlessUI(output_format="text", stream=io.StringIO()),
        project_root=tmp_path,
    )
    loop._mcp_runtime = runtime
    loop.tools[recovery_tool.name] = recovery_tool
    try:
        recovered = await loop.start_session(session.session_id)

        assert methods == ["tools/call", "tasks/get"]
        assert store.list_mcp_tasks(session.session_id) == []
        recovered_call = next(
            call for call in recovered.tool_calls if call.call_id == call_id
        )
        assert recovered_call.executed is True
        assert recovered_call.error is None
        assert "recovered result" in (recovered_call.result or "")
        tool_messages = [
            message
            for message in recovered.messages
            if message.role == "tool" and message.metadata.get("call_id") == call_id
        ]
        assert len(tool_messages) == 1
        assert "recovered result" in tool_messages[0].content

        recovered_again = await loop.start_session(session.session_id)
        repeated = [
            message
            for message in recovered_again.messages
            if message.role == "tool" and message.metadata.get("call_id") == call_id
        ]
        assert len(repeated) == 1
        assert methods == ["tools/call", "tasks/get"]
    finally:
        await loop.aclose()


@pytest.mark.asyncio
async def test_loop_preserves_deferred_mcp_task_until_server_returns(
    tmp_path: Path,
) -> None:
    store = SessionStore(tmp_path / "sessions.db")
    session = store.create_session(str(tmp_path))
    turn_id = "turn-deferred"
    call_id = "call-deferred"
    task_id = "task-deferred"
    definition = {
        "name": "long",
        "description": "Long-running durable task",
        "inputSchema": {"type": "object", "additionalProperties": False},
    }
    probe_tool = MCPTool(
        SafetyGuard(tmp_path),
        client=StubMCPClient({"content": []}),  # type: ignore[arg-type]
        server_name="server",
        definition=definition,
        protocol_version="2026-07-28",
    )
    recovery_config = MCPServerConfig(name="server", command="fake", args=[], env={})
    store.start_turn(session.session_id, turn_id, "resume durable task")
    store.save_tool_call(
        session.session_id,
        ToolCallRecord(
            call_id=call_id,
            tool_name="mcp__server__long",
            arguments={},
            approved=True,
            executed=False,
            dispatched=True,
            timestamp=datetime.now(timezone.utc),
        ),
        turn_id=turn_id,
    )
    store.save_mcp_task(
        task_id=task_id,
        session_id=session.session_id,
        turn_id=turn_id,
        call_id=call_id,
        server_name="server",
        remote_tool_name="long",
        contract_fingerprint=probe_tool.contract_fingerprint(),
        server_fingerprint=mcp_server_fingerprint(recovery_config, {}),
        protocol_version="2026-07-28",
        task={
            "taskId": task_id,
            "status": "working",
            "createdAt": "2026-09-20T00:00:00Z",
            "lastUpdatedAt": "2026-09-20T00:00:01Z",
            "ttlMs": 60_000,
            "pollIntervalMs": 0,
        },
        answered_inputs={},
    )

    guard = SafetyGuard(tmp_path)
    loop = AshLoop(
        session_store=store,
        provider=IdleProvider(),
        safety_guard=guard,
        ui=HeadlessUI(output_format="text", stream=io.StringIO()),
        project_root=tmp_path,
    )
    with pytest.raises(RuntimeError, match="waiting for durable MCP task recovery"):
        await loop.start_session(session.session_id)

    assert loop.current_session is None
    assert len(store.list_mcp_tasks(session.session_id)) == 1
    pending = store.tool_call_for_recovery(session.session_id, turn_id, call_id)
    assert pending is not None
    assert bool(pending["executed"]) is False
    assert pending["error"] is None

    methods: list[str] = []
    client = MCPClient(recovery_config)
    client.protocol_version = "2026-07-28"
    client.server_capabilities = {
        "extensions": {"io.modelcontextprotocol/tasks": {}}
    }

    async def request(method: str, params: dict, **kwargs: Any) -> dict:
        del kwargs
        methods.append(method)
        assert method == "tasks/get"
        assert params == {"taskId": task_id}
        return {
            "resultType": "complete",
            "taskId": task_id,
            "status": "completed",
            "createdAt": "2026-09-20T00:00:00Z",
            "lastUpdatedAt": "2026-09-20T00:00:02Z",
            "ttlMs": 60_000,
            "result": {
                "content": [{"type": "text", "text": "eventually recovered"}],
                "isError": False,
            },
        }

    client.request = request  # type: ignore[method-assign]
    runtime = MCPRuntime({}, guard)
    runtime.clients["server"] = client
    recovery_tool = MCPTool(
        guard,
        client=client,
        server_name="server",
        definition=definition,
        protocol_version="2026-07-28",
    )
    loop._mcp_runtime = runtime
    loop.tools[recovery_tool.name] = recovery_tool
    try:
        recovered = await loop.start_session(session.session_id)
        assert methods == ["tasks/get"]
        assert store.list_mcp_tasks(session.session_id) == []
        assert any(
            message.role == "tool"
            and message.metadata.get("call_id") == call_id
            and "eventually recovered" in message.content
            for message in recovered.messages
        )
    finally:
        await loop.aclose()


@pytest.mark.asyncio
async def test_loop_never_resumes_task_on_repointed_mcp_server_alias(
    tmp_path: Path,
) -> None:
    store = SessionStore(tmp_path / "sessions.db")
    session = store.create_session(str(tmp_path))
    turn_id = "turn-repointed"
    call_id = "call-repointed"
    task_id = "task-repointed"
    definition = {
        "name": "long",
        "description": "Long-running durable task",
        "inputSchema": {"type": "object", "additionalProperties": False},
    }
    original_config = MCPServerConfig(
        name="server",
        command="original-server",
        args=[],
        env={"TENANT": "original"},
    )
    replacement_config = MCPServerConfig(
        name="server",
        command="replacement-server",
        args=[],
        env={"TENANT": "replacement"},
    )
    replacement_client = MCPClient(replacement_config)
    replacement_client.protocol_version = "2026-07-28"
    replacement_client.server_capabilities = {
        "extensions": {"io.modelcontextprotocol/tasks": {}}
    }
    replacement_client.request = AsyncMock(  # type: ignore[method-assign]
        side_effect=AssertionError("replacement server must not receive task ID")
    )
    guard = SafetyGuard(tmp_path)
    replacement_tool = MCPTool(
        guard,
        client=replacement_client,
        server_name="server",
        definition=definition,
        protocol_version="2026-07-28",
    )
    store.start_turn(session.session_id, turn_id, "resume durable task")
    store.save_tool_call(
        session.session_id,
        ToolCallRecord(
            call_id=call_id,
            tool_name=replacement_tool.name,
            arguments={},
            approved=True,
            executed=False,
            dispatched=True,
            timestamp=datetime.now(timezone.utc),
        ),
        turn_id=turn_id,
    )
    store.save_mcp_task(
        task_id=task_id,
        session_id=session.session_id,
        turn_id=turn_id,
        call_id=call_id,
        server_name="server",
        remote_tool_name="long",
        contract_fingerprint=replacement_tool.contract_fingerprint(),
        server_fingerprint=mcp_server_fingerprint(original_config, {}),
        protocol_version="2026-07-28",
        task={
            "taskId": task_id,
            "status": "working",
            "createdAt": "2026-09-20T00:00:00Z",
            "lastUpdatedAt": "2026-09-20T00:00:01Z",
            "ttlMs": 60_000,
            "pollIntervalMs": 0,
        },
        answered_inputs={},
    )
    assert mcp_server_fingerprint(original_config, {}) != mcp_server_fingerprint(
        replacement_config,
        {},
    )

    runtime = MCPRuntime({}, guard)
    runtime.clients["server"] = replacement_client
    loop = AshLoop(
        session_store=store,
        provider=IdleProvider(),
        safety_guard=guard,
        ui=HeadlessUI(output_format="text", stream=io.StringIO()),
        project_root=tmp_path,
    )
    loop._mcp_runtime = runtime
    loop.tools[replacement_tool.name] = replacement_tool
    try:
        with pytest.raises(RuntimeError, match="waiting for durable MCP task recovery"):
            await loop.start_session(session.session_id)

        replacement_client.request.assert_not_awaited()  # type: ignore[attr-defined]
        assert len(store.list_mcp_tasks(session.session_id)) == 1
        pending = store.tool_call_for_recovery(session.session_id, turn_id, call_id)
        assert pending is not None
        assert bool(pending["executed"]) is False
        assert pending["error"] is None
    finally:
        await loop.aclose()


@pytest.mark.asyncio
async def test_loop_falls_back_to_unknown_for_legacy_v12_mcp_task(
    tmp_path: Path,
) -> None:
    store = SessionStore(tmp_path / "sessions.db")
    session = store.create_session(str(tmp_path))
    turn_id = "turn-v12"
    call_id = "call-v12"
    task_id = "task-v12"
    store.start_turn(session.session_id, turn_id, "legacy durable task")
    store.save_tool_call(
        session.session_id,
        ToolCallRecord(
            call_id=call_id,
            tool_name="mcp__server__long",
            arguments={},
            approved=True,
            executed=False,
            dispatched=True,
            timestamp=datetime.now(timezone.utc),
        ),
        turn_id=turn_id,
    )
    store.save_mcp_task(
        task_id=task_id,
        session_id=session.session_id,
        turn_id=turn_id,
        call_id=call_id,
        server_name="server",
        remote_tool_name="long",
        contract_fingerprint="legacy-contract",
        server_fingerprint="",
        protocol_version="2026-07-28",
        task={
            "taskId": task_id,
            "status": "working",
            "createdAt": "2026-09-20T00:00:00Z",
            "lastUpdatedAt": "2026-09-20T00:00:01Z",
            "ttlMs": 60_000,
        },
        answered_inputs={},
    )

    loop = AshLoop(
        session_store=store,
        provider=IdleProvider(),
        safety_guard=SafetyGuard(tmp_path),
        ui=HeadlessUI(output_format="text", stream=io.StringIO()),
        project_root=tmp_path,
    )
    try:
        recovered = await loop.start_session(session.session_id)

        assert store.list_mcp_tasks(session.session_id) == []
        call = next(call for call in recovered.tool_calls if call.call_id == call_id)
        assert call.executed is True
        assert call.error is not None
        assert "outcome is unknown" in call.error
    finally:
        await loop.aclose()


@pytest.mark.asyncio
async def test_loop_recovers_locally_finalized_mcp_call_without_server_or_duplicates(
    tmp_path: Path,
) -> None:
    store = SessionStore(tmp_path / "sessions.db")
    session = store.create_session(str(tmp_path))
    turn_id = "turn-local-final"
    call_id = "call-local-final"
    task_id = "task-local-final"
    store.start_turn(session.session_id, turn_id, "finish local persistence")
    store.save_tool_call(
        session.session_id,
        ToolCallRecord(
            call_id=call_id,
            tool_name="mcp__server__long",
            arguments={},
            approved=True,
            executed=True,
            dispatched=True,
            result="already persisted result",
            timestamp=datetime.now(timezone.utc),
        ),
        turn_id=turn_id,
    )

    task = {
        "taskId": task_id,
        "status": "completed",
        "createdAt": "2026-09-20T00:00:00Z",
        "lastUpdatedAt": "2026-09-20T00:00:02Z",
        "ttlMs": 60_000,
        "result": {
            "content": [{"type": "text", "text": "wire result"}],
            "isError": False,
        },
    }

    def persist_stale_task() -> None:
        store.save_mcp_task(
            task_id=task_id,
            session_id=session.session_id,
            turn_id=turn_id,
            call_id=call_id,
            server_name="server",
            remote_tool_name="long",
            contract_fingerprint="old-contract",
            server_fingerprint="",
            protocol_version="2026-07-28",
            task=task,
            answered_inputs={},
        )

    persist_stale_task()
    loop = AshLoop(
        session_store=store,
        provider=IdleProvider(),
        safety_guard=SafetyGuard(tmp_path),
        ui=HeadlessUI(output_format="text", stream=io.StringIO()),
        project_root=tmp_path,
    )
    try:
        recovered = await loop.start_session(session.session_id)
        messages = [
            message
            for message in recovered.messages
            if message.role == "tool" and message.metadata.get("call_id") == call_id
        ]
        assert len(messages) == 1
        assert "already persisted result" in messages[0].content
        assert store.list_mcp_tasks(session.session_id) == []

        persist_stale_task()
        recovered_again = await loop.start_session(session.session_id)
        messages_again = [
            message
            for message in recovered_again.messages
            if message.role == "tool" and message.metadata.get("call_id") == call_id
        ]
        assert len(messages_again) == 1
        assert store.list_mcp_tasks(session.session_id) == []
    finally:
        await loop.aclose()


@pytest.mark.asyncio
async def test_loop_applies_live_mcp_tool_refresh(tmp_path: Path) -> None:
    config = MCPServerConfig(
        name="dynamic",
        command=sys.executable,
        args=["-u", "-c", DYNAMIC_MCP_SERVER],
        env={},
    )
    loop = AshLoop(
        session_store=SessionStore(tmp_path / "sessions.db"),
        provider=IdleProvider(),
        safety_guard=SafetyGuard(tmp_path),
        ui=HeadlessUI(output_format="text", stream=io.StringIO()),
        project_root=tmp_path,
        mcp_configs={"dynamic": config},
    )
    await loop.start_session()
    try:
        await loop._mcp_runtime.wait_for_refreshes()
        assert "mcp__dynamic__old" not in loop.tools
        assert "mcp__dynamic__new" in loop.tools
    finally:
        await loop.aclose()


@pytest.mark.asyncio
async def test_reload_keeps_in_flight_mcp_snapshot_alive_until_turn_end(
    tmp_path: Path,
) -> None:
    config = MCPServerConfig(
        name="fake",
        command=sys.executable,
        args=["-u", "-c", FAKE_MCP_SERVER],
        env={},
    )
    loop = AshLoop(
        session_store=SessionStore(tmp_path / "sessions.db"),
        provider=IdleProvider(),
        safety_guard=SafetyGuard(tmp_path),
        ui=HeadlessUI(output_format="text", stream=io.StringIO()),
        project_root=tmp_path,
        mcp_configs={"fake": config},
    )
    await loop.start_session()
    old_tool = loop.tools["mcp__fake__echo"]
    loop._turn_running = True
    try:
        assert await loop.reload_mcp_servers({}) == {}
        assert "mcp__fake__echo" not in loop.tools
        result = await old_tool.run(text="in flight")
        assert result.success is True
        assert result.output == "in flight"
        assert loop._retired_mcp_runtimes
    finally:
        loop._turn_running = False
        await loop._close_retired_mcp_runtimes()
        await loop.aclose()


@pytest.mark.asyncio
async def test_failed_mcp_reload_preserves_working_runtime(tmp_path: Path) -> None:
    working = MCPServerConfig(
        name="fake",
        command=sys.executable,
        args=["-u", "-c", FAKE_MCP_SERVER],
        env={},
    )
    loop = AshLoop(
        session_store=SessionStore(tmp_path / "sessions.db"),
        provider=IdleProvider(),
        safety_guard=SafetyGuard(tmp_path),
        ui=HeadlessUI(output_format="text", stream=io.StringIO()),
        project_root=tmp_path,
        mcp_configs={"fake": working},
    )
    await loop.start_session()
    broken = MCPServerConfig(
        name="broken",
        command=str(tmp_path / "missing-server"),
        args=[],
        env={},
    )
    try:
        errors = await loop.reload_mcp_servers({"broken": broken})
        assert "broken" in errors
        assert "mcp__fake__echo" in loop.tools
        result = await loop.tools["mcp__fake__echo"].run(text="still works")
        assert result.success is True
        assert result.output == "still works"
    finally:
        await loop.aclose()


@pytest.mark.asyncio
async def test_loop_shutdown_serializes_with_in_progress_mcp_reload(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    start_entered = asyncio.Event()
    release_start = asyncio.Event()
    instances = []

    class PausedRuntime:
        def __init__(self, configs, safety_guard, **kwargs) -> None:
            del safety_guard, kwargs
            self.configs = configs
            self.clients = {"paused": object()}
            self.errors = {}
            self.close_calls = 0
            instances.append(self)

        async def start(self) -> dict:
            start_entered.set()
            await release_start.wait()
            return {}

        def server_tools_snapshot(self) -> dict:
            return {}

        def activate_notifications(self) -> None:
            return None

        async def close(self) -> None:
            self.close_calls += 1
            self.clients.clear()

    monkeypatch.setattr("ash.mcp.runtime.MCPRuntime", PausedRuntime)
    loop = AshLoop(
        session_store=SessionStore(tmp_path / "sessions.db"),
        provider=IdleProvider(),
        safety_guard=SafetyGuard(tmp_path),
        ui=HeadlessUI(output_format="text", stream=io.StringIO()),
        project_root=tmp_path,
    )
    await loop.start_session()
    config = MCPServerConfig(name="paused", command="unused", args=[], env={})
    reload_task = asyncio.create_task(loop.reload_mcp_servers({"paused": config}))
    await asyncio.wait_for(start_entered.wait(), timeout=1)
    close_task = asyncio.create_task(loop.aclose())
    await asyncio.sleep(0)
    assert close_task.done() is False

    release_start.set()
    assert await reload_task == {}
    await asyncio.wait_for(close_task, timeout=1)

    assert len(instances) == 1
    assert instances[0].close_calls == 1
    assert loop._mcp_runtime is None
    assert loop._closed is True
    with pytest.raises(RuntimeError, match="after loop shutdown"):
        await loop.reload_mcp_servers({})


@pytest.mark.asyncio
async def test_streamable_http_tracks_session_and_parses_sse() -> None:
    seen_session = []
    seen_protocol = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "DELETE":
            seen_session.append(request.headers.get("Mcp-Session-Id"))
            seen_protocol.append(request.headers.get("MCP-Protocol-Version"))
            return httpx.Response(204)
        if request.content.startswith(b'{"jsonrpc":"2.0","id":0'):
            return httpx.Response(
                200,
                json={"jsonrpc": "2.0", "id": 0, "result": {}},
            )
        payload = json.loads(request.content)
        if "id" not in payload:
            return httpx.Response(202)
        if payload["method"] == "initialize":
            body = (
                'event: message\ndata: {"jsonrpc":"2.0","id":1,'
                '"result":{"protocolVersion":"2025-06-18","capabilities":{}}}\n\n'
            )
            return httpx.Response(
                200,
                text=body,
                headers={
                    "content-type": "text/event-stream",
                    "Mcp-Session-Id": "session-1",
                },
            )
        assert request.headers["Mcp-Session-Id"] == "session-1"
        assert request.headers["MCP-Protocol-Version"] == "2025-06-18"
        return httpx.Response(
            200,
            json={"jsonrpc": "2.0", "id": payload["id"], "result": {"tools": []}},
        )

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = MCPClient(
        MCPServerConfig(
            name="remote",
            command="",
            args=[],
            env={},
            transport="http",
            url="https://mcp.example.test/rpc",
        ),
        http_client=http,
    )
    await client.connect()
    assert await client.list_tools() == []
    await client.disconnect()
    assert seen_session == ["session-1"]
    assert seen_protocol == ["2025-06-18"]
    await http.aclose()


@pytest.mark.asyncio
async def test_http_post_rejects_oversized_response_before_json_parsing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(mcp_client_module, "MAX_HTTP_RESPONSE_BYTES", 32)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "application/json"},
            content=b"x" * 33,
        )

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = MCPClient(
        MCPServerConfig(
            name="remote",
            command="",
            args=[],
            env={},
            transport="http",
            url="https://mcp.example.test/rpc",
        ),
        http_client=http,
    )
    try:
        with pytest.raises(MCPProtocolError, match="response exceeded 32 bytes"):
            await client._post_http(
                {"jsonrpc": "2.0", "id": 1, "method": "ping"},
                is_initialize=True,
                bypass_session_readiness=True,
            )
    finally:
        await client.disconnect()
        await http.aclose()


@pytest.mark.asyncio
async def test_sse_line_reader_rejects_unterminated_event() -> None:
    response = httpx.Response(200, content=b"data: " + b"x" * 33)

    with pytest.raises(MCPProtocolError, match="SSE event exceeded 32 bytes"):
        async for _line in mcp_client_module._iter_bounded_sse_lines(response, 32):
            pass


def test_http_sse_parser_rejects_oversized_event(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(mcp_client_module, "MAX_HTTP_RESPONSE_BYTES", 128)
    monkeypatch.setattr(mcp_client_module, "MAX_HTTP_SSE_EVENT_BYTES", 32)
    response = httpx.Response(
        200,
        headers={"content-type": "text/event-stream"},
        content=b"data: " + b"x" * 33 + b"\n\n",
    )

    with pytest.raises(MCPProtocolError, match="SSE event exceeded 32 bytes"):
        mcp_client_module._parse_http_messages(response)


@pytest.mark.parametrize(
    ("content_type", "body"),
    [
        (
            "application/json",
            b'{"jsonrpc":"2.0","id":1,"result":{},"result":{"x":1}}',
        ),
        (
            "text/event-stream",
            b'data: {"jsonrpc":"2.0","id":1,"result":{},"result":{"x":1}}\n\n',
        ),
    ],
)
def test_http_parsers_reject_duplicate_json_keys(content_type: str, body: bytes) -> None:
    response = httpx.Response(
        200,
        headers={"content-type": content_type},
        content=body,
    )

    with pytest.raises(MCPProtocolError, match="invalid JSON"):
        mcp_client_module._parse_http_messages(response)


@pytest.mark.asyncio
async def test_http_get_stream_dispatches_events_and_honors_405() -> None:
    requests: list[httpx.Request] = []
    mode = {"get": True}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            requests.append(request)
            if not mode["get"]:
                return httpx.Response(405)
            body = (
                "retry: 5\n"
                'id: event-1\ndata: {"jsonrpc":"2.0","method":"notifications/message",'
                '"params":{"level":"info","data":"ready"}}\n'
                "\n"
            )
            return httpx.Response(
                200,
                text=body,
                headers={"content-type": "text/event-stream"},
            )
        if request.method == "DELETE":
            return httpx.Response(204)
        payload = json.loads(request.content)
        if "id" not in payload:
            return httpx.Response(202)
        if payload["method"] == "initialize":
            return httpx.Response(
                200,
                headers={"Mcp-Session-Id": "stream-session"},
                json={
                    "jsonrpc": "2.0",
                    "id": payload["id"],
                    "result": {
                        "protocolVersion": "2025-06-18",
                        "capabilities": {},
                    },
                },
            )
        if payload["method"] == "notifications/initialized":
            return httpx.Response(202)
        return httpx.Response(
            200,
            json={"jsonrpc": "2.0", "id": payload["id"], "result": []},
        )

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = MCPClient(
        MCPServerConfig(
            name="remote",
            command="",
            args=[],
            env={},
            transport="http",
            url="https://mcp.example.test/rpc",
        ),
        http_client=http,
    )
    notifications = []
    client.notification_handler = lambda method, params: notifications.append(method)
    await client.connect()
    await asyncio.sleep(0.01)
    assert notifications.count("notifications/message") >= 1
    assert requests[0].headers["Accept"] == "text/event-stream"
    assert requests[0].headers["Mcp-Session-Id"] == "stream-session"
    assert requests[0].headers["MCP-Protocol-Version"] == "2025-06-18"
    assert "Last-Event-ID" not in requests[0].headers

    mode["get"] = False
    client._sse_supported = True
    client._sse_generation += 1
    client._sse_task = asyncio.create_task(client._read_http_events())
    for _ in range(20):
        await asyncio.sleep(0.01)
        if client._sse_supported is False:
            break
    assert requests[-1].headers.get("Last-Event-ID") == "event-1"
    assert client._sse_supported is False

    await client.disconnect()
    await http.aclose()


@pytest.mark.asyncio
async def test_http_get_stream_handles_huge_valid_retry_without_overflow(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    retry = "9" * 400

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            text=f"retry: {retry}\n\n",
            headers={"content-type": "text/event-stream"},
        )

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = MCPClient(
        MCPServerConfig(
            name="remote",
            command="",
            args=[],
            env={},
            transport="http",
            url="https://mcp.example.test/rpc",
        ),
        http_client=http,
    )
    sleeps: list[float] = []

    async def fake_sleep(delay: float) -> None:
        sleeps.append(delay)
        client._initialized = False

    monkeypatch.setattr(mcp_client_module.asyncio, "sleep", fake_sleep)
    client._initialized = True
    await client._read_http_events()

    assert client._sse_retry_ms == int(retry)
    assert sleeps == [mcp_client_module.MAX_SSE_RETRY_SLEEP_SLICE_MS / 1000]
    await http.aclose()


@pytest.mark.asyncio
async def test_http_recovers_expired_session_without_replaying_tool_call() -> None:
    trace: list[tuple[str, str | None, int | None]] = []
    initialize_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal initialize_count
        if request.method == "DELETE":
            return httpx.Response(405)
        if request.content.startswith(b'{"jsonrpc":"2.0","id":0'):
            return httpx.Response(
                200,
                json={"jsonrpc": "2.0", "id": 0, "result": {}},
            )
        payload = json.loads(request.content)
        method = payload["method"]
        session = request.headers.get("Mcp-Session-Id")
        trace.append((method, session, payload.get("id")))
        if method == "initialize":
            initialize_count += 1
            return httpx.Response(
                200,
                headers={"Mcp-Session-Id": f"session-{initialize_count}"},
                json={
                    "jsonrpc": "2.0",
                    "id": payload["id"],
                    "result": {
                        "protocolVersion": "2025-11-25",
                        "capabilities": {"tools": {}},
                    },
                },
            )
        if method == "notifications/initialized":
            return httpx.Response(202)
        if session == "session-1":
            return httpx.Response(404)
        return httpx.Response(
            200,
            headers={"Mcp-Session-Id": "must-not-replace-session-2"},
            json={
                "jsonrpc": "2.0",
                "id": payload["id"],
                "result": {"content": [{"type": "text", "text": "ok"}]},
            },
        )

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = MCPClient(
        MCPServerConfig(
            name="remote",
            command="",
            args=[],
            env={},
            transport="http",
            url="https://mcp.example.test/rpc",
        ),
        http_client=http,
    )
    await client.connect()
    try:
        with pytest.raises(MCPProtocolError, match="operation was not replayed"):
            await client.call_tool("echo", {})
        assert client._http_session_id == "session-2"
    finally:
        await client.disconnect()
        await http.aclose()

    calls = [item for item in trace if item[0] == "tools/call"]
    assert [(method, session) for method, session, _ in trace] == [
        ("initialize", None),
        ("notifications/initialized", "session-1"),
        ("tools/call", "session-1"),
        ("initialize", None),
        ("notifications/initialized", "session-2"),
    ]
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_http_concurrent_expiry_uses_one_recovery_handshake() -> None:
    initialize_count = 0
    old_calls = 0
    both_old_calls = asyncio.Event()

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal initialize_count, old_calls
        if request.method == "DELETE":
            return httpx.Response(405)
        payload = json.loads(request.content)
        method = payload["method"]
        session = request.headers.get("Mcp-Session-Id")
        if method == "initialize":
            initialize_count += 1
            return httpx.Response(
                200,
                headers={"Mcp-Session-Id": f"session-{initialize_count}"},
                json={
                    "jsonrpc": "2.0",
                    "id": payload["id"],
                    "result": {
                        "protocolVersion": "2025-11-25",
                        "capabilities": {"tools": {}},
                    },
                },
            )
        if method == "notifications/initialized":
            return httpx.Response(202)
        if session == "session-1":
            old_calls += 1
            if old_calls == 2:
                both_old_calls.set()
            await asyncio.wait_for(both_old_calls.wait(), timeout=1)
            return httpx.Response(404)
        return httpx.Response(
            200,
            json={"jsonrpc": "2.0", "id": payload["id"], "result": {}},
        )

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = MCPClient(
        MCPServerConfig(
            name="remote",
            command="",
            args=[],
            env={},
            transport="http",
            url="https://mcp.example.test/rpc",
        ),
        http_client=http,
    )
    await client.connect()
    try:
        with pytest.raises(MCPProtocolError, match="operation was not replayed"):
            await asyncio.gather(
                client.call_tool("first", {}), client.call_tool("second", {})
            )
        assert initialize_count == 2
    finally:
        await client.disconnect()
        await http.aclose()


@pytest.mark.asyncio
async def test_http_session_404_recovers_without_replaying_tool_attempt() -> None:
    initialize_count = 0
    tool_attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal initialize_count, tool_attempts
        if request.method == "DELETE":
            return httpx.Response(405)
        if request.content.startswith(b'{"jsonrpc":"2.0","id":0'):
            return httpx.Response(
                200,
                json={"jsonrpc": "2.0", "id": 0, "result": {}},
            )
        payload = json.loads(request.content)
        if payload["method"] == "initialize":
            initialize_count += 1
            return httpx.Response(
                200,
                headers={"Mcp-Session-Id": f"session-{initialize_count}"},
                json={
                    "jsonrpc": "2.0",
                    "id": payload["id"],
                    "result": {
                        "protocolVersion": "2025-11-25",
                        "capabilities": {"tools": {}},
                    },
                },
            )
        if payload["method"] == "notifications/initialized":
            return httpx.Response(202)
        tool_attempts += 1
        return httpx.Response(404)

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = MCPClient(
        MCPServerConfig(
            name="remote",
            command="",
            args=[],
            env={},
            transport="http",
            url="https://mcp.example.test/rpc",
        ),
        http_client=http,
    )
    await client.connect()
    try:
        with pytest.raises(MCPProtocolError, match="operation was not replayed"):
            await client.call_tool("write", {})
        assert tool_attempts == 1
        assert initialize_count == 2
        assert client._http_session_id == "session-2"
    finally:
        await client.disconnect()
        await http.aclose()


@pytest.mark.asyncio
async def test_http_rejects_invalid_initialize_session_id() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if not request.content:
            return httpx.Response(204)
        if request.content.startswith(b'{"jsonrpc":"2.0","id":0'):
            return httpx.Response(
                200,
                json={"jsonrpc": "2.0", "id": 0, "result": {}},
            )
        payload = json.loads(request.content)
        return httpx.Response(
            200,
            headers={"Mcp-Session-Id": b"not-visible-\xff"},
            json={
                "jsonrpc": "2.0",
                "id": payload["id"],
                "result": {
                    "protocolVersion": "2025-11-25",
                    "capabilities": {},
                },
            },
        )

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = MCPClient(
        MCPServerConfig(
            name="remote",
            command="",
            args=[],
            env={},
            transport="http",
            url="https://mcp.example.test/rpc",
        ),
        http_client=http,
    )
    with pytest.raises(MCPProtocolError, match="visible ASCII"):
        await client.connect()
    await http.aclose()


@pytest.mark.asyncio
async def test_http_malformed_sse_never_replays_tool_call() -> None:
    tool_attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal tool_attempts
        if request.method == "DELETE":
            return httpx.Response(405)
        if request.content.startswith(b'{"jsonrpc":"2.0","id":0'):
            return httpx.Response(
                200,
                json={"jsonrpc": "2.0", "id": 0, "result": {}},
            )
        payload = json.loads(request.content)
        if payload["method"] == "initialize":
            return httpx.Response(
                200,
                json={
                    "jsonrpc": "2.0",
                    "id": payload["id"],
                    "result": {
                        "protocolVersion": "2025-11-25",
                        "capabilities": {"tools": {}},
                    },
                },
            )
        if payload["method"] == "notifications/initialized":
            return httpx.Response(202)
        tool_attempts += 1
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            text="event: message\ndata: {not-json}\n\n",
        )

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = MCPClient(
        MCPServerConfig(
            name="remote",
            command="",
            args=[],
            env={},
            transport="http",
            url="https://mcp.example.test/rpc",
        ),
        http_client=http,
    )
    await client.connect()
    try:
        with pytest.raises(MCPProtocolError, match="SSE event contained invalid JSON"):
            await client.call_tool("write", {})
        assert tool_attempts == 1
    finally:
        await client.disconnect()
        await http.aclose()


@pytest.mark.asyncio
async def test_concurrent_http_connect_initializes_once() -> None:
    initialize_count = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal initialize_count
        if request.method == "DELETE":
            return httpx.Response(405)
        if request.content.startswith(b'{"jsonrpc":"2.0","id":0'):
            return httpx.Response(
                200,
                json={"jsonrpc": "2.0", "id": 0, "result": {}},
            )
        payload = json.loads(request.content)
        if payload["method"] == "initialize":
            initialize_count += 1
            await asyncio.sleep(0)
            return httpx.Response(
                200,
                json={
                    "jsonrpc": "2.0",
                    "id": payload["id"],
                    "result": {
                        "protocolVersion": "2025-11-25",
                        "capabilities": {},
                    },
                },
            )
        return httpx.Response(202)

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = MCPClient(
        MCPServerConfig(
            name="remote",
            command="",
            args=[],
            env={},
            transport="http",
            url="https://mcp.example.test/rpc",
        ),
        http_client=http,
    )
    await asyncio.gather(client.connect(), client.connect())
    try:
        assert initialize_count == 1
    finally:
        await client.disconnect()
        await http.aclose()


@pytest.mark.asyncio
async def test_paginated_list_restarts_after_session_recovery() -> None:
    initialize_count = 0
    cursors: list[tuple[str, str | None]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal initialize_count
        if request.method == "DELETE":
            return httpx.Response(405)
        if request.content.startswith(b'{"jsonrpc":"2.0","id":0'):
            return httpx.Response(
                200,
                json={"jsonrpc": "2.0", "id": 0, "result": {}},
            )
        payload = json.loads(request.content)
        method = payload["method"]
        session = request.headers.get("Mcp-Session-Id")
        if method == "initialize":
            initialize_count += 1
            return httpx.Response(
                200,
                headers={"Mcp-Session-Id": f"session-{initialize_count}"},
                json={
                    "jsonrpc": "2.0",
                    "id": payload["id"],
                    "result": {
                        "protocolVersion": "2025-11-25",
                        "capabilities": {"tools": {}},
                    },
                },
            )
        if method == "notifications/initialized":
            return httpx.Response(202)
        cursor = payload.get("params", {}).get("cursor")
        cursors.append((session or "", cursor))
        if session == "session-1" and cursor == "page-2":
            return httpx.Response(404)
        prefix = "old" if session == "session-1" else "new"
        result = (
            {
                "tools": [{"name": f"{prefix}-first"}],
                "nextCursor": "page-2",
            }
            if cursor is None
            else {"tools": [{"name": f"{prefix}-second"}]}
        )
        return httpx.Response(
            200,
            json={"jsonrpc": "2.0", "id": payload["id"], "result": result},
        )

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = MCPClient(
        MCPServerConfig(
            name="remote",
            command="",
            args=[],
            env={},
            transport="http",
            url="https://mcp.example.test/rpc",
        ),
        http_client=http,
    )
    await client.connect()
    try:
        tools = await client.list_tools()
        assert [tool["name"] for tool in tools] == ["new-first", "new-second"]
        assert cursors == [
            ("session-1", None),
            ("session-1", "page-2"),
            ("session-2", None),
            ("session-2", "page-2"),
        ]
    finally:
        await client.disconnect()
        await http.aclose()


@pytest.mark.asyncio
async def test_list_rejects_non_object_catalog_entries() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if not request.content:
            return httpx.Response(204)
        if request.content.startswith(b'{"jsonrpc":"2.0","id":0'):
            return httpx.Response(
                200,
                json={"jsonrpc": "2.0", "id": 0, "result": {}},
            )
        payload = json.loads(request.content)
        if payload["method"] == "initialize":
            result = {
                "protocolVersion": "2025-11-25",
                "capabilities": {"tools": {}},
            }
        elif payload["method"] == "notifications/initialized":
            return httpx.Response(202)
        else:
            result = {"tools": [{"name": "valid"}, "invalid"]}
        return httpx.Response(
            200,
            json={"jsonrpc": "2.0", "id": payload["id"], "result": result},
        )

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = MCPClient(
        MCPServerConfig(
            name="remote",
            command="",
            args=[],
            env={},
            transport="http",
            url="https://mcp.example.test/rpc",
        ),
        http_client=http,
    )
    await client.connect()
    try:
        with pytest.raises(MCPProtocolError, match="non-object tools entry"):
            await client.list_tools()
    finally:
        await client.disconnect()
        await http.aclose()


@pytest.mark.asyncio
async def test_failed_initialize_deletes_pending_server_session() -> None:
    deleted_sessions: list[str | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "DELETE":
            deleted_sessions.append(request.headers.get("Mcp-Session-Id"))
            return httpx.Response(204)
        if not request.content:
            return httpx.Response(204)
        payload = json.loads(request.content)
        return httpx.Response(
            200,
            headers={"Mcp-Session-Id": "allocated-session"},
            json={
                "jsonrpc": "2.0",
                "id": payload["id"],
                "result": {"protocolVersion": "unsupported", "capabilities": {}},
            },
        )

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = MCPClient(
        MCPServerConfig(
            name="remote",
            command="",
            args=[],
            env={},
            transport="http",
            url="https://mcp.example.test/rpc",
        ),
        http_client=http,
    )
    with pytest.raises(MCPProtocolError, match="unsupported protocol version"):
        await client.connect()
    assert deleted_sessions == ["allocated-session"]
    await http.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("capability", [None, False, []])
async def test_initialize_rejects_non_object_capabilities(capability: object) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method != "POST":
            return httpx.Response(204)
        payload = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "jsonrpc": "2.0",
                "id": payload["id"],
                "result": {
                    "protocolVersion": "2025-11-25",
                    "capabilities": {"tools": capability},
                },
            },
        )

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = MCPClient(
        MCPServerConfig(
            name="remote",
            command="",
            args=[],
            env={},
            transport="http",
            url="https://mcp.example.test/rpc",
        ),
        http_client=http,
    )
    with pytest.raises(
        MCPProtocolError, match="capabilities must contain objects: tools"
    ):
        await client.connect()
    await http.aclose()


@pytest.mark.asyncio
async def test_failed_replacement_initialize_deletes_allocated_session() -> None:
    initialize_count = 0
    deleted_sessions: list[str | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal initialize_count
        if request.method == "DELETE":
            deleted_sessions.append(request.headers.get("Mcp-Session-Id"))
            return httpx.Response(204)
        if request.content.startswith(b'{"jsonrpc":"2.0","id":0'):
            return httpx.Response(
                200,
                json={"jsonrpc": "2.0", "id": 0, "result": {}},
            )
        payload = json.loads(request.content)
        method = payload["method"]
        session = request.headers.get("Mcp-Session-Id")
        if method == "initialize":
            initialize_count += 1
            capabilities = {"tools": None} if initialize_count == 2 else {"tools": {}}
            return httpx.Response(
                200,
                headers={"Mcp-Session-Id": f"session-{initialize_count}"},
                json={
                    "jsonrpc": "2.0",
                    "id": payload["id"],
                    "result": {
                        "protocolVersion": "2025-11-25",
                        "capabilities": capabilities,
                    },
                },
            )
        if method == "notifications/initialized":
            return httpx.Response(202)
        if session == "session-1":
            return httpx.Response(404)
        return httpx.Response(
            200,
            json={
                "jsonrpc": "2.0",
                "id": payload["id"],
                "result": {"content": []},
            },
        )

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = MCPClient(
        MCPServerConfig(
            name="remote",
            command="",
            args=[],
            env={},
            transport="http",
            url="https://mcp.example.test/rpc",
        ),
        http_client=http,
    )
    await client.connect()
    try:
        with pytest.raises(
            MCPProtocolError, match="capabilities must contain objects: tools"
        ):
            await client.call_tool("echo", {})
        assert deleted_sessions == ["session-2"]

        assert await client.call_tool("echo", {}) == {"content": []}
        assert initialize_count == 3
    finally:
        await client.disconnect()
        await http.aclose()
    assert deleted_sessions == ["session-2", "session-3"]


@pytest.mark.asyncio
async def test_cancelled_session_recovery_restores_readiness() -> None:
    initialize_count = 0
    replacement_initialize_started = asyncio.Event()

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal initialize_count
        if request.method == "DELETE":
            return httpx.Response(204)
        payload = json.loads(request.content)
        method = payload["method"]
        session = request.headers.get("Mcp-Session-Id")
        if method == "initialize":
            initialize_count += 1
            if initialize_count == 2:
                replacement_initialize_started.set()
                await asyncio.Event().wait()
            return httpx.Response(
                200,
                headers={"Mcp-Session-Id": f"session-{initialize_count}"},
                json={
                    "jsonrpc": "2.0",
                    "id": payload["id"],
                    "result": {
                        "protocolVersion": "2025-11-25",
                        "capabilities": {"tools": {}},
                    },
                },
            )
        if method == "notifications/initialized":
            return httpx.Response(202)
        if session == "session-1":
            return httpx.Response(404)
        return httpx.Response(
            200,
            json={
                "jsonrpc": "2.0",
                "id": payload["id"],
                "result": {"content": []},
            },
        )

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = MCPClient(
        MCPServerConfig(
            name="remote",
            command="",
            args=[],
            env={},
            transport="http",
            url="https://mcp.example.test/rpc",
        ),
        http_client=http,
    )
    await client.connect()
    task = asyncio.create_task(client.call_tool("echo", {}))
    try:
        await asyncio.wait_for(replacement_initialize_started.wait(), timeout=1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=1)
        assert client._session_ready.is_set()

        assert await client.call_tool("echo", {}) == {"content": []}
        assert initialize_count == 3
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await client.disconnect()
        await http.aclose()


@pytest.mark.asyncio
async def test_cancelled_http_recovery_owns_one_session_delete_until_settled() -> None:
    client = MCPClient(
        MCPServerConfig(
            name="remote",
            command="",
            args=[],
            env={},
            transport="http",
            url="https://mcp.example.test/rpc",
        ),
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(204))),
    )
    initialize_started = asyncio.Event()
    delete_started = asyncio.Event()
    delete_release = asyncio.Event()
    delete_finished = asyncio.Event()
    delete_calls = 0

    async def blocked_initialize() -> None:
        client._http_session_id = "recovery-session"
        initialize_started.set()
        await asyncio.Event().wait()

    async def blocked_delete(session_id: str) -> None:
        nonlocal delete_calls
        assert session_id == "recovery-session"
        delete_calls += 1
        delete_started.set()
        await delete_release.wait()
        delete_finished.set()

    client._initialize_protocol = blocked_initialize  # type: ignore[method-assign]
    client._delete_http_session = blocked_delete  # type: ignore[method-assign]
    recovery = asyncio.create_task(
        client._recover_http_session(mcp_client_module.MCPSessionExpired("old", 0))
    )
    await initialize_started.wait()
    recovery.cancel()
    await delete_started.wait()
    recovery.cancel()
    await asyncio.sleep(0)
    assert not recovery.done()
    assert delete_calls == 1
    assert not delete_finished.is_set()

    delete_release.set()
    with pytest.raises(asyncio.CancelledError):
        await recovery
    assert delete_finished.is_set()
    assert client._session_ready.is_set()
    await client._http.aclose()  # type: ignore[union-attr]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("response_factory", "message"),
    [
        (
            lambda request_id: httpx.Response(
                200,
                json=[{"jsonrpc": "2.0", "id": request_id, "result": {}}],
            ),
            "must contain one JSON-RPC object",
        ),
        (
            lambda request_id: httpx.Response(
                200,
                json={"jsonrpc": "2.0", "id": True, "result": {}},
            ),
            "id must be a string or integer",
        ),
        (
            lambda request_id: httpx.Response(
                200,
                text='{"jsonrpc":"2.0","id":2,"result":{}}',
                headers={"content-type": "text/plain"},
            ),
            "must use application/json or text/event-stream",
        ),
        (
            lambda request_id: httpx.Response(
                200,
                text='{"jsonrpc":"2.0","id":2,"result":{}}',
                headers={"content-type": "application/jsonp"},
            ),
            "must use application/json or text/event-stream",
        ),
        (
            lambda request_id: httpx.Response(
                200,
                text='data: {"jsonrpc":"2.0","id":2,"result":{}}\n\n',
                headers={"content-type": "text/event-stream-evil"},
            ),
            "must use application/json or text/event-stream",
        ),
    ],
)
async def test_http_rejects_invalid_jsonrpc_envelopes(
    response_factory, message: str
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.content.startswith(b'{"jsonrpc":"2.0","id":0'):
            return httpx.Response(
                200,
                json={"jsonrpc": "2.0", "id": 0, "result": {}},
            )
        payload = json.loads(request.content)
        if payload["method"] == "initialize":
            return httpx.Response(
                200,
                json={
                    "jsonrpc": "2.0",
                    "id": payload["id"],
                    "result": {
                        "protocolVersion": "2025-11-25",
                        "capabilities": {"tools": {}},
                    },
                },
            )
        if payload["method"] == "notifications/initialized":
            return httpx.Response(202)
        return response_factory(payload["id"])

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = MCPClient(
        MCPServerConfig(
            name="remote",
            command="",
            args=[],
            env={},
            transport="http",
            url="https://mcp.example.test/rpc",
        ),
        http_client=http,
    )
    await client.connect()
    try:
        with pytest.raises(MCPProtocolError, match=message):
            await client.call_tool("echo", {})
    finally:
        await client.disconnect()
        await http.aclose()


INTERACTIVE_MCP_SERVER = r"""
import json, sys
pending_tools_id = None
root_uri = ""
for line in sys.stdin:
    message = json.loads(line)
    if message.get("method") == "server/discover":
        print(json.dumps({"jsonrpc": "2.0", "id": message["id"], "error": {"code": -32601, "message": "legacy"}}), flush=True)
    elif message.get("method") == "initialize":
        result = {
            "protocolVersion": "2025-11-25",
            "capabilities": {"tools": {"listChanged": True}, "logging": {}},
            "serverInfo": {"name": "interactive", "version": "1"},
            "instructions": "server guidance",
        }
        print(json.dumps({"jsonrpc": "2.0", "id": message["id"], "result": result}), flush=True)
    elif message.get("method") == "notifications/initialized":
        print(json.dumps({"jsonrpc": "2.0", "id": "roots-1", "method": "roots/list", "params": {}}), flush=True)
        print(json.dumps({"jsonrpc": "2.0", "method": "notifications/message", "params": {"level": "info", "data": "ready"}}), flush=True)
    elif message.get("method") == "tools/list":
        if root_uri:
            result = {"tools": [{"name": "root", "description": root_uri, "inputSchema": {"type": "object"}}]}
            print(json.dumps({"jsonrpc": "2.0", "id": message["id"], "result": result}), flush=True)
        else:
            pending_tools_id = message["id"]
    elif message.get("id") == "roots-1" and "result" in message:
        root_uri = message["result"]["roots"][0]["uri"]
        if pending_tools_id is not None:
            result = {"tools": [{"name": "root", "description": root_uri, "inputSchema": {"type": "object"}}]}
            print(json.dumps({"jsonrpc": "2.0", "id": pending_tools_id, "result": result}), flush=True)
            pending_tools_id = None
"""


@pytest.mark.asyncio
async def test_stdio_dispatches_server_requests_and_notifications(
    tmp_path: Path,
) -> None:
    notifications: list[tuple[str, dict]] = []
    client = MCPClient(
        MCPServerConfig(
            name="interactive",
            command=sys.executable,
            args=["-u", "-c", INTERACTIVE_MCP_SERVER],
            env={},
        ),
        roots=(tmp_path,),
        notification_handler=lambda method, params: notifications.append(
            (method, params)
        ),
    )

    await client.connect()
    try:
        instructions = client.server_instructions
        tools = await client.list_tools()
        for _ in range(20):
            if notifications:
                break
            await asyncio.sleep(0.01)
    finally:
        await client.disconnect()

    assert instructions == "server guidance"
    assert tools[0]["description"] == tmp_path.resolve().as_uri()
    assert notifications == [
        ("notifications/message", {"level": "info", "data": "ready"})
    ]


@pytest.mark.asyncio
async def test_http_tool_listing_follows_pagination() -> None:
    cursors: list[str | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        method = payload.get("method")
        if method == "initialize":
            return httpx.Response(
                200,
                json={
                    "jsonrpc": "2.0",
                    "id": payload["id"],
                    "result": {
                        "protocolVersion": "2025-11-25",
                        "capabilities": {"tools": {}},
                    },
                },
            )
        if "id" not in payload:
            return httpx.Response(202)
        cursor = payload.get("params", {}).get("cursor")
        cursors.append(cursor)
        result = (
            {"tools": [{"name": "first"}], "nextCursor": "page-2"}
            if cursor is None
            else {"tools": [{"name": "second"}]}
        )
        return httpx.Response(
            200,
            json={"jsonrpc": "2.0", "id": payload["id"], "result": result},
        )

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = MCPClient(
        MCPServerConfig(
            name="remote",
            command="",
            args=[],
            env={},
            transport="http",
            url="https://mcp.example.test/rpc",
        ),
        http_client=http,
    )
    await client.connect()
    try:
        tools = await client.list_tools()
    finally:
        await client.disconnect()
        await http.aclose()

    assert [tool["name"] for tool in tools] == ["first", "second"]
    assert cursors == [None, None, "page-2"]


@pytest.mark.asyncio
async def test_mcp_paginated_catalog_rejects_oversized_aggregate_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = MCPClient(
        MCPServerConfig(name="fake", command="fake", args=[], env={}),
    )
    client.request = AsyncMock(  # type: ignore[method-assign]
        side_effect=[
            {"resources": [{"uri": "file:///first"}], "nextCursor": "page-two"},
            {"resources": [{"uri": "file:///second"}]},
        ]
    )
    monkeypatch.setattr(mcp_client_module, "MAX_PAGINATION_RESULT_BYTES", 49)

    with pytest.raises(MCPProtocolError, match="aggregate result exceeds"):
        await client.list_resources()


@pytest.mark.asyncio
async def test_request_timeout_sends_cancellation_notification() -> None:
    client = MCPClient(MCPServerConfig(name="fake", command="fake", args=[], env={}))
    client._request_stdio = AsyncMock(side_effect=asyncio.TimeoutError())
    client.notify = AsyncMock()

    with pytest.raises(asyncio.TimeoutError):
        await client.request("tools/call", {"name": "slow"})

    client.notify.assert_awaited_once_with(
        "notifications/cancelled",
        {"requestId": 1, "reason": "tools/call timed out"},
        _allow_session_recovery=False,
    )


@pytest.mark.asyncio
async def test_stdio_send_failure_cleans_pending_future() -> None:
    client = MCPClient(MCPServerConfig(name="fake", command="fake", args=[], env={}))
    client._process = Mock(stdin=object())
    client._send_message = AsyncMock(side_effect=BrokenPipeError("closed"))

    with pytest.raises(BrokenPipeError):
        await client._request_stdio(9, {"jsonrpc": "2.0", "id": 9})

    assert client._pending == {}


@pytest.mark.asyncio
async def test_mcp_rejects_oversized_outbound_message_before_writing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import ash.mcp.client as mcp_client

    monkeypatch.setattr(mcp_client, "MAX_OUTBOUND_MESSAGE_BYTES", 64)
    stdin = Mock()
    stdin.drain = AsyncMock()
    client = MCPClient(MCPServerConfig(name="fake", command="fake", args=[], env={}))
    client._process = Mock(stdin=stdin)

    with pytest.raises(MCPProtocolError, match="outbound message exceeds 64 bytes"):
        await client._send_message(
            {
                "jsonrpc": "2.0",
                "method": "notifications/message",
                "params": {"value": "x" * 100},
            }
        )

    stdin.write.assert_not_called()
    stdin.drain.assert_not_awaited()


@pytest.mark.asyncio
async def test_stdio_revalidates_tool_contract_inside_write_lock() -> None:
    client = MCPClient(MCPServerConfig(name="fake", command="fake", args=[], env={}))
    stdin = Mock()
    client._process = Mock(stdin=stdin)
    client._session_generation = 1
    catalog_valid = True
    client.tool_contract_validator = lambda name, fingerprint, generation: catalog_valid
    await client._write_lock.acquire()
    task = asyncio.create_task(
        client.call_tool("echo", {}, expected_contract="fingerprint")
    )
    try:
        await asyncio.sleep(0)
        catalog_valid = False
        client._write_lock.release()
        with pytest.raises(MCPProtocolError, match="active verified server contract"):
            await asyncio.wait_for(task, timeout=1)
        stdin.write.assert_not_called()
    finally:
        if client._write_lock.locked():
            client._write_lock.release()
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_stdio_reader_fails_pending_request_on_framing_error() -> None:
    class BrokenReader:
        async def readline(self) -> bytes:
            raise ValueError("line exceeds configured limit")

    client = MCPClient(MCPServerConfig(name="fake", command="fake", args=[], env={}))
    client._process = Mock(stdout=BrokenReader())
    future = asyncio.get_running_loop().create_future()
    client._pending[3] = future

    await client._read_stdio()

    with pytest.raises(MCPProtocolError, match="invalid stdio framing"):
        await future
    assert client._pending == {}


@pytest.mark.asyncio
async def test_stdio_reader_fails_pending_request_on_invalid_utf8() -> None:
    class InvalidUtf8Reader:
        async def readline(self) -> bytes:
            return b"\xff\n"

    client = MCPClient(MCPServerConfig(name="fake", command="fake", args=[], env={}))
    client._process = Mock(stdout=InvalidUtf8Reader())
    future = asyncio.get_running_loop().create_future()
    client._pending[3] = future

    await client._read_stdio()

    with pytest.raises(MCPProtocolError, match="invalid JSON"):
        await future
    assert client._pending == {}


@pytest.mark.asyncio
async def test_stdio_reader_fails_pending_request_on_duplicate_json_keys() -> None:
    class DuplicateKeyReader:
        sent = False

        async def readline(self) -> bytes:
            if self.sent:
                return b""
            self.sent = True
            return b'{"jsonrpc":"2.0","id":3,"result":{},"result":{"x":1}}\n'

    client = MCPClient(MCPServerConfig(name="fake", command="fake", args=[], env={}))
    client._process = Mock(stdout=DuplicateKeyReader())
    future = asyncio.get_running_loop().create_future()
    client._pending[3] = future

    await client._read_stdio()

    with pytest.raises(MCPProtocolError, match="duplicate JSON object key"):
        await future
    assert client._pending == {}


@pytest.mark.asyncio
async def test_cancelled_stdio_connect_cleans_process_and_reader_tasks() -> None:
    client = MCPClient(
        MCPServerConfig(
            name="blocked",
            command=sys.executable,
            args=["-u", "-c", "import time; time.sleep(60)"],
            env={},
        )
    )
    task = asyncio.create_task(client.connect())
    for _ in range(100):
        if client._process is not None and client._reader_task is not None:
            break
        await asyncio.sleep(0.01)
    process = client._process
    assert process is not None

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=2)

    assert client._process is None
    assert client._reader_task is None
    assert client._stderr_task is None
    assert process.returncode is not None


@pytest.mark.asyncio
async def test_cancelled_mcp_disconnect_waits_for_process_cleanup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = MCPClient(MCPServerConfig(name="blocked", command="server", args=[], env={}))
    process = Mock()
    client._process = process
    cleanup_started = asyncio.Event()
    cleanup_finished = asyncio.Event()
    release_cleanup = asyncio.Event()

    async def cleanup(*args: object, **kwargs: object) -> None:
        cleanup_started.set()
        await release_cleanup.wait()
        cleanup_finished.set()

    monkeypatch.setattr(mcp_client_module, "terminate_process_tree", cleanup)
    task = asyncio.create_task(client.disconnect())
    await asyncio.wait_for(cleanup_started.wait(), timeout=1)

    task.cancel()
    await asyncio.sleep(0)
    assert not cleanup_finished.is_set()

    release_cleanup.set()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=1)

    assert cleanup_finished.is_set()
    assert client._process is None
    assert client._reader_task is None
    assert client._stderr_task is None


@pytest.mark.asyncio
async def test_mcp_disconnect_retains_process_when_tree_cleanup_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = MCPClient(MCPServerConfig(name="broken", command="server", args=[], env={}))
    process = Mock()
    client._process = process
    monkeypatch.setattr(
        mcp_client_module,
        "terminate_process_tree",
        AsyncMock(side_effect=ProcessTreeTerminationError("unconfirmed")),
    )

    with pytest.raises(MCPProtocolError, match="process cleanup failed"):
        await client.disconnect()

    assert client._process is process
    assert client._disconnect_cleanup_task is None


@pytest.mark.asyncio
async def test_cancelled_runtime_start_cleans_initialized_clients(
    tmp_path: Path,
) -> None:
    blocked_server = r"""
import json, sys, time
for line in sys.stdin:
    message = json.loads(line)
    method = message.get("method")
    if method == "server/discover":
        print(json.dumps({"jsonrpc": "2.0", "id": message["id"], "error": {"code": -32601, "message": "legacy"}}), flush=True)
        continue
    if method == "initialize":
        result = {
            "protocolVersion": "2025-11-25",
            "capabilities": {"tools": {}},
        }
        print(json.dumps({"jsonrpc": "2.0", "id": message["id"], "result": result}), flush=True)
    elif method == "tools/list":
        time.sleep(60)
"""
    runtime = MCPRuntime(
        {
            "blocked": MCPServerConfig(
                name="blocked",
                command=sys.executable,
                args=["-u", "-c", blocked_server],
                env={},
            )
        },
        SafetyGuard(tmp_path),
    )
    task = asyncio.create_task(runtime.start())
    client = None
    for _ in range(200):
        client = runtime.clients.get("blocked")
        if client is not None and client._initialized and client._pending:
            break
        await asyncio.sleep(0.01)
    assert client is not None and client._process is not None
    process = client._process

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=2)

    assert runtime.clients == {}
    assert client._process is None
    assert client._reader_task is None
    assert client._stderr_task is None
    assert process.returncode is not None


@pytest.mark.asyncio
async def test_initialize_timeout_does_not_send_cancellation_notification() -> None:
    client = MCPClient(MCPServerConfig(name="fake", command="fake", args=[], env={}))
    client._request_stdio = AsyncMock(side_effect=asyncio.TimeoutError())
    client.notify = AsyncMock()

    with pytest.raises(asyncio.TimeoutError):
        await client.request("initialize")

    client.notify.assert_not_awaited()


@pytest.mark.asyncio
async def test_sessionless_http_timeout_sends_cancellation_without_reinitialize() -> (
    None
):
    trace: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if not request.content:
            return httpx.Response(204)
        payload = json.loads(request.content)
        method = payload["method"]
        trace.append(method)
        if method == "initialize":
            return httpx.Response(
                200,
                json={
                    "jsonrpc": "2.0",
                    "id": payload["id"],
                    "result": {
                        "protocolVersion": "2025-11-25",
                        "capabilities": {"tools": {}},
                    },
                },
            )
        if method == "notifications/initialized":
            return httpx.Response(202)
        if method == "notifications/cancelled":
            assert request.headers.get("Mcp-Session-Id") is None
            return httpx.Response(202)
        raise httpx.ReadTimeout("timed out", request=request)

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = MCPClient(
        MCPServerConfig(
            name="remote",
            command="",
            args=[],
            env={},
            transport="http",
            url="https://mcp.example.test/rpc",
        ),
        http_client=http,
    )
    await client.connect()
    try:
        with pytest.raises(httpx.ReadTimeout):
            await client.call_tool("slow", {})
            assert trace == [
                "ping",
                "initialize",
                "notifications/initialized",
                "tools/call",
                "notifications/cancelled",
            ]
    finally:
        await client.disconnect()
        await http.aclose()


@pytest.mark.asyncio
async def test_server_cancellation_stops_incoming_request_without_response() -> None:
    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def handle_request(method: str, params: dict) -> dict:
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()
        return {}

    client = MCPClient(
        MCPServerConfig(name="fake", command="fake", args=[], env={}),
        server_request_handler=handle_request,
    )
    client._send_message = AsyncMock()
    client._dispatch_incoming(
        {"jsonrpc": "2.0", "id": "server-1", "method": "custom", "params": {}}
    )
    await asyncio.wait_for(started.wait(), timeout=1)

    client._dispatch_incoming(
        {
            "jsonrpc": "2.0",
            "method": "notifications/cancelled",
            "params": {"requestId": "server-1", "reason": "no longer needed"},
        }
    )
    await asyncio.wait_for(cancelled.wait(), timeout=1)
    await asyncio.sleep(0)

    client._send_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_mcp_bounds_pending_notification_handler_tasks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gate = asyncio.Event()
    started = 0

    async def handle_notification(method: str, params: dict) -> None:
        nonlocal started
        started += 1
        await gate.wait()

    monkeypatch.setattr(mcp_client_module, "MAX_PENDING_MCP_NOTIFICATIONS", 2)
    client = MCPClient(
        MCPServerConfig(name="fake", command="fake", args=[], env={}),
        notification_handler=handle_notification,
    )
    message = {
        "jsonrpc": "2.0",
        "method": "notifications/message",
        "params": {"level": "info", "data": "ready"},
    }

    for _ in range(10):
        client._dispatch_incoming(message)
    await asyncio.sleep(0)

    assert started == 2
    assert len(client._server_tasks) == 2
    gate.set()
    await asyncio.gather(*tuple(client._server_tasks))
    await asyncio.sleep(0)
    assert client._server_tasks == set()


@pytest.mark.asyncio
async def test_mcp_bounds_pending_server_request_tasks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gate = asyncio.Event()
    started = 0

    async def handle_request(method: str, params: dict) -> dict:
        nonlocal started
        started += 1
        await gate.wait()
        return {}

    monkeypatch.setattr(mcp_client_module, "MAX_PENDING_MCP_SERVER_REQUESTS", 2)
    monkeypatch.setattr(mcp_client_module, "MAX_PENDING_MCP_OVERLOAD_RESPONSES", 3)
    client = MCPClient(
        MCPServerConfig(name="fake", command="fake", args=[], env={}),
        server_request_handler=handle_request,
    )
    client._send_message = AsyncMock()

    for index in range(100):
        client._dispatch_incoming(
            {
                "jsonrpc": "2.0",
                "id": f"server-{index}",
                "method": "custom",
                "params": {},
            }
        )
    observed_tasks = len(client._server_tasks)
    await asyncio.sleep(0)
    observed_started = started
    observed_requests = len(client._incoming_requests)
    await asyncio.sleep(0)
    overload_responses = [
        call.args[0]
        for call in client._send_message.await_args_list
        if isinstance(call.args[0], dict) and "error" in call.args[0]
    ]

    gate.set()
    await asyncio.gather(*tuple(client._server_tasks))
    await asyncio.sleep(0)

    assert observed_started == 2
    assert observed_tasks == 5
    assert observed_requests == 2
    assert len(overload_responses) == 3
    assert all(response["error"]["code"] == -32000 for response in overload_responses)
    assert client._server_tasks == set()
    assert client._incoming_requests == {}


@pytest.mark.asyncio
async def test_mcp_duplicate_server_request_id_does_not_replace_owner() -> None:
    gate = asyncio.Event()
    started = 0

    async def handle_request(method: str, params: dict) -> dict:
        nonlocal started
        started += 1
        await gate.wait()
        return {}

    client = MCPClient(
        MCPServerConfig(name="fake", command="fake", args=[], env={}),
        server_request_handler=handle_request,
    )
    client._send_message = AsyncMock()
    message = {
        "jsonrpc": "2.0",
        "id": "same-id",
        "method": "custom",
        "params": {},
    }

    client._dispatch_incoming(message)
    await asyncio.sleep(0)
    owner = client._incoming_requests["same-id"]
    client._dispatch_incoming(message)
    await asyncio.sleep(0)
    observed_started = started
    observed_owner = client._incoming_requests.get("same-id")

    gate.set()
    await asyncio.gather(*tuple(client._server_tasks))
    await asyncio.sleep(0)

    assert observed_started == 1
    assert observed_owner is owner
    assert client._send_message.await_count == 1
    assert client._server_tasks == set()
    assert client._incoming_requests == {}


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["roots/list", "sampling/createMessage", "elicitation/create"])
async def test_mcp_rejects_unassociated_contextual_server_requests(method: str) -> None:
    sampling = AsyncMock(return_value={"role": "assistant", "content": {"type": "text", "text": "ok"}, "model": "test"})
    elicitation = AsyncMock(return_value={"action": "cancel"})
    client = MCPClient(
        MCPServerConfig(name="fake", command="fake", args=[], env={}),
        roots=(Path.cwd(),),
        sampling_handler=sampling,
        elicitation_handler=elicitation,
    )
    client._send_message = AsyncMock()

    client._dispatch_incoming(
        {"jsonrpc": "2.0", "id": "server-1", "method": method, "params": {}}
    )
    await asyncio.sleep(0)
    await asyncio.gather(*tuple(client._server_tasks))

    sampling.assert_not_awaited()
    elicitation.assert_not_awaited()
    response = client._send_message.await_args.args[0]
    assert response["id"] == "server-1"
    assert response["error"]["code"] == -32600
    assert "associated" in response["error"]["message"]


@pytest.mark.asyncio
async def test_mcp_allows_sampling_nested_under_active_client_request() -> None:
    sampling = AsyncMock(
        return_value={
            "role": "assistant",
            "content": {"type": "text", "text": "ok"},
            "model": "test-model",
        }
    )
    client = MCPClient(
        MCPServerConfig(name="fake", command="fake", args=[], env={}),
        sampling_handler=sampling,
    )
    client._send_message = AsyncMock()
    pending = asyncio.get_running_loop().create_future()
    client._pending[41] = pending
    try:
        client._dispatch_incoming(
            {
                "jsonrpc": "2.0",
                "id": "server-2",
                "method": "sampling/createMessage",
                "params": {"messages": [], "maxTokens": 1},
            }
        )
        await asyncio.sleep(0)
        await asyncio.gather(*tuple(client._server_tasks))
    finally:
        client._pending.pop(41, None)
        pending.cancel()

    sampling.assert_awaited_once()
    response = client._send_message.await_args.args[0]
    assert response["result"]["model"] == "test-model"


@pytest.mark.asyncio
async def test_mcp_server_request_preserves_protocol_error_code_and_data() -> None:
    async def reject(params: dict) -> dict:
        del params
        raise MCPProtocolError("invalid sampling params", code=-32602, data={"field": "tools"})

    client = MCPClient(
        MCPServerConfig(name="fake", command="fake", args=[], env={}),
        sampling_handler=reject,
    )
    client._send_message = AsyncMock()
    pending = asyncio.get_running_loop().create_future()
    client._pending[7] = pending
    try:
        client._dispatch_incoming(
            {
                "jsonrpc": "2.0",
                "id": "server-3",
                "method": "sampling/createMessage",
                "params": {},
            }
        )
        await asyncio.sleep(0)
        await asyncio.gather(*tuple(client._server_tasks))
    finally:
        client._pending.pop(7, None)
        pending.cancel()

    response = client._send_message.await_args.args[0]
    assert response["error"] == {
        "code": -32602,
        "message": "invalid sampling params",
        "data": {"field": "tools"},
    }


DYNAMIC_MCP_SERVER = r"""
import json, sys
state = "old"
list_count = 0
for line in sys.stdin:
    message = json.loads(line)
    method = message.get("method")
    if method == "server/discover":
        print(json.dumps({"jsonrpc": "2.0", "id": message["id"], "error": {"code": -32601, "message": "legacy"}}), flush=True)
        continue
    if method == "initialize":
        result = {
            "protocolVersion": "2025-11-25",
            "capabilities": {"tools": {"listChanged": True}},
            "serverInfo": {"name": "dynamic", "version": "1"},
        }
        print(json.dumps({"jsonrpc": "2.0", "id": message["id"], "result": result}), flush=True)
    elif method == "tools/list":
        list_count += 1
        schema = {
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
            "additionalProperties": False,
        }
        result = {"tools": [{"name": state, "description": state, "inputSchema": schema}]}
        print(json.dumps({"jsonrpc": "2.0", "id": message["id"], "result": result}), flush=True)
        if list_count == 1:
            state = "new"
            print(json.dumps({"jsonrpc": "2.0", "method": "notifications/tools/list_changed"}), flush=True)
    elif method == "tools/call":
        name = message["params"]["name"]
        result = {"content": [{"type": "text", "text": name}]}
        print(json.dumps({"jsonrpc": "2.0", "id": message["id"], "result": result}), flush=True)
"""


@pytest.mark.asyncio
async def test_runtime_applies_startup_tool_list_change_atomically(
    tmp_path: Path,
) -> None:
    live_tools: dict[str, object] = {}
    replacements: list[tuple[set[str], set[str]]] = []

    async def replace(server: str, previous: dict, replacement: dict) -> None:
        assert server == "dynamic"
        replacements.append((set(previous), set(replacement)))
        for name in previous:
            live_tools.pop(name, None)
        live_tools.update(replacement)

    events: list[dict] = []
    runtime = MCPRuntime(
        {
            "dynamic": MCPServerConfig(
                name="dynamic",
                command=sys.executable,
                args=["-u", "-c", DYNAMIC_MCP_SERVER],
                env={},
            )
        },
        SafetyGuard(tmp_path),
        tool_change_handler=replace,
        event_sink=events.append,
    )
    live_tools.update(await runtime.start())
    try:
        await runtime.wait_for_refreshes()
        assert "mcp__dynamic__old" not in live_tools
        assert "mcp__dynamic__new" in live_tools
        result = await live_tools["mcp__dynamic__new"].run(text="hello")
        assert result.success is True
        assert result.output == "new"
        assert replacements == [({"mcp__dynamic__old"}, {"mcp__dynamic__new"})]
        assert any(
            event.get("type") == "mcp.catalog.changed"
            and event.get("added") == ["mcp__dynamic__new"]
            and event.get("removed") == ["mcp__dynamic__old"]
            for event in events
        )
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_failed_dynamic_catalog_preserves_last_good_tools(
    tmp_path: Path,
) -> None:
    broken_server = DYNAMIC_MCP_SERVER.replace(
        '"required": ["text"]',
        '"required": ["text"] if state == "old" else "invalid"',
    )
    replacements: list[dict] = []

    async def replace(server: str, previous: dict, replacement: dict) -> None:
        replacements.append(replacement)

    runtime = MCPRuntime(
        {
            "dynamic": MCPServerConfig(
                name="dynamic",
                command=sys.executable,
                args=["-u", "-c", broken_server],
                env={},
            )
        },
        SafetyGuard(tmp_path),
        tool_change_handler=replace,
    )
    tools = await runtime.start()
    try:
        await runtime.wait_for_refreshes()
        assert "mcp__dynamic__old" in tools
        assert replacements == []
        assert "invalid tool catalog" in runtime.errors["dynamic:tools/refresh"]
        quarantined = await tools["mcp__dynamic__old"].run(text="blocked")
        assert quarantined.success is False
        assert "no longer matches the active verified" in quarantined.error
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_tool_list_change_quarantines_calls_until_refresh_finishes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    refresh_started = asyncio.Event()
    release_refresh = asyncio.Event()

    class PausedRefreshClient:
        server_capabilities = {"tools": {"listChanged": True}}
        protocol_version = "2025-11-25"
        session_generation = 1

        def __init__(self, config, *, roots=()) -> None:
            self.notification_handler = None
            self.session_reinitialized_handler = None
            self.tool_contract_validator = None
            self.list_calls = 0
            self.tool_calls = 0

        async def connect(self) -> None:
            return None

        def supports_server_capability(self, name: str) -> bool:
            return name == "tools"

        async def list_tools(self) -> list[dict]:
            self.list_calls += 1
            if self.list_calls == 2:
                refresh_started.set()
                await release_refresh.wait()
            return [{"name": "echo", "inputSchema": {"type": "object"}}]

        async def call_tool(
            self,
            name: str,
            arguments: dict,
            *,
            expected_contract: str | None = None,
            as_task: bool = False,
            header_annotations: list | None = None,
        ) -> dict:
            del name, arguments, expected_contract, as_task, header_annotations
            self.tool_calls += 1
            return {"content": []}

        async def disconnect(self) -> None:
            return None

    monkeypatch.setattr("ash.mcp.runtime.MCPClient", PausedRefreshClient)
    runtime = MCPRuntime(
        {"paused": MCPServerConfig(name="paused", command="unused", args=[], env={})},
        SafetyGuard(tmp_path),
    )
    tools = await runtime.start()
    client = runtime.clients["paused"]
    try:
        await client.notification_handler("notifications/tools/list_changed", {})
        await asyncio.wait_for(refresh_started.wait(), timeout=1)

        quarantined = await tools["mcp__paused__echo"].run()
        assert quarantined.success is False
        assert "no longer matches the active verified" in quarantined.error
        assert client.tool_calls == 0

        release_refresh.set()
        await runtime.wait_for_refreshes()
        restored = await tools["mcp__paused__echo"].run()
        assert restored.success is True
        assert client.tool_calls == 1
    finally:
        release_refresh.set()
        await runtime.close()


@pytest.mark.asyncio
async def test_resource_and_prompt_list_changes_emit_live_revisions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class CatalogClient:
        server_capabilities = {
            "resources": {"listChanged": True},
            "prompts": {"listChanged": True},
        }
        protocol_version = "2025-11-25"

        def __init__(self, config, *, roots=()) -> None:
            self.config = config
            self.notification_handler = None

        async def connect(self) -> None:
            return None

        def supports_server_capability(self, name: str) -> bool:
            return name in self.server_capabilities

        async def disconnect(self) -> None:
            return None

    monkeypatch.setattr("ash.mcp.runtime.MCPClient", CatalogClient)
    events: list[dict] = []
    runtime = MCPRuntime(
        {"catalog": MCPServerConfig(name="catalog", command="unused", args=[], env={})},
        SafetyGuard(tmp_path),
        event_sink=events.append,
    )
    await runtime.start()
    client = runtime.clients["catalog"]
    assert client.notification_handler is not None
    try:
        await client.notification_handler("notifications/resources/list_changed", {})
        await client.notification_handler("notifications/prompts/list_changed", {})
        assert [(event["capability"], event["revision"]) for event in events] == [
            ("resources", 1),
            ("prompts", 2),
        ]
    finally:
        await runtime.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("contract_mode", ["unchanged", "renamed", "removed"])
async def test_runtime_reconciles_catalog_without_http_tool_replay(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    contract_mode: str,
) -> None:
    initialize_count = 0
    tool_attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal initialize_count, tool_attempts
        if request.method == "DELETE":
            return httpx.Response(405)
        if request.content.startswith(b'{"jsonrpc":"2.0","id":0'):
            return httpx.Response(
                200,
                json={"jsonrpc": "2.0", "id": 0, "result": {}},
            )
        payload = json.loads(request.content)
        method = payload["method"]
        session = request.headers.get("Mcp-Session-Id")
        if method == "initialize":
            initialize_count += 1
            capabilities = (
                {}
                if contract_mode == "removed" and initialize_count == 2
                else {"tools": {}}
            )
            return httpx.Response(
                200,
                headers={"Mcp-Session-Id": f"session-{initialize_count}"},
                json={
                    "jsonrpc": "2.0",
                    "id": payload["id"],
                    "result": {
                        "protocolVersion": "2025-11-25",
                        "capabilities": capabilities,
                    },
                },
            )
        if method == "notifications/initialized":
            return httpx.Response(202)
        if method == "tools/list":
            assert not (contract_mode == "removed" and session == "session-2")
            name = (
                "replacement"
                if contract_mode == "renamed" and session == "session-2"
                else "echo"
            )
            result = {
                "tools": [
                    {
                        "name": name,
                        "description": name,
                        "inputSchema": {
                            "type": "object",
                            "properties": {"text": {"type": "string"}},
                            "required": ["text"],
                        },
                    }
                ]
            }
            return httpx.Response(
                200,
                json={"jsonrpc": "2.0", "id": payload["id"], "result": result},
            )
        tool_attempts += 1
        if session == "session-1":
            return httpx.Response(404)
        return httpx.Response(
            200,
            json={
                "jsonrpc": "2.0",
                "id": payload["id"],
                "result": {"content": [{"type": "text", "text": "ok"}]},
            },
        )

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    real_client = MCPClient

    def client_factory(config, **kwargs):
        return real_client(config, http_client=http, **kwargs)

    monkeypatch.setattr("ash.mcp.runtime.MCPClient", client_factory)
    live: dict[str, object] = {}

    async def replace(server: str, previous: dict, replacement: dict) -> None:
        for name in previous:
            live.pop(name, None)
        live.update(replacement)

    runtime = MCPRuntime(
        {
            "remote": MCPServerConfig(
                name="remote",
                command="",
                args=[],
                env={},
                transport="http",
                url="https://mcp.example.test/rpc",
            )
        },
        SafetyGuard(tmp_path),
        tool_change_handler=replace,
    )
    live.update(await runtime.start())
    old_tool = live["mcp__remote__echo"]
    try:
        result = await old_tool.run(text="hello")
        assert result.success is False
        assert "operation was not replayed" in result.error
        assert tool_attempts == 1
        if contract_mode != "unchanged":
            assert "mcp__remote__echo" not in live
            assert ("mcp__remote__replacement" in live) is (contract_mode == "renamed")
            stale_result = await old_tool.run(text="again")
            assert stale_result.success is False
            assert "no longer matches the active verified" in stale_result.error
            assert tool_attempts == 1
        else:
            assert "mcp__remote__echo" in live
        assert initialize_count == 2
    finally:
        await runtime.close()
        await http.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("replacement_supports_tools", [True, False])
async def test_runtime_start_recovers_session_expiry_during_initial_catalog(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    replacement_supports_tools: bool,
) -> None:
    initialize_count = 0
    list_sessions: list[str | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal initialize_count
        if request.method == "DELETE":
            return httpx.Response(405)
        if request.content.startswith(b'{"jsonrpc":"2.0","id":0'):
            return httpx.Response(
                200,
                json={"jsonrpc": "2.0", "id": 0, "result": {}},
            )
        payload = json.loads(request.content)
        method = payload["method"]
        session = request.headers.get("Mcp-Session-Id")
        if method == "initialize":
            initialize_count += 1
            capabilities = (
                {"tools": {}}
                if initialize_count == 1 or replacement_supports_tools
                else {}
            )
            return httpx.Response(
                200,
                headers={"Mcp-Session-Id": f"session-{initialize_count}"},
                json={
                    "jsonrpc": "2.0",
                    "id": payload["id"],
                    "result": {
                        "protocolVersion": "2025-11-25",
                        "capabilities": capabilities,
                    },
                },
            )
        if method == "notifications/initialized":
            return httpx.Response(202)
        assert method == "tools/list"
        list_sessions.append(session)
        if session == "session-1":
            return httpx.Response(404)
        return httpx.Response(
            200,
            json={
                "jsonrpc": "2.0",
                "id": payload["id"],
                "result": {
                    "tools": [{"name": "echo", "inputSchema": {"type": "object"}}]
                },
            },
        )

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    real_client = MCPClient

    def client_factory(config, **kwargs):
        return real_client(config, http_client=http, **kwargs)

    monkeypatch.setattr("ash.mcp.runtime.MCPClient", client_factory)
    runtime = MCPRuntime(
        {
            "remote": MCPServerConfig(
                name="remote",
                command="",
                args=[],
                env={},
                transport="http",
                url="https://mcp.example.test/rpc",
            )
        },
        SafetyGuard(tmp_path),
    )
    try:
        tools = await asyncio.wait_for(runtime.start(), timeout=1)
        assert ("mcp__remote__echo" in tools) is replacement_supports_tools
        assert initialize_count == 2
        expected_sessions = (
            ["session-1", "session-2"] if replacement_supports_tools else ["session-1"]
        )
        assert list_sessions == expected_sessions
        assert runtime.errors == {}
    finally:
        await runtime.close()
        await http.aclose()


@pytest.mark.asyncio
async def test_runtime_blocks_concurrent_stale_call_until_catalog_reconciles(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    initialize_count = 0
    tool_attempts = 0
    replacement_list_started = asyncio.Event()
    release_replacement_list = asyncio.Event()

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal initialize_count, tool_attempts
        if request.method == "DELETE":
            return httpx.Response(405)
        if request.content.startswith(b'{"jsonrpc":"2.0","id":0'):
            return httpx.Response(
                200,
                json={"jsonrpc": "2.0", "id": 0, "result": {}},
            )
        payload = json.loads(request.content)
        method = payload["method"]
        session = request.headers.get("Mcp-Session-Id")
        if method == "initialize":
            initialize_count += 1
            return httpx.Response(
                200,
                headers={"Mcp-Session-Id": f"session-{initialize_count}"},
                json={
                    "jsonrpc": "2.0",
                    "id": payload["id"],
                    "result": {
                        "protocolVersion": "2025-11-25",
                        "capabilities": {"tools": {}},
                    },
                },
            )
        if method == "notifications/initialized":
            return httpx.Response(202)
        if method == "tools/list":
            if session == "session-2":
                replacement_list_started.set()
                await asyncio.wait_for(release_replacement_list.wait(), timeout=1)
            name = "replacement" if session == "session-2" else "echo"
            return httpx.Response(
                200,
                json={
                    "jsonrpc": "2.0",
                    "id": payload["id"],
                    "result": {
                        "tools": [
                            {
                                "name": name,
                                "inputSchema": {"type": "object"},
                            }
                        ]
                    },
                },
            )
        tool_attempts += 1
        if session == "session-1":
            return httpx.Response(404)
        return httpx.Response(
            200,
            json={"jsonrpc": "2.0", "id": payload["id"], "result": {}},
        )

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    real_client = MCPClient

    def client_factory(config, **kwargs):
        return real_client(config, http_client=http, **kwargs)

    monkeypatch.setattr("ash.mcp.runtime.MCPClient", client_factory)
    runtime = MCPRuntime(
        {
            "remote": MCPServerConfig(
                name="remote",
                command="",
                args=[],
                env={},
                transport="http",
                url="https://mcp.example.test/rpc",
            )
        },
        SafetyGuard(tmp_path),
    )
    tools = await runtime.start()
    old_tool = tools["mcp__remote__echo"]
    first = asyncio.create_task(old_tool.run())
    try:
        await asyncio.wait_for(replacement_list_started.wait(), timeout=1)
        second = asyncio.create_task(old_tool.run())
        await asyncio.sleep(0.05)
        assert second.done() is False
        assert tool_attempts == 1

        release_replacement_list.set()
        first_result, second_result = await asyncio.gather(first, second)
        assert first_result.success is False
        assert "operation was not replayed" in first_result.error
        assert second_result.success is False
        assert "no longer matches the active verified" in second_result.error
        assert tool_attempts == 1
    finally:
        release_replacement_list.set()
        if not first.done():
            first.cancel()
            await asyncio.gather(first, return_exceptions=True)
        await runtime.close()
        await http.aclose()


@pytest.mark.asyncio
async def test_output_schema_only_refresh_is_reported_as_changed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class ChangingClient:
        server_capabilities = {"tools": {"listChanged": True}}
        protocol_version = "2025-11-25"

        def __init__(self, config, *, roots=()) -> None:
            self.notification_handler = None
            self.session_reinitialized_handler = None
            self.calls = 0

        async def connect(self) -> None:
            return None

        def supports_server_capability(self, name: str) -> bool:
            return name == "tools"

        async def list_tools(self) -> list[dict]:
            self.calls += 1
            return [
                {
                    "name": "same",
                    "description": "same",
                    "inputSchema": {"type": "object"},
                    "outputSchema": {
                        "type": "object",
                        "properties": {"version": {"const": self.calls}},
                    },
                }
            ]

        async def disconnect(self) -> None:
            return None

    monkeypatch.setattr("ash.mcp.runtime.MCPClient", ChangingClient)
    events: list[dict] = []
    runtime = MCPRuntime(
        {
            "changing": MCPServerConfig(
                name="changing", command="unused", args=[], env={}
            )
        },
        SafetyGuard(tmp_path),
        event_sink=events.append,
    )
    await runtime.start()
    client = runtime.clients["changing"]
    try:
        await client.notification_handler("notifications/tools/list_changed", {})
        await runtime.wait_for_refreshes()
        tool_events = [
            event
            for event in events
            if event.get("type") == "mcp.catalog.changed"
            and event.get("capability") == "tools"
        ]
        assert tool_events[-1]["changed"] == ["mcp__changing__same"]
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_refresh_storm_is_bounded_and_reported(tmp_path: Path) -> None:
    storm_server = DYNAMIC_MCP_SERVER.replace("if list_count == 1:", "if True:")
    events: list[dict] = []
    runtime = MCPRuntime(
        {
            "storm": MCPServerConfig(
                name="storm",
                command=sys.executable,
                args=["-u", "-c", storm_server],
                env={},
            )
        },
        SafetyGuard(tmp_path),
        event_sink=events.append,
    )
    await runtime.start()
    try:
        await asyncio.wait_for(runtime.wait_for_refreshes(), timeout=2)
        assert "refresh storm" in runtime.errors["storm:tools/refresh"]
        assert any(
            event.get("type") == "mcp.catalog.refresh_suppressed" for event in events
        )
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_legacy_sse_discovers_endpoint_and_receives_async_response() -> None:
    requests: list[tuple[str, str]] = []
    initialized = asyncio.Event()

    class StreamingSSETransport(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request):
            requests.append((request.method, str(request.url)))
            if request.url.path != "/sse":
                return httpx.Response(202)

            class SSEStream(httpx.AsyncByteStream):
                async def __aiter__(self):
                    yield b"event: endpoint\ndata: /messages?session=legacy-1\n\n"
                    yield (
                        'event: message\ndata: {"jsonrpc":"2.0","id":1,'
                        '"result":{"protocolVersion":"2025-06-18","capabilities":{}}}\n\n'
                    ).encode()
                    yield (
                        'event: message\ndata: {"jsonrpc":"2.0","id":2,'
                        '"result":{"tools":[]}}\n\n'
                    ).encode()
                    yield (
                        'event: message\ndata: {"jsonrpc":"2.0",'
                        '"method":"notifications/message","params":{"level":"info",'
                        '"data":"ready"}}\n\n'
                    ).encode()
                    await initialized.wait()

            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                stream=SSEStream(),
            )

    http = httpx.AsyncClient(transport=StreamingSSETransport())
    client = MCPClient(
        MCPServerConfig(
            name="legacy",
            command="",
            args=[],
            env={},
            transport="sse",
            url="https://legacy.example.test/sse",
        ),
        http_client=http,
    )
    notifications: list[str] = []
    client.notification_handler = lambda method, params: notifications.append(method)
    await asyncio.wait_for(client.connect(), timeout=1)
    tools = await asyncio.wait_for(client.list_tools(), timeout=1)
    initialized.set()
    await asyncio.wait_for(
        client.notify(
            "notifications/initialized",
            {},
            _allow_session_recovery=False,
        ),
        timeout=1,
    )
    assert tools == []
    assert client._pending == {}
    assert client._legacy_sse_endpoint == (
        "https://legacy.example.test/messages?session=legacy-1"
    )
    assert "notifications/message" in notifications
    await client.disconnect()
    await http.aclose()
    assert all(method != "DELETE" for method, _ in requests)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("event_body", "message"),
    [
        ("", "requires an endpoint event"),
        (
            "event: wrong\ndata: https://other.test/messages\n\n",
            "requires an endpoint event",
        ),
        (
            "event: endpoint\ndata: https://attacker.test/messages\n\n",
            "origin does not match",
        ),
    ],
)
async def test_legacy_sse_rejects_invalid_discovery(
    event_body: str,
    message: str,
) -> None:
    http = httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200,
                text=event_body,
                headers={"content-type": "text/event-stream"},
            )
        )
    )
    client = MCPClient(
        MCPServerConfig(
            name="legacy",
            command="",
            args=[],
            env={},
            transport="sse",
            url="https://legacy.example.test/sse",
        ),
        http_client=http,
    )
    with pytest.raises(MCPProtocolError, match=message):
        await client.connect()
    await http.aclose()


def test_mcp_header_annotations_reject_invalid_locations_and_names() -> None:
    with pytest.raises(ValueError, match="invalid x-mcp-header"):
        _extract_mcp_header_annotations(
            {
                "type": "object",
                "properties": {"value": {"type": "string", "x-mcp-header": "bad name"}},
            },
            label="test schema",
        )
    with pytest.raises(ValueError, match="x-mcp-header"):
        _extract_mcp_header_annotations(
            {
                "type": "object",
                "properties": {
                    "items": {"type": "array", "items": {"x-mcp-header": "Value"}}
                },
            },
            label="test schema",
        )


@pytest.mark.asyncio
async def test_streamable_http_tool_call_sends_validated_parameter_headers() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.method == "DELETE" or not request.content:
            return httpx.Response(204)
        if request.method == "GET":
            return httpx.Response(405)
        payload = json.loads(request.content)
        if "id" not in payload:
            return httpx.Response(202)
        if payload["method"] == "initialize":
            return httpx.Response(
                200,
                headers={"Mcp-Session-Id": "session-1"},
                json={
                    "jsonrpc": "2.0",
                    "id": payload["id"],
                    "result": {"protocolVersion": "2025-11-25", "capabilities": {}},
                },
            )
        return httpx.Response(
            200,
            headers={"Mcp-Session-Id": "session-1"},
            json={
                "jsonrpc": "2.0",
                "id": payload["id"],
                "result": {"content": [{"type": "text", "text": "ok"}]},
            },
        )

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = MCPClient(
        MCPServerConfig(
            name="remote",
            command="",
            args=[],
            env={},
            transport="http",
            url="https://mcp.example.test/rpc",
        ),
        http_client=http,
        timeout=1,
    )
    await client.connect()
    await client.call_tool(
        "execute_sql",
        {
            "region": "us-west1",
            "greeting": "Hello, 世界",
            "count": 7,
            "enabled": True,
        },
        header_annotations=[
            (
                (
                    "properties",
                    "region",
                ),
                "Region",
            ),
            (
                (
                    "properties",
                    "greeting",
                ),
                "Greeting",
            ),
            (
                (
                    "properties",
                    "count",
                ),
                "Count",
            ),
            (
                (
                    "properties",
                    "enabled",
                ),
                "Enabled",
            ),
        ],
    )
    await client.disconnect()
    await http.aclose()
    call = next(
        request
        for request in requests
        if request.url.path == "/rpc" and b'"tools/call"' in request.content
    )
    assert call.headers["Mcp-Method"] == "tools/call"
    assert call.headers["Mcp-Name"] == "execute_sql"
    assert call.headers["Mcp-Param-Region"] == "us-west1"
    assert call.headers["Mcp-Param-Greeting"].startswith("=?base64?")
    assert call.headers["Mcp-Param-Count"] == "7"
    assert call.headers["Mcp-Param-Enabled"] == "true"


@pytest.mark.asyncio
async def test_legacy_sse_tool_calls_reject_http_parameter_headers() -> None:
    class StreamingSSETransport(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request):
            if request.url.path != "/sse":
                return httpx.Response(202)

            class SSEStream(httpx.AsyncByteStream):
                async def __aiter__(self):
                    yield b"event: endpoint\ndata: /messages\n\n"
                    yield (
                        'event: message\ndata: {"jsonrpc":"2.0","id":1,'
                        '"result":{"protocolVersion":"2024-11-05","capabilities":{}}}\n\n'
                    ).encode()
                    await asyncio.Event().wait()

            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                stream=SSEStream(),
            )

    http = httpx.AsyncClient(transport=StreamingSSETransport())
    client = MCPClient(
        MCPServerConfig(
            name="legacy",
            command="",
            args=[],
            env={},
            transport="sse",
            url="https://legacy.example.test/sse",
        ),
        http_client=http,
        timeout=0.2,
    )
    await client.connect()
    with pytest.raises(MCPProtocolError, match="require the http transport"):
        await client.call_tool(
            "tool",
            {},
            header_annotations=[(("properties", "region"), "Region")],
        )
    await client.disconnect()
    await http.aclose()


def test_runtime_mcp_tool_extracts_nested_header_annotations(
    tmp_path: Path,
) -> None:
    captured: dict[str, object] = {}

    class HeaderClient:
        protocol_version = "2026-07-28"
        session_generation = 1

        async def connect(self) -> None:
            return None

        async def list_tools(self) -> list[dict]:
            return [
                {
                    "name": "sql",
                    "inputSchema": {
                        "type": "object",
                        "properties": {
                            "options": {
                                "type": "object",
                                "properties": {
                                    "region": {
                                        "type": "string",
                                        "x-mcp-header": "Region",
                                    }
                                },
                            }
                        },
                    },
                }
            ]

        async def call_tool(
            self,
            name: str,
            arguments: dict,
            *,
            expected_contract: str | None = None,
            as_task: bool = False,
            header_annotations: list | None = None,
        ) -> dict:
            del name, expected_contract, as_task
            captured["arguments"] = arguments
            captured["annotations"] = header_annotations
            return {"content": [{"type": "text", "text": "ok"}]}

        async def disconnect(self) -> None:
            return None

    runtime = MCPRuntime(
        {"remote": MCPServerConfig(name="remote", command="unused", args=[], env={})},
        SafetyGuard(tmp_path),
    )
    tool = MCPTool(
        runtime.safety_guard,
        client=HeaderClient(),  # type: ignore[arg-type]
        server_name="remote",
        definition={
            "name": "sql",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "options": {
                        "type": "object",
                        "properties": {
                            "region": {"type": "string", "x-mcp-header": "Region"}
                        },
                    }
                },
            },
        },
        protocol_version="2026-07-28",
    )
    result = asyncio.run(tool.run(options={"region": "us-east1"}))
    assert result.success is True
    assert captured["annotations"] == [(("options", "region"), "Region")]



@pytest.mark.asyncio
async def test_stdio_negotiates_modern_protocol_without_initialize() -> None:
    server = r"""
import json, sys
for line in sys.stdin:
    message = json.loads(line)
    method = message.get("method")
    params = message.get("params", {})
    if method == "server/discover":
        meta = params.get("_meta", {})
        caps = meta.get("io.modelcontextprotocol/clientCapabilities", {})
        assert meta.get("io.modelcontextprotocol/protocolVersion") == "2026-07-28"
        assert caps.get("extensions", {}).get("io.modelcontextprotocol/tasks") == {}
        result = {
            "resultType": "complete",
            "supportedVersions": ["2026-07-28"],
            "capabilities": {"tools": {}},
            "_meta": {"io.modelcontextprotocol/serverInfo": {"name": "modern", "version": "1"}},
        }
    elif method == "initialize":
        result = None
        print(json.dumps({"jsonrpc": "2.0", "id": message["id"], "error": {"code": -32603, "message": "initialize forbidden"}}), flush=True)
        continue
    elif method == "tools/list":
        meta = params.get("_meta", {})
        assert meta.get("io.modelcontextprotocol/protocolVersion") == "2026-07-28"
        result = {
            "resultType": "complete",
            "tools": [{"name": "echo", "description": "echo", "inputSchema": {"type": "object"}}],
            "ttlMs": 1000,
            "cacheScope": "private",
        }
    else:
        result = {"resultType": "complete"}
    print(json.dumps({"jsonrpc": "2.0", "id": message["id"], "result": result}), flush=True)
"""
    client = MCPClient(
        MCPServerConfig(
            name="modern", command=sys.executable, args=["-u", "-c", server], env={}
        )
    )
    await client.connect()

    assert client.protocol_version == "2026-07-28"
    assert client.server_info == {"name": "modern", "version": "1"}
    tools = await client.list_tools()
    assert [tool["name"] for tool in tools] == ["echo"]
    await client.disconnect()


@pytest.mark.asyncio
async def test_http_modern_requests_use_stateless_routing_headers() -> None:
    seen: list[tuple[str, httpx.Headers, dict]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        seen.append((payload["method"], request.headers, payload))
        assert request.headers["MCP-Protocol-Version"] == "2026-07-28"
        assert request.headers["Mcp-Method"] == payload["method"]
        assert "Mcp-Session-Id" not in request.headers
        meta = payload["params"]["_meta"]
        assert meta["io.modelcontextprotocol/protocolVersion"] == "2026-07-28"
        if payload["method"] == "server/discover":
            result = {
                "resultType": "complete",
                "supportedVersions": ["2026-07-28", "2025-11-25"],
                "capabilities": {"tools": {}},
            }
        elif payload["method"] == "tools/call":
            assert request.headers["Mcp-Name"] == "echo"
            assert request.headers["Mcp-Param-Region"] == "us-east1"
            result = {
                "resultType": "complete",
                "content": [{"type": "text", "text": "works"}],
            }
        else:
            raise AssertionError(payload["method"])
        return httpx.Response(
            200,
            json={"jsonrpc": "2.0", "id": payload["id"], "result": result},
            request=request,
        )

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = MCPClient(
        MCPServerConfig(
            name="modern",
            command="",
            args=[],
            env={},
            transport="http",
            url="https://mcp.example.test/rpc",
        ),
        http_client=http,
    )
    await client.connect()
    result = await client.call_tool(
        "echo",
        {"region": "us-east1"},
        header_annotations=[(("region",), "Region")],
    )

    assert result["content"][0]["text"] == "works"
    assert [method for method, _, _ in seen] == ["server/discover", "tools/call"]
    await client.disconnect()
    await http.aclose()


@pytest.mark.asyncio
async def test_modern_task_extension_drives_input_update_and_completion() -> None:
    seen: list[tuple[str, httpx.Headers, dict]] = []
    persisted: list[tuple[str, dict[str, str]]] = []
    task_gets = 0
    task_id = "task-123"

    def task_state(status: str, **extra: object) -> dict:
        return {
            "taskId": task_id,
            "status": status,
            "createdAt": "2026-09-20T00:00:00Z",
            "lastUpdatedAt": "2026-09-20T00:00:01Z",
            "ttlMs": 60_000,
            "pollIntervalMs": 0,
            **extra,
        }

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal task_gets
        payload = json.loads(request.content)
        method = payload["method"]
        params = payload.get("params", {})
        seen.append((method, request.headers, payload))
        capabilities = params.get("_meta", {}).get(
            "io.modelcontextprotocol/clientCapabilities", {}
        )
        assert capabilities.get("extensions", {}).get(
            "io.modelcontextprotocol/tasks"
        ) == {}
        if method == "server/discover":
            result = {
                "resultType": "complete",
                "supportedVersions": ["2026-07-28"],
                "capabilities": {
                    "tools": {},
                    "extensions": {"io.modelcontextprotocol/tasks": {}},
                },
            }
        elif method == "tools/call":
            result = {"resultType": "task", **task_state("working")}
        elif method == "tasks/get":
            assert request.headers["Mcp-Name"] == task_id
            task_gets += 1
            if task_gets == 1:
                result = {
                    "resultType": "complete",
                    **task_state(
                        "input_required",
                        inputRequests={
                            "approve": {
                                "method": "elicitation/create",
                                "params": {
                                    "mode": "form",
                                    "message": "Approve?",
                                    "requestedSchema": {
                                        "type": "object",
                                        "properties": {
                                            "approved": {"type": "boolean"}
                                        },
                                        "required": ["approved"],
                                    },
                                },
                            }
                        },
                    ),
                }
            else:
                result = {
                    "resultType": "complete",
                    **task_state(
                        "completed",
                        result={
                            "content": [{"type": "text", "text": "finished"}],
                            "isError": False,
                        },
                    ),
                }
        elif method == "tasks/update":
            assert request.headers["Mcp-Name"] == task_id
            assert params["taskId"] == task_id
            assert params["inputResponses"] == {
                "approve": {
                    "action": "accept",
                    "content": {"approved": True},
                }
            }
            result = {"resultType": "complete"}
        else:
            raise AssertionError(method)
        return httpx.Response(
            200,
            json={"jsonrpc": "2.0", "id": payload["id"], "result": result},
            request=request,
        )

    async def elicit(params: dict) -> dict:
        assert params["message"] == "Approve?"
        return {"action": "accept", "content": {"approved": True}}

    async def persist_task_state(
        task: dict[str, Any], answered_inputs: dict[str, str]
    ) -> None:
        persisted.append((str(task["status"]), dict(answered_inputs)))

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = MCPClient(
        MCPServerConfig(
            name="modern-task",
            command="",
            args=[],
            env={},
            transport="http",
            url="https://mcp.example.test/rpc",
        ),
        http_client=http,
        elicitation_handler=elicit,
    )
    await client.connect()

    result = await client.call_tool(
        "long",
        {},
        modern_task_state_callback=persist_task_state,
    )

    assert result["content"][0]["text"] == "finished"
    task_requests = [entry for entry in seen if entry[0] != "subscriptions/listen"]
    assert [method for method, _, _ in task_requests] == [
        "server/discover",
        "tools/call",
        "tasks/get",
        "tasks/update",
        "tasks/get",
    ]
    assert any(method == "subscriptions/listen" for method, _, _ in seen)
    for method, headers, _ in task_requests[2:]:
        assert headers["Mcp-Method"] == method
        assert headers["Mcp-Name"] == task_id
    assert [status for status, _ in persisted] == [
        "working",
        "input_required",
        "input_required",
        "completed",
    ]
    assert persisted[0][1] == {}
    assert persisted[1][1] == {}
    assert set(persisted[2][1]) == {"approve"}
    assert persisted[3][1] == persisted[2][1]
    await client.disconnect()
    await http.aclose()


@pytest.mark.asyncio
async def test_modern_task_persistence_failure_requests_cancellation() -> None:
    client = MCPClient(MCPServerConfig(name="modern", command="fake", args=[], env={}))
    client.protocol_version = "2026-07-28"
    client.server_capabilities = {
        "extensions": {"io.modelcontextprotocol/tasks": {}}
    }
    request = AsyncMock(return_value={"resultType": "complete"})
    client.request = request  # type: ignore[method-assign]
    initial = {
        "resultType": "task",
        "taskId": "durability-failed",
        "status": "working",
        "createdAt": "2026-09-20T00:00:00Z",
        "lastUpdatedAt": "2026-09-20T00:00:01Z",
        "ttlMs": None,
    }

    async def fail_persistence(
        task: dict[str, Any], answered_inputs: dict[str, str]
    ) -> None:
        del task, answered_inputs
        raise OSError("database unavailable")

    with pytest.raises(MCPProtocolError, match="could not be persisted"):
        await client._await_modern_tool_task(
            initial,
            timeout=1,
            state_callback=fail_persistence,
        )

    assert request.await_count == 1
    assert request.await_args.args == (
        "tasks/cancel",
        {"taskId": "durability-failed"},
    )


@pytest.mark.asyncio
async def test_resume_modern_task_uses_task_id_without_replaying_tool_call() -> None:
    client = MCPClient(MCPServerConfig(name="modern", command="fake", args=[], env={}))
    client.protocol_version = "2026-07-28"
    client.server_capabilities = {
        "extensions": {"io.modelcontextprotocol/tasks": {}}
    }
    methods: list[str] = []
    task_id = "resume-me"

    async def request(method: str, params: dict, **kwargs: Any) -> dict:
        del kwargs
        methods.append(method)
        assert params == {"taskId": task_id}
        if method != "tasks/get":
            raise AssertionError(method)
        return {
            "resultType": "complete",
            "taskId": task_id,
            "status": "completed",
            "createdAt": "2026-09-20T00:00:00Z",
            "lastUpdatedAt": "2026-09-20T00:00:02Z",
            "ttlMs": 60_000,
            "result": {
                "content": [{"type": "text", "text": "recovered"}],
                "isError": False,
            },
        }

    client.request = request  # type: ignore[method-assign]
    persisted = {
        "taskId": task_id,
        "status": "working",
        "createdAt": "2026-09-20T00:00:00Z",
        "lastUpdatedAt": "2026-09-20T00:00:01Z",
        "ttlMs": 60_000,
        "pollIntervalMs": 0,
    }

    result = await client.resume_modern_task(persisted, {})

    assert result["content"][0]["text"] == "recovered"
    assert methods == ["tasks/get"]


@pytest.mark.asyncio
async def test_resume_modern_task_timeout_preserves_remote_task() -> None:
    client = MCPClient(MCPServerConfig(name="modern", command="fake", args=[], env={}))
    client.protocol_version = "2026-07-28"
    client.server_capabilities = {
        "extensions": {"io.modelcontextprotocol/tasks": {}}
    }
    task_id = "still-working"
    methods: list[str] = []
    client._start_modern_task_subscription = Mock()  # type: ignore[method-assign]
    client._stop_modern_task_subscription = AsyncMock()  # type: ignore[method-assign]

    async def request(method: str, params: dict, **kwargs: Any) -> dict:
        del kwargs
        methods.append(method)
        assert params == {"taskId": task_id}
        if method != "tasks/get":
            raise AssertionError(method)
        return {
            "resultType": "complete",
            "taskId": task_id,
            "status": "working",
            "createdAt": "2026-09-20T00:00:00Z",
            "lastUpdatedAt": "2026-09-20T00:00:01Z",
            "ttlMs": 60_000,
            "pollIntervalMs": 1000,
        }

    client.request = request  # type: ignore[method-assign]
    persisted = {
        "taskId": task_id,
        "status": "working",
        "createdAt": "2026-09-20T00:00:00Z",
        "lastUpdatedAt": "2026-09-20T00:00:01Z",
        "ttlMs": 60_000,
        "pollIntervalMs": 1000,
    }

    with pytest.raises(MCPTaskTimeout):
        await client.resume_modern_task(persisted, {}, timeout=0.001)

    assert "tasks/cancel" not in methods
    assert "tools/call" not in methods


@pytest.mark.asyncio
async def test_resume_modern_task_does_not_repeat_answered_input_request() -> None:
    client = MCPClient(MCPServerConfig(name="modern", command="fake", args=[], env={}))
    client.protocol_version = "2026-07-28"
    client.server_capabilities = {
        "extensions": {"io.modelcontextprotocol/tasks": {}}
    }
    task_id = "resume-input"
    methods: list[str] = []
    gets = 0
    input_request = {
        "method": "elicitation/create",
        "params": {
            "mode": "form",
            "message": "Approve?",
            "requestedSchema": {
                "type": "object",
                "properties": {"approved": {"type": "boolean"}},
                "required": ["approved"],
            },
        },
    }
    fingerprint = json.dumps(
        input_request,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )

    async def request(method: str, params: dict, **kwargs: Any) -> dict:
        nonlocal gets
        del kwargs
        methods.append(method)
        assert params == {"taskId": task_id}
        assert method == "tasks/get"
        gets += 1
        if gets == 1:
            return {
                "resultType": "complete",
                "taskId": task_id,
                "status": "input_required",
                "createdAt": "2026-09-20T00:00:00Z",
                "lastUpdatedAt": "2026-09-20T00:00:02Z",
                "ttlMs": 60_000,
                "pollIntervalMs": 0,
                "inputRequests": {"approve": input_request},
            }
        return {
            "resultType": "complete",
            "taskId": task_id,
            "status": "completed",
            "createdAt": "2026-09-20T00:00:00Z",
            "lastUpdatedAt": "2026-09-20T00:00:03Z",
            "ttlMs": 60_000,
            "result": {
                "content": [{"type": "text", "text": "approved earlier"}],
                "isError": False,
            },
        }

    client.request = request  # type: ignore[method-assign]
    client._start_modern_task_subscription = Mock()  # type: ignore[method-assign]
    client._stop_modern_task_subscription = AsyncMock()  # type: ignore[method-assign]
    persisted = {
        "taskId": task_id,
        "status": "working",
        "createdAt": "2026-09-20T00:00:00Z",
        "lastUpdatedAt": "2026-09-20T00:00:01Z",
        "ttlMs": 60_000,
        "pollIntervalMs": 0,
    }

    result = await client.resume_modern_task(
        persisted,
        {"approve": fingerprint},
    )

    assert result["content"][0]["text"] == "approved earlier"
    assert methods == ["tasks/get", "tasks/get"]
    assert "tasks/update" not in methods


@pytest.mark.asyncio
async def test_modern_task_result_requires_server_extension() -> None:
    client = MCPClient(MCPServerConfig(name="modern", command="fake", args=[], env={}))
    client.protocol_version = "2026-07-28"
    client.server_capabilities = {"tools": {}}

    with pytest.raises(MCPProtocolError, match="without advertising"):
        await client._resolve_modern_result(
            "tools/call",
            {"name": "long", "arguments": {}},
            {
                "resultType": "task",
                "taskId": "task-1",
                "status": "working",
                "createdAt": "2026-09-20T00:00:00Z",
                "lastUpdatedAt": "2026-09-20T00:00:01Z",
                "ttlMs": None,
            },
            input_required_round=0,
            expected_tool_contract=None,
            header_annotations=[],
        )


@pytest.mark.asyncio
async def test_modern_task_cancel_returns_acknowledgement_and_routes_task_id() -> None:
    seen: list[tuple[str, httpx.Headers]] = []
    task_id = "cancel-me"

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        method = payload["method"]
        seen.append((method, request.headers))
        if method == "server/discover":
            result = {
                "resultType": "complete",
                "supportedVersions": ["2026-07-28"],
                "capabilities": {
                    "extensions": {"io.modelcontextprotocol/tasks": {}}
                },
            }
        elif method == "tasks/cancel":
            assert payload["params"]["taskId"] == task_id
            result = {"resultType": "complete"}
        else:
            raise AssertionError(method)
        return httpx.Response(
            200,
            json={"jsonrpc": "2.0", "id": payload["id"], "result": result},
            request=request,
        )

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = MCPClient(
        MCPServerConfig(
            name="modern-task",
            command="",
            args=[],
            env={},
            transport="http",
            url="https://mcp.example.test/rpc",
        ),
        http_client=http,
    )
    await client.connect()

    result = await client.cancel_mcp_task(task_id)

    assert result == {"taskId": task_id, "acknowledged": True}
    assert seen[-1][1]["Mcp-Method"] == "tasks/cancel"
    assert seen[-1][1]["Mcp-Name"] == task_id
    await client.disconnect()
    await http.aclose()


@pytest.mark.asyncio
async def test_modern_task_deduplicates_repeated_input_request_keys() -> None:
    client = MCPClient(
        MCPServerConfig(name="modern", command="fake", args=[], env={}),
        elicitation_handler=AsyncMock(
            return_value={"action": "accept", "content": {"ok": True}}
        ),
    )
    client.protocol_version = "2026-07-28"
    client.server_capabilities = {
        "extensions": {"io.modelcontextprotocol/tasks": {}}
    }
    input_request = {
        "confirm": {
            "method": "elicitation/create",
            "params": {
                "mode": "form",
                "message": "Continue?",
                "requestedSchema": {
                    "type": "object",
                    "properties": {"ok": {"type": "boolean"}},
                },
            },
        }
    }
    responses = iter(
        [
            {"resultType": "complete"},
            {
                "taskId": "dedupe",
                "status": "input_required",
                "createdAt": "2026-09-20T00:00:00Z",
                "lastUpdatedAt": "2026-09-20T00:00:01Z",
                "ttlMs": None,
                "pollIntervalMs": 0,
                "inputRequests": input_request,
            },
            {
                "taskId": "dedupe",
                "status": "completed",
                "createdAt": "2026-09-20T00:00:00Z",
                "lastUpdatedAt": "2026-09-20T00:00:02Z",
                "ttlMs": None,
                "result": {"content": [{"type": "text", "text": "done"}]},
            },
        ]
    )

    async def request(method: str, params: dict, **_: object) -> dict:
        if method == "tasks/update":
            return next(responses)
        if method == "tasks/get":
            return next(responses)
        raise AssertionError(method)

    client.request = AsyncMock(side_effect=request)  # type: ignore[method-assign]
    initial = {
        "resultType": "task",
        "taskId": "dedupe",
        "status": "input_required",
        "createdAt": "2026-09-20T00:00:00Z",
        "lastUpdatedAt": "2026-09-20T00:00:01Z",
        "ttlMs": None,
        "pollIntervalMs": 0,
        "inputRequests": input_request,
    }

    result = await client._await_modern_tool_task(initial, timeout=1)

    assert result["content"][0]["text"] == "done"
    assert client.elicitation_handler.await_count == 1
    assert [call.args[0] for call in client.request.await_args_list] == [
        "tasks/update",
        "tasks/get",
        "tasks/get",
    ]


@pytest.mark.asyncio
async def test_modern_task_failure_preserves_jsonrpc_error() -> None:
    client = MCPClient(MCPServerConfig(name="modern", command="fake", args=[], env={}))
    client.protocol_version = "2026-07-28"
    client.server_capabilities = {
        "extensions": {"io.modelcontextprotocol/tasks": {}}
    }
    failed = {
        "resultType": "task",
        "taskId": "failed-task",
        "status": "failed",
        "createdAt": "2026-09-20T00:00:00Z",
        "lastUpdatedAt": "2026-09-20T00:00:01Z",
        "ttlMs": None,
        "error": {
            "code": -32044,
            "message": "remote execution failed",
            "data": {"kind": "upstream"},
        },
    }

    with pytest.raises(MCPProtocolError, match="remote execution failed") as caught:
        await client._await_modern_tool_task(failed, timeout=1)

    assert caught.value.code == -32044
    assert caught.value.has_data is True
    assert caught.value.data == {"kind": "upstream"}


@pytest.mark.asyncio
async def test_modern_task_timeout_cancels_without_post_deadline_poll(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = MCPClient(MCPServerConfig(name="modern", command="fake", args=[], env={}))
    client.protocol_version = "2026-07-28"
    client.server_capabilities = {
        "extensions": {"io.modelcontextprotocol/tasks": {}}
    }
    monkeypatch.setattr(client, "_modern_task_poll_delay", lambda task: 1.0)
    request = AsyncMock(return_value={})
    client.request = request  # type: ignore[method-assign]
    initial = {
        "resultType": "task",
        "taskId": "slow-task",
        "status": "working",
        "createdAt": "2026-09-20T00:00:00Z",
        "lastUpdatedAt": "2026-09-20T00:00:01Z",
        "ttlMs": None,
        "pollIntervalMs": 1000,
    }

    with pytest.raises(MCPTaskTimeout):
        await client._await_modern_tool_task(initial, timeout=0.01)

    assert [call.args[0] for call in request.await_args_list] == ["tasks/cancel"]
    assert request.await_args.args[1] == {"taskId": "slow-task"}


@pytest.mark.asyncio
async def test_modern_tool_call_never_sends_removed_task_opt_in() -> None:
    client = MCPClient(MCPServerConfig(name="modern", command="fake", args=[], env={}))
    client.protocol_version = "2026-07-28"
    client.server_capabilities = {
        "tools": {},
        "extensions": {"io.modelcontextprotocol/tasks": {}},
    }
    request = AsyncMock(
        return_value={"content": [{"type": "text", "text": "sync"}]}
    )
    client.request = request  # type: ignore[method-assign]

    result = await client.call_tool("long", {}, as_task=True)

    assert result["content"][0]["text"] == "sync"
    assert request.await_args.args[:2] == ("tools/call", {"name": "long", "arguments": {}})


@pytest.mark.asyncio
async def test_modern_mrtr_can_transition_to_task_over_stdio() -> None:
    server = r"""
import json, sys
for line in sys.stdin:
    message = json.loads(line)
    method = message.get("method")
    params = message.get("params", {})
    if method == "server/discover":
        result = {
            "resultType": "complete",
            "supportedVersions": ["2026-07-28"],
            "capabilities": {
                "tools": {},
                "extensions": {"io.modelcontextprotocol/tasks": {}},
            },
        }
    elif method == "tools/call":
        if "inputResponses" not in params:
            result = {
                "resultType": "input_required",
                "inputRequests": {
                    "confirm": {
                        "method": "elicitation/create",
                        "params": {
                            "mode": "form",
                            "message": "Continue into task?",
                            "requestedSchema": {
                                "type": "object",
                                "properties": {"ok": {"type": "boolean"}},
                                "required": ["ok"],
                            },
                        },
                    }
                },
                "requestState": "before-task",
            }
        else:
            assert params["requestState"] == "before-task"
            result = {
                "resultType": "task",
                "taskId": "stdio-task",
                "status": "working",
                "createdAt": "2026-09-20T00:00:00Z",
                "lastUpdatedAt": "2026-09-20T00:00:01Z",
                "ttlMs": 60000,
                "pollIntervalMs": 0,
            }
    elif method == "tasks/get":
        assert params["taskId"] == "stdio-task"
        result = {
            "resultType": "complete",
            "taskId": "stdio-task",
            "status": "completed",
            "createdAt": "2026-09-20T00:00:00Z",
            "lastUpdatedAt": "2026-09-20T00:00:02Z",
            "ttlMs": 60000,
            "result": {
                "content": [{"type": "text", "text": "task complete"}],
                "isError": False,
            },
        }
    else:
        result = {"resultType": "complete"}
    print(json.dumps({"jsonrpc": "2.0", "id": message["id"], "result": result}), flush=True)
"""

    async def elicit(params: dict) -> dict:
        assert params["message"] == "Continue into task?"
        return {"action": "accept", "content": {"ok": True}}

    client = MCPClient(
        MCPServerConfig(
            name="modern-task", command=sys.executable, args=["-u", "-c", server], env={}
        ),
        elicitation_handler=elicit,
    )
    await client.connect()
    try:
        result = await client.call_tool("long", {})
        assert result["content"][0]["text"] == "task complete"
    finally:
        await client.disconnect()


@pytest.mark.asyncio
async def test_modern_task_notification_completes_without_polling_over_stdio() -> None:
    server = r"""
import json, sys
listen_id = None
for line in sys.stdin:
    message = json.loads(line)
    method = message.get("method")
    params = message.get("params", {})
    if method == "server/discover":
        result = {
            "resultType": "complete",
            "supportedVersions": ["2026-07-28"],
            "capabilities": {
                "tools": {},
                "extensions": {"io.modelcontextprotocol/tasks": {}},
            },
        }
        print(json.dumps({"jsonrpc": "2.0", "id": message["id"], "result": result}), flush=True)
    elif method == "tools/call":
        result = {
            "resultType": "task",
            "taskId": "notify-task",
            "status": "working",
            "createdAt": "2026-09-20T00:00:00Z",
            "lastUpdatedAt": "2026-09-20T00:00:01Z",
            "ttlMs": 60000,
            "pollIntervalMs": 100000,
        }
        print(json.dumps({"jsonrpc": "2.0", "id": message["id"], "result": result}), flush=True)
    elif method == "subscriptions/listen":
        listen_id = message["id"]
        assert params["notifications"] == {"taskIds": ["notify-task"]}
        meta = {"io.modelcontextprotocol/subscriptionId": listen_id}
        print(json.dumps({
            "jsonrpc": "2.0",
            "method": "notifications/subscriptions/acknowledged",
            "params": {"notifications": {"taskIds": ["notify-task"]}, "_meta": meta},
        }), flush=True)
        print(json.dumps({
            "jsonrpc": "2.0",
            "method": "notifications/tasks",
            "params": {
                "taskId": "notify-task",
                "status": "completed",
                "createdAt": "2026-09-20T00:00:00Z",
                "lastUpdatedAt": "2026-09-20T00:00:02Z",
                "ttlMs": 60000,
                "pollIntervalMs": 100000,
                "result": {
                    "content": [{"type": "text", "text": "notified"}],
                    "isError": False,
                },
                "_meta": meta,
            },
        }), flush=True)
    elif method == "tasks/get":
        result = {
            "resultType": "complete",
            "taskId": "notify-task",
            "status": "completed",
            "createdAt": "2026-09-20T00:00:00Z",
            "lastUpdatedAt": "2026-09-20T00:00:03Z",
            "ttlMs": 60000,
            "result": {"content": [{"type": "text", "text": "polled"}]},
        }
        print(json.dumps({"jsonrpc": "2.0", "id": message["id"], "result": result}), flush=True)
    elif method == "notifications/cancelled":
        assert message["params"]["requestId"] == listen_id
        print(json.dumps({
            "jsonrpc": "2.0",
            "id": listen_id,
            "result": {
                "resultType": "complete",
                "_meta": {"io.modelcontextprotocol/subscriptionId": listen_id},
            },
        }), flush=True)
"""
    client = MCPClient(
        MCPServerConfig(
            name="modern-task-notify",
            command=sys.executable,
            args=["-u", "-c", server],
            env={},
        )
    )
    await client.connect()
    try:
        result = await asyncio.wait_for(client.call_tool("long", {}), timeout=1)
        assert result["content"][0]["text"] == "notified"
        assert client._task_subscription_tasks == {}
        assert client._task_subscription_request_ids == {}
        assert client._modern_task_updates == {}
    finally:
        await client.disconnect()


@pytest.mark.asyncio
async def test_modern_task_subscription_decline_falls_back_to_polling() -> None:
    server = r"""
import json, sys
for line in sys.stdin:
    message = json.loads(line)
    method = message.get("method")
    params = message.get("params", {})
    if method == "server/discover":
        result = {
            "resultType": "complete",
            "supportedVersions": ["2026-07-28"],
            "capabilities": {
                "tools": {},
                "extensions": {"io.modelcontextprotocol/tasks": {}},
            },
        }
    elif method == "tools/call":
        result = {
            "resultType": "task",
            "taskId": "poll-task",
            "status": "working",
            "createdAt": "2026-09-20T00:00:00Z",
            "lastUpdatedAt": "2026-09-20T00:00:01Z",
            "ttlMs": 60000,
            "pollIntervalMs": 10,
        }
    elif method == "subscriptions/listen":
        listen_id = message["id"]
        assert params["notifications"] == {"taskIds": ["poll-task"]}
        meta = {"io.modelcontextprotocol/subscriptionId": listen_id}
        print(json.dumps({
            "jsonrpc": "2.0",
            "method": "notifications/subscriptions/acknowledged",
            "params": {"notifications": {}, "_meta": meta},
        }), flush=True)
        result = {"resultType": "complete", "_meta": meta}
    elif method == "tasks/get":
        result = {
            "resultType": "complete",
            "taskId": "poll-task",
            "status": "completed",
            "createdAt": "2026-09-20T00:00:00Z",
            "lastUpdatedAt": "2026-09-20T00:00:02Z",
            "ttlMs": 60000,
            "result": {"content": [{"type": "text", "text": "polled"}]},
        }
    else:
        result = {"resultType": "complete"}
    print(json.dumps({"jsonrpc": "2.0", "id": message["id"], "result": result}), flush=True)
"""
    client = MCPClient(
        MCPServerConfig(
            name="modern-task-poll",
            command=sys.executable,
            args=["-u", "-c", server],
            env={},
        )
    )
    await client.connect()
    try:
        result = await asyncio.wait_for(client.call_tool("long", {}), timeout=1)
        assert result["content"][0]["text"] == "polled"
        assert client._task_subscription_tasks == {}
        assert client._task_subscription_request_ids == {}
        assert client._modern_task_updates == {}
    finally:
        await client.disconnect()


@pytest.mark.asyncio
async def test_modern_input_required_is_auto_fulfilled_and_retried() -> None:
    server = r"""
import json, sys
for line in sys.stdin:
    message = json.loads(line)
    method = message.get("method")
    params = message.get("params", {})
    if method == "server/discover":
        result = {
            "resultType": "complete",
            "supportedVersions": ["2026-07-28"],
            "capabilities": {"tools": {}},
        }
    elif method == "tools/call":
        if "inputResponses" not in params:
            result = {
                "resultType": "input_required",
                "inputRequests": {
                    "confirm": {
                        "method": "elicitation/create",
                        "params": {
                            "mode": "form",
                            "message": "Continue?",
                            "requestedSchema": {
                                "type": "object",
                                "properties": {"ok": {"type": "boolean"}},
                                "required": ["ok"],
                            },
                        },
                    }
                },
                "requestState": "opaque-state",
            }
        else:
            assert params["requestState"] == "opaque-state"
            assert params["inputResponses"] == {
                "confirm": {"action": "accept", "content": {"ok": True}}
            }
            result = {
                "resultType": "complete",
                "content": [{"type": "text", "text": "confirmed"}],
            }
    else:
        result = {"resultType": "complete"}
    print(json.dumps({"jsonrpc": "2.0", "id": message["id"], "result": result}), flush=True)
"""

    async def elicit(params: dict) -> dict:
        assert params["message"] == "Continue?"
        return {"action": "accept", "content": {"ok": True}}

    client = MCPClient(
        MCPServerConfig(
            name="modern", command=sys.executable, args=["-u", "-c", server], env={}
        ),
        elicitation_handler=elicit,
    )
    await client.connect()
    result = await client.call_tool("confirm", {})

    assert result == {"content": [{"type": "text", "text": "confirmed"}]}
    await client.disconnect()


@pytest.mark.asyncio
async def test_modern_result_requires_result_type() -> None:
    server = r"""
import json, sys
for line in sys.stdin:
    message = json.loads(line)
    method = message.get("method")
    if method == "server/discover":
        result = {"resultType": "complete", "supportedVersions": ["2026-07-28"], "capabilities": {"tools": {}}}
    else:
        result = {"content": [{"type": "text", "text": "missing discriminator"}]}
    print(json.dumps({"jsonrpc": "2.0", "id": message["id"], "result": result}), flush=True)
"""
    client = MCPClient(MCPServerConfig(name="modern", command=sys.executable, args=["-u", "-c", server], env={}))
    await client.connect()
    with pytest.raises(MCPProtocolError, match="missing resultType"):
        await client.call_tool("broken", {})
    await client.disconnect()


@pytest.mark.asyncio
async def test_modern_input_required_round_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(mcp_client_module, "INPUT_REQUIRED_RETRY_DELAY_SECONDS", 0)
    server = r"""
import json, sys
for line in sys.stdin:
    message = json.loads(line)
    if message.get("method") == "server/discover":
        result = {"resultType": "complete", "supportedVersions": ["2026-07-28"], "capabilities": {"tools": {}}}
    else:
        result = {"resultType": "input_required", "requestState": "retry"}
    print(json.dumps({"jsonrpc": "2.0", "id": message["id"], "result": result}), flush=True)
"""
    client = MCPClient(MCPServerConfig(name="modern", command=sys.executable, args=["-u", "-c", server], env={}))
    await client.connect()
    with pytest.raises(MCPProtocolError, match="exceeded 10 input_required rounds"):
        await client.call_tool("loop", {})
    await client.disconnect()


@pytest.mark.asyncio
async def test_modern_input_required_rejects_undeclared_client_capability() -> None:
    server = r"""
import json, sys
for line in sys.stdin:
    message = json.loads(line)
    if message.get("method") == "server/discover":
        result = {"resultType": "complete", "supportedVersions": ["2026-07-28"], "capabilities": {"tools": {}}}
    else:
        result = {"resultType": "input_required", "inputRequests": {"roots": {"method": "roots/list", "params": {}}}}
    print(json.dumps({"jsonrpc": "2.0", "id": message["id"], "result": result}), flush=True)
"""
    client = MCPClient(MCPServerConfig(name="modern", command=sys.executable, args=["-u", "-c", server], env={}))
    await client.connect()
    with pytest.raises(MCPProtocolError, match="undeclared client capability"):
        await client.call_tool("needs-roots", {})
    await client.disconnect()


@pytest.mark.asyncio
async def test_modern_http_400_jsonrpc_error_is_delivered_in_band() -> None:
    methods: list[str] = []
    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        methods.append(payload["method"])
        if payload["method"] == "server/discover":
            result = {"resultType": "complete", "supportedVersions": ["2026-07-28"], "capabilities": {"tools": {}}}
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": payload["id"], "result": result}, request=request)
        return httpx.Response(400, headers={"content-type": "application/json"}, json={"jsonrpc": "2.0", "id": payload["id"], "error": {"code": -32099, "message": "denied"}}, request=request)
    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = MCPClient(MCPServerConfig(name="modern", command="", args=[], env={}, transport="http", url="https://mcp.example.test/rpc"), http_client=http)
    await client.connect()
    with pytest.raises(MCPProtocolError, match=r"tools/call failed \(-32099\): denied") as exc_info:
        await client.call_tool("blocked", {})
    assert exc_info.value.code == -32099
    assert methods == ["server/discover", "tools/call"]
    await client.disconnect()
    await http.aclose()


@pytest.mark.asyncio
async def test_modern_http_timeout_does_not_post_cancel_notification() -> None:
    methods: list[str] = []
    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        methods.append(payload["method"])
        if payload["method"] == "server/discover":
            result = {"resultType": "complete", "supportedVersions": ["2026-07-28"], "capabilities": {"tools": {}}}
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": payload["id"], "result": result}, request=request)
        if payload["method"] == "tools/call":
            raise httpx.ReadTimeout("slow", request=request)
        raise AssertionError(payload["method"])
    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = MCPClient(MCPServerConfig(name="modern", command="", args=[], env={}, transport="http", url="https://mcp.example.test/rpc"), http_client=http)
    await client.connect()
    with pytest.raises(httpx.ReadTimeout):
        await client.call_tool("slow", {})
    assert methods == ["server/discover", "tools/call"]
    await client.disconnect()
    await http.aclose()


@pytest.mark.asyncio
async def test_stdio_discover_rejects_unsupported_future_version() -> None:
    probe_response = {
        "resultType": "complete",
        "supportedVersions": ["2026-08-01"],
        "capabilities": {},
    }
    server = f"""
import json, sys
for line in sys.stdin:
    message = json.loads(line)
    if message.get("method") == "server/discover":
        print(json.dumps({{"jsonrpc": "2.0", "id": message["id"], "result": {json.dumps(probe_response)}}}), flush=True)
"""
    client = MCPClient(
        MCPServerConfig(
            name="future", command=sys.executable, args=["-u", "-c", server], env={}
        )
    )
    with pytest.raises(
        MCPProtocolError, match="no mutually supported protocol version"
    ):
        await asyncio.wait_for(client.connect(), timeout=1)


@pytest.mark.asyncio
async def test_stdio_unsupported_modern_version_fails_deterministically() -> None:
    server = r"""
import json, sys
for line in sys.stdin:
    message = json.loads(line)
    if message.get("method") == "server/discover":
        error = {
            "code": -32022,
            "message": "unsupported version",
            "data": {"supported": ["2026-07-28"]},
        }
        print(json.dumps({"jsonrpc": "2.0", "id": message["id"], "error": error}), flush=True)
"""
    client = MCPClient(
        MCPServerConfig(
            name="modern", command=sys.executable, args=["-u", "-c", server], env={}
        )
    )
    with pytest.raises(
        MCPProtocolError, match="no mutually supported protocol version"
    ):
        await asyncio.wait_for(client.connect(), timeout=1)


@pytest.mark.asyncio
async def test_http_modern_probe_recognizes_supported_headers_and_oauth() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.method != "POST":
            return httpx.Response(204)
        payload = json.loads(request.content)
        if payload["method"] == "initialize":
            return httpx.Response(
                200,
                json={
                    "jsonrpc": "2.0",
                    "id": payload["id"],
                    "result": {"protocolVersion": "2025-11-25", "capabilities": {}},
                },
            )
        if payload["method"] == "notifications/initialized":
            return httpx.Response(202)
        assert request.headers["Authorization"] == "Bearer probe-token"
        assert request.headers["x-required"] == "probe"
        meta = payload["params"]["_meta"]
        assert meta["io.modelcontextprotocol/protocolVersion"] == "2026-07-28"
        return httpx.Response(
            200,
            json={"jsonrpc": "2.0", "id": payload["id"], "result": {}},
        )

    oauth = SimpleNamespace(
        http_client=None,
        authorization_header=AsyncMock(return_value="Bearer probe-token"),
    )
    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = MCPClient(
        MCPServerConfig(
            name="remote",
            command="",
            args=[],
            env={},
            transport="http",
            url="https://mcp.example.test/rpc",
            headers={"x-required": "probe"},
        ),
        http_client=http,
        oauth_session=cast(MCPOAuthSession, oauth),
    )
    await client.connect()

    assert len([request for request in requests if request.method == "POST"]) == 3
    await client.disconnect()
    await http.aclose()


@pytest.mark.asyncio
async def test_http_unrecognized_400_falls_back_to_legacy_initialize() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.method != "POST":
            return httpx.Response(204)
        if request.content.startswith(b'{"jsonrpc":"2.0","id":0'):
            return httpx.Response(400, text="legacy gateway rejection")
        payload = json.loads(request.content)
        if payload["method"] == "initialize":
            return httpx.Response(
                200,
                headers={"Mcp-Session-Id": "session-1"},
                json={
                    "jsonrpc": "2.0",
                    "id": payload["id"],
                    "result": {
                        "protocolVersion": "2025-06-18",
                        "capabilities": {},
                    },
                },
            )
        if payload["method"] == "notifications/initialized":
            return httpx.Response(202)
        return httpx.Response(
            200,
            json={"jsonrpc": "2.0", "id": payload["id"], "result": {"tools": []}},
        )

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = MCPClient(
        MCPServerConfig(
            name="remote",
            command="",
            args=[],
            env={},
            transport="http",
            url="https://mcp.example.test/rpc",
        ),
        http_client=http,
    )
    await client.connect()
    protocol_version = client.protocol_version
    await client.disconnect()

    assert [
        json.loads(request.content)["method"]
        for request in requests
        if request.method == "POST"
    ] == [
        "server/discover",
        "initialize",
        "notifications/initialized",
    ]
    assert protocol_version == "2025-06-18"
    await http.aclose()


@pytest.mark.asyncio
async def test_http_modern_unsupported_version_fails_without_fallback() -> None:
    methods = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        methods.append(payload["method"])
        return httpx.Response(
            400,
            json={
                "jsonrpc": "2.0",
                "id": payload["id"],
                "error": {
                    "code": -32022,
                    "message": "unsupported version",
                    "data": {"requested": "2026-07-28", "supported": ["2026-07-28"]},
                },
            },
        )

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = MCPClient(
        MCPServerConfig(
            name="remote",
            command="",
            args=[],
            env={},
            transport="http",
            url="https://mcp.example.test/rpc",
        ),
        http_client=http,
    )
    with pytest.raises(
        MCPProtocolError, match="no mutually supported protocol version"
    ):
        await asyncio.wait_for(client.connect(), timeout=1)

    assert methods == ["server/discover"]
    await http.aclose()


@pytest.mark.parametrize("code", [-32020, -32021])
@pytest.mark.asyncio
async def test_http_recognized_modern_error_fails_without_fallback(
    code: int,
) -> None:
    methods = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        methods.append(payload["method"])
        return httpx.Response(
            400,
            json={
                "jsonrpc": "2.0",
                "id": payload["id"],
                "error": {"code": code, "message": "modern rejection"},
            },
        )

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = MCPClient(
        MCPServerConfig(
            name="remote",
            command="",
            args=[],
            env={},
            transport="http",
            url="https://mcp.example.test/rpc",
        ),
        http_client=http,
    )
    with pytest.raises(MCPProtocolError, match="modern discovery request"):
        await asyncio.wait_for(client.connect(), timeout=1)

    assert methods == ["server/discover"]
    await http.aclose()


@pytest.mark.asyncio
async def test_http_probe_network_failure_falls_back_to_initialize() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if request.method != "POST":
            return httpx.Response(204)
        if request.content.startswith(b'{"jsonrpc":"2.0","id":0'):
            raise httpx.ConnectError("network unavailable", request=request)
        payload = json.loads(request.content)
        if "id" not in payload:
            return httpx.Response(202)
        return httpx.Response(
            200,
            json={
                "jsonrpc": "2.0",
                "id": payload["id"],
                "result": {"protocolVersion": "2025-06-18", "capabilities": {}},
            },
        )

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = MCPClient(
        MCPServerConfig(
            name="remote",
            command="",
            args=[],
            env={},
            transport="http",
            url="https://mcp.example.test/rpc",
        ),
        http_client=http,
    )
    await client.connect()
    protocol_version = client.protocol_version
    await client.disconnect()

    assert calls >= 2
    assert protocol_version == "2025-06-18"
    await http.aclose()


@pytest.mark.asyncio
async def test_mcp_runtime_wires_opt_in_client_interaction_capabilities(
    tmp_path: Path,
) -> None:
    sampling = AsyncMock(return_value={"role": "assistant"})
    elicitation = AsyncMock(return_value={"action": "decline"})
    runtime = MCPRuntime(
        {},
        SafetyGuard(tmp_path),
        sampling_handler=sampling,
        elicitation_handler=elicitation,
    )
    config = MCPServerConfig(name="fake", command="fake", args=[], env={})

    client = runtime._configure_client("fake", config)

    assert client.client_capabilities["sampling"] == {}
    assert client.client_capabilities["elicitation"] == {"form": {}}
    await client.sampling_handler({"maxTokens": 1})
    await client.elicitation_handler({"message": "hello"})
    sampling.assert_awaited_once_with("fake", {"maxTokens": 1})
    elicitation.assert_awaited_once_with("fake", {"message": "hello"})


@pytest.mark.asyncio
async def test_modern_stdio_subscription_delivers_list_change_and_cancels() -> None:
    server = r"""
import json, sys
listen_id = None
for line in sys.stdin:
    message = json.loads(line)
    method = message.get("method")
    if method == "server/discover":
        result = {
            "resultType": "complete",
            "supportedVersions": ["2026-07-28"],
            "capabilities": {"tools": {"listChanged": True}},
        }
        print(json.dumps({"jsonrpc": "2.0", "id": message["id"], "result": result}), flush=True)
    elif method == "subscriptions/listen":
        listen_id = message["id"]
        assert message["params"]["notifications"] == {"toolsListChanged": True}
        meta = {"io.modelcontextprotocol/subscriptionId": listen_id}
        print(json.dumps({
            "jsonrpc": "2.0",
            "method": "notifications/subscriptions/acknowledged",
            "params": {"notifications": {"toolsListChanged": True}, "_meta": meta},
        }), flush=True)
        print(json.dumps({
            "jsonrpc": "2.0",
            "method": "notifications/tools/list_changed",
            "params": {"_meta": meta},
        }), flush=True)
    elif method == "notifications/cancelled":
        assert message["params"]["requestId"] == listen_id
        print(json.dumps({
            "jsonrpc": "2.0",
            "id": listen_id,
            "result": {
                "resultType": "complete",
                "_meta": {"io.modelcontextprotocol/subscriptionId": listen_id},
            },
        }), flush=True)
"""
    changed = asyncio.Event()
    seen: list[str] = []

    async def on_notification(method: str, params: dict) -> None:
        seen.append(method)
        if method == "notifications/tools/list_changed":
            changed.set()

    client = MCPClient(
        MCPServerConfig(
            name="modern", command=sys.executable, args=["-u", "-c", server], env={}
        ),
        notification_handler=on_notification,
    )
    await client.connect()
    await asyncio.wait_for(changed.wait(), timeout=0.5)
    assert seen == ["notifications/tools/list_changed"]
    await client.disconnect()


@pytest.mark.asyncio
async def test_modern_http_subscription_uses_listen_stream_without_cancel_post() -> None:
    changed = asyncio.Event()
    stream_closed = asyncio.Event()
    methods: list[str] = []

    class SubscriptionStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            listen_id = 1
            meta = {"io.modelcontextprotocol/subscriptionId": listen_id}
            ack = {
                "jsonrpc": "2.0",
                "method": "notifications/subscriptions/acknowledged",
                "params": {
                    "notifications": {"toolsListChanged": True},
                    "_meta": meta,
                },
            }
            change = {
                "jsonrpc": "2.0",
                "method": "notifications/tools/list_changed",
                "params": {"_meta": meta},
            }
            yield f"data: {json.dumps(ack)}\n\n".encode()
            yield f"data: {json.dumps(change)}\n\n".encode()
            await asyncio.Event().wait()

        async def aclose(self) -> None:
            stream_closed.set()

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        methods.append(payload["method"])
        if payload["method"] == "server/discover":
            result = {
                "resultType": "complete",
                "supportedVersions": ["2026-07-28"],
                "capabilities": {"tools": {"listChanged": True}},
            }
            return httpx.Response(
                200,
                json={"jsonrpc": "2.0", "id": payload["id"], "result": result},
                request=request,
            )
        assert payload["method"] == "subscriptions/listen"
        assert request.headers["MCP-Protocol-Version"] == "2026-07-28"
        assert request.headers["Mcp-Method"] == "subscriptions/listen"
        assert payload["params"]["notifications"] == {"toolsListChanged": True}
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            stream=SubscriptionStream(),
            request=request,
        )

    async def on_notification(method: str, params: dict) -> None:
        if method == "notifications/tools/list_changed":
            changed.set()

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = MCPClient(
        MCPServerConfig(
            name="modern",
            command="",
            args=[],
            env={},
            transport="http",
            url="https://mcp.example.test/rpc",
        ),
        http_client=http,
        notification_handler=on_notification,
    )
    await client.connect()
    await asyncio.wait_for(changed.wait(), timeout=0.5)
    await client.disconnect()
    await asyncio.wait_for(stream_closed.wait(), timeout=0.5)
    await http.aclose()

    assert methods == ["server/discover", "subscriptions/listen"]


@pytest.mark.asyncio
async def test_modern_subscription_rejects_unrequested_ack_filter() -> None:
    server = r"""
import json, sys
listen_id = None
for line in sys.stdin:
    message = json.loads(line)
    method = message.get("method")
    if method == "server/discover":
        result = {
            "resultType": "complete",
            "supportedVersions": ["2026-07-28"],
            "capabilities": {"tools": {"listChanged": True}},
        }
        print(json.dumps({"jsonrpc": "2.0", "id": message["id"], "result": result}), flush=True)
    elif method == "subscriptions/listen":
        listen_id = message["id"]
        meta = {"io.modelcontextprotocol/subscriptionId": listen_id}
        print(json.dumps({
            "jsonrpc": "2.0",
            "method": "notifications/subscriptions/acknowledged",
            "params": {
                "notifications": {
                    "toolsListChanged": True,
                    "promptsListChanged": True,
                },
                "_meta": meta,
            },
        }), flush=True)
    elif method == "notifications/cancelled":
        print(json.dumps({
            "jsonrpc": "2.0",
            "id": listen_id,
            "result": {"resultType": "complete"},
        }), flush=True)
"""
    client = MCPClient(
        MCPServerConfig(
            name="modern", command=sys.executable, args=["-u", "-c", server], env={}
        ),
        notification_handler=lambda _method, _params: None,
    )
    with pytest.raises(MCPProtocolError, match="acknowledged unrequested filter"):
        await client.connect()


@pytest.mark.asyncio
async def test_modern_http_subscription_loss_is_reported_after_ack() -> None:
    release = asyncio.Event()
    lost = asyncio.Event()
    errors: list[str] = []

    class DroppingSubscriptionStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            meta = {"io.modelcontextprotocol/subscriptionId": 1}
            ack = {
                "jsonrpc": "2.0",
                "method": "notifications/subscriptions/acknowledged",
                "params": {
                    "notifications": {"toolsListChanged": True},
                    "_meta": meta,
                },
            }
            yield f"data: {json.dumps(ack)}\n\n".encode()
            await release.wait()

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        if payload["method"] == "server/discover":
            result = {
                "resultType": "complete",
                "supportedVersions": ["2026-07-28"],
                "capabilities": {"tools": {"listChanged": True}},
            }
            return httpx.Response(
                200,
                json={"jsonrpc": "2.0", "id": payload["id"], "result": result},
                request=request,
            )
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            stream=DroppingSubscriptionStream(),
            request=request,
        )

    async def on_failure(error: BaseException) -> None:
        errors.append(str(error))
        lost.set()

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = MCPClient(
        MCPServerConfig(
            name="modern",
            command="",
            args=[],
            env={},
            transport="http",
            url="https://mcp.example.test/rpc",
        ),
        http_client=http,
        notification_handler=lambda _method, _params: None,
        subscription_failure_handler=on_failure,
    )
    await client.connect()
    release.set()
    await asyncio.wait_for(lost.wait(), timeout=0.5)
    assert errors == ["MCP subscription HTTP stream ended without a graceful result"]
    await client.disconnect()
    await http.aclose()


@pytest.mark.asyncio
async def test_modern_resource_watch_uses_explicit_subscription_and_delivers_update() -> None:
    server = r"""
import json, sys
listen_id = None
for line in sys.stdin:
    message = json.loads(line)
    method = message.get("method")
    if method == "server/discover":
        result = {
            "resultType": "complete",
            "supportedVersions": ["2026-07-28"],
            "capabilities": {"resources": {"subscribe": True}},
        }
        print(json.dumps({"jsonrpc": "2.0", "id": message["id"], "result": result}), flush=True)
    elif method == "subscriptions/listen":
        listen_id = message["id"]
        assert message["params"]["notifications"] == {
            "resourceSubscriptions": ["file:///watched.txt"]
        }
        meta = {"io.modelcontextprotocol/subscriptionId": listen_id}
        print(json.dumps({
            "jsonrpc": "2.0",
            "method": "notifications/subscriptions/acknowledged",
            "params": {"notifications": {"resourceSubscriptions": ["file:///watched.txt"]}, "_meta": meta},
        }), flush=True)
        print(json.dumps({
            "jsonrpc": "2.0",
            "method": "notifications/resources/updated",
            "params": {"uri": "file:///watched.txt", "_meta": meta},
        }), flush=True)
    elif method == "notifications/cancelled":
        assert message["params"]["requestId"] == listen_id
        print(json.dumps({
            "jsonrpc": "2.0",
            "id": listen_id,
            "result": {"resultType": "complete"},
        }), flush=True)
"""
    updated = asyncio.Event()
    seen: list[str] = []

    async def on_notification(method: str, params: dict) -> None:
        if method == "notifications/resources/updated":
            seen.append(str(params.get("uri")))
            updated.set()

    client = MCPClient(
        MCPServerConfig(
            name="modern-resource-watch",
            command=sys.executable,
            args=["-u", "-c", server],
            env={},
        ),
        notification_handler=on_notification,
    )
    await client.connect()
    try:
        await client.watch_resource("file:///watched.txt")
        await asyncio.wait_for(updated.wait(), timeout=0.5)
        assert client.watched_resources == ("file:///watched.txt",)
        assert seen == ["file:///watched.txt"]
        await client.unwatch_resource("file:///watched.txt")
        assert client.watched_resources == ()
    finally:
        await client.disconnect()


@pytest.mark.asyncio
async def test_runtime_resource_watch_emits_update_event(tmp_path) -> None:
    server = r"""
import json, sys
for line in sys.stdin:
    message = json.loads(line)
    method = message.get("method")
    if method == "server/discover":
        result = {
            "resultType": "complete",
            "supportedVersions": ["2026-07-28"],
            "capabilities": {"resources": {"subscribe": True}},
        }
        print(json.dumps({"jsonrpc": "2.0", "id": message["id"], "result": result}), flush=True)
    elif method == "subscriptions/listen":
        listen_id = message["id"]
        uri = message["params"]["notifications"]["resourceSubscriptions"][0]
        meta = {"io.modelcontextprotocol/subscriptionId": listen_id}
        print(json.dumps({
            "jsonrpc": "2.0",
            "method": "notifications/subscriptions/acknowledged",
            "params": {"notifications": {"resourceSubscriptions": [uri]}, "_meta": meta},
        }), flush=True)
        print(json.dumps({
            "jsonrpc": "2.0",
            "method": "notifications/resources/updated",
            "params": {"uri": uri, "_meta": meta},
        }), flush=True)
    elif method == "notifications/cancelled":
        print(json.dumps({
            "jsonrpc": "2.0",
            "id": message["params"]["requestId"],
            "result": {"resultType": "complete"},
        }), flush=True)
"""
    events: list[dict] = []
    runtime = MCPRuntime(
        {
            "modern": MCPServerConfig(
                name="modern",
                command=sys.executable,
                args=["-u", "-c", server],
                env={},
            )
        },
        SafetyGuard(tmp_path),
        event_sink=events.append,
    )
    await runtime.start()
    try:
        await runtime.watch_resource("modern", "file:///watched.txt")
        for _ in range(50):
            if any(event.get("type") == "mcp.resource.updated" for event in events):
                break
            await asyncio.sleep(0.01)
        assert runtime.resource_watches() == [
            {"server": "modern", "uri": "file:///watched.txt"}
        ]
        assert any(
            event.get("type") == "mcp.resource.updated"
            and event.get("server") == "modern"
            and event.get("uri") == "file:///watched.txt"
            for event in events
        )
        await runtime.unwatch_resource("modern", "file:///watched.txt")
        assert runtime.resource_watches() == []
        assert any(event.get("type") == "mcp.resource.watch_stopped" for event in events)
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_legacy_resource_watch_uses_subscribe_and_unsubscribe() -> None:
    server = r"""
import json, sys
for line in sys.stdin:
    message = json.loads(line)
    method = message.get("method")
    if method == "server/discover":
        print(json.dumps({"jsonrpc": "2.0", "id": message["id"], "error": {"code": -32601, "message": "legacy"}}), flush=True)
    elif method == "initialize":
        result = {
            "protocolVersion": "2025-11-25",
            "capabilities": {"resources": {"subscribe": True}},
            "serverInfo": {"name": "legacy-watch", "version": "1"},
        }
        print(json.dumps({"jsonrpc": "2.0", "id": message["id"], "result": result}), flush=True)
    elif method == "resources/subscribe":
        uri = message["params"]["uri"]
        print(json.dumps({"jsonrpc": "2.0", "id": message["id"], "result": {}}), flush=True)
        print(json.dumps({
            "jsonrpc": "2.0",
            "method": "notifications/resources/updated",
            "params": {"uri": uri},
        }), flush=True)
    elif method == "resources/unsubscribe":
        print(json.dumps({"jsonrpc": "2.0", "id": message["id"], "result": {}}), flush=True)
"""
    updated = asyncio.Event()
    seen: list[str] = []

    async def on_notification(method: str, params: dict) -> None:
        if method == "notifications/resources/updated":
            seen.append(str(params.get("uri")))
            updated.set()

    client = MCPClient(
        MCPServerConfig(
            name="legacy-resource-watch",
            command=sys.executable,
            args=["-u", "-c", server],
            env={},
        ),
        notification_handler=on_notification,
    )
    await client.connect()
    try:
        await client.watch_resource("file:///legacy.txt")
        await asyncio.wait_for(updated.wait(), timeout=0.5)
        assert client.watched_resources == ("file:///legacy.txt",)
        assert seen == ["file:///legacy.txt"]
        await client.unwatch_resource("file:///legacy.txt")
        assert client.watched_resources == ()
    finally:
        await client.disconnect()


@pytest.mark.asyncio
async def test_modern_resource_watch_rejects_mismatched_acknowledgment() -> None:
    server = r"""
import json, sys
for line in sys.stdin:
    message = json.loads(line)
    method = message.get("method")
    if method == "server/discover":
        result = {
            "resultType": "complete",
            "supportedVersions": ["2026-07-28"],
            "capabilities": {"resources": {"subscribe": True}},
        }
        print(json.dumps({"jsonrpc": "2.0", "id": message["id"], "result": result}), flush=True)
    elif method == "subscriptions/listen":
        listen_id = message["id"]
        meta = {"io.modelcontextprotocol/subscriptionId": listen_id}
        print(json.dumps({
            "jsonrpc": "2.0",
            "method": "notifications/subscriptions/acknowledged",
            "params": {
                "notifications": {"resourceSubscriptions": ["file:///other.txt"]},
                "_meta": meta,
            },
        }), flush=True)
"""
    client = MCPClient(
        MCPServerConfig(
            name="modern-resource-watch-bad-ack",
            command=sys.executable,
            args=["-u", "-c", server],
            env={},
        ),
        notification_handler=lambda method, params: None,
    )
    await client.connect()
    try:
        with pytest.raises(MCPProtocolError, match="did not match the requested URI"):
            await client.watch_resource("file:///watched.txt")
        assert client.watched_resources == ()
    finally:
        await client.disconnect()


def test_resource_watch_limit_fails_closed() -> None:
    client = MCPClient(
        MCPServerConfig(name="limit", command=sys.executable, args=["-c", "pass"], env={})
    )
    client._initialized = True
    client.protocol_version = "2026-07-28"
    client.server_capabilities = {"resources": {"subscribe": True}}
    client._watched_resources = {f"file:///{index}.txt" for index in range(32)}

    with pytest.raises(MCPProtocolError, match="resource watch limit reached"):
        asyncio.run(client.watch_resource("file:///overflow.txt"))


@pytest.mark.asyncio
async def test_http_session_recovery_restores_legacy_resource_watch() -> None:
    trace: list[tuple[str, str | None]] = []
    initialize_count = 0
    uri = "file:///watched-after-recovery.txt"

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal initialize_count
        if request.method == "DELETE":
            return httpx.Response(405)
        if request.content.startswith(b'{"jsonrpc":"2.0","id":0'):
            return httpx.Response(
                200, json={"jsonrpc": "2.0", "id": 0, "result": {}}
            )
        payload = json.loads(request.content)
        method = payload["method"]
        session = request.headers.get("Mcp-Session-Id")
        trace.append((method, session))
        if method == "initialize":
            initialize_count += 1
            return httpx.Response(
                200,
                headers={"Mcp-Session-Id": f"session-{initialize_count}"},
                json={
                    "jsonrpc": "2.0",
                    "id": payload["id"],
                    "result": {
                        "protocolVersion": "2025-11-25",
                        "capabilities": {"resources": {"subscribe": True}},
                    },
                },
            )
        if method == "notifications/initialized":
            return httpx.Response(202)
        if method == "resources/subscribe":
            assert payload["params"]["uri"] == uri
            return httpx.Response(
                200,
                json={"jsonrpc": "2.0", "id": payload["id"], "result": {}},
            )
        if method == "resources/read" and session == "session-1":
            return httpx.Response(404)
        if method == "resources/read" and session == "session-2":
            return httpx.Response(
                200,
                json={
                    "jsonrpc": "2.0",
                    "id": payload["id"],
                    "result": {
                        "contents": [{"uri": uri, "text": "restored"}]
                    },
                },
            )
        raise AssertionError((method, session))

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = MCPClient(
        MCPServerConfig(
            name="recover-watch",
            command="",
            args=[],
            env={},
            transport="http",
            url="https://mcp.example.test/rpc",
        ),
        http_client=http,
    )
    await client.connect()
    try:
        await client.watch_resource(uri)
        result = await client.read_resource(uri)
        assert result["contents"][0]["text"] == "restored"
        assert client.watched_resources == (uri,)
        assert client._http_session_id == "session-2"
    finally:
        await client.disconnect()
        await http.aclose()

    assert trace == [
        ("initialize", None),
        ("notifications/initialized", "session-1"),
        ("resources/subscribe", "session-1"),
        ("resources/read", "session-1"),
        ("initialize", None),
        ("notifications/initialized", "session-2"),
        ("resources/subscribe", "session-2"),
        ("resources/read", "session-2"),
    ]


@pytest.mark.asyncio
async def test_http_session_recovery_drops_watch_if_replacement_loses_capability() -> None:
    initialize_count = 0
    uri = "file:///lost-capability.txt"

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal initialize_count
        if request.method == "DELETE":
            return httpx.Response(204)
        if request.content.startswith(b'{"jsonrpc":"2.0","id":0'):
            return httpx.Response(
                200, json={"jsonrpc": "2.0", "id": 0, "result": {}}
            )
        payload = json.loads(request.content)
        method = payload["method"]
        session = request.headers.get("Mcp-Session-Id")
        if method == "initialize":
            initialize_count += 1
            resources = {"subscribe": True} if initialize_count == 1 else {}
            return httpx.Response(
                200,
                headers={"Mcp-Session-Id": f"session-{initialize_count}"},
                json={
                    "jsonrpc": "2.0",
                    "id": payload["id"],
                    "result": {
                        "protocolVersion": "2025-11-25",
                        "capabilities": {"resources": resources},
                    },
                },
            )
        if method == "notifications/initialized":
            return httpx.Response(202)
        if method == "resources/subscribe":
            assert session == "session-1"
            return httpx.Response(
                200,
                json={"jsonrpc": "2.0", "id": payload["id"], "result": {}},
            )
        if method == "resources/read" and session == "session-1":
            return httpx.Response(404)
        raise AssertionError((method, session))

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = MCPClient(
        MCPServerConfig(
            name="recover-watch-lost",
            command="",
            args=[],
            env={},
            transport="http",
            url="https://mcp.example.test/rpc",
        ),
        http_client=http,
    )
    await client.connect()
    try:
        await client.watch_resource(uri)
        with pytest.raises(
            MCPProtocolError,
            match="replacement session no longer supports watched resources",
        ):
            await client.read_resource(uri)
        assert client.watched_resources == ()
        assert client._initialized is False
        assert client._http_session_id == ""
    finally:
        await client.disconnect()
        await http.aclose()


@pytest.mark.asyncio
async def test_deferred_runtime_buffers_resource_updates_until_publication(tmp_path) -> None:
    events: list[dict[str, object]] = []
    runtime = MCPRuntime(
        {"server": MCPServerConfig(name="server", command="fake", args=[], env={})},
        SafetyGuard(tmp_path),
        event_sink=events.append,
        defer_notifications=True,
    )
    uri = "file:///startup-update.txt"
    client = SimpleNamespace(
        server_capabilities={"resources": {"subscribe": True}},
        watched_resources=(uri,),
    )
    runtime.clients["server"] = client
    runtime._started = True

    await runtime._handle_notification(
        "server",
        client,
        "notifications/resources/updated",
        {"uri": uri},
    )
    await runtime._handle_notification(
        "server",
        client,
        "notifications/resources/updated",
        {"uri": uri},
    )

    assert events == []
    assert runtime._startup_resource_updates == {("server", uri)}

    runtime.activate_notifications()

    assert events == [
        {
            "type": "mcp.resource.updated",
            "server": "server",
            "uri": uri,
        }
    ]
    assert runtime._startup_resource_updates == set()


@pytest.mark.asyncio
async def test_runtime_watch_resource_emits_started_event_only_once(tmp_path) -> None:
    events: list[dict[str, object]] = []

    class FakeClient:
        def __init__(self) -> None:
            self._watched: set[str] = set()

        @property
        def watched_resources(self) -> tuple[str, ...]:
            return tuple(sorted(self._watched))

        async def watch_resource(self, uri: str) -> None:
            self._watched.add(uri)

    runtime = MCPRuntime(
        {"server": MCPServerConfig(name="server", command="fake", args=[], env={})},
        SafetyGuard(tmp_path),
        event_sink=events.append,
    )
    client = FakeClient()
    runtime.clients["server"] = client
    runtime._refresh_locks["server"] = asyncio.Lock()
    uri = "file:///idempotent.txt"

    await runtime.watch_resource("server", uri)
    await runtime.watch_resource("server", uri)

    assert events == [
        {
            "type": "mcp.resource.watch_started",
            "server": "server",
            "uri": uri,
        }
    ]
