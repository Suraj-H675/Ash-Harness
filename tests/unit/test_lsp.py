from __future__ import annotations

import asyncio
import json
import sys
from dataclasses import replace
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

import ash.cli as ash_cli
import ash.commands.lsp as cli_lsp
import ash.lsp.manager as manager_module
from ash.lsp.client import (
    MAX_LSP_DOCUMENT_BYTES,
    LSPClient,
    LSPError,
    _lsp_position,
    _read_document_text,
    _read_message,
)
from ash.lsp.config import LSPServerConfig, load_lsp_server_configs
from ash.lsp.manager import LanguageServerManager, _has_capability
from ash.lsp.middleware import LSPDiagnosticsMiddleware
from ash.safety.guard import SafetyGuard
from ash.safety.policy import PermissionMode, PermissionPolicy, PolicyAction
from ash.tools.base import ToolResult
from ash.tools.lsp import LSPTool
from ash.commands.lsp import inspect_lsp, render_lsp
from ash.config import AshConfig


FIXTURE_SERVER = Path(__file__).parents[1] / "fixtures" / "fake_lsp_server.py"


def fake_config(log_path: Path) -> LSPServerConfig:
    return LSPServerConfig(
        name="fake",
        command=(sys.executable, str(FIXTURE_SERVER)),
        extensions={".py": "python"},
        root_markers=("pyproject.toml",),
        env={"FAKE_LSP_LOG": str(log_path)},
        settings={"fake": {"enabled": True}},
        source="test",
    )


def test_custom_config_allows_no_root_markers(tmp_path: Path) -> None:
    config_dir = tmp_path / ".ash"
    config_dir.mkdir()
    config_dir.joinpath("lsp.json").write_text(
        json.dumps(
            {
                "servers": {
                    "custom": {
                        "command": [sys.executable, str(FIXTURE_SERVER)],
                        "extensions": {".xyz": "xyz"},
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    configs = load_lsp_server_configs(
        tmp_path, include_project=True, detect_builtins=False
    )

    assert configs["custom"].root_markers == ()
    assert configs["custom"].source.endswith(".ash/lsp.json")


def test_project_config_is_ignored_without_trust(tmp_path: Path) -> None:
    config_dir = tmp_path / ".ash"
    config_dir.mkdir()
    config_dir.joinpath("lsp.json").write_text(
        '{"servers":{"custom":{"command":["server"],"extensions":{".x":"x"}}}}',
        encoding="utf-8",
    )

    configs = load_lsp_server_configs(
        tmp_path, include_project=False, detect_builtins=False
    )

    assert configs == {}


def test_config_rejects_duplicate_keys_and_traversing_markers(
    tmp_path: Path,
) -> None:
    config_dir = tmp_path / ".ash"
    config_dir.mkdir()
    config_path = config_dir / "lsp.json"
    config_path.write_text(
        '{"servers":{},"servers":{}}',
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="duplicate JSON object key"):
        load_lsp_server_configs(tmp_path, include_project=True, detect_builtins=False)

    config_path.write_text(
        json.dumps(
            {
                "servers": {
                    "bad": {
                        "command": ["server"],
                        "extensions": {".PY": "python", ".py": "python"},
                        "root_markers": ["../outside"],
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="duplicate extension"):
        load_lsp_server_configs(tmp_path, include_project=True, detect_builtins=False)

    payload = json.loads(config_path.read_text(encoding="utf-8"))
    del payload["servers"]["bad"]["extensions"][".PY"]
    config_path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError, match="relative workspace paths"):
        load_lsp_server_configs(tmp_path, include_project=True, detect_builtins=False)


def test_workspace_server_detection_requires_trust_and_executable_bit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    executable = tmp_path / "node_modules" / ".bin" / "basedpyright-langserver"
    executable.parent.mkdir(parents=True)
    executable.write_text("#!/bin/sh\n", encoding="utf-8")
    executable.chmod(0o644)
    monkeypatch.setattr("ash.lsp.config.shutil.which", lambda command: None)

    assert load_lsp_server_configs(tmp_path, include_project=False) == {}
    assert load_lsp_server_configs(tmp_path, include_project=True) == {}

    executable.chmod(0o755)
    configs = load_lsp_server_configs(tmp_path, include_project=True)
    assert configs["basedpyright"].command[0] == str(executable.resolve())


def test_disabling_basedpyright_preserves_detected_pyright_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_dir = tmp_path / ".ash"
    config_dir.mkdir()
    config_path = config_dir / "lsp.json"
    config_path.write_text(
        '{"servers":{"basedpyright":{"disabled":true}}}', encoding="utf-8"
    )
    monkeypatch.setattr(
        "ash.lsp.config.shutil.which", lambda command: f"/installed/{command}"
    )

    configs = load_lsp_server_configs(tmp_path, include_project=True)

    assert "basedpyright" not in configs
    assert configs["pyright"].command[0].endswith("pyright-langserver")

    config_path.write_text(
        '{"servers":{"pyright":{"settings":{"python":{"analysis":{}}}}}}',
        encoding="utf-8",
    )
    configs = load_lsp_server_configs(tmp_path, include_project=True)
    assert "basedpyright" not in configs
    assert configs["pyright"].extensions[".py"] == "python"


def test_position_encoding_uses_negotiated_units() -> None:
    text = 'x = "\U0001f600"\n'
    assert _lsp_position(text, 1, 7, "utf-8") == {"line": 0, "character": 9}
    assert _lsp_position(text, 1, 7, "utf-16") == {"line": 0, "character": 7}
    assert _lsp_position(text, 1, 7, "utf-32") == {"line": 0, "character": 6}
    with pytest.raises(ValueError, match="outside line"):
        _lsp_position(text, 1, 99, "utf-16")


def test_positions_only_split_lsp_line_endings() -> None:
    assert _lsp_position("a\u2028b", 1, 4, "utf-32") == {
        "line": 0,
        "character": 3,
    }
    with pytest.raises(ValueError, match="outside the document"):
        _lsp_position("a\u2028b", 2, 1, "utf-32")


@pytest.mark.asyncio
async def test_manager_uses_real_lsp_subprocess(tmp_path: Path) -> None:
    source = tmp_path / "example.py"
    source.write_text('x = "\U0001f600"\nproblem\n', encoding="utf-8")
    log_path = tmp_path / "lsp.jsonl"
    manager = LanguageServerManager(tmp_path, {"fake": fake_config(log_path)})
    tool = LSPTool(SafetyGuard(tmp_path), manager)
    try:
        hover = await manager.query(
            "hover", file_path="example.py", line=1, character=7
        )
        assert hover[0]["contents"]["value"] == "character=9"

        definitions = await manager.query(
            "definition", file_path="example.py", line=1, character=1
        )
        assert definitions == [
            {
                "uri": "example.py",
                "range": {
                    "start": {"line": 0, "character": 0},
                    "end": {"line": 0, "character": 1},
                },
            }
        ]

        diagnostics = await manager.query("diagnostics", file_path="example.py")
        assert diagnostics[0]["message"] == "fake problem"
        assert diagnostics[0]["source"] == "fake-lsp"

        symbols = await manager.query("workspaceSymbol", query="needle")
        assert symbols == [{"name": "needle", "kind": 12}]

        result = await tool.run(operation="documentSymbol", file_path="example.py")
        assert result.success is True
        assert json.loads(result.output) == [{"name": "example", "kind": 12}]
        assert manager.status()[0].status == "running"
    finally:
        await tool.aclose()

    events = [json.loads(line) for line in log_path.read_text().splitlines()]
    methods = {event.get("method") for event in events}
    assert {
        "initialize",
        "initialized",
        "textDocument/didOpen",
        "shutdown",
        "exit",
    } <= methods


@pytest.mark.asyncio
async def test_unsupported_query_does_not_poison_server(tmp_path: Path) -> None:
    source = tmp_path / "example.py"
    source.write_text("value = 1\n", encoding="utf-8")
    manager = LanguageServerManager(
        tmp_path, {"fake": fake_config(tmp_path / "lsp.jsonl")}
    )
    try:
        with pytest.raises(LSPError, match="does not advertise"):
            await manager.query(
                "implementation", file_path="example.py", line=1, character=1
            )

        hover = await manager.query(
            "hover", file_path="example.py", line=1, character=1
        )

        assert hover[0]["contents"]["value"] == "character=0"
        assert manager.status()[0].status == "running"
    finally:
        await manager.aclose()


@pytest.mark.asyncio
async def test_incremental_sync_and_empty_diagnostics_clear_cache(
    tmp_path: Path,
) -> None:
    source = tmp_path / "example.py"
    source.write_text("problem\n", encoding="utf-8")
    log_path = tmp_path / "lsp.jsonl"
    config = fake_config(log_path)
    config = replace(config, env={**config.env, "FAKE_LSP_INCREMENTAL": "1"})
    manager = LanguageServerManager(tmp_path, {"fake": config})
    try:
        assert (await manager.query("diagnostics", file_path="example.py"))[0][
            "code"
        ] == "F001"

        source.write_text("fixed\n", encoding="utf-8")
        assert await manager.query("diagnostics", file_path="example.py") == []
        assert await manager.query("diagnostics", file_path="example.py") == []
    finally:
        await manager.aclose()

    events = [json.loads(line) for line in log_path.read_text().splitlines()]
    change = next(
        event for event in events if event.get("method") == "textDocument/didChange"
    )
    assert change["params"]["contentChanges"][0]["range"]["start"] == {
        "line": 0,
        "character": 0,
    }
    save = next(
        event for event in events if event.get("method") == "textDocument/didSave"
    )
    assert "text" not in save["params"]
    diagnostic_requests = [
        event for event in events if event.get("method") == "textDocument/diagnostic"
    ]
    assert diagnostic_requests[-1]["params"]["previousResultId"] == "v2"


@pytest.mark.asyncio
async def test_push_only_diagnostics_remain_cached_when_document_is_unchanged(
    tmp_path: Path,
) -> None:
    source = tmp_path / "example.py"
    source.write_text("problem\n", encoding="utf-8")
    config = fake_config(tmp_path / "lsp.jsonl")
    config = replace(config, env={**config.env, "FAKE_LSP_PUSH_ONLY": "1"})
    manager = LanguageServerManager(tmp_path, {"fake": config})
    try:
        first = await manager.query("diagnostics", file_path="example.py")
        started = asyncio.get_running_loop().time()
        second = await manager.query("diagnostics", file_path="example.py")
        elapsed = asyncio.get_running_loop().time() - started
    finally:
        await manager.aclose()

    assert first == second
    assert second[0]["message"] == "fake problem"
    assert elapsed < 0.5


@pytest.mark.asyncio
async def test_failed_document_open_does_not_commit_sync_state(tmp_path: Path) -> None:
    source = tmp_path / "example.py"
    source.write_text("value = 1\n", encoding="utf-8")

    async def diagnostics(uri: str, items: list[dict[str, object]]) -> None:
        return None

    client = LSPClient(
        fake_config(tmp_path / "lsp.jsonl"),
        tmp_path,
        diagnostics_callback=diagnostics,
    )
    client.capabilities = {"textDocumentSync": 1}
    client.notify = AsyncMock(side_effect=LSPError("write failed"))

    with pytest.raises(LSPError, match="write failed"):
        await client.sync_document(source, "python")
    assert client.has_document(source.as_uri()) is False

    client.notify = AsyncMock(return_value=None)
    sync = await client.sync_document(source, "python")
    assert sync.changed is True
    assert client.has_document(source.as_uri()) is True


@pytest.mark.asyncio
async def test_write_timeout_marks_client_channel_failed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def blocked(payload: dict[str, object]) -> None:
        await asyncio.sleep(60)

    async def diagnostics(uri: str, items: list[dict[str, object]]) -> None:
        return None

    client = LSPClient(
        fake_config(tmp_path / "lsp.jsonl"),
        tmp_path,
        diagnostics_callback=diagnostics,
    )
    monkeypatch.setattr(client, "_write_message", blocked)

    with pytest.raises(LSPError, match="stopped reading"):
        await client.notify("test", {}, timeout=0.01)
    assert client._channel_error is not None


@pytest.mark.asyncio
@pytest.mark.parametrize("_iteration", range(3))
async def test_startup_error_includes_bounded_stderr(
    tmp_path: Path, _iteration: int
) -> None:
    source = tmp_path / "example.py"
    source.write_text("value = 1\n", encoding="utf-8")
    config = fake_config(tmp_path / "lsp.jsonl")
    config = replace(config, env={**config.env, "FAKE_LSP_FAIL": "useful failure"})
    manager = LanguageServerManager(tmp_path, {"fake": config})
    try:
        with pytest.raises(LSPError, match="useful failure"):
            await manager.query("hover", file_path="example.py")
    finally:
        await manager.aclose()


@pytest.mark.asyncio
async def test_transient_startup_failure_restarts_after_backoff(
    tmp_path: Path,
) -> None:
    source = tmp_path / "example.py"
    source.write_text("value = 1\n", encoding="utf-8")
    marker = tmp_path / "failed-once"
    config = fake_config(tmp_path / "lsp.jsonl")
    config = replace(config, env={**config.env, "FAKE_LSP_FAIL_ONCE_FILE": str(marker)})
    manager = LanguageServerManager(tmp_path, {"fake": config})
    try:
        with pytest.raises(LSPError, match="transient startup failure"):
            await manager.query("hover", file_path="example.py")
        await asyncio.sleep(1.05)
        hover = await manager.query("hover", file_path="example.py")
    finally:
        await manager.aclose()

    assert hover[0]["contents"]["value"] == "character=0"


def test_empty_capability_options_are_supported(tmp_path: Path) -> None:
    async def diagnostics(uri: str, items: list[dict[str, object]]) -> None:
        return None

    client = LSPClient(
        fake_config(tmp_path / "lsp.jsonl"),
        tmp_path,
        diagnostics_callback=diagnostics,
    )
    client.capabilities = {"definitionProvider": {}, "diagnosticProvider": {}}
    assert _has_capability(client, "definition") is True
    assert client.supports_pull_diagnostics is True


@pytest.mark.asyncio
@pytest.mark.parametrize("_iteration", range(5))
async def test_close_during_initialize_reaps_subprocess(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, _iteration: int
) -> None:
    source = tmp_path / "example.py"
    source.write_text("value = 1\n", encoding="utf-8")
    config = fake_config(tmp_path / "lsp.jsonl")
    config = replace(config, env={**config.env, "FAKE_LSP_INIT_DELAY": "0.2"})
    clients: list[LSPClient] = []

    class RecordingClient(LSPClient):
        def __init__(self, *args: object, **kwargs: object) -> None:
            super().__init__(*args, **kwargs)  # type: ignore[arg-type]
            clients.append(self)

    monkeypatch.setattr(manager_module, "LSPClient", RecordingClient)
    manager = LanguageServerManager(tmp_path, {"fake": config})
    query = asyncio.create_task(manager.clients_for(source))
    for _ in range(100):
        if clients and clients[0].process is not None:
            break
        await asyncio.sleep(0.01)
    assert clients and clients[0].process is not None
    assert len(manager._starting) == 1
    starting = next(iter(manager._starting.values()))

    await manager.aclose()
    await asyncio.gather(query, return_exceptions=True)

    assert starting.done()
    assert clients[0]._close_task is not None
    assert clients[0]._close_task.done()
    clients[0]._close_task.result()
    assert clients[0]._closed is True
    assert clients[0].process.returncode is not None
    assert manager._broken == {}


@pytest.mark.asyncio
async def test_cancelled_close_finishes_process_cleanup(tmp_path: Path) -> None:
    source = tmp_path / "example.py"
    source.write_text("value = 1\n", encoding="utf-8")
    manager = LanguageServerManager(
        tmp_path, {"fake": fake_config(tmp_path / "lsp.jsonl")}
    )
    await manager.query("hover", file_path="example.py", line=1, character=1)
    client = next(iter(manager._clients.values()))

    closing = asyncio.create_task(manager.aclose())
    await asyncio.sleep(0)
    closing.cancel()
    with pytest.raises(asyncio.CancelledError):
        await closing

    assert client.process is not None
    assert client.process.returncode is not None


@pytest.mark.asyncio
async def test_manager_rechecks_closed_state_inside_lock(tmp_path: Path) -> None:
    config = fake_config(tmp_path / "lsp.jsonl")
    manager = LanguageServerManager(tmp_path, {"fake": config})
    await manager._lock.acquire()
    getting = asyncio.create_task(manager._get_client(config, tmp_path))
    await asyncio.sleep(0)
    manager._closed = True
    manager._lock.release()

    with pytest.raises(LSPError, match="closed"):
        await getting
    assert manager._starting == {}


@pytest.mark.asyncio
async def test_post_edit_diagnostics_are_advisory(tmp_path: Path) -> None:
    source = tmp_path / "example.py"
    source.write_text("problem\n", encoding="utf-8")
    manager = LanguageServerManager(
        tmp_path,
        {
            "missing": LSPServerConfig(
                "missing",
                (str(tmp_path / "does-not-exist"),),
                {".py": "python"},
            )
        },
    )
    middleware = LSPDiagnosticsMiddleware(manager, SafetyGuard(tmp_path))
    result = ToolResult(success=True, output="File written.")

    await middleware.after_tool("write_file", {"file_path": "example.py"}, result)

    assert result.success is True
    assert result.output == "File written."
    await manager.aclose()


@pytest.mark.asyncio
async def test_read_message_rejects_duplicate_content_length() -> None:
    reader = asyncio.StreamReader()
    reader.feed_data(b"Content-Length: 2\r\nContent-Length: 2\r\n\r\n{}")
    reader.feed_eof()
    with pytest.raises(LSPError, match="duplicate"):
        await _read_message(reader)


def test_document_read_is_bounded(tmp_path: Path) -> None:
    source = tmp_path / "large.py"
    source.write_bytes(b"x" * (MAX_LSP_DOCUMENT_BYTES + 1))
    with pytest.raises(LSPError, match="exceeds"):
        _read_document_text(source, SafetyGuard(tmp_path))


def test_lsp_tool_is_read_only_in_plan_mode() -> None:
    decision = PermissionPolicy(PermissionMode.PLAN).evaluate(
        "lsp", {"operation": "definition"}
    )
    assert decision.action is PolicyAction.ALLOW


def test_lsp_cli_status_json(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config = AshConfig(
        model="ollama/test",
        workspace_root=tmp_path,
        db_directory=tmp_path / "db",
    )
    monkeypatch.setattr(
        ash_cli, "_load_config_or_report", lambda **overrides: (config, 0)
    )
    monkeypatch.setattr(cli_lsp, "is_workspace_trusted", lambda workspace: True)
    monkeypatch.setattr(
        cli_lsp,
        "load_lsp_server_configs",
        lambda workspace, include_project: {"fake": fake_config(tmp_path / "log")},
    )

    assert ash_cli.main(["lsp", "status", "--json"]) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["servers"][0]["name"] == "fake"
    assert payload["servers"][0]["argument_count"] == 1
    assert not (tmp_path / "log").exists()


@pytest.mark.asyncio
async def test_lsp_status_is_side_effect_free_and_renderable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("ash.commands.lsp.is_workspace_trusted", lambda workspace: True)
    monkeypatch.setattr(
        "ash.commands.lsp.load_lsp_server_configs",
        lambda workspace, include_project: {"fake": fake_config(tmp_path / "log")},
    )
    config = AshConfig(
        model="ollama/test",
        workspace_root=tmp_path,
        db_directory=tmp_path / "db",
    )

    payload = await inspect_lsp(config, action="status")

    assert payload["trusted"] is True
    assert payload["servers"][0]["name"] == "fake"
    assert "Managed language servers" in render_lsp(payload, json_output=False)
    assert not (tmp_path / "log").exists()


@pytest.mark.asyncio
async def test_lsp_query_refuses_untrusted_workspace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "ash.commands.lsp.is_workspace_trusted", lambda workspace: False
    )
    config = AshConfig(
        model="ollama/test",
        workspace_root=tmp_path,
        db_directory=tmp_path / "db",
    )

    with pytest.raises(ValueError, match="untrusted"):
        await inspect_lsp(
            config,
            action="query",
            operation="workspaceSymbol",
            query="example",
        )
