import json
import sys

import pytest

from ash.hooks.config import HookConfigSource, load_command_hooks
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
