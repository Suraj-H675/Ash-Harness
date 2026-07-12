from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock

from ash.cli import main
from mcp.server import load_mcp_servers


def test_mcp_cli_add_persists_env_headers_and_json_hides_values(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    monkeypatch.chdir(tmp_path)

    status = main(
        [
            "mcp",
            "add",
            "local",
            "--env",
            "TOKEN=${MCP_TOKEN}",
            "--header",
            "Authorization=Bearer ${MCP_TOKEN}",
            "--json",
            "--",
            "python",
            "-m",
            "server",
        ]
    )

    assert status == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["servers"][0]["name"] == "local"
    assert payload["servers"][0]["env_keys"] == ["TOKEN"]
    assert payload["servers"][0]["header_keys"] == ["Authorization"]
    assert "${MCP_TOKEN}" not in json.dumps(payload)
    loaded = load_mcp_servers(tmp_path / ".mcp.json")
    assert loaded["local"].env == {"TOKEN": "${MCP_TOKEN}"}
    assert loaded["local"].headers == {"Authorization": "Bearer ${MCP_TOKEN}"}
    assert loaded["local"].command == "python"
    assert loaded["local"].args == ["-m", "server"]


def test_mcp_cli_list_json_reports_config_without_secret_values(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    monkeypatch.chdir(tmp_path)
    assert (
        main(
            [
                "mcp",
                "add",
                "remote",
                "--transport",
                "http",
                "--url",
                "https://example.test/mcp",
                "--header",
                "X-Api-Key=secret",
            ]
        )
        == 0
    )
    capsys.readouterr()

    assert main(["mcp", "list", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)

    assert payload["servers"][0]["transport"] == "http"
    assert payload["servers"][0]["url"] == "https://example.test/mcp"
    assert payload["servers"][0]["header_keys"] == ["X-Api-Key"]
    assert "secret" not in json.dumps(payload)


def test_mcp_cli_rejects_invalid_env_option(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    monkeypatch.chdir(tmp_path)

    status = main(["mcp", "add", "bad", "--env", "TOKEN", "--", "server"])

    assert status == 2
    assert "--env must use KEY=VALUE syntax" in capsys.readouterr().err


def test_mcp_cli_rejects_oauth_options_without_oauth_mode(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    monkeypatch.chdir(tmp_path)

    status = main(
        [
            "mcp",
            "add",
            "remote",
            "--transport",
            "http",
            "--url",
            "https://mcp.example.test/rpc",
            "--oauth-scope",
            "files:read",
        ]
    )

    assert status == 2
    assert "OAuth options require auth mode oauth" in capsys.readouterr().err
    assert not (tmp_path / ".mcp.json").exists()


def test_mcp_cli_adds_oauth_without_persisting_client_secret(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("MCP_CLIENT_SECRET", "resolved-secret")

    status = main(
        [
            "mcp",
            "add",
            "protected",
            "--transport",
            "http",
            "--url",
            "https://mcp.example.test/rpc",
            "--auth",
            "oauth",
            "--oauth-client-id",
            "registered-client",
            "--oauth-client-secret-env",
            "MCP_CLIENT_SECRET",
            "--oauth-scope",
            "files:read files:write",
            "--oauth-redirect-port",
            "43123",
            "--json",
        ]
    )

    assert status == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["servers"][0]["auth"] == "oauth"
    assert payload["servers"][0]["oauth_client_configured"] is True
    assert "resolved-secret" not in json.dumps(payload)
    persisted = (tmp_path / ".mcp.json").read_text(encoding="utf-8")
    assert "resolved-secret" not in persisted
    loaded = load_mcp_servers(tmp_path / ".mcp.json")["protected"]
    assert loaded.oauth == {
        "client_id": "registered-client",
        "client_secret": "${MCP_CLIENT_SECRET}",
        "redirect_port": 43123,
        "scope": "files:read files:write",
    }
    assert loaded.resolved_oauth["client_secret"] == "resolved-secret"


def test_mcp_cli_login_is_explicit_and_logout_removes_credentials(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    assert (
        main(
            [
                "mcp",
                "add",
                "protected",
                "--transport",
                "http",
                "--url",
                "https://mcp.example.test/rpc",
                "--auth",
                "oauth",
            ]
        )
        == 0
    )
    capsys.readouterr()

    authorize = AsyncMock(return_value=None)
    monkeypatch.setattr("mcp.oauth.authorize_mcp_server", authorize)

    assert (
        main(
            [
                "mcp",
                "login",
                "protected",
                "--no-browser",
                "--scope",
                "files:read files:write",
            ]
        )
        == 0
    )
    assert "Authorized MCP server protected." in capsys.readouterr().out
    assert authorize.await_count == 1
    assert authorize.await_args.kwargs["manual_paste"] is True
    assert authorize.await_args.kwargs["requested_scope"] == "files:read files:write"
    assert authorize.await_args.kwargs["open_browser"]("unused") is False

    store = authorize.await_args.kwargs["store"]
    store.directory.mkdir(parents=True, exist_ok=True)
    store.path.write_text("credential", encoding="utf-8")
    assert main(["mcp", "logout", "protected"]) == 0
    assert "Removed OAuth credentials" in capsys.readouterr().out
    assert not store.path.exists()
