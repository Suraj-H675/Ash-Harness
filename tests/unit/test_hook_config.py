import json
import sys

import pytest

from ash.hooks.config import (
    MAX_HOOK_CONFIG_BYTES,
    MAX_HOOKS_PER_EVENT,
    HookConfigSource,
    load_command_hooks,
)
from ash.hooks.registry import HookBlock


@pytest.mark.asyncio
async def test_command_hook_receives_json_and_injects_session_text(tmp_path) -> None:
    config = tmp_path / "hooks.json"
    command = [
        sys.executable,
        "-c",
        "import json,sys; p=json.load(sys.stdin); print(p['event'])",
    ]
    config.write_text(json.dumps({"session_start": [{"command": command}]}))
    registry = load_command_hooks([config])
    await registry.fire_session_start()
    assert registry.get_injected_prompt() == "session_start"


@pytest.mark.asyncio
async def test_plugin_hook_uses_root_environment_and_working_directory(
    tmp_path,
) -> None:
    plugin = tmp_path / "plugin"
    plugin.mkdir()
    config = plugin / "hooks.json"
    command = [
        sys.executable,
        "-c",
        "import os,sys; print(os.getcwd() + '|' + sys.argv[1])",
        "${ASH_PLUGIN_ROOT}",
    ]
    config.write_text(json.dumps({"session_start": [{"command": command}]}))
    registry = load_command_hooks(
        [
            HookConfigSource(
                config,
                cwd=plugin,
                environment=(("ASH_PLUGIN_ROOT", str(plugin)),),
            )
        ]
    )

    await registry.fire_session_start()

    assert registry.get_injected_prompt() == f"{plugin}|{plugin}"


@pytest.mark.asyncio
async def test_plugin_hook_refuses_working_directory_symlink_swap(tmp_path) -> None:
    plugin = tmp_path / "plugin"
    plugin.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    marker = outside / "marker"
    config = plugin / "hooks.json"
    command = [
        sys.executable,
        "-c",
        "from pathlib import Path; Path('marker').write_text('executed')",
    ]
    config.write_text(json.dumps({"session_start": [{"command": command}]}))
    registry = load_command_hooks(
        [
            HookConfigSource(
                config,
                cwd=plugin,
                trusted_root=plugin,
            )
        ]
    )

    original = tmp_path / "plugin-original"
    plugin.rename(original)
    try:
        plugin.symlink_to(outside, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"symlink creation is unavailable: {exc}")

    await registry.fire_session_start()

    assert registry.get_injected_prompt() == ""
    assert not marker.exists()
    assert len(registry.diagnostics) == 1
    assert registry.diagnostics[0].error == "working directory identity changed"


@pytest.mark.asyncio
async def test_aba_swapped_hook_command_is_blocked_by_cwd_identity(tmp_path) -> None:
    workspace = tmp_path / "workspace"
    saved = tmp_path / "workspace-original"
    replacement = tmp_path / "workspace-replacement"
    workspace.mkdir()
    replacement.mkdir()
    marker = tmp_path / "replacement-hook-ran"
    replacement_config = replacement / ".ash" / "hooks.json"
    replacement_config.parent.mkdir(parents=True)
    replacement_config.write_text(
        json.dumps(
            {
                "pre_tool": [
                    {
                        "matcher": "read_file",
                        "command": [
                            sys.executable,
                            "-c",
                            f"from pathlib import Path; Path({str(marker)!r}).write_text('ran')",
                        ],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    source = HookConfigSource(
        workspace / ".ash" / "hooks.json",
        cwd=workspace,
        trusted_root=workspace,
    )

    workspace.rename(saved)
    replacement.rename(workspace)
    try:
        registry = load_command_hooks([source])
    finally:
        workspace.rename(replacement)
        saved.rename(workspace)

    with pytest.raises(Exception, match="working directory identity changed"):
        await registry.fire_pre_tool("read_file", {"file_path": "README.md"})

    assert not marker.exists()


@pytest.mark.asyncio
async def test_command_pre_tool_hook_can_deny_with_structured_output(tmp_path) -> None:
    config = tmp_path / "hooks.json"
    command = [
        sys.executable,
        "-c",
        "import json; print(json.dumps({'decision':'deny','reason':'blocked'}))",
    ]
    config.write_text(
        json.dumps({"pre_tool": [{"matcher": "write_.*", "command": command}]})
    )
    registry = load_command_hooks([config])

    with pytest.raises(HookBlock, match="blocked"):
        await registry.fire_pre_tool("write_file", {"file_path": "x"})
    await registry.fire_pre_tool("read_file", {"file_path": "x"})


@pytest.mark.asyncio
async def test_command_pre_tool_hook_rejects_duplicate_decisions(tmp_path) -> None:
    config = tmp_path / "hooks.json"
    command = [
        sys.executable,
        "-c",
        'print(\'{"decision":"deny","decision":"allow"}\')',
    ]
    config.write_text(json.dumps({"pre_tool": [{"command": command}]}))
    registry = load_command_hooks([config])

    with pytest.raises(ValueError, match="JSON object or empty"):
        await registry.fire_pre_tool("read_file", {})


@pytest.mark.asyncio
async def test_command_pre_tool_legacy_stdout_remains_non_blocking(tmp_path) -> None:
    config = tmp_path / "hooks.json"
    command = [sys.executable, "-c", "print('legacy diagnostic')"]
    config.write_text(json.dumps({"pre_tool": [{"command": command}]}))
    registry = load_command_hooks([config])

    await registry.fire_pre_tool("read_file", {})


@pytest.mark.asyncio
async def test_command_lifecycle_hook_receives_versioned_payload(tmp_path) -> None:
    config = tmp_path / "hooks.json"
    output = tmp_path / "event.json"
    command = [
        sys.executable,
        "-c",
        "import json,sys; open(sys.argv[1],'w').write(json.dumps(json.load(sys.stdin)))",
        str(output),
    ]
    config.write_text(json.dumps({"turn_end": [{"command": command}]}))
    registry = load_command_hooks([config])

    await registry.fire_lifecycle(
        "turn_end", {"session_id": "session-1", "status": "completed"}
    )

    payload = json.loads(output.read_text())
    assert payload == {
        "schema_version": 1,
        "event": "turn_end",
        "session_id": "session-1",
        "status": "completed",
    }


@pytest.mark.asyncio
async def test_session_start_supports_structured_context_and_caps_output(
    tmp_path,
) -> None:
    config = tmp_path / "hooks.json"
    structured = [
        sys.executable,
        "-c",
        "import json; print(json.dumps({'additional_context':'use this context'}))",
    ]
    config.write_text(json.dumps({"session_start": [{"command": structured}]}))
    registry = load_command_hooks([config])

    await registry.fire_session_start()
    assert registry.get_injected_prompt() == "use this context"

    oversized = [sys.executable, "-c", "print('x' * (1024 * 1024 + 1))"]
    config.write_text(json.dumps({"session_start": [{"command": oversized}]}))
    registry = load_command_hooks([config])
    await registry.fire_session_start()

    assert registry.get_injected_prompt() == ""
    assert "exceeded" in registry.diagnostics[0].error


def test_hook_config_rejects_oversized_file(tmp_path) -> None:
    config = tmp_path / "hooks.json"
    config.write_bytes(b" " * (MAX_HOOK_CONFIG_BYTES + 1))

    with pytest.raises(ValueError, match="exceeds 1 MiB"):
        load_command_hooks([config])


def test_hook_config_rejects_duplicate_json_keys(tmp_path) -> None:
    config = tmp_path / "hooks.json"
    config.write_text(
        '{"pre_tool":[{"command":["echo","first"]}],"pre_tool":[]}',
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="duplicate JSON object key"):
        load_command_hooks([config])


def test_hook_config_rejects_unknown_event_names(tmp_path) -> None:
    config = tmp_path / "hooks.json"
    config.write_text(
        json.dumps({"pre_toll": [{"command": ["echo", "blocked"]}]}),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="unknown events.*pre_toll"):
        load_command_hooks([config])


def test_hook_config_bounds_entries_per_event(tmp_path) -> None:
    config = tmp_path / "hooks.json"
    config.write_text(
        json.dumps(
            {
                "pre_tool": [
                    {"command": ["echo", str(index)]}
                    for index in range(MAX_HOOKS_PER_EVENT + 1)
                ]
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="pre_tool.*32"):
        load_command_hooks([config])


def test_hook_config_bounds_merged_entries_per_event(tmp_path) -> None:
    first = tmp_path / "hooks-a.json"
    second = tmp_path / "hooks-b.json"
    payload = {
        "pre_tool": [
            {"command": ["echo", str(index)]}
            for index in range(20)
        ]
    }
    first.write_text(json.dumps(payload), encoding="utf-8")
    second.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValueError, match="pre_tool.*32 registered hooks"):
        load_command_hooks([first, second])


@pytest.mark.parametrize(
    "matcher",
    [
        "(a+)+$",
        "(a|aa)+$",
        r"(read_file)\1",
        "(?=read)read_file",
        ".*.*.*.*.*blocked",
    ],
)
def test_hook_config_rejects_potentially_pathological_matchers(
    tmp_path,
    matcher: str,
) -> None:
    config = tmp_path / "hooks.json"
    config.write_text(
        json.dumps(
            {
                "pre_tool": [
                    {"matcher": matcher, "command": ["echo", "blocked"]}
                ]
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="Hook matcher"):
        load_command_hooks([config])


def test_hook_config_accepts_bounded_simple_regex_matchers(tmp_path) -> None:
    config = tmp_path / "hooks.json"
    config.write_text(
        json.dumps(
            {
                "pre_tool": [
                    {
                        "matcher": "read_.*|write_.*",
                        "command": ["echo", "ok"],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    registry = load_command_hooks([config])

    assert len(registry._pre_tool) == 1


def test_hook_config_rejects_symlinked_file(tmp_path) -> None:
    outside = tmp_path / "outside-hooks.json"
    outside.write_text(json.dumps({"session_start": []}), encoding="utf-8")
    config = tmp_path / "hooks.json"
    try:
        config.symlink_to(outside)
    except OSError as exc:
        pytest.skip(f"symlink creation is unavailable: {exc}")

    with pytest.raises(ValueError, match="symlinked hook config"):
        load_command_hooks([config])


def test_hook_config_rejects_symlinked_parent_inside_trusted_root(tmp_path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    outside.joinpath("hooks.json").write_text(
        json.dumps({"session_start": []}),
        encoding="utf-8",
    )
    try:
        workspace.joinpath(".ash").symlink_to(outside, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"symlink creation is unavailable: {exc}")

    with pytest.raises(ValueError, match="contains a symlink or junction"):
        load_command_hooks(
            [
                HookConfigSource(
                    workspace / ".ash" / "hooks.json",
                    trusted_root=workspace,
                )
            ]
        )
