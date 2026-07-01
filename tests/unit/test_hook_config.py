import json
import sys

import pytest

from hooks.config import HookConfigSource, load_command_hooks


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
