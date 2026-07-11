"""Versioned, isolated process boundary for executable plugin tools."""

from __future__ import annotations

import asyncio
import json
import os
import sys
from collections import deque
from contextlib import suppress
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator  # type: ignore[import-untyped]
from jsonschema.exceptions import ValidationError  # type: ignore[import-untyped]

from plugins.manifest import (
    PLUGIN_RUNTIME_PROTOCOL_VERSION,
    PluginToolManifest,
    namespaced_plugin_tool_name,
)
from plugins.registry import DiscoveredPlugin
from safety.guard import SafetyGuard
from sandbox import SandboxBackendUnavailable, SandboxManager
from sandbox.process_utils import process_group_options, terminate_process_tree
from tools.base import BaseTool, ToolResult, count_output_tokens

MAX_PLUGIN_MESSAGE_BYTES = 1024 * 1024
MAX_PLUGIN_STDERR_BYTES = 64 * 1024
MAX_PLUGIN_RESULT_TEXT_BYTES = 768 * 1024


class PluginRuntimeError(RuntimeError):
    """An executable plugin violated or could not fulfill the runtime contract."""


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
        self.runtime = plugin.manifest.runtime
        self.sandbox_manager = sandbox_manager
        self.allow_unisolated = allow_unisolated
        self._process: asyncio.subprocess.Process | None = None
        self._stderr_task: asyncio.Task[None] | None = None
        self._stderr_chunks: deque[bytes] = deque()
        self._stderr_size = 0
        self._next_id = 1
        self._lock = asyncio.Lock()
        self._closed = False

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
            except asyncio.CancelledError:
                await self._discard_process()
                raise
            except PluginRuntimeError:
                await self._discard_process()
                raise
            except Exception as exc:  # noqa: BLE001
                await self._discard_process()
                raise PluginRuntimeError(str(exc)) from exc

    async def aclose(self) -> None:
        """Stop the shared host and all of its descendants, idempotently."""

        async with self._lock:
            if self._closed:
                return
            self._closed = True
            process = self._process
            if process is not None and process.returncode is None:
                with suppress(Exception):
                    await self._exchange("shutdown", {}, timeout=1.0)
            await self._discard_process()

    async def _ensure_started(self) -> None:
        if self.running:
            return
        if not self.sandbox_manager.is_fully_isolated() and not self.allow_unisolated:
            raise PluginRuntimeError(
                "executable plugin refused: no OS sandbox is available; install a "
                "supported sandbox or explicitly set "
                "ASH_ALLOW_UNSAFE_PLUGIN_RUNTIME=true"
            )
        try:
            invocation = self.sandbox_manager.prepare(
                self.runtime.command,
                cwd=self.plugin.root,
            )
        except SandboxBackendUnavailable as exc:
            raise PluginRuntimeError(f"plugin sandbox unavailable: {exc}") from exc
        self._stderr_chunks.clear()
        self._stderr_size = 0
        env = _plugin_environment()
        try:
            self._process = await asyncio.create_subprocess_exec(
                *invocation.argv,
                cwd=invocation.cwd,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
                limit=MAX_PLUGIN_MESSAGE_BYTES + 1,
                **process_group_options(),
            )
        except OSError as exc:
            self._process = None
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
                response = json.loads(line, parse_constant=_reject_json_constant)
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
        self._process = None
        if process is not None:
            if process.stdin is not None:
                process.stdin.close()
            await terminate_process_tree(process)
            if process.stdin is not None:
                with suppress(BrokenPipeError, ConnectionResetError):
                    await process.stdin.wait_closed()
        task = self._stderr_task
        self._stderr_task = None
        if task is not None:
            if not task.done():
                task.cancel()
            with suppress(asyncio.CancelledError):
                await task


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
            return ToolResult(success=False, output="", error=str(exc))

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


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"non-standard JSON constant: {value}")
