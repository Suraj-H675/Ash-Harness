"""Versioned, isolated process boundary for executable plugin tools."""

from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
from collections import deque
from contextlib import AbstractContextManager, nullcontext, suppress
from pathlib import Path
from typing import Any, BinaryIO, cast

from jsonschema import Draft202012Validator  # type: ignore[import-untyped]
from jsonschema.exceptions import ValidationError  # type: ignore[import-untyped]

from ash.safe_io import strict_json_loads
from ash.plugins.manifest import (
    PLUGIN_RUNTIME_PROTOCOL_VERSION,
    PluginToolManifest,
    namespaced_plugin_tool_name,
)
from ash.plugins.anchored_fs import (
    AnchoredDirectory,
    AnchoredFilesystemError,
    supports_anchored_mutation,
)
from ash.plugins.registry import (
    MAX_PLUGIN_TREE_DEPTH,
    MAX_PLUGIN_TREE_ENTRIES,
    DiscoveredPlugin,
)
from ash.plugins.snapshot import (
    MAX_PLUGIN_BYTES,
    MAX_PLUGIN_FILES,
    PluginSnapshot,
    PluginSnapshotError,
)
from ash.safety.guard import SafetyGuard, SafetyViolation
from ash.safety.scoped_io import ScopedIOError
from ash.sandbox import SandboxBackendUnavailable, SandboxManager
from ash.sandbox.process_utils import (
    ProcessTreeError,
    ProcessTreePlan,
    ProcessTreeUnavailable,
    ScopedProcessLaunch,
    prepare_process_tree,
    prepare_scoped_process_launch,
    terminate_process_tree,
)
from ash.tools.base import (
    BaseTool,
    ToolExecutionOutcome,
    ToolResult,
    count_output_tokens,
)

MAX_PLUGIN_MESSAGE_BYTES = 1024 * 1024
MAX_PLUGIN_STDERR_BYTES = 64 * 1024
MAX_PLUGIN_RESULT_TEXT_BYTES = 768 * 1024


class PluginRuntimeError(RuntimeError):
    """An executable plugin violated or could not fulfill the runtime contract."""


async def _settle_task_after_cancellation(
    task: asyncio.Task[Any],
) -> tuple[BaseException | None, bool]:
    """Wait for a cleanup task while preserving caller cancellation semantics."""

    cancelled = False
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            cancelled = True
            current = asyncio.current_task()
            if current is not None:
                current.uncancel()
    try:
        task.result()
    except BaseException as exc:
        return exc, cancelled
    return None, cancelled


def plugin_tool_name(plugin_name: str, tool_name: str) -> str:
    """Build a deterministic provider-portable name for one plugin tool."""

    return namespaced_plugin_tool_name(plugin_name, tool_name)


class PluginHostClient:
    """Own one lazily started JSON-RPC subprocess for a discovered plugin."""

    def __init__(
        self,
        plugin: DiscoveredPlugin,
        sandbox_manager: SandboxManager,
        *,
        allow_unisolated: bool = False,
    ) -> None:
        if plugin.manifest.runtime is None:
            raise ValueError("plugin has no executable runtime")
        self.plugin = plugin
        try:
            self._plugin_root_metadata: os.stat_result | None = plugin.root.stat()
        except OSError:
            self._plugin_root_metadata = None
        self.runtime = plugin.manifest.runtime
        self.sandbox_manager = sandbox_manager
        self.allow_unisolated = allow_unisolated
        self._process: asyncio.subprocess.Process | None = None
        self._process_tree_plan: ProcessTreePlan | None = None
        self._stderr_task: asyncio.Task[None] | None = None
        self._stderr_chunks: deque[bytes] = deque()
        self._stderr_size = 0
        self._next_id = 1
        self._lock = asyncio.Lock()
        self._closed = False
        self._docker_workspace_volume: str | None = None

    @property
    def running(self) -> bool:
        return self._process is not None and self._process.returncode is None

    @property
    def stderr_tail(self) -> str:
        return b"".join(self._stderr_chunks).decode("utf-8", errors="replace")

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> ToolResult:
        """Execute exactly one tool request without automatic replay."""

        async with self._lock:
            if self._closed:
                raise PluginRuntimeError("plugin runtime is closed")
            try:
                await self._ensure_started()
                raw = await self._exchange(
                    "tool/call",
                    {"name": name, "arguments": arguments},
                    timeout=self.runtime.timeout_seconds,
                )
                return _parse_tool_result(raw)
            except asyncio.CancelledError as cancellation:
                cleanup_task = asyncio.create_task(self._discard_process())
                cleanup_error, _ = await _settle_task_after_cancellation(cleanup_task)
                if cleanup_error is not None:
                    cancellation.add_note(
                        f"Process-tree cleanup failed: {cleanup_error}"
                    )
                raise
            except PluginRuntimeError as primary:
                try:
                    await self._discard_process()
                except PluginRuntimeError as cleanup_error:
                    primary.add_note(f"Process-tree cleanup failed: {cleanup_error}")
                raise
            except Exception as primary:  # noqa: BLE001
                try:
                    await self._discard_process()
                except PluginRuntimeError as cleanup_error:
                    primary.add_note(f"Process-tree cleanup failed: {cleanup_error}")
                raise PluginRuntimeError(str(primary)) from primary

    async def aclose(self) -> None:
        """Stop the shared host and all of its descendants, idempotently."""

        async with self._lock:
            if (
                self._closed
                and self._process is None
                and self._docker_workspace_volume is None
            ):
                return
            was_closed = self._closed
            self._closed = True
            process = self._process
            cancelled = False
            if not was_closed and process is not None and process.returncode is None:
                try:
                    await self._exchange("shutdown", {}, timeout=1.0)
                except asyncio.CancelledError:
                    cancelled = True
                    current = asyncio.current_task()
                    if current is not None:
                        current.uncancel()
                except Exception:
                    pass
            cleanup_task = asyncio.create_task(self._discard_process())
            cleanup_error, cleanup_cancelled = await _settle_task_after_cancellation(
                cleanup_task
            )
            cancelled = cancelled or cleanup_cancelled
            if cleanup_error is not None:
                if cancelled:
                    cancellation = asyncio.CancelledError()
                    cancellation.add_note(
                        f"Process-tree cleanup failed: {cleanup_error}"
                    )
                    raise cancellation from cleanup_error
                raise cleanup_error
            if cancelled:
                raise asyncio.CancelledError

    async def _ensure_started(self) -> None:
        if self.running:
            return
        if not self.sandbox_manager.is_fully_isolated() and not self.allow_unisolated:
            raise PluginRuntimeError(
                "executable plugin refused: no OS sandbox is available; install a "
                "supported sandbox or explicitly set "
                "ASH_ALLOW_UNSAFE_PLUGIN_RUNTIME=true"
            )
        self._stderr_chunks.clear()
        self._stderr_size = 0
        env = _plugin_environment()
        try:
            if (
                self.sandbox_manager.backend_name == "docker"
                and supports_anchored_mutation()
            ):
                self._docker_workspace_volume = (
                    await self._stage_docker_plugin_workspace()
                )
                invocation_context = self.sandbox_manager.prepare_docker_volume_launch(
                    self.runtime.command,
                    workspace_volume=self._docker_workspace_volume,
                )
            else:
                invocation_context = self.sandbox_manager.prepare_launch(
                    self.runtime.command,
                    cwd=self.plugin.root,
                )
            with invocation_context as invocation:
                try:
                    process_tree_plan = prepare_process_tree(
                        workspace_root=self.plugin.root
                    )
                except ProcessTreeUnavailable as exc:
                    raise PluginRuntimeError(
                        f"plugin process was not started: {exc}"
                    ) from exc
                self._process_tree_plan = process_tree_plan
                launch_context: AbstractContextManager[ScopedProcessLaunch]
                if invocation.cwd is None:
                    launch_context = nullcontext(
                        ScopedProcessLaunch(tuple(invocation.argv), None)
                    )
                else:
                    expected_identity = (
                        None
                        if self._plugin_root_metadata is None
                        else (
                            self._plugin_root_metadata.st_dev,
                            self._plugin_root_metadata.st_ino,
                        )
                    )
                    launch_context = prepare_scoped_process_launch(
                        invocation.argv,
                        cwd=invocation.cwd,
                        guard=SafetyGuard(self.plugin.root),
                        search_path=env.get("PATH"),
                        expected_cwd_identity=expected_identity,
                    )
                spawn_options = dict(process_tree_plan.spawn_options)
                with launch_context as launch:
                    inherited_fds = tuple(
                        dict.fromkeys((*invocation.pass_fds, *launch.pass_fds))
                    )
                    if inherited_fds:
                        spawn_options["pass_fds"] = inherited_fds
                    self._process = await asyncio.create_subprocess_exec(
                        *launch.argv,
                        cwd=launch.cwd,
                        stdin=asyncio.subprocess.PIPE,
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.PIPE,
                        env=env,
                        limit=MAX_PLUGIN_MESSAGE_BYTES + 1,
                        **spawn_options,
                    )
        except SandboxBackendUnavailable as exc:
            self._process = None
            self._process_tree_plan = None
            raise PluginRuntimeError(f"plugin sandbox unavailable: {exc}") from exc
        except (ProcessTreeUnavailable, SafetyViolation, ScopedIOError) as exc:
            self._process = None
            self._process_tree_plan = None
            raise PluginRuntimeError(
                f"plugin process was not started: {exc}"
            ) from exc
        except OSError as exc:
            self._process = None
            self._process_tree_plan = None
            raise PluginRuntimeError(f"cannot start plugin runtime: {exc}") from exc
        self._stderr_task = asyncio.create_task(self._drain_stderr())
        try:
            initialized = await self._exchange(
                "initialize",
                {
                    "protocol_version": PLUGIN_RUNTIME_PROTOCOL_VERSION,
                    "plugin": {
                        "name": self.plugin.manifest.name,
                        "version": self.plugin.manifest.version,
                    },
                },
                timeout=self.runtime.timeout_seconds,
            )
        except Exception:
            await self._discard_process()
            raise
        if (
            not isinstance(initialized, dict)
            or initialized.get("protocol_version") != PLUGIN_RUNTIME_PROTOCOL_VERSION
        ):
            await self._discard_process()
            raise PluginRuntimeError(
                "plugin initialize response has an unsupported protocol_version"
            )

    async def _stage_docker_plugin_workspace(self) -> str:
        """Capture the discovered plugin inode and stage immutable bytes in Docker."""

        if self._plugin_root_metadata is None:
            raise PluginRuntimeError(
                "plugin source is unavailable for Docker staging"
            )
        try:
            with AnchoredDirectory.open(
                self.plugin.root,
                create=False,
                private=False,
                expected=self._plugin_root_metadata,
            ) as source:
                snapshot = PluginSnapshot.capture(
                    source,
                    max_files=MAX_PLUGIN_FILES,
                    max_bytes=MAX_PLUGIN_BYTES,
                    max_entries=MAX_PLUGIN_TREE_ENTRIES,
                    max_depth=MAX_PLUGIN_TREE_DEPTH,
                )
        except (AnchoredFilesystemError, OSError, PluginSnapshotError) as exc:
            raise PluginRuntimeError(
                f"plugin source changed before Docker staging: {exc}"
            ) from exc
        try:
            with tempfile.TemporaryFile(prefix="ash-plugin-runtime-") as archive:
                getuid: Any = getattr(os, "getuid", None)
                getgid: Any = getattr(os, "getgid", None)
                uid = int(getuid()) if sys.platform != "win32" and callable(getuid) else 0
                gid = int(getgid()) if sys.platform != "win32" and callable(getgid) else 0
                archive_stream = cast(BinaryIO, archive)
                snapshot.write_tar(archive_stream, uid=uid, gid=gid)
                archive_stream.flush()
                return await self.sandbox_manager.stage_docker_workspace(archive_stream)
        except (OSError, PluginSnapshotError, SandboxBackendUnavailable) as exc:
            raise PluginRuntimeError(f"plugin Docker staging failed: {exc}") from exc
        finally:
            snapshot.close()

    async def _exchange(
        self,
        method: str,
        params: dict[str, Any],
        *,
        timeout: float,
    ) -> Any:
        process = self._process
        if process is None or process.stdin is None or process.stdout is None:
            raise PluginRuntimeError("plugin runtime is not running")
        stdin = process.stdin
        stdout = process.stdout
        request_id = self._next_id
        self._next_id += 1
        try:
            encoded = (
                json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": request_id,
                        "method": method,
                        "params": params,
                    },
                    separators=(",", ":"),
                    ensure_ascii=True,
                    allow_nan=False,
                ).encode("utf-8")
                + b"\n"
            )
        except (TypeError, ValueError) as exc:
            raise PluginRuntimeError(
                f"plugin request is not JSON serializable: {exc}"
            ) from exc
        if len(encoded) > MAX_PLUGIN_MESSAGE_BYTES:
            raise PluginRuntimeError("plugin request exceeds the 1 MiB protocol limit")

        async def transact() -> Any:
            try:
                stdin.write(encoded)
                await stdin.drain()
            except (BrokenPipeError, ConnectionResetError) as exc:
                raise self._exited_error("plugin stdin closed") from exc
            try:
                line = await stdout.readline()
            except (ValueError, asyncio.LimitOverrunError) as exc:
                raise PluginRuntimeError(
                    "plugin response exceeds the 1 MiB protocol limit"
                ) from exc
            if not line:
                while process.returncode is None:
                    await asyncio.sleep(0.01)
                if self._stderr_task is not None:
                    await self._stderr_task
                raise self._exited_error("plugin stdout closed")
            if len(line) > MAX_PLUGIN_MESSAGE_BYTES:
                raise PluginRuntimeError(
                    "plugin response exceeds the 1 MiB protocol limit"
                )
            try:
                response = strict_json_loads(line)
            except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
                raise PluginRuntimeError("plugin returned malformed JSON") from exc
            if not isinstance(response, dict) or response.get("jsonrpc") != "2.0":
                raise PluginRuntimeError("plugin returned an invalid JSON-RPC response")
            unknown_fields = response.keys() - {"jsonrpc", "id", "result", "error"}
            if unknown_fields:
                raise PluginRuntimeError(
                    "plugin JSON-RPC response contains unsupported fields: "
                    + ", ".join(sorted(unknown_fields))
                )
            response_id = response.get("id")
            if type(response_id) is not int or response_id != request_id:
                raise PluginRuntimeError("plugin returned a mismatched JSON-RPC id")
            has_error = "error" in response
            has_result = "result" in response
            if has_error == has_result:
                raise PluginRuntimeError(
                    "plugin response must contain exactly one of result or error"
                )
            if has_error:
                error = response["error"]
                message = error.get("message") if isinstance(error, dict) else None
                if not isinstance(message, str) or not message:
                    raise PluginRuntimeError(
                        "plugin returned an invalid JSON-RPC error"
                    )
                raise PluginRuntimeError(message[:4096])
            return response["result"]

        try:
            return await asyncio.wait_for(transact(), timeout=timeout)
        except asyncio.TimeoutError as exc:
            raise PluginRuntimeError(
                f"plugin {method} timed out after {timeout:g} seconds"
            ) from exc

    async def _drain_stderr(self) -> None:
        process = self._process
        if process is None or process.stderr is None:
            return
        while chunk := await process.stderr.read(4096):
            self._stderr_chunks.append(chunk)
            self._stderr_size += len(chunk)
            while self._stderr_size > MAX_PLUGIN_STDERR_BYTES and self._stderr_chunks:
                removed = self._stderr_chunks.popleft()
                self._stderr_size -= len(removed)

    def _exited_error(self, prefix: str) -> PluginRuntimeError:
        process = self._process
        code = process.returncode if process is not None else None
        stderr = self.stderr_tail.strip()
        detail = f" (exit code {code})" if code is not None else ""
        if stderr:
            detail += f": {stderr[-4096:]}"
        return PluginRuntimeError(prefix + detail)

    async def _discard_process(self) -> None:
        process = self._process
        plan = self._process_tree_plan
        cleanup_error: ProcessTreeError | None = None
        if process is not None:
            if process.stdin is not None:
                process.stdin.close()
            try:
                await terminate_process_tree(process, plan=plan)
            except ProcessTreeError as exc:
                cleanup_error = exc
            if process.stdin is not None:
                with suppress(BrokenPipeError, ConnectionResetError):
                    await process.stdin.wait_closed()
        if cleanup_error is None:
            self._process = None
            self._process_tree_plan = None
        task = self._stderr_task
        self._stderr_task = None
        if task is not None:
            if not task.done():
                task.cancel()
            with suppress(asyncio.CancelledError):
                await task
        volume_error: BaseException | None = None
        volume = self._docker_workspace_volume
        if cleanup_error is None and volume is not None:
            try:
                await self.sandbox_manager.remove_docker_workspace(volume)
            except BaseException as exc:
                volume_error = exc
            else:
                self._docker_workspace_volume = None
        if cleanup_error is not None or volume_error is not None:
            self._closed = True
            details: list[str] = []
            if cleanup_error is not None:
                details.append(f"process-tree cleanup failed: {cleanup_error}")
            if volume_error is not None:
                details.append(f"Docker workspace cleanup failed: {volume_error}")
            error = PluginRuntimeError("plugin cleanup failed: " + "; ".join(details))
            raise error from (cleanup_error or volume_error)


class PluginRuntimeTool(BaseTool):
    """Ash tool proxy backed by a shared executable plugin host."""

    args_schema = None
    plugin_runtime_tool = True

    def __init__(
        self,
        safety_guard: SafetyGuard,
        plugin: DiscoveredPlugin,
        declaration: PluginToolManifest,
        client: PluginHostClient,
    ) -> None:
        super().__init__(safety_guard)
        self.plugin = plugin
        self.declaration = declaration
        self.client = client
        self.name = plugin_tool_name(plugin.manifest.name, declaration.name)
        self.description = declaration.description
        self._validator = Draft202012Validator(declaration.input_schema)

    def json_schema(self) -> dict[str, Any]:
        return self.declaration.input_schema

    async def run(self, **kwargs: Any) -> ToolResult:
        try:
            self._validator.validate(kwargs)
        except ValidationError as exc:
            return ToolResult(
                success=False,
                output="",
                error=f"invalid plugin tool arguments: {exc.message}",
            )
        try:
            return await self.client.call_tool(self.declaration.name, kwargs)
        except PluginRuntimeError as exc:
            return ToolResult(
                success=False,
                output="",
                error=_plugin_error_text(exc),
                outcome=ToolExecutionOutcome.UNKNOWN,
            )

    async def aclose(self) -> None:
        await self.client.aclose()


def build_plugin_runtime_tools(
    plugins: list[DiscoveredPlugin],
    safety_guard: SafetyGuard,
    *,
    backend_preference: str,
    docker_image: str,
    allow_unisolated: bool,
) -> list[PluginRuntimeTool]:
    """Create lazy proxies without executing plugin code during discovery."""

    tools: list[PluginRuntimeTool] = []
    seen: set[str] = set()
    for plugin in plugins:
        if plugin.manifest.runtime is None:
            continue
        manager = SandboxManager(
            workspace_root=plugin.root,
            workspace_read_only=True,
            require_read_isolation=True,
            network=False,
            timeout_seconds=max(1, int(plugin.manifest.runtime.timeout_seconds)),
            backend_preference=backend_preference,
            docker_image=docker_image,
        )
        client = PluginHostClient(
            plugin,
            manager,
            allow_unisolated=allow_unisolated,
        )
        for declaration in plugin.manifest.tools:
            tool = PluginRuntimeTool(safety_guard, plugin, declaration, client)
            if tool.name in seen:
                raise ValueError(f"duplicate executable plugin tool name: {tool.name}")
            seen.add(tool.name)
            tools.append(tool)
    return tools


def _plugin_error_text(error: BaseException) -> str:
    parts = [str(error)]
    parts.extend(str(note) for note in getattr(error, "__notes__", ()))
    return "; ".join(part for part in parts if part)[:MAX_PLUGIN_RESULT_TEXT_BYTES]


def _parse_tool_result(value: Any) -> ToolResult:
    if not isinstance(value, dict):
        raise PluginRuntimeError("plugin tool result must be an object")
    allowed = {"success", "output", "error", "token_count", "truncated"}
    unknown = value.keys() - allowed
    if unknown:
        raise PluginRuntimeError(
            "plugin tool result contains unsupported fields: "
            + ", ".join(sorted(unknown))
        )
    success = value.get("success")
    output = value.get("output")
    error = value.get("error")
    token_count = value.get("token_count")
    truncated = value.get("truncated", False)
    if not isinstance(success, bool) or not isinstance(output, str):
        raise PluginRuntimeError(
            "plugin result requires boolean success and string output"
        )
    if error is not None and not isinstance(error, str):
        raise PluginRuntimeError("plugin result error must be a string or null")
    if "token_count" in value and (
        not isinstance(token_count, int)
        or isinstance(token_count, bool)
        or token_count < 0
    ):
        raise PluginRuntimeError(
            "plugin result token_count must be a non-negative integer"
        )
    if not isinstance(truncated, bool):
        raise PluginRuntimeError("plugin result truncated must be a boolean")
    if len(output.encode("utf-8")) > MAX_PLUGIN_RESULT_TEXT_BYTES:
        raise PluginRuntimeError("plugin result output exceeds 768 KiB")
    if error is not None and len(error.encode("utf-8")) > 64 * 1024:
        raise PluginRuntimeError("plugin result error exceeds 64 KiB")
    return ToolResult(
        success=success,
        output=output,
        error=error,
        token_count=(
            token_count if isinstance(token_count, int) else count_output_tokens(output)
        ),
        truncated=truncated,
    )


def _plugin_environment() -> dict[str, str]:
    executable_dir = str(Path(sys.executable).resolve().parent)
    paths = list(dict.fromkeys((executable_dir, "/usr/local/bin", "/usr/bin", "/bin")))
    return {
        "PATH": os.pathsep.join(paths),
        "HOME": "/tmp",
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "PYTHONIOENCODING": "utf-8",
        "PYTHONUNBUFFERED": "1",
    }
