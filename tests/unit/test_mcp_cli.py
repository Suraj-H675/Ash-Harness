from __future__ import annotations

import json
from pathlib import Path

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
