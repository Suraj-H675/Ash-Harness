from __future__ import annotations

import asyncio
import json
import os
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
    _client_capabilities,
    _lsp_position,
    _read_document_text,
    _read_message,
)
from ash.lsp.config import (
    LSPServerConfig,
    load_lsp_server_configs,
    lsp_command_available,
    resolve_lsp_command,
)
from ash.lsp.manager import LanguageServerManager, _has_capability
from ash.lsp.middleware import LSPDiagnosticsMiddleware
from ash.safety.guard import SafetyGuard
from ash.safety.policy import PermissionMode, PermissionPolicy, PolicyAction
from ash.tools.base import ToolResult
from ash.tools.lsp import LSPQueryArgs, LSPTool
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


def test_project_lsp_config_symlink_is_not_followed(tmp_path: Path) -> None:
    config_dir = tmp_path / ".ash"
    config_dir.mkdir()
    target = tmp_path / "outside-lsp.json"
    target.write_text('{"servers": {}}', encoding="utf-8")
    config_path = config_dir / "lsp.json"
    try:
        config_path.symlink_to(target)
    except OSError:
        pytest.skip("symlinks are unavailable")

    with pytest.raises(ValueError, match="symlinked LSP config"):
        load_lsp_server_configs(tmp_path, include_project=True, detect_builtins=False)


def test_project_lsp_config_rejects_symlinked_parent(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    outside = tmp_path / "outside"
    workspace.mkdir()
    outside.mkdir()
    (outside / "lsp.json").write_text(
        '{"servers":{"escaped":{"command":["server"],"extensions":{".x":"x"}}}}',
        encoding="utf-8",
    )
    try:
        (workspace / ".ash").symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("symlinks are unavailable")

    with pytest.raises(ValueError, match="symlink or junction"):
        load_lsp_server_configs(
            workspace,
            include_project=True,
            detect_builtins=False,
        )


def test_workspace_server_detection_requires_trust_and_executable_bit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    executable = tmp_path / "node_modules" / ".bin" / "basedpyright-langserver"
    executable.parent.mkdir(parents=True)
    executable.write_text("#!/bin/sh\n", encoding="utf-8")
    executable.chmod(0o644)
    monkeypatch.setattr(
        "ash.lsp.config.resolve_host_executable",
        lambda *args, **kwargs: None,
    )

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
        "ash.lsp.config.resolve_host_executable",
        lambda command, **kwargs: f"/installed/{command}",
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


def test_builtin_lsp_excludes_workspace_shadow_and_uses_trusted_host(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    host_bin = tmp_path / "host-bin"
    workspace.mkdir()
    host_bin.mkdir()
    workspace_binary = workspace / "rust-analyzer"
    host_binary = host_bin / "rust-analyzer"
    for binary in (workspace_binary, host_binary):
        binary.write_text("#!/bin/sh\n", encoding="utf-8")
        binary.chmod(0o755)
    monkeypatch.setenv("PATH", f"{workspace}{os.pathsep}{host_bin}")

    configs = load_lsp_server_configs(workspace, include_project=False)

    assert configs["rust-analyzer"].command[0] == str(host_binary.resolve())
    assert configs["rust-analyzer"].command[0] != str(workspace_binary)
    assert lsp_command_available(configs["rust-analyzer"], workspace)


def test_bare_lsp_runtime_command_resolves_without_workspace_shadow(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    host_bin = tmp_path / "host-bin"
    workspace.mkdir()
    host_bin.mkdir()
    (workspace / "server").write_text("shadowed", encoding="utf-8")
    host_server = host_bin / "server"
    host_server.write_text("trusted", encoding="utf-8")
    host_server.chmod(0o755)
    monkeypatch.setenv("PATH", f"{workspace}{os.pathsep}{host_bin}")
    config = LSPServerConfig(
        name="custom",
        command=("server", "--stdio"),
        extensions={".py": "python"},
        source="user",
    )

    assert lsp_command_available(config, workspace) is True
    assert resolve_lsp_command(config.command, workspace)[0] == str(
        host_server.resolve()
    )


@pytest.mark.asyncio
async def test_lsp_client_launches_resolved_host_command(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    host_bin = tmp_path / "host-bin"
    workspace.mkdir()
    host_bin.mkdir()
    marker = tmp_path / "workspace-shadow-ran"
    workspace_shadow = workspace / "server"
    workspace_shadow.write_text(
        f"#!{sys.executable}\n"
        "from pathlib import Path\n"
        f"Path({str(marker)!r}).write_text('shadowed', encoding='utf-8')\n"
        "raise SystemExit(99)\n",
        encoding="utf-8",
    )
    workspace_shadow.chmod(0o755)
    host_server = host_bin / "server"
    host_server.write_text(
        f"#!{sys.executable}\n"
        "import runpy\n"
        f"runpy.run_path({str(FIXTURE_SERVER)!r}, run_name='__main__')\n",
        encoding="utf-8",
    )
    host_server.chmod(0o755)
    monkeypatch.setenv("PATH", f"{workspace}{os.pathsep}{host_bin}")

    config = replace(
        fake_config(tmp_path / "lsp.jsonl"),
        command=("server",),
    )
    client = LSPClient(
        config,
        workspace,
        diagnostics_callback=AsyncMock(return_value=None),
    )
    try:
        await client.start()
        assert client.process is not None
        assert client.process.returncode is None
    finally:
        await client.aclose()

    assert not marker.exists()


@pytest.mark.skipif(os.name == "nt", reason="POSIX cwd race regression")
@pytest.mark.asyncio
async def test_lsp_client_refuses_root_replaced_before_start(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    saved = tmp_path / "workspace-saved"
    workspace.mkdir()
    client = LSPClient(
        fake_config(tmp_path / "lsp.jsonl"),
        workspace,
        diagnostics_callback=AsyncMock(return_value=None),
    )
    workspace.rename(saved)
    workspace.mkdir()
    create = AsyncMock(side_effect=AssertionError("LSP server must not launch"))
    monkeypatch.setattr("ash.lsp.client.asyncio.create_subprocess_exec", create)

    with pytest.raises(LSPError, match="working directory identity changed"):
        await client.start()

    create.assert_not_awaited()
    assert client.process is None


@pytest.mark.skipif(os.name == "nt", reason="POSIX cwd race regression")
@pytest.mark.asyncio
async def test_lsp_client_cwd_swap_cannot_escape_workspace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ash.sandbox import process_utils as process_utils_module

    workspace = tmp_path / "workspace"
    outside = tmp_path / "outside"
    saved = tmp_path / "workspace-saved"
    workspace.mkdir()
    outside.mkdir()
    cwd_log = tmp_path / "lsp-cwd.txt"
    wrapper = tmp_path / "host-lsp-server"
    wrapper.write_text(
        f"#!{sys.executable}\n"
        "import runpy\n"
        "from pathlib import Path\n"
        f"Path({str(cwd_log)!r}).write_text(str(Path.cwd()), encoding='utf-8')\n"
        f"runpy.run_path({str(FIXTURE_SERVER)!r}, run_name='__main__')\n",
        encoding="utf-8",
    )
    wrapper.chmod(0o755)
    config = replace(
        fake_config(tmp_path / "lsp.jsonl"),
        command=(str(wrapper),),
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
                pytest.skip(f"Symlink creation is unavailable: {exc}")
        return plan

    monkeypatch.setattr("ash.lsp.client.prepare_process_tree", prepare_then_swap)
    client = LSPClient(
        config,
        workspace,
        diagnostics_callback=AsyncMock(return_value=None),
    )
    try:
        await client.start()
    finally:
        await client.aclose()

    assert swapped is True
    assert Path(cwd_log.read_text(encoding="utf-8")).resolve() == saved.resolve()


@pytest.mark.asyncio
async def test_lsp_client_fails_closed_when_stable_cwd_is_unavailable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ash.sandbox.process_utils import ProcessTreeUnavailable

    client = LSPClient(
        fake_config(tmp_path / "lsp.jsonl"),
        tmp_path,
        diagnostics_callback=AsyncMock(return_value=None),
    )

    def unavailable(*args, **kwargs):
        raise ProcessTreeUnavailable("stable cwd unavailable")

    monkeypatch.setattr(
        "ash.lsp.client.prepare_scoped_process_launch",
        unavailable,
    )
    create = AsyncMock()
    monkeypatch.setattr("ash.lsp.client.asyncio.create_subprocess_exec", create)

    with pytest.raises(LSPError, match="stable cwd unavailable"):
        await client.start()

    create.assert_not_awaited()
    assert client.process is None


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

        prepared = await manager.query(
            "prepareRename", file_path="example.py", line=1, character=1
        )
        assert prepared == [
            {
                "range": {
                    "start": {"line": 0, "character": 0},
                    "end": {"line": 0, "character": 1},
                },
                "placeholder": "x",
            }
        ]

        renamed = await manager.query(
            "rename",
            file_path="example.py",
            line=1,
            character=1,
            new_name="renamed",
        )
        assert renamed == [
            {
                "server": "fake",
                "edit": {
                    "changes": {
                        "example.py": [
                            {
                                "range": {
                                    "start": {"line": 0, "character": 0},
                                    "end": {"line": 0, "character": 1},
                                },
                                "newText": "renamed",
                            }
                        ]
                    }
                },
            }
        ]

        actions = await manager.query(
            "codeAction",
            file_path="example.py",
            line=1,
            character=1,
            end_line=1,
            end_character=2,
            code_action_kind="quickfix",
        )
        assert actions == [
            {
                "server": "fake",
                "title": "Replace example",
                "kind": "quickfix",
                "isPreferred": True,
                "edit": {
                    "changes": {
                        "example.py": [
                            {
                                "range": {
                                    "start": {"line": 0, "character": 0},
                                    "end": {"line": 0, "character": 1},
                                },
                                "newText": "fixed",
                            }
                        ]
                    }
                },
                "command": {
                    "title": "Apply opaque command",
                    "command": "fake.apply",
                    "execution": "not_performed",
                },
            },
            {
                "server": "fake",
                "title": "Run formatter",
                "command": {
                    "title": "Run formatter",
                    "command": "fake.format",
                    "execution": "not_performed",
                },
            },
        ]

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
        "textDocument/prepareRename",
        "textDocument/rename",
        "textDocument/codeAction",
        "shutdown",
        "exit",
    } <= methods


@pytest.mark.asyncio
async def test_lsp_rename_rejects_external_workspace_edit(tmp_path: Path) -> None:
    source = tmp_path / "example.py"
    source.write_text("x = 1\n", encoding="utf-8")
    config = fake_config(tmp_path / "lsp.jsonl")
    config = replace(config, env={**config.env, "FAKE_LSP_UNSAFE_RENAME": "1"})
    manager = LanguageServerManager(tmp_path, {"fake": config})
    try:
        with pytest.raises(LSPError, match="unsafe workspace edit"):
            await manager.query(
                "rename",
                file_path="example.py",
                line=1,
                character=1,
                new_name="renamed",
            )
        assert (tmp_path / "example.py").read_text(encoding="utf-8") == "x = 1\n"
    finally:
        await manager.aclose()


@pytest.mark.asyncio
async def test_lsp_rename_normalizes_document_changes_and_preserves_long_paths(
    tmp_path: Path,
) -> None:
    nested = tmp_path / ("long-segment-" * 12) / "example.py"
    nested.parent.mkdir()
    nested.write_text("x = 1\n", encoding="utf-8")
    config = fake_config(tmp_path / "lsp.jsonl")
    config = replace(config, env={**config.env, "FAKE_LSP_DOCUMENT_CHANGES": "1"})
    manager = LanguageServerManager(tmp_path, {"fake": config})
    try:
        result = await manager.query(
            "rename",
            file_path=str(nested.relative_to(tmp_path)),
            line=1,
            character=1,
            new_name="renamed",
        )
    finally:
        await manager.aclose()

    relative = nested.relative_to(tmp_path).as_posix()
    edit = result[0]["edit"]
    assert edit["documentChanges"][0]["textDocument"] == {
        "path": relative,
        "version": 1,
    }
    assert edit["documentChanges"][1] == {
        "kind": "rename",
        "oldPath": relative,
        "newPath": "renamed.py",
    }
    assert len(relative) > 128


@pytest.mark.asyncio
async def test_lsp_rename_rejects_duplicate_normalized_targets(tmp_path: Path) -> None:
    source = tmp_path / "example.py"
    source.write_text("x = 1\n", encoding="utf-8")
    config = fake_config(tmp_path / "lsp.jsonl")
    config = replace(config, env={**config.env, "FAKE_LSP_DUPLICATE_RENAME": "1"})
    manager = LanguageServerManager(tmp_path, {"fake": config})
    try:
        with pytest.raises(LSPError, match="unsafe workspace edit"):
            await manager.query(
                "rename",
                file_path="example.py",
                line=1,
                character=1,
                new_name="renamed",
            )
    finally:
        await manager.aclose()


def test_lsp_refactor_client_capabilities_and_argument_ownership() -> None:
    client_capabilities = _client_capabilities()
    capabilities = client_capabilities["textDocument"]
    assert capabilities["rename"]["prepareSupport"] is True
    assert capabilities["codeAction"]["isPreferredSupport"] is True
    assert capabilities["formatting"]["dynamicRegistration"] is False
    assert client_capabilities["workspace"]["applyEdit"] is False
    assert client_capabilities["workspace"]["workspaceEdit"] == {
        "documentChanges": True,
        "resourceOperations": ["create", "rename", "delete"],
        "failureHandling": "abort",
    }

    rename = LSPQueryArgs(
        operation="rename", file_path="example.py", new_name="next_name"
    )
    assert rename.new_name == "next_name"
    with pytest.raises(ValueError, match="only valid for rename"):
        LSPQueryArgs(
            operation="definition", file_path="example.py", new_name="unexpected"
        )
    with pytest.raises(ValueError, match="provided together"):
        LSPQueryArgs(operation="codeAction", file_path="example.py", end_line=2)
    with pytest.raises(ValueError, match="only valid for codeAction"):
        LSPQueryArgs(
            operation="definition",
            file_path="example.py",
            code_action_kind="quickfix",
        )
    with pytest.raises(ValueError, match="only valid for formatting"):
        LSPQueryArgs(
            operation="definition",
            file_path="example.py",
            server="fake",
        )


@pytest.mark.asyncio
async def test_lsp_formatting_returns_advisory_edits_without_modifying_file(
    tmp_path: Path,
) -> None:
    source = tmp_path / "example.py"
    source.write_text("x=1\n", encoding="utf-8")
    log_path = tmp_path / "lsp.jsonl"
    manager = LanguageServerManager(tmp_path, {"fake": fake_config(log_path)})
    try:
        proposal = await manager.formatting_for(
            "example.py",
            tab_size=2,
            insert_spaces=False,
        )
    finally:
        await manager.aclose()

    assert proposal.server == "fake"
    assert proposal.position_encoding == "utf-8"
    assert proposal.document_text == "x=1\n"
    assert proposal.edits == (
        {
            "range": {
                "start": {"line": 0, "character": 0},
                "end": {"line": 0, "character": 3},
            },
            "newText": "x = 1",
        },
    )
    assert source.read_text(encoding="utf-8") == "x=1\n"

    events = [json.loads(line) for line in log_path.read_text().splitlines()]
    request = next(
        event for event in events if event.get("method") == "textDocument/formatting"
    )
    assert request["params"]["options"] == {
        "tabSize": 2,
        "insertSpaces": False,
    }


@pytest.mark.asyncio
async def test_lsp_tool_exposes_bounded_advisory_formatting(tmp_path: Path) -> None:
    source = tmp_path / "example.py"
    source.write_text("x=1\n", encoding="utf-8")
    manager = LanguageServerManager(
        tmp_path, {"fake": fake_config(tmp_path / "lsp.jsonl")}
    )
    tool = LSPTool(SafetyGuard(tmp_path), manager)
    try:
        result = await tool.run(
            operation="formatting",
            file_path="example.py",
            server="fake",
            tab_size=2,
            insert_spaces=False,
        )
    finally:
        await tool.aclose()

    assert result.success is True
    payload = json.loads(result.output)
    assert payload == [
        {
            "server": "fake",
            "position_encoding": "utf-8",
            "edits": [
                {
                    "range": {
                        "start": {"line": 0, "character": 0},
                        "end": {"line": 0, "character": 3},
                    },
                    "newText": "x = 1",
                }
            ],
        }
    ]
    assert "document_text" not in result.output
    assert source.read_text(encoding="utf-8") == "x=1\n"


@pytest.mark.asyncio
async def test_lsp_formatting_requires_server_selection_when_multiple_support_it(
    tmp_path: Path,
) -> None:
    source = tmp_path / "example.py"
    source.write_text("x=1\n", encoding="utf-8")
    first = replace(fake_config(tmp_path / "first-ambiguous.jsonl"), name="first")
    second = replace(fake_config(tmp_path / "second-ambiguous.jsonl"), name="second")
    manager = LanguageServerManager(tmp_path, {"first": first, "second": second})
    try:
        with pytest.raises(LSPError, match="multiple language servers advertise formatting"):
            await manager.formatting_for("example.py")
    finally:
        await manager.aclose()

    first = replace(fake_config(tmp_path / "first-selected.jsonl"), name="first")
    second = replace(fake_config(tmp_path / "second-selected.jsonl"), name="second")
    manager = LanguageServerManager(tmp_path, {"first": first, "second": second})
    try:
        proposal = await manager.formatting_for("example.py", server="second")
    finally:
        await manager.aclose()

    assert proposal.server == "second"
    assert not (tmp_path / "first-selected.jsonl").exists()
    assert (tmp_path / "second-selected.jsonl").exists()


@pytest.mark.asyncio
async def test_lsp_formatting_rejects_invalid_server_result(tmp_path: Path) -> None:
    source = tmp_path / "example.py"
    source.write_text("x=1\n", encoding="utf-8")
    config = fake_config(tmp_path / "lsp.jsonl")
    config = replace(config, env={**config.env, "FAKE_LSP_BAD_FORMATTING": "1"})
    manager = LanguageServerManager(tmp_path, {"fake": config})
    try:
        with pytest.raises(LSPError, match="invalid formatting edits"):
            await manager.formatting_for("example.py")
    finally:
        await manager.aclose()


@pytest.mark.asyncio
async def test_lsp_rename_fails_closed_across_multiple_servers(tmp_path: Path) -> None:
    source = tmp_path / "example.py"
    source.write_text("x = 1\n", encoding="utf-8")
    safe = replace(
        fake_config(tmp_path / "safe.jsonl"),
        name="safe",
    )
    unsafe = replace(
        fake_config(tmp_path / "unsafe.jsonl"),
        name="unsafe",
        env={
            **fake_config(tmp_path / "unsafe.jsonl").env,
            "FAKE_LSP_UNSAFE_RENAME": "1",
        },
    )
    manager = LanguageServerManager(tmp_path, {"safe": safe, "unsafe": unsafe})
    try:
        with pytest.raises(LSPError, match="unsafe workspace edit"):
            await manager.query(
                "rename",
                file_path="example.py",
                line=1,
                character=1,
                new_name="renamed",
            )
    finally:
        await manager.aclose()

    assert source.read_text(encoding="utf-8") == "x = 1\n"


def test_lsp_cli_routes_refactor_arguments_and_rejects_cross_operation_flags(
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
    observed: dict[str, object] = {}

    async def fake_inspect(config, **kwargs):
        observed.update(kwargs)
        return {
            "schema_version": 1,
            "workspace": str(tmp_path),
            "operation": kwargs["operation"],
            "result": [],
        }

    monkeypatch.setattr(cli_lsp, "inspect_lsp", fake_inspect)

    assert (
        ash_cli.main(
            [
                "lsp",
                "query",
                "codeAction",
                "example.py",
                "--line",
                "2",
                "--character",
                "3",
                "--end-line",
                "4",
                "--end-character",
                "5",
                "--code-action-kind",
                "quickfix",
                "--json",
            ]
        )
        == 0
    )
    assert observed["end_line"] == 4
    assert observed["end_character"] == 5
    assert observed["code_action_kind"] == "quickfix"
    assert '"operation": "codeAction"' in capsys.readouterr().out

    observed.clear()
    assert (
        ash_cli.main(
            [
                "lsp",
                "query",
                "formatting",
                "example.py",
                "--server",
                "fake",
                "--tab-size",
                "2",
                "--no-insert-spaces",
                "--json",
            ]
        )
        == 0
    )
    assert observed["server"] == "fake"
    assert observed["tab_size"] == 2
    assert observed["insert_spaces"] is False
    assert '"operation": "formatting"' in capsys.readouterr().out

    with pytest.raises(SystemExit):
        ash_cli.main(
            [
                "lsp",
                "query",
                "definition",
                "example.py",
                "--new-name",
                "not-allowed",
            ]
        )

    with pytest.raises(SystemExit):
        ash_cli.main(
            [
                "lsp",
                "query",
                "definition",
                "example.py",
                "--server",
                "fake",
            ]
        )

    with pytest.raises(SystemExit):
        ash_cli.main(
            [
                "lsp",
                "query",
                "formatting",
                "example.py",
                "--tab-size",
                "0",
            ]
        )


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
