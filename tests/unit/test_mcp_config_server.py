# tests/unit/test_mcp_config_server.py
import json
import os
from pathlib import Path

import pytest

from ash.mcp.client import (
    MCPClient,
    MCPProtocolError,
)
from ash.mcp.server import (
    MAX_MCP_ARGS,
    MAX_MCP_CONFIG_BYTES,
    MAX_MCP_SERVERS,
    MAX_MCP_VALUE_BYTES,
    MCPConfigSource,
    MCPServerConfig,
    expand_env_vars,
    load_mcp_server_sources,
    load_mcp_servers,
    save_mcp_servers,
)


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


def test_load_mcp_servers_bounds_server_count(tmp_path: Path) -> None:
    path = tmp_path / ".mcp.json"
    path.write_text(
        json.dumps(
            {
                f"server-{index}": {"command": "server"}
                for index in range(MAX_MCP_SERVERS + 1)
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match=r"MCP server count exceeds 32: 33"):
        load_mcp_servers(path)


def test_load_mcp_server_sources_bounds_merged_server_count(tmp_path: Path) -> None:
    first = tmp_path / "first.json"
    second = tmp_path / "second.json"
    first.write_text(
        json.dumps(
            {f"first-{index}": {"command": "server"} for index in range(20)}
        ),
        encoding="utf-8",
    )
    second.write_text(
        json.dumps(
            {f"second-{index}": {"command": "server"} for index in range(20)}
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match=r"MCP server count exceeds 32: 40"):
        load_mcp_server_sources(
            [
                MCPConfigSource(first, namespace="one"),
                MCPConfigSource(second, namespace="two"),
            ]
        )


def test_mcp_server_config_bounds_argument_count() -> None:
    with pytest.raises(ValueError, match=r"more than 256 arguments"):
        MCPServerConfig(
            name="bounded",
            command="server",
            args=["value"] * (MAX_MCP_ARGS + 1),
            env={},
        )


def test_mcp_server_config_bounds_expanded_environment_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ASH_TEST_MCP_OVERSIZED", "x" * (MAX_MCP_VALUE_BYTES + 1))

    with pytest.raises(ValueError, match=r"environment value exceeds 16384 bytes"):
        MCPServerConfig(
            name="bounded",
            command="server",
            args=[],
            env={"TOKEN": "${ASH_TEST_MCP_OVERSIZED}"},
        )


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
    real_open = mcp_module.AnchoredDirectory.open
    swapped = False

    def open_then_swap(directory_path, **kwargs):
        nonlocal swapped
        directory = real_open(directory_path, **kwargs)
        if Path(directory_path) == project and not swapped:
            swapped = True
            project.rename(tmp_path / "project-real")
            try:
                project.symlink_to(outside, target_is_directory=True)
            except OSError as exc:
                pytest.skip(f"symlink creation is unavailable: {exc}")
        return directory

    monkeypatch.setattr(
        mcp_module.AnchoredDirectory,
        "open",
        staticmethod(open_then_swap),
    )

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


def test_expand_env_vars(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TEST_VAR", "hello")
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
    real_open = mcp_module.AnchoredDirectory.open
    swapped = False

    def open_then_swap(directory_path, **kwargs):
        nonlocal swapped
        directory = real_open(directory_path, **kwargs)
        if Path(directory_path) == project and not swapped:
            swapped = True
            project.rename(tmp_path / "project-real")
            try:
                project.symlink_to(outside, target_is_directory=True)
            except OSError as exc:
                pytest.skip(f"symlink creation is unavailable: {exc}")
        return directory

    monkeypatch.setattr(
        mcp_module.AnchoredDirectory,
        "open",
        staticmethod(open_then_swap),
    )

    with pytest.raises(ValueError, match="symlink or junction"):
        save_mcp_servers({"local": config}, path)
    assert swapped is True
    assert victim.read_text(encoding="utf-8") == '{"marker":"do-not-touch"}\n'


def test_save_mcp_servers_rejects_plain_parent_directory_swap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import ash.mcp.server as mcp_module

    project = tmp_path / "project"
    saved = tmp_path / "project-original"
    replacement = tmp_path / "replacement"
    project.mkdir()
    replacement.mkdir()
    path = project / ".mcp.json"
    victim = replacement / ".mcp.json"
    victim.write_text('{"marker":"do-not-touch"}\n', encoding="utf-8")
    config = MCPServerConfig(
        name="local",
        command="echo",
        args=["ok"],
        env={"TOKEN": "SECRET"},
    )
    real_open = mcp_module.AnchoredDirectory.open
    swapped = False

    def open_then_swap(directory_path, **kwargs):
        nonlocal swapped
        directory = real_open(directory_path, **kwargs)
        if Path(directory_path) == project and not swapped:
            swapped = True
            project.rename(saved)
            replacement.rename(project)
        return directory

    monkeypatch.setattr(
        mcp_module.AnchoredDirectory,
        "open",
        staticmethod(open_then_swap),
    )

    with pytest.raises(ValueError):
        save_mcp_servers({"local": config}, path)

    assert swapped is True
    assert (project / ".mcp.json").read_text(encoding="utf-8") == (
        '{"marker":"do-not-touch"}\n'
    )
    assert not (saved / ".mcp.json").exists()


def test_load_mcp_servers_rejects_plain_parent_directory_swap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import ash.mcp.server as mcp_module

    project = tmp_path / "project"
    saved = tmp_path / "project-original"
    replacement = tmp_path / "replacement"
    project.mkdir()
    replacement.mkdir()
    path = project / ".mcp.json"
    path.write_text(
        '{"safe":{"command":"echo","args":["safe"]}}\n',
        encoding="utf-8",
    )
    (replacement / ".mcp.json").write_text(
        '{"evil":{"command":"echo","args":["attacker"]}}\n',
        encoding="utf-8",
    )
    real_open = mcp_module.AnchoredDirectory.open
    swapped = False

    def open_then_swap(directory_path, **kwargs):
        nonlocal swapped
        directory = real_open(directory_path, **kwargs)
        if Path(directory_path) == project and not swapped:
            swapped = True
            project.rename(saved)
            replacement.rename(project)
        return directory

    monkeypatch.setattr(
        mcp_module.AnchoredDirectory,
        "open",
        staticmethod(open_then_swap),
    )

    with pytest.raises(ValueError):
        load_mcp_servers(path)

    assert swapped is True


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


@pytest.mark.skipif(os.name == "nt", reason="POSIX executable fixture")
@pytest.mark.asyncio
async def test_client_rejects_workspace_shadowed_bare_stdio_command_without_cwd(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    marker = tmp_path / "workspace-mcp-client-ran"
    shadow = workspace / "mcp-shadow-client"
    shadow.write_text(
        f"#!/bin/sh\nprintf ran > {marker}\nsleep 30\n",
        encoding="utf-8",
    )
    shadow.chmod(0o755)
    monkeypatch.setenv("PATH", f"{workspace}{os.pathsep}{os.environ.get('PATH', '')}")
    monkeypatch.chdir(workspace)
    client = MCPClient(
        MCPServerConfig(
            name="shadowed-client",
            command="mcp-shadow-client",
            args=[],
            env={},
        )
    )

    with pytest.raises(MCPProtocolError, match="outside the workspace"):
        await client._connect_stdio()

    assert not marker.exists()


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
