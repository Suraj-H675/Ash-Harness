"""Bounded asynchronous Language Server Protocol 3.18 stdio client."""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ash.lsp.config import LSPServerConfig
from ash.core.redaction import redact_text
from ash.safety.environment import build_scrubbed_environment
from ash.safety.guard import SafetyGuard
from ash.safety.scoped_io import ScopedIOError, read_scoped_bytes
from ash.sandbox.process_utils import process_group_options, terminate_process_tree


MAX_LSP_HEADER_BYTES = 8 * 1024
MAX_LSP_MESSAGE_BYTES = 8 * 1024 * 1024
MAX_LSP_STDERR_BYTES = 64 * 1024
MAX_LSP_DOCUMENT_BYTES = 8 * 1024 * 1024
MAX_LSP_OPEN_DOCUMENTS = 16
MAX_LSP_OPEN_DOCUMENT_BYTES = 32 * 1024 * 1024
LSP_INITIALIZE_TIMEOUT = 45.0
LSP_REQUEST_TIMEOUT = 10.0
LSP_WRITE_TIMEOUT = 5.0

DiagnosticsCallback = Callable[[str, list[dict[str, Any]]], Awaitable[None]]


class LSPError(RuntimeError):
    """A language server failed its lifecycle or protocol contract."""


class LSPResponseError(LSPError):
    """A healthy server rejected one request."""


@dataclass(frozen=True)
class DocumentDiagnosticReport:
    kind: str
    items: list[dict[str, Any]]
    result_id: str | None = None


@dataclass(frozen=True)
class DocumentSyncResult:
    uri: str
    changed: bool


class LSPClient:
    def __init__(
        self,
        config: LSPServerConfig,
        root: Path,
        *,
        diagnostics_callback: DiagnosticsCallback,
    ) -> None:
        self.config = config
        self.root = root.resolve()
        self._guard = SafetyGuard(self.root)
        self._diagnostics_callback = diagnostics_callback
        self.process: asyncio.subprocess.Process | None = None
        self.capabilities: dict[str, Any] = {}
        self.position_encoding = "utf-16"
        self._next_id = 1
        self._pending: dict[int, asyncio.Future[Any]] = {}
        self._write_lock = asyncio.Lock()
        self._reader_task: asyncio.Task[None] | None = None
        self._stderr_task: asyncio.Task[None] | None = None
        self._documents: OrderedDict[str, tuple[int, str]] = OrderedDict()
        self._pending_documents: dict[str, tuple[int, str]] = {}
        self._document_lock = asyncio.Lock()
        self._closed = False
        self._close_task: asyncio.Task[None] | None = None
        self._channel_error: LSPError | None = None
        self.stderr = ""

    async def start(self) -> None:
        if self.process is not None:
            return
        environment = build_scrubbed_environment(overrides=self.config.env)
        try:
            self.process = await asyncio.create_subprocess_exec(
                *self.config.command,
                cwd=self.root,
                env=environment,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                limit=MAX_LSP_HEADER_BYTES + 4,
                **process_group_options(),
            )
        except OSError as exc:
            raise LSPError(
                f"failed to start LSP server {self.config.name}: {exc}"
            ) from exc
        self._reader_task = asyncio.create_task(
            self._read_loop(), name=f"ash-lsp-reader-{self.config.name}"
        )
        self._stderr_task = asyncio.create_task(
            self._drain_stderr(), name=f"ash-lsp-stderr-{self.config.name}"
        )
        try:
            response = await self.request(
                "initialize",
                {
                    "processId": os.getpid(),
                    "clientInfo": {"name": "ash", "version": "0.1.0"},
                    "locale": "en",
                    "rootUri": self.root.as_uri(),
                    "workspaceFolders": [
                        {"uri": self.root.as_uri(), "name": self.root.name}
                    ],
                    "initializationOptions": self.config.initialization_options,
                    "capabilities": _client_capabilities(),
                    "trace": "off",
                },
                timeout=LSP_INITIALIZE_TIMEOUT,
            )
            if not isinstance(response, dict):
                raise LSPError("initialize returned a non-object result")
            capabilities = response.get("capabilities", {})
            if not isinstance(capabilities, dict):
                raise LSPError("initialize capabilities are invalid")
            self.capabilities = capabilities
            position_encoding = capabilities.get("positionEncoding", "utf-16")
            if position_encoding in {"utf-8", "utf-16", "utf-32"}:
                self.position_encoding = position_encoding
            await self.notify("initialized", {})
            if self.config.settings:
                await self.notify(
                    "workspace/didChangeConfiguration",
                    {"settings": self.config.settings},
                )
        except BaseException as exc:
            cleanup = self._ensure_close_task()
            current = asyncio.current_task()
            if current is not None:
                while current.cancelling():
                    current.uncancel()
            while not cleanup.done():
                try:
                    await asyncio.shield(cleanup)
                except asyncio.CancelledError:
                    if current is not None:
                        current.uncancel()
            with contextlib.suppress(Exception):
                cleanup.result()
            if isinstance(exc, LSPError):
                detail = self._with_stderr(str(exc))
                if detail != str(exc):
                    raise LSPError(detail) from exc
            raise

    async def request(
        self,
        method: str,
        params: Any,
        *,
        timeout: float = LSP_REQUEST_TIMEOUT,
    ) -> Any:
        if self._closed:
            raise LSPError(f"LSP server {self.config.name} is closed")
        if self._channel_error is not None:
            raise self._channel_error
        request_id = self._next_id
        self._next_id += 1
        future: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        try:
            await self._write_message_bounded(
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "method": method,
                    "params": params,
                },
                timeout=min(timeout, LSP_WRITE_TIMEOUT),
            )
            async with asyncio.timeout(timeout):
                return await future
        except (asyncio.TimeoutError, asyncio.CancelledError):
            with contextlib.suppress(LSPError, OSError, asyncio.TimeoutError):
                await self.notify("$/cancelRequest", {"id": request_id}, timeout=1.0)
            raise
        finally:
            self._pending.pop(request_id, None)

    async def notify(
        self, method: str, params: Any, *, timeout: float = LSP_WRITE_TIMEOUT
    ) -> None:
        if self._closed:
            return
        await self._write_message_bounded(
            {"jsonrpc": "2.0", "method": method, "params": params},
            timeout=timeout,
        )

    async def sync_document(self, path: Path, language_id: str) -> DocumentSyncResult:
        async with self._document_lock:
            return await self._sync_document(path, language_id)

    async def _sync_document(self, path: Path, language_id: str) -> DocumentSyncResult:
        resolved = path.resolve(strict=True)
        text = _read_document_text(resolved, self._guard)
        uri = resolved.as_uri()
        previous = self._documents.get(uri)
        changed = False
        if previous is None:
            await self._evict_documents(uri, len(text.encode("utf-8")))
            version = 1
            if self._open_close_enabled():
                self._pending_documents[uri] = (version, text)
                try:
                    await self.notify(
                        "textDocument/didOpen",
                        {
                            "textDocument": {
                                "uri": uri,
                                "languageId": language_id,
                                "version": version,
                                "text": text,
                            }
                        },
                    )
                finally:
                    self._pending_documents.pop(uri, None)
            self._documents[uri] = (version, text)
            changed = True
        elif previous[1] != text:
            await self._evict_documents(uri, len(text.encode("utf-8")))
            version = previous[0] + 1
            change_kind = self._change_kind()
            if change_kind:
                change: dict[str, Any] = {"text": text}
                if change_kind == 2:
                    change["range"] = {
                        "start": {"line": 0, "character": 0},
                        "end": _document_end_position(
                            previous[1], self.position_encoding
                        ),
                    }
                self._pending_documents[uri] = (version, text)
                try:
                    await self.notify(
                        "textDocument/didChange",
                        {
                            "textDocument": {"uri": uri, "version": version},
                            "contentChanges": [change],
                        },
                    )
                finally:
                    self._pending_documents.pop(uri, None)
            self._documents[uri] = (version, text)
            changed = True
            save = self._save_options()
            if save is not None:
                params: dict[str, Any] = {"textDocument": {"uri": uri}}
                if save:
                    params["text"] = text
                await self.notify("textDocument/didSave", params)
        self._documents.move_to_end(uri)
        return DocumentSyncResult(uri, changed)

    def position(self, uri: str, line: int, character: int) -> dict[str, int]:
        document = self._documents.get(uri)
        if document is None:
            raise LSPError("document must be synchronized before requesting a position")
        return _lsp_position(document[1], line, character, self.position_encoding)

    async def pull_document_diagnostics(
        self, uri: str, previous_result_id: str | None = None
    ) -> DocumentDiagnosticReport:
        if not self.supports_pull_diagnostics:
            raise LSPResponseError("server does not support pull diagnostics")
        params: dict[str, Any] = {"textDocument": {"uri": uri}}
        if previous_result_id:
            params["previousResultId"] = previous_result_id
        result = await self.request(
            "textDocument/diagnostic",
            params,
            timeout=5.0,
        )
        if not isinstance(result, dict):
            raise LSPResponseError("invalid document diagnostic report")
        kind = result.get("kind", "full")
        result_id = result.get("resultId")
        bounded_result_id = result_id[:512] if isinstance(result_id, str) else None
        if kind == "unchanged":
            return DocumentDiagnosticReport("unchanged", [], bounded_result_id)
        if kind != "full":
            raise LSPResponseError("invalid document diagnostic report kind")
        items = result.get("items", [])
        if not isinstance(items, list):
            raise LSPResponseError("invalid document diagnostic items")
        return DocumentDiagnosticReport(
            "full",
            [item for item in items[:100] if isinstance(item, dict)],
            bounded_result_id,
        )

    @property
    def supports_pull_diagnostics(self) -> bool:
        value = self.capabilities.get("diagnosticProvider")
        return value is True or isinstance(value, dict)

    @property
    def supports_push_diagnostics(self) -> bool:
        return self._open_close_enabled() or bool(self._change_kind())

    def has_document(self, uri: str) -> bool:
        return uri in self._documents or uri in self._pending_documents

    @property
    def healthy(self) -> bool:
        return bool(
            not self._closed
            and self.process is not None
            and self.process.returncode is None
            and self._reader_task is not None
            and not self._reader_task.done()
            and self._channel_error is None
        )

    async def aclose(self) -> None:
        await _await_task_cancellation_safe(self._ensure_close_task())

    def _ensure_close_task(self) -> asyncio.Task[None]:
        if self._close_task is None:
            self._close_task = asyncio.create_task(self._close())
        return self._close_task

    async def _close(self) -> None:
        if self._closed:
            return
        process = self.process
        if process is None:
            self._closed = True
            return
        if (
            process.returncode is None
            and self._reader_task is not asyncio.current_task()
        ):
            try:
                if self._open_close_enabled():
                    async with self._document_lock:
                        for uri in tuple(self._documents):
                            await self.notify(
                                "textDocument/didClose",
                                {"textDocument": {"uri": uri}},
                            )
                await self.request("shutdown", None, timeout=2.0)
                await self.notify("exit", None)
                await asyncio.wait_for(process.wait(), timeout=2.0)
            except (LSPError, OSError, asyncio.TimeoutError):
                await terminate_process_tree(process)
        self._closed = True
        if process.returncode is None:
            await terminate_process_tree(process)
        await self._wait_for_stderr()
        current = asyncio.current_task()
        tasks = [
            task
            for task in (self._reader_task, self._stderr_task)
            if task is not None and task is not current and not task.done()
        ]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self._fail_pending(LSPError(f"LSP server {self.config.name} closed"))

    async def _write_message(self, payload: dict[str, Any]) -> None:
        process = self.process
        if process is None or process.stdin is None or process.returncode is not None:
            raise LSPError(f"LSP server {self.config.name} is not running")
        try:
            body = json.dumps(payload, separators=(",", ":"), allow_nan=False).encode(
                "utf-8"
            )
        except (TypeError, ValueError) as exc:
            raise LSPError("outbound LSP payload is not valid JSON") from exc
        if len(body) > MAX_LSP_MESSAGE_BYTES:
            raise LSPError("outbound LSP message exceeds 8 MiB")
        frame = f"Content-Length: {len(body)}\r\n\r\n".encode("ascii") + body
        async with self._write_lock:
            try:
                process.stdin.write(frame)
                await process.stdin.drain()
            except (BrokenPipeError, ConnectionResetError) as exc:
                raise LSPError(
                    f"LSP server {self.config.name} closed its stdin"
                ) from exc

    async def _read_loop(self) -> None:
        assert self.process is not None and self.process.stdout is not None
        failure: LSPError | None = None
        try:
            while True:
                payload = await _read_message(self.process.stdout)
                await self._handle_message(payload)
        except asyncio.IncompleteReadError:
            if not self._closed:
                failure = LSPError(
                    f"LSP server {self.config.name} closed its output unexpectedly"
                )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - fail every pending RPC consistently
            failure = exc if isinstance(exc, LSPError) else LSPError(str(exc))
        finally:
            if failure is not None:
                if self.process.returncode is None:
                    await terminate_process_tree(self.process)
                await self._wait_for_stderr()
                detailed = LSPError(self._with_stderr(str(failure)))
                self._channel_error = detailed
                self._fail_pending(detailed)

    async def _handle_message(self, payload: dict[str, Any]) -> None:
        if "id" in payload and ("result" in payload or "error" in payload):
            request_id = payload.get("id")
            future = (
                self._pending.get(request_id) if isinstance(request_id, int) else None
            )
            if future is None or future.done():
                return
            error = payload.get("error")
            if error is not None:
                future.set_exception(LSPResponseError(_render_rpc_error(error)))
            else:
                future.set_result(payload.get("result"))
            return
        method = payload.get("method")
        if not isinstance(method, str):
            return
        if "id" in payload:
            await self._handle_server_request(payload)
            return
        params = payload.get("params")
        if method == "textDocument/publishDiagnostics" and isinstance(params, dict):
            uri = params.get("uri")
            diagnostics = params.get("diagnostics")
            version = params.get("version")
            current = (
                self._pending_documents.get(uri) or self._documents.get(uri)
                if isinstance(uri, str)
                else None
            )
            if (
                isinstance(uri, str)
                and isinstance(diagnostics, list)
                and current is not None
                and (
                    version is None
                    or (
                        isinstance(version, int)
                        and not isinstance(version, bool)
                        and version == current[0]
                    )
                )
            ):
                await self._diagnostics_callback(
                    uri,
                    [item for item in diagnostics[:100] if isinstance(item, dict)],
                )

    async def _handle_server_request(self, payload: dict[str, Any]) -> None:
        request_id = payload.get("id")
        if not isinstance(request_id, str | int) or isinstance(request_id, bool):
            await self._write_response(
                {
                    "jsonrpc": "2.0",
                    "id": None,
                    "error": {"code": -32600, "message": "Invalid Request"},
                }
            )
            return
        method = payload.get("method")
        params = payload.get("params")
        result: Any
        if method == "workspace/configuration":
            configuration_items = (
                params.get("items", []) if isinstance(params, dict) else []
            )
            if not isinstance(configuration_items, list):
                configuration_items = []
            result = [
                _configuration_value(
                    self.config.settings,
                    item.get("section") if isinstance(item, dict) else None,
                )
                for item in configuration_items
            ]
        elif method == "workspace/workspaceFolders":
            result = [{"uri": self.root.as_uri(), "name": self.root.name}]
        elif method in {
            "window/workDoneProgress/create",
            "workspace/diagnostic/refresh",
        }:
            result = None
        elif method == "workspace/applyEdit":
            result = {
                "applied": False,
                "failureReason": "Ash applies edits through guarded tools",
            }
        elif method == "window/showMessageRequest":
            result = None
        else:
            await self._write_response(
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "error": {"code": -32601, "message": "Method not supported"},
                }
            )
            return
        await self._write_response(
            {"jsonrpc": "2.0", "id": request_id, "result": result}
        )

    async def _drain_stderr(self) -> None:
        assert self.process is not None and self.process.stderr is not None
        captured = bytearray()
        while True:
            chunk = await self.process.stderr.read(4096)
            if not chunk:
                break
            if len(captured) < MAX_LSP_STDERR_BYTES:
                captured.extend(chunk[: MAX_LSP_STDERR_BYTES - len(captured)])
                self.stderr = captured.decode("utf-8", errors="replace")
        self.stderr = captured.decode("utf-8", errors="replace")

    async def _wait_for_stderr(self) -> None:
        if self._stderr_task is None or self._stderr_task.done():
            return
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(asyncio.shield(self._stderr_task), timeout=1.0)

    async def _write_response(self, payload: dict[str, Any]) -> None:
        await self._write_message_bounded(payload, timeout=LSP_WRITE_TIMEOUT)

    async def _write_message_bounded(
        self, payload: dict[str, Any], *, timeout: float
    ) -> None:
        try:
            async with asyncio.timeout(timeout):
                await self._write_message(payload)
        except asyncio.TimeoutError as exc:
            error = LSPError(
                f"LSP server {self.config.name} stopped reading client messages"
            )
            self._channel_error = error
            raise error from exc

    def _with_stderr(self, message: str) -> str:
        detail = redact_text(self.stderr.strip())[:2000]
        if detail and detail in message:
            return message
        return f"{message}; stderr: {detail}" if detail else message

    def _fail_pending(self, exc: Exception) -> None:
        for future in list(self._pending.values()):
            if not future.done():
                future.set_exception(exc)

    async def _evict_documents(self, incoming_uri: str, incoming_bytes: int) -> None:
        def retained_bytes() -> int:
            return sum(
                len(text.encode("utf-8"))
                for uri, (_, text) in self._documents.items()
                if uri != incoming_uri
            )

        while self._documents and (
            len(self._documents) - int(incoming_uri in self._documents)
            >= MAX_LSP_OPEN_DOCUMENTS
            or retained_bytes() + incoming_bytes > MAX_LSP_OPEN_DOCUMENT_BYTES
        ):
            uri = next(iter(self._documents))
            if uri == incoming_uri:
                self._documents.move_to_end(uri)
                if len(self._documents) == 1:
                    break
                continue
            self._documents.pop(uri)
            if self._open_close_enabled():
                await self.notify(
                    "textDocument/didClose", {"textDocument": {"uri": uri}}
                )

    def _sync_capability(self) -> int | dict[str, Any] | None:
        value = self.capabilities.get("textDocumentSync")
        return (
            value
            if isinstance(value, int | dict) and not isinstance(value, bool)
            else None
        )

    def _open_close_enabled(self) -> bool:
        sync = self._sync_capability()
        if isinstance(sync, dict):
            return bool(sync.get("openClose"))
        return isinstance(sync, int) and sync > 0

    def _change_kind(self) -> int:
        sync = self._sync_capability()
        raw = sync.get("change", 0) if isinstance(sync, dict) else sync
        return raw if raw in {1, 2} else 0

    def _save_options(self) -> bool | None:
        sync = self._sync_capability()
        if not isinstance(sync, dict):
            return None
        save = sync.get("save")
        if save is True:
            return False
        if isinstance(save, dict):
            return bool(save.get("includeText"))
        return None


async def _read_message(reader: asyncio.StreamReader) -> dict[str, Any]:
    try:
        header = await reader.readuntil(b"\r\n\r\n")
    except asyncio.LimitOverrunError as exc:
        raise LSPError("LSP header exceeds stream limit") from exc
    if len(header) > MAX_LSP_HEADER_BYTES:
        raise LSPError("LSP header exceeds 8 KiB")
    content_length: int | None = None
    for raw_line in header[:-4].split(b"\r\n"):
        name, separator, value = raw_line.partition(b":")
        if not separator:
            raise LSPError("malformed LSP header")
        if name.strip().lower() == b"content-length":
            if content_length is not None:
                raise LSPError("duplicate LSP Content-Length header")
            try:
                content_length = int(value.strip())
            except ValueError as exc:
                raise LSPError("invalid LSP Content-Length") from exc
    if content_length is None or not 0 <= content_length <= MAX_LSP_MESSAGE_BYTES:
        raise LSPError("LSP Content-Length is missing or exceeds 8 MiB")
    body = await reader.readexactly(content_length)
    try:
        payload = json.loads(
            body,
            object_pairs_hook=_unique_json_object,
            parse_constant=_reject_json_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise LSPError("invalid LSP JSON payload") from exc
    if not isinstance(payload, dict) or payload.get("jsonrpc") != "2.0":
        raise LSPError("invalid LSP JSON-RPC envelope")
    return payload


def _client_capabilities() -> dict[str, Any]:
    return {
        "workspace": {
            "configuration": True,
            "workspaceFolders": True,
            "symbol": {"dynamicRegistration": False},
            "diagnostics": {"refreshSupport": False},
        },
        "window": {"workDoneProgress": True},
        "textDocument": {
            "synchronization": {"didSave": True, "dynamicRegistration": False},
            "publishDiagnostics": {"relatedInformation": False, "versionSupport": True},
            "diagnostic": {
                "dynamicRegistration": False,
                "relatedDocumentSupport": False,
            },
            "definition": {"linkSupport": True},
            "implementation": {"linkSupport": True},
            "references": {"dynamicRegistration": False},
            "hover": {"contentFormat": ["markdown", "plaintext"]},
            "documentSymbol": {"hierarchicalDocumentSymbolSupport": True},
            "callHierarchy": {"dynamicRegistration": False},
        },
        "general": {"positionEncodings": ["utf-16", "utf-8", "utf-32"]},
    }


def _configuration_value(settings: dict[str, Any], section: Any) -> Any:
    if not isinstance(section, str) or not section:
        return settings
    value: Any = settings
    for key in section.split("."):
        if not isinstance(value, dict) or key not in value:
            return None
        value = value[key]
    return value


def _render_rpc_error(error: Any) -> str:
    if not isinstance(error, dict):
        return "language server returned an invalid error"
    code = error.get("code", "unknown")
    message = str(error.get("message", "request failed"))
    return f"LSP error {code}: {message[:2000]}"


def _unique_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError(f"duplicate JSON key: {key!r}")
        value[key] = item
    return value


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"invalid JSON constant: {value}")


def _read_document_text(path: Path, guard: SafetyGuard) -> str:
    try:
        _, raw = read_scoped_bytes(path, guard, max_bytes=MAX_LSP_DOCUMENT_BYTES)
    except ScopedIOError as exc:
        raise LSPError(f"cannot read LSP document safely: {exc}") from exc
    encodings = (
        (b"\xef\xbb\xbf", "utf-8"),
        (b"\xff\xfe\x00\x00", "utf-32-le"),
        (b"\x00\x00\xfe\xff", "utf-32-be"),
        (b"\xff\xfe", "utf-16-le"),
        (b"\xfe\xff", "utf-16-be"),
    )
    for bom, encoding in encodings:
        if raw.startswith(bom):
            try:
                return raw[len(bom) :].decode(encoding)
            except UnicodeDecodeError as exc:
                raise LSPError(f"document is not valid {encoding} text") from exc
    if b"\x00" in raw[:8192]:
        raise LSPError("LSP cannot open a binary document")
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise LSPError("LSP document is not valid UTF-8 text") from exc


def _lsp_position(
    text: str, line: int, character: int, encoding: str
) -> dict[str, int]:
    lines = _lsp_lines(text)
    if not 1 <= line <= len(lines):
        raise ValueError(f"line {line} is outside the document")
    content = lines[line - 1]
    if not 1 <= character <= len(content) + 1:
        raise ValueError(f"character {character} is outside line {line}")
    prefix = content[: character - 1]
    if encoding == "utf-8":
        offset = len(prefix.encode("utf-8"))
    elif encoding == "utf-32":
        offset = len(prefix)
    else:
        offset = len(prefix.encode("utf-16-le")) // 2
    return {"line": line - 1, "character": offset}


def _document_end_position(text: str, encoding: str) -> dict[str, int]:
    lines = _lsp_lines(text)
    last = lines[-1]
    if encoding == "utf-8":
        character = len(last.encode("utf-8"))
    elif encoding == "utf-32":
        character = len(last)
    else:
        character = len(last.encode("utf-16-le")) // 2
    return {"line": len(lines) - 1, "character": character}


def _lsp_lines(text: str) -> list[str]:
    lines: list[str] = []
    start = 0
    index = 0
    while index < len(text):
        if text[index] == "\r":
            lines.append(text[start:index])
            index += 1
            if index < len(text) and text[index] == "\n":
                index += 1
            start = index
            continue
        if text[index] == "\n":
            lines.append(text[start:index])
            index += 1
            start = index
            continue
        index += 1
    lines.append(text[start:])
    return lines


async def _await_task_cancellation_safe(task: asyncio.Task[None]) -> None:
    cancelled = False
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            cancelled = True
            current = asyncio.current_task()
            if current is not None:
                current.uncancel()
    task.result()
    if cancelled:
        raise asyncio.CancelledError
