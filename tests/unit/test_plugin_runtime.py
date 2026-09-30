import asyncio
import os
import sys
import tarfile
from contextlib import contextmanager, nullcontext
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import pytest

from ash.runtime import build_tools
from ash.config import AshConfig
from ash.core.loop import AshLoop, _execute_tool_once
from ash.core.session import SessionStore
from ash.plugins.manifest import PluginManifest
from ash.safety.anchored_fs import supports_anchored_mutation
from ash.plugins.registry import DiscoveredPlugin
from ash.plugins.runtime import (
    PluginHostClient,
    PluginRuntimeError,
    PluginRuntimeTool,
    build_plugin_runtime_tools,
    plugin_tool_name,
)
from ash.providers.base import ProviderABC, StreamChunk
from ash.safety.guard import SafetyGuard
from ash.sandbox import (
    BubblewrapSandbox,
    SANDBOX_TIER_DOCKER,
    SandboxBackendUnavailable,
    SandboxInvocation,
    SandboxManager,
    has_bwrap,
)
from ash.tools.base import BaseTool, ToolResult
from ash.tools.tool_search import SearchToolsTool


HOST_SOURCE = r"""
import json
import os
import sys
import time

for line in sys.stdin:
    request = json.loads(line)
    method = request["method"]
    params = request["params"]
    if method == "initialize":
        result = {"protocol_version": int(os.environ.get("PROTOCOL", "1"))}
    elif method == "shutdown":
        result = {}
    else:
        arguments = params["arguments"]
        action = arguments.get("action", "echo")
        counter = arguments.get("counter")
        if counter:
            with open(counter, "a", encoding="utf-8") as handle:
                handle.write("called\n")
        if action == "crash":
            print("host crashed intentionally", file=sys.stderr, flush=True)
            raise SystemExit(23)
        if action == "sleep":
            time.sleep(5)
        if action == "malformed":
            print("not-json", flush=True)
            continue
        if action == "oversized":
            print("x" * (1024 * 1024 + 10), flush=True)
            continue
        if action == "nonstandard_json":
            print('{"jsonrpc":"2.0","id":%d,"result":NaN}' % request["id"], flush=True)
            continue
        if action == "duplicate_json_key":
            print('{"jsonrpc":"2.0","id":%d,"result":{},"result":{"success":true}}' % request["id"], flush=True)
            continue
        if action == "extra_response_field":
            print(json.dumps({"jsonrpc": "2.0", "id": request["id"], "result": {}, "extra": True}), flush=True)
            continue
        if action == "result_and_error":
            print(json.dumps({"jsonrpc": "2.0", "id": request["id"], "result": {}, "error": {"message": "bad"}}), flush=True)
            continue
        if action == "environment":
            arguments["text"] = os.environ.get("ASH_TEST_SECRET", "missing")
        result = {
            "success": True,
            "output": str(arguments.get("text", os.getpid())),
            "error": None,
            "token_count": 0 if action == "zero_tokens" else 1,
            "truncated": False,
        }
    print(json.dumps({"jsonrpc": "2.0", "id": request["id"], "result": result}), flush=True)
"""


class NoopProvider(ProviderABC):
    model_name = "noop"

    def count_tokens(self, text: str) -> int:
        return len(text)

    async def stream_chat(self, messages, temperature=0.0, tools=None):
        yield StreamChunk(content="done", is_done=True)


class NoopUI:
    has_approval_callback = False

    def emit_event(self, event):
        return None

    def request_tool_approval(self, tool_name, arguments):
        return True


def _plugin(
    root: Path,
    *,
    timeout: float = 1.0,
    schema: dict | None = None,
    python_executable: str | None = None,
) -> DiscoveredPlugin:
    root.mkdir(parents=True, exist_ok=True)
    (root / "runtime.py").write_text(HOST_SOURCE, encoding="utf-8")
    manifest = PluginManifest.from_dict(
        {
            "name": "example-plugin",
            "version": "1.2.3",
            "runtime": {
                "command": [python_executable or sys.executable, "runtime.py"],
                "timeoutSeconds": timeout,
            },
            "tools": [
                {
                    "name": "echo",
                    "description": "Echo text",
                    "inputSchema": schema
                    or {
                        "type": "object",
                        "properties": {
                            "text": {"type": "string"},
                            "action": {"type": "string"},
                            "counter": {"type": "string"},
                        },
                        "additionalProperties": False,
                    },
                }
            ],
        }
    )
    return DiscoveredPlugin(manifest, root, "test")


def _direct_manager(root: Path) -> SandboxManager:
    return SandboxManager(workspace_root=root, backend_preference="direct")


def _tool(
    tmp_path: Path,
    *,
    timeout: float = 1.0,
    allow_unisolated: bool = True,
    schema: dict | None = None,
) -> PluginRuntimeTool:
    plugin = _plugin(tmp_path / "plugin", timeout=timeout, schema=schema)
    client = PluginHostClient(
        plugin,
        _direct_manager(plugin.root),
        allow_unisolated=allow_unisolated,
    )
    return PluginRuntimeTool(
        SafetyGuard(tmp_path), plugin, plugin.manifest.tools[0], client
    )


def test_plugin_runtime_tools_have_aggregate_active_limit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plugins = [_plugin(tmp_path / f"plugin-{index}") for index in range(3)]
    monkeypatch.setattr(
        "ash.plugins.runtime.MAX_TOTAL_PLUGIN_RUNTIME_TOOLS",
        2,
    )

    with pytest.raises(ValueError, match=r"too many executable tools: 3 > 2"):
        build_plugin_runtime_tools(
            plugins,
            SafetyGuard(tmp_path),
            backend_preference="direct",
            docker_image="python:3.12-slim",
            allow_unisolated=True,
        )


class _ReloadLifecycleTool(BaseTool):
    description = "reload lifecycle test tool"
    args_schema = None

    def __init__(
        self,
        guard: SafetyGuard,
        *,
        name: str,
        plugin_runtime_tool: bool,
        fail_close_times: int = 0,
    ) -> None:
        super().__init__(guard)
        self.name = name
        self.plugin_runtime_tool = plugin_runtime_tool
        self.fail_close_times = fail_close_times
        self.close_calls = 0

    async def run(self, **kwargs) -> ToolResult:
        del kwargs
        return ToolResult(success=True, output=self.name)

    async def aclose(self) -> None:
        self.close_calls += 1
        if self.close_calls <= self.fail_close_times:
            raise RuntimeError(f"{self.name} close failed")


@pytest.mark.asyncio
async def test_real_plugin_host_handshake_call_and_close(tmp_path: Path) -> None:
    tool = _tool(tmp_path)

    result = await tool.run(text="hello")

    assert result.success is True
    assert result.output == "hello"
    assert tool.client.running is True
    process = tool.client._process
    assert process is not None
    pid = process.pid
    await tool.aclose()
    assert tool.client.running is False
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


@pytest.mark.asyncio
async def test_isolated_plugin_host_pins_root_across_path_swap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    if not has_bwrap():
        pytest.skip("bwrap not installed or usable on this host")
    python = next(
        (
            candidate
            for candidate in (Path("/usr/bin/python3"), Path("/usr/bin/python"))
            if candidate.exists()
        ),
        None,
    )
    if python is None:
        pytest.skip("no system Python inside Bubblewrap system mounts")

    plugin = _plugin(tmp_path / "plugin", python_executable=str(python))
    replacement = tmp_path / "replacement"
    saved = tmp_path / "plugin-saved"
    replacement.mkdir()
    replacement_source = HOST_SOURCE.replace(
        'str(arguments.get("text", os.getpid()))',
        '"REPLACEMENT"',
    )
    assert replacement_source != HOST_SOURCE
    (replacement / "runtime.py").write_text(replacement_source, encoding="utf-8")

    manager = SandboxManager(
        workspace_root=plugin.root,
        workspace_read_only=True,
        require_read_isolation=True,
        network=False,
        backend_preference="native",
    )
    assert manager.backend_name == "bubblewrap"
    original_wrap = BubblewrapSandbox.wrap
    swapped = False

    def swap_after_wrap(
        backend: BubblewrapSandbox,
        command: list[str] | tuple[str, ...],
        **kwargs: object,
    ) -> list[str]:
        nonlocal swapped
        argv = original_wrap(backend, command, **kwargs)
        if not swapped:
            plugin.root.rename(saved)
            plugin.root.symlink_to(replacement, target_is_directory=True)
            swapped = True
        return argv

    monkeypatch.setattr(BubblewrapSandbox, "wrap", swap_after_wrap)
    client = PluginHostClient(plugin, manager, allow_unisolated=False)
    try:
        result = await client.call_tool("echo", {"text": "ORIGINAL"})
        assert result.output == "ORIGINAL"
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_direct_plugin_host_refuses_root_swap_before_spawn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plugin = _plugin(tmp_path / "plugin")
    replacement = tmp_path / "replacement"
    saved = tmp_path / "plugin-saved"
    replacement.mkdir()
    replacement_source = (
        "from pathlib import Path\n"
        "Path('replacement-executed').write_text('yes')\n"
        + HOST_SOURCE
    )
    (replacement / "runtime.py").write_text(replacement_source, encoding="utf-8")
    manager = _direct_manager(plugin.root)
    client = PluginHostClient(plugin, manager, allow_unisolated=True)
    original_prepare_launch = manager.prepare_launch
    swapped = False

    @contextmanager
    def prepare_then_swap(*args: object, **kwargs: object):
        nonlocal swapped
        with original_prepare_launch(*args, **kwargs) as invocation:
            if not swapped:
                plugin.root.rename(saved)
                try:
                    plugin.root.symlink_to(replacement, target_is_directory=True)
                except OSError as exc:
                    pytest.skip(f"symlink creation is unavailable: {exc}")
                swapped = True
            yield invocation

    monkeypatch.setattr(manager, "prepare_launch", prepare_then_swap)

    with pytest.raises(PluginRuntimeError, match="working directory identity changed"):
        await client.call_tool("echo", {"text": "must-not-run"})

    assert swapped is True
    assert not (replacement / "replacement-executed").exists()
    assert client.running is False


@pytest.mark.asyncio
@pytest.mark.skipif(
    not supports_anchored_mutation(),
    reason="descriptor-anchored Docker plugin staging is unavailable",
)
async def test_docker_plugin_staging_streams_immutable_snapshot(tmp_path: Path) -> None:
    plugin = _plugin(tmp_path / "plugin")
    observed: dict[str, object] = {}
    manager = Mock()
    manager.backend_name = "docker"

    async def stage(archive) -> str:
        archive.seek(0)
        with tarfile.open(fileobj=archive, mode="r:*") as tar:
            members = {member.name: member for member in tar.getmembers()}
            observed["names"] = set(members)
            runtime = members["workspace/runtime.py"]
            runtime_file = tar.extractfile(runtime)
            assert runtime_file is not None
            observed["runtime"] = runtime_file.read()
        return "ash-plugin-testvolume"

    manager.stage_docker_workspace = AsyncMock(side_effect=stage)
    client = PluginHostClient(plugin, manager, allow_unisolated=False)

    volume = await client._stage_docker_plugin_workspace()

    assert volume == "ash-plugin-testvolume"
    assert observed["names"] == {"workspace", "workspace/runtime.py"}
    assert observed["runtime"] == HOST_SOURCE.encode("utf-8")


@pytest.mark.asyncio
@pytest.mark.skipif(
    not supports_anchored_mutation(),
    reason="descriptor-anchored Docker plugin staging is unavailable",
)
async def test_docker_plugin_staging_rejects_replaced_source_root(
    tmp_path: Path,
) -> None:
    plugin = _plugin(tmp_path / "plugin")
    manager = Mock()
    manager.backend_name = "docker"
    manager.stage_docker_workspace = AsyncMock(return_value="ash-plugin-unused")
    client = PluginHostClient(plugin, manager, allow_unisolated=False)
    saved = tmp_path / "plugin-saved"
    replacement = tmp_path / "replacement"
    replacement.mkdir()
    (replacement / "runtime.py").write_text("print('replacement')\n", encoding="utf-8")
    plugin.root.rename(saved)
    try:
        plugin.root.symlink_to(replacement, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"symlink creation is unavailable: {exc}")

    with pytest.raises(PluginRuntimeError, match="source changed"):
        await client._stage_docker_plugin_workspace()

    manager.stage_docker_workspace.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.skipif(
    not supports_anchored_mutation(),
    reason="descriptor-anchored Docker plugin staging is unavailable",
)
async def test_docker_plugin_runtime_uses_staged_volume_and_cleans_it(
    tmp_path: Path,
) -> None:
    plugin = _plugin(tmp_path / "plugin")
    manager = Mock()
    manager.backend_name = "docker"
    manager.is_fully_isolated.return_value = True
    manager.stage_docker_workspace = AsyncMock(return_value="ash-plugin-testvolume")
    manager.remove_docker_workspace = AsyncMock(return_value=None)
    manager.prepare_docker_volume_launch.return_value = nullcontext(
        SandboxInvocation(
            (sys.executable, "-c", HOST_SOURCE),
            None,
            SANDBOX_TIER_DOCKER,
            "docker",
        )
    )
    client = PluginHostClient(plugin, manager, allow_unisolated=False)

    result = await client.call_tool("echo", {"text": "hello"})

    assert result.success is True
    assert result.output == "hello"
    manager.prepare_docker_volume_launch.assert_called_once_with(
        plugin.manifest.runtime.command,
        workspace_volume="ash-plugin-testvolume",
    )
    await client.aclose()
    manager.remove_docker_workspace.assert_awaited_once_with(
        "ash-plugin-testvolume"
    )
    assert client._docker_workspace_volume is None


@pytest.mark.asyncio
@pytest.mark.skipif(
    not supports_anchored_mutation(),
    reason="descriptor-anchored Docker plugin staging is unavailable",
)
async def test_docker_plugin_volume_cleanup_is_retryable(tmp_path: Path) -> None:
    plugin = _plugin(tmp_path / "plugin")
    manager = Mock()
    manager.backend_name = "docker"
    manager.remove_docker_workspace = AsyncMock(
        side_effect=[SandboxBackendUnavailable("volume busy"), None]
    )
    client = PluginHostClient(plugin, manager, allow_unisolated=False)
    client._closed = True
    client._docker_workspace_volume = "ash-plugin-retryvolume"

    with pytest.raises(PluginRuntimeError, match="Docker workspace cleanup failed"):
        await client.aclose()

    assert client._docker_workspace_volume == "ash-plugin-retryvolume"
    await client.aclose()
    assert client._docker_workspace_volume is None
    assert manager.remove_docker_workspace.await_count == 2


@pytest.mark.asyncio
async def test_docker_plugin_refuses_bind_fallback_when_snapshot_anchors_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plugin = _plugin(tmp_path / "plugin")
    manager = Mock()
    manager.backend_name = "docker"
    manager.is_fully_isolated.return_value = True
    manager.stage_docker_workspace = AsyncMock(return_value="ash-plugin-unused")
    monkeypatch.setattr(
        "ash.plugins.runtime.supports_anchored_mutation",
        lambda: False,
    )
    client = PluginHostClient(plugin, manager, allow_unisolated=False)

    with pytest.raises(
        PluginRuntimeError,
        match="descriptor-anchored Docker plugin staging is unavailable",
    ):
        await client.call_tool("echo", {"text": "must-not-run"})

    manager.stage_docker_workspace.assert_not_awaited()
    manager.prepare_launch.assert_not_called()
    manager.prepare_docker_volume_launch.assert_not_called()
    assert client.running is False
    await client.aclose()


@pytest.mark.asyncio
async def test_plugin_argument_validation_happens_before_start(tmp_path: Path) -> None:
    tool = _tool(
        tmp_path,
        schema={
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
            "additionalProperties": False,
        },
    )

    result = await tool.run(text=42)

    assert result.success is False
    assert "not of type 'string'" in (result.error or "")
    assert tool.client.running is False


@pytest.mark.asyncio
async def test_unisolated_plugin_is_refused_without_explicit_opt_in(
    tmp_path: Path,
) -> None:
    tool = _tool(tmp_path, allow_unisolated=False)

    result = await tool.run(text="blocked")

    assert result.success is False
    assert "ASH_ALLOW_UNSAFE_PLUGIN_RUNTIME=true" in (result.error or "")
    assert tool.client.running is False


@pytest.mark.asyncio
async def test_crashed_plugin_call_is_not_automatically_replayed(
    tmp_path: Path,
) -> None:
    tool = _tool(tmp_path)
    counter = tmp_path / "calls.txt"

    result = await _execute_tool_once(
        tool,
        {"action": "crash", "counter": str(counter)},
    )

    assert result["success"] is False
    assert "host crashed intentionally" in (result["error"] or "")
    assert result["outcome"] == "unknown"
    assert counter.read_text(encoding="utf-8").splitlines() == ["called"]
    assert tool.client.running is False


@pytest.mark.asyncio
async def test_plugin_protocol_version_mismatch_is_rejected(tmp_path: Path) -> None:
    tool = _tool(tmp_path)
    (tool.plugin.root / "runtime.py").write_text(
        """import json, sys
request = json.loads(sys.stdin.readline())
print(json.dumps({"jsonrpc": "2.0", "id": request["id"], "result": {"protocol_version": 2}}), flush=True)
""",
        encoding="utf-8",
    )

    result = await tool.run(text="hello")

    assert result.success is False
    assert "unsupported protocol_version" in (result.error or "")
    assert tool.client.running is False


@pytest.mark.asyncio
async def test_plugin_host_does_not_inherit_ambient_secrets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ASH_TEST_SECRET", "must-not-leak")
    tool = _tool(tmp_path)

    result = await tool.run(action="environment")

    assert result.success is True
    assert result.output == "missing"
    await tool.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "action",
    [
        "malformed",
        "oversized",
        "nonstandard_json",
        "duplicate_json_key",
        "extra_response_field",
        "result_and_error",
    ],
)
async def test_invalid_plugin_protocol_response_fails_closed(
    tmp_path: Path, action: str
) -> None:
    tool = _tool(tmp_path)

    result = await tool.run(action=action)

    assert result.success is False
    assert "plugin" in (result.error or "")
    assert tool.client.running is False


@pytest.mark.asyncio
async def test_plugin_explicit_zero_token_count_is_preserved(tmp_path: Path) -> None:
    tool = _tool(tmp_path)

    result = await tool.run(action="zero_tokens", text="two words")

    assert result.success is True
    assert result.token_count == 0
    await tool.aclose()


@pytest.mark.asyncio
async def test_plugin_timeout_terminates_host(tmp_path: Path) -> None:
    tool = _tool(tmp_path, timeout=0.1)

    result = await tool.run(action="sleep")

    assert result.success is False
    assert "timed out" in (result.error or "")
    assert tool.client.running is False


@pytest.mark.asyncio
async def test_plugin_failure_cleanup_survives_late_cancellation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tool = _tool(tmp_path)
    cleanup_started = asyncio.Event()
    release_cleanup = asyncio.Event()
    cleanup_finished = asyncio.Event()

    async def fail_exchange(*args: object, **kwargs: object) -> object:
        del args, kwargs
        raise PluginRuntimeError("protocol failure")

    async def blocked_cleanup() -> None:
        cleanup_started.set()
        await release_cleanup.wait()
        cleanup_finished.set()

    monkeypatch.setattr(tool.client, "_ensure_started", AsyncMock(return_value=None))
    monkeypatch.setattr(tool.client, "_exchange", fail_exchange)
    monkeypatch.setattr(tool.client, "_discard_process", blocked_cleanup)

    task = asyncio.create_task(tool.client.call_tool("echo", {}))
    await asyncio.wait_for(cleanup_started.wait(), timeout=1)
    task.cancel()
    await asyncio.sleep(0)

    assert not task.done()
    assert not cleanup_finished.is_set()

    release_cleanup.set()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=1)

    assert cleanup_finished.is_set()


@pytest.mark.asyncio
async def test_dry_run_denies_plugin_before_process_start(tmp_path: Path) -> None:
    tool = _tool(tmp_path)
    loop = AshLoop(
        SessionStore(tmp_path / "sessions.db"),
        NoopProvider(),
        SafetyGuard(tmp_path),
        NoopUI(),
        tmp_path,
        tools={tool.name: tool},
        safety_tier="dry_run",
    )
    session = await loop.start_session()

    result = await loop._execute_tool_calls(
        [{"call_id": "plugin-1", "name": tool.name, "arguments": {"text": "no"}}],
        session,
    )

    assert result[0]["success"] is False
    assert "dry-run" in result[0]["error"]
    assert tool.client.running is False
    await loop.aclose()


def test_plugin_schema_is_exposed_exactly_to_provider(tmp_path: Path) -> None:
    schema = {
        "type": "object",
        "properties": {"text": {"type": "string", "minLength": 2}},
        "required": ["text"],
        "additionalProperties": False,
    }
    tool = _tool(tmp_path, schema=schema)
    loop = AshLoop(
        SessionStore(tmp_path / "schema.db"),
        NoopProvider(),
        SafetyGuard(tmp_path),
        NoopUI(),
        tmp_path,
        tools={tool.name: tool},
    )

    encoded = loop._tools_to_openai_format(loop.tools)

    assert encoded[0]["function"]["parameters"] == schema


@pytest.mark.asyncio
async def test_reload_replaces_and_closes_old_plugin_host(tmp_path: Path) -> None:
    old = _tool(tmp_path / "old")
    assert (await old.run(text="old")).success is True
    loop = AshLoop(
        SessionStore(tmp_path / "reload.db"),
        NoopProvider(),
        SafetyGuard(tmp_path),
        NoopUI(),
        tmp_path,
        tools={old.name: old},
    )
    new = _tool(tmp_path / "new")

    await loop.reload_plugin_runtime_tools([new])

    assert old.client.running is False
    assert loop.tools[new.name] is new
    assert (await new.run(text="new")).output == "new"
    await loop.aclose()


@pytest.mark.asyncio
async def test_reload_forgets_lifecycle_ids_for_unpublished_plugin_tools(
    tmp_path: Path,
) -> None:
    guard = SafetyGuard(tmp_path)
    old = _ReloadLifecycleTool(
        guard,
        name="plugin_example__old",
        plugin_runtime_tool=True,
    )
    replacement = _ReloadLifecycleTool(
        guard,
        name="plugin_example__replacement",
        plugin_runtime_tool=True,
    )
    loop = AshLoop(
        SessionStore(tmp_path / "reload-lifecycle-ids.db"),
        NoopProvider(),
        guard,
        NoopUI(),
        tmp_path,
        tools={old.name: old},
    )
    loop._started_tool_ids.add(id(old))
    loop._closed_tool_ids.add(id(old))

    await loop.reload_plugin_runtime_tools([replacement])

    assert id(old) not in loop._started_tool_ids
    assert id(old) not in loop._closed_tool_ids
    assert loop.tools[replacement.name] is replacement
    await loop.aclose()


@pytest.mark.asyncio
async def test_failed_plugin_family_cleanup_forgets_ids_for_unpublished_tools(
    tmp_path: Path,
) -> None:
    guard = SafetyGuard(tmp_path)
    failing = _ReloadLifecycleTool(
        guard,
        name="plugin_example__failing",
        plugin_runtime_tool=True,
        fail_close_times=1,
    )
    closed = _ReloadLifecycleTool(
        guard,
        name="plugin_example__closed",
        plugin_runtime_tool=True,
    )
    loop = AshLoop(
        SessionStore(tmp_path / "reload-failed-family-ids.db"),
        NoopProvider(),
        guard,
        NoopUI(),
        tmp_path,
        tools={failing.name: failing, closed.name: closed},
    )
    loop._started_tool_ids.update({id(failing), id(closed)})

    with pytest.raises(RuntimeError, match="failed to close 1 executable plugin tool"):
        await loop.reload_plugin_runtime_tools([])

    assert failing.name not in loop.tools
    assert closed.name not in loop.tools
    assert id(failing) not in loop._started_tool_ids
    assert id(closed) not in loop._started_tool_ids
    assert id(closed) not in loop._closed_tool_ids
    assert failing in loop._retired_plugin_tools

    await loop.reload_plugin_runtime_tools([])
    assert failing not in loop._retired_plugin_tools
    await loop.aclose()


@pytest.mark.asyncio
async def test_reload_retains_rejected_candidate_until_cleanup_retry(
    tmp_path: Path,
) -> None:
    guard = SafetyGuard(tmp_path)
    occupied = _ReloadLifecycleTool(
        guard,
        name="collision",
        plugin_runtime_tool=False,
    )
    candidate = _ReloadLifecycleTool(
        guard,
        name="collision",
        plugin_runtime_tool=True,
        fail_close_times=1,
    )
    loop = AshLoop(
        SessionStore(tmp_path / "reload-collision.db"),
        NoopProvider(),
        guard,
        NoopUI(),
        tmp_path,
        tools={occupied.name: occupied},
    )

    with pytest.raises(ValueError, match="collides with an existing tool") as exc_info:
        await loop.reload_plugin_runtime_tools([candidate])

    assert candidate.close_calls == 1
    assert candidate in loop._retired_plugin_tools
    assert any("cleanup remains unresolved" in note for note in exc_info.value.__notes__)

    await loop.reload_plugin_runtime_tools([])

    assert candidate.close_calls == 2
    assert candidate not in loop._retired_plugin_tools
    assert loop.tools[occupied.name] is occupied
    await loop.aclose()


@pytest.mark.asyncio
async def test_reload_closes_all_duplicate_candidates_and_retains_cleanup_failures(
    tmp_path: Path,
) -> None:
    guard = SafetyGuard(tmp_path)
    first = _ReloadLifecycleTool(
        guard,
        name="plugin_duplicate__echo",
        plugin_runtime_tool=True,
        fail_close_times=1,
    )
    second = _ReloadLifecycleTool(
        guard,
        name="plugin_duplicate__echo",
        plugin_runtime_tool=True,
    )
    loop = AshLoop(
        SessionStore(tmp_path / "reload-duplicate.db"),
        NoopProvider(),
        guard,
        NoopUI(),
        tmp_path,
        tools={},
    )

    with pytest.raises(ValueError, match="duplicate executable plugin tool name"):
        await loop.reload_plugin_runtime_tools([first, second])

    assert first.close_calls == 1
    assert second.close_calls == 1
    assert first in loop._retired_plugin_tools
    assert second not in loop._retired_plugin_tools

    await loop.reload_plugin_runtime_tools([])

    assert first.close_calls == 2
    assert not loop._retired_plugin_tools
    await loop.aclose()


@pytest.mark.asyncio
async def test_reload_refuses_active_turn_and_closes_unpublished_candidate(
    tmp_path: Path,
) -> None:
    guard = SafetyGuard(tmp_path)
    old = _ReloadLifecycleTool(
        guard,
        name="plugin_example__echo",
        plugin_runtime_tool=True,
    )
    candidate = _ReloadLifecycleTool(
        guard,
        name="plugin_example__echo",
        plugin_runtime_tool=True,
    )
    loop = AshLoop(
        SessionStore(tmp_path / "reload-active-turn.db"),
        NoopProvider(),
        guard,
        NoopUI(),
        tmp_path,
        tools={old.name: old},
    )
    loop._turn_running = True

    with pytest.raises(RuntimeError, match="while a turn is running"):
        await loop.reload_plugin_runtime_tools([candidate])

    assert candidate.close_calls == 1
    assert loop.tools[old.name] is old
    assert old.close_calls == 0
    loop._turn_running = False
    await loop.aclose()


@pytest.mark.asyncio
async def test_plugin_reload_refuses_replaced_workspace_and_closes_candidate(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    saved = tmp_path / "workspace-original"
    replacement = tmp_path / "workspace-replacement"
    workspace.mkdir()
    replacement.mkdir()
    guard = SafetyGuard(workspace)
    loop = AshLoop(
        SessionStore(tmp_path / "reload-root-swap.db"),
        NoopProvider(),
        guard,
        NoopUI(),
        workspace,
        tools={},
    )
    candidate = _ReloadLifecycleTool(
        guard,
        name="plugin_example__echo",
        plugin_runtime_tool=True,
    )

    workspace.rename(saved)
    replacement.rename(workspace)

    with pytest.raises(RuntimeError, match="workspace root changed after runtime startup"):
        await loop.reload_plugin_runtime_tools([candidate])

    assert candidate.close_calls == 1
    assert candidate.name not in loop.tools
    await loop.aclose()


@pytest.mark.asyncio
async def test_overlapping_plugin_reloads_serialize_lifecycle_replacement(
    tmp_path: Path,
) -> None:
    guard = SafetyGuard(tmp_path)
    close_started = asyncio.Event()
    allow_close = asyncio.Event()

    class BlockingCloseTool(_ReloadLifecycleTool):
        async def aclose(self) -> None:
            self.close_calls += 1
            close_started.set()
            await allow_close.wait()

    old = BlockingCloseTool(
        guard,
        name="plugin_example__echo",
        plugin_runtime_tool=True,
    )
    first = _ReloadLifecycleTool(
        guard,
        name="plugin_example__echo",
        plugin_runtime_tool=True,
    )
    second = _ReloadLifecycleTool(
        guard,
        name="plugin_example__echo",
        plugin_runtime_tool=True,
    )
    loop = AshLoop(
        SessionStore(tmp_path / "reload-overlap.db"),
        NoopProvider(),
        guard,
        NoopUI(),
        tmp_path,
        tools={old.name: old},
    )

    first_reload = asyncio.create_task(loop.reload_plugin_runtime_tools([first]))
    await asyncio.wait_for(close_started.wait(), timeout=1)
    second_reload = asyncio.create_task(loop.reload_plugin_runtime_tools([second]))
    await asyncio.sleep(0)

    assert not second_reload.done()
    assert first.close_calls == 0
    assert second.close_calls == 0

    allow_close.set()
    await first_reload
    await second_reload

    assert old.close_calls == 1
    assert first.close_calls == 1
    assert loop.tools[second.name] is second
    await loop.aclose()


@pytest.mark.asyncio
async def test_reload_retries_failed_candidate_cleanup_before_replacing_old_host(
    tmp_path: Path,
) -> None:
    guard = SafetyGuard(tmp_path)
    old = _ReloadLifecycleTool(
        guard,
        name="plugin_example__echo",
        plugin_runtime_tool=True,
        fail_close_times=1,
    )
    candidate = _ReloadLifecycleTool(
        guard,
        name="plugin_example__echo",
        plugin_runtime_tool=True,
        fail_close_times=1,
    )
    loop = AshLoop(
        SessionStore(tmp_path / "reload-retry.db"),
        NoopProvider(),
        guard,
        NoopUI(),
        tmp_path,
        tools={old.name: old},
    )

    with pytest.raises(RuntimeError, match="failed to close 1 executable plugin tool"):
        await loop.reload_plugin_runtime_tools([candidate])

    assert old.name not in loop.tools
    assert old.close_calls == 1
    assert candidate.close_calls == 1
    assert old in loop._retired_plugin_tools
    assert candidate in loop._retired_plugin_tools

    replacement = _ReloadLifecycleTool(
        guard,
        name="plugin_example__echo",
        plugin_runtime_tool=True,
    )
    await loop.reload_plugin_runtime_tools([replacement])

    assert candidate.close_calls == 2
    assert old.close_calls == 2
    assert not loop._retired_plugin_tools
    assert loop.tools[replacement.name] is replacement
    await loop.aclose()


@pytest.mark.asyncio
async def test_failed_old_plugin_cleanup_prunes_deferred_tool_activation(
    tmp_path: Path,
) -> None:
    guard = SafetyGuard(tmp_path)
    old = _ReloadLifecycleTool(
        guard,
        name="plugin_example__echo",
        plugin_runtime_tool=True,
        fail_close_times=1,
    )
    search = SearchToolsTool(guard, lambda: {}, threshold=1)
    loop = AshLoop(
        SessionStore(tmp_path / "reload-search-prune.db"),
        NoopProvider(),
        guard,
        NoopUI(),
        tmp_path,
        tools={search.name: search, old.name: old},
    )
    search.activated_names.add(old.name)
    candidate = _ReloadLifecycleTool(
        guard,
        name=old.name,
        plugin_runtime_tool=True,
    )

    with pytest.raises(RuntimeError, match="failed to close 1 executable plugin tool"):
        await loop.reload_plugin_runtime_tools([candidate])

    assert old.name not in loop.tools
    assert old.name not in search.activated_names
    assert old in loop._retired_plugin_tools
    await loop.aclose()


@pytest.mark.asyncio
async def test_successful_plugin_tool_removal_prunes_deferred_activation(
    tmp_path: Path,
) -> None:
    guard = SafetyGuard(tmp_path)
    old = _ReloadLifecycleTool(
        guard,
        name="plugin_example__echo",
        plugin_runtime_tool=True,
    )
    search = SearchToolsTool(guard, lambda: {}, threshold=1)
    loop = AshLoop(
        SessionStore(tmp_path / "reload-search-success.db"),
        NoopProvider(),
        guard,
        NoopUI(),
        tmp_path,
        tools={search.name: search, old.name: old},
    )
    search.activated_names.add(old.name)

    await loop.reload_plugin_runtime_tools([])

    assert old.close_calls == 1
    assert old.name not in loop.tools
    assert old.name not in search.activated_names
    await loop.aclose()


@pytest.mark.asyncio
async def test_shutdown_retries_retired_plugin_candidate_cleanup(
    tmp_path: Path,
) -> None:
    guard = SafetyGuard(tmp_path)
    occupied = _ReloadLifecycleTool(
        guard,
        name="collision",
        plugin_runtime_tool=False,
    )
    candidate = _ReloadLifecycleTool(
        guard,
        name="collision",
        plugin_runtime_tool=True,
        fail_close_times=2,
    )
    loop = AshLoop(
        SessionStore(tmp_path / "reload-shutdown.db"),
        NoopProvider(),
        guard,
        NoopUI(),
        tmp_path,
        tools={occupied.name: occupied},
    )
    with pytest.raises(ValueError, match="collides with an existing tool"):
        await loop.reload_plugin_runtime_tools([candidate])

    with pytest.raises(RuntimeError, match="retired plugin tool"):
        await loop.aclose()

    assert candidate.close_calls == 2
    assert candidate in loop._retired_plugin_tools

    await loop.aclose()

    assert candidate.close_calls == 3
    assert not loop._retired_plugin_tools


def test_runtime_tool_names_are_portable_and_collisions_are_rejected(
    tmp_path: Path,
) -> None:
    assert plugin_tool_name("2.cool-plugin", "do-work") == (
        "plugin_13_2_dot_cool-plugin__do-work"
    )
    first = _plugin(tmp_path / "one")
    second = DiscoveredPlugin(first.manifest, tmp_path / "two", "test")

    with pytest.raises(ValueError, match="duplicate executable plugin tool"):
        build_plugin_runtime_tools(
            [first, second],
            SafetyGuard(tmp_path),
            backend_preference="direct",
            docker_image="ash-sandbox:latest",
            allow_unisolated=True,
        )


@pytest.mark.asyncio
async def test_standard_runtime_assembly_executes_through_loop_and_persists(
    tmp_path: Path,
) -> None:
    plugin = _plugin(tmp_path / "plugin")
    config = AshConfig(
        model="ollama/test",
        workspace_root=tmp_path,
        sandbox_backend="direct",
        sandbox_docker_memory_mb=1536,
        sandbox_docker_cpus=1.25,
        allow_unsafe_plugin_runtime=True,
    )

    tools = build_tools(
        SafetyGuard(tmp_path),
        tmp_path,
        runtime_config=config,
        active_plugins=[plugin],
    )

    tool_name = "plugin_14_example-plugin__echo"
    assert tool_name in tools
    plugin_tool = tools[tool_name]
    assert isinstance(plugin_tool, PluginRuntimeTool)
    assert plugin_tool.client.sandbox_manager.workspace_read_only is True
    assert plugin_tool.client.sandbox_manager.require_read_isolation is True
    assert plugin_tool.client.sandbox_manager.docker_memory_mb == 1536
    assert plugin_tool.client.sandbox_manager.docker_cpus == 1.25
    store = SessionStore(tmp_path / "assembled.db")
    loop = AshLoop(
        store,
        NoopProvider(),
        SafetyGuard(tmp_path),
        NoopUI(),
        tmp_path,
        tools=tools,
    )
    session = await loop.start_session()

    results = await loop._execute_tool_calls(
        [
            {
                "call_id": "assembled-plugin-call",
                "name": tool_name,
                "arguments": {"text": "through-loop"},
            }
        ],
        session,
    )

    assert results[0]["output"] == "through-loop"
    persisted = store.load_session(session.session_id).tool_calls
    assert persisted[0].approved is True
    assert persisted[0].executed is True
    assert persisted[0].result == "through-loop"
    await loop.aclose()
