"""Workspace-scoped lifecycle and query manager for language servers."""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse
from urllib.request import url2pathname

from ash.lsp.client import DocumentDiagnosticReport, LSPClient, LSPError
from ash.lsp.config import LSPServerConfig
from ash.safety.path_scope import is_relative_to, resolve_target_path


MAX_LSP_RESULTS = 200
MAX_LSP_RESULT_BYTES = 512 * 1024
MAX_DIAGNOSTICS_PER_FILE = 100
MAX_LSP_STRING_CHARS = 64 * 1024
MAX_DIAGNOSTIC_CACHE_FILES = 256
MAX_LSP_RESTART_ATTEMPTS = 3
_DROP = object()


@dataclass(frozen=True)
class LSPStatus:
    name: str
    root: str
    status: str
    detail: str = ""


@dataclass(frozen=True)
class LSPFailure:
    detail: str
    failures: int
    retry_at: float


@dataclass(frozen=True)
class LSPFormattingProposal:
    server: str
    position_encoding: str
    document_text: str
    edits: tuple[dict[str, Any], ...]


class _UnsafeWorkspaceEdit(LSPError):
    """A server proposed an edit outside Ash's advisory safety contract."""


class LanguageServerManager:
    def __init__(
        self,
        workspace: Path,
        configs: dict[str, LSPServerConfig],
    ) -> None:
        self.workspace = workspace.resolve()
        self.configs = dict(configs)
        self._clients: dict[tuple[str, Path], LSPClient] = {}
        self._starting: dict[tuple[str, Path], asyncio.Task[LSPClient]] = {}
        self._broken: dict[tuple[str, Path], LSPFailure] = {}
        self._failure_counts: dict[tuple[str, Path], int] = {}
        self._lock = asyncio.Lock()
        self._diagnostics: dict[tuple[str, str], list[dict[str, Any]]] = {}
        self._diagnostic_result_ids: dict[tuple[str, str], str] = {}
        self._diagnostic_events: dict[tuple[str, str], set[asyncio.Event]] = {}
        self._closed = False
        self._close_task: asyncio.Task[None] | None = None

    async def query(
        self,
        operation: str,
        *,
        file_path: str = "",
        line: int = 1,
        character: int = 1,
        query: str = "",
        new_name: str = "",
        end_line: int | None = None,
        end_character: int | None = None,
        code_action_kind: str = "",
        server: str = "",
        tab_size: int | None = None,
        insert_spaces: bool | None = None,
    ) -> Any:
        if operation == "status":
            return [status.__dict__ for status in self.status()]
        if operation == "workspaceSymbol":
            return await self._workspace_symbols(query)
        if operation == "rename":
            if not new_name.strip():
                raise ValueError("new_name is required for rename")
        elif new_name:
            raise ValueError("new_name is only valid for rename")
        if operation == "codeAction":
            if (end_line is None) != (end_character is None):
                raise ValueError("end_line and end_character must be provided together")
        elif end_line is not None or end_character is not None or code_action_kind:
            raise ValueError("code-action range and kind are only valid for codeAction")
        formatting_options_used = bool(server) or tab_size is not None or insert_spaces is not None
        if operation != "formatting" and formatting_options_used:
            raise ValueError(
                "server and formatting options are only valid for formatting"
            )
        if operation == "formatting":
            selected_tab_size = 4 if tab_size is None else tab_size
            if selected_tab_size < 1:
                raise ValueError("tab_size must be positive")
            proposal = await self.formatting_for(
                file_path,
                server=server,
                tab_size=selected_tab_size,
                insert_spaces=True if insert_spaces is None else insert_spaces,
            )
            return [
                {
                    "server": proposal.server,
                    "position_encoding": proposal.position_encoding,
                    "edits": list(proposal.edits),
                }
            ]
        path = self.resolve_file(file_path)
        if operation == "diagnostics":
            return await self.diagnostics_for(path)
        clients = await self.clients_for(path)
        if not clients:
            raise LSPError(
                f"no configured LSP server is available for {path.suffix or path.name}"
            )
        uri = path.as_uri()
        method = {
            "hover": "textDocument/hover",
            "definition": "textDocument/definition",
            "references": "textDocument/references",
            "implementation": "textDocument/implementation",
            "documentSymbol": "textDocument/documentSymbol",
            "prepareRename": "textDocument/prepareRename",
            "rename": "textDocument/rename",
            "codeAction": "textDocument/codeAction",
            "prepareCallHierarchy": "textDocument/prepareCallHierarchy",
        }.get(operation)
        if method is None and operation not in {"incomingCalls", "outgoingCalls"}:
            raise ValueError(f"unsupported LSP operation: {operation}")
        results: list[Any] = []
        errors: list[str] = []
        for client, language_id in clients:
            try:
                await client.sync_document(path, language_id)
                _require_capability(client, operation)
                params = {
                    "textDocument": {"uri": uri},
                    "position": client.position(uri, line, character),
                }
                if operation == "references":
                    assert method is not None
                    request_params = {**params, "context": {"includeDeclaration": True}}
                    value = await client.request(method, request_params)
                elif operation == "rename":
                    assert method is not None
                    value = await client.request(method, {**params, "newName": new_name})
                    if value is None:
                        continue
                    edit = _normalize_workspace_edit(value, self.workspace)
                    if edit is None:
                        raise _UnsafeWorkspaceEdit(
                            "unsafe workspace edit returned by rename"
                        )
                    results.append({"server": client.config.name, "edit": edit})
                    continue
                elif operation == "codeAction":
                    assert method is not None
                    end_position = client.position(
                        uri,
                        end_line if end_line is not None else line,
                        end_character if end_character is not None else character,
                    )
                    context: dict[str, Any] = {"diagnostics": []}
                    if code_action_kind:
                        context["only"] = [code_action_kind]
                    value = await client.request(
                        method,
                        {
                            "textDocument": {"uri": uri},
                            "range": {
                                "start": params["position"],
                                "end": end_position,
                            },
                            "context": context,
                        },
                    )
                    if value is None:
                        continue
                    if not isinstance(value, list):
                        raise LSPError("codeAction returned a non-list result")
                    for action in value[: max(0, MAX_LSP_RESULTS - len(results))]:
                        normalized = _normalize_code_action(
                            action, self.workspace, client.config.name
                        )
                        if normalized is not None:
                            results.append(normalized)
                    continue
                elif operation == "documentSymbol":
                    assert method is not None
                    value = await client.request(method, {"textDocument": {"uri": uri}})
                elif operation in {"incomingCalls", "outgoingCalls"}:
                    prepared = await client.request(
                        "textDocument/prepareCallHierarchy", params
                    )
                    items = prepared if isinstance(prepared, list) else []
                    value = []
                    call_method = (
                        "callHierarchy/incomingCalls"
                        if operation == "incomingCalls"
                        else "callHierarchy/outgoingCalls"
                    )
                    for item in items[:20]:
                        if isinstance(item, dict):
                            calls = await client.request(call_method, {"item": item})
                            if isinstance(calls, list):
                                value.extend(
                                    calls[: max(0, MAX_LSP_RESULTS - len(value))]
                                )
                            if len(value) >= MAX_LSP_RESULTS:
                                break
                else:
                    assert method is not None
                    value = await client.request(method, params)
                if isinstance(value, list):
                    results.extend(value[: max(0, MAX_LSP_RESULTS - len(results))])
                elif value is not None:
                    results.append(value)
            except (OSError, ValueError, asyncio.TimeoutError) as exc:
                errors.append(f"{client.config.name}: {exc}")
            except LSPError as exc:
                if isinstance(exc, _UnsafeWorkspaceEdit):
                    raise
                errors.append(f"{client.config.name}: {exc}")
                if not client.healthy:
                    await self._mark_broken(client, str(exc))
        if not results and errors:
            raise LSPError("; ".join(errors))
        if operation in {"rename", "codeAction"}:
            return _bound_advisory_results(results)
        return self._bounded_results(results)

    async def diagnostics_for(self, path: Path) -> list[dict[str, Any]]:
        resolved = self.resolve_file(str(path))
        clients = await self.clients_for(resolved)
        if not clients:
            raise LSPError(
                f"no configured LSP server is available for {resolved.suffix or resolved.name}"
            )
        uri = resolved.as_uri()
        diagnostic_events: dict[str, asyncio.Event] = {}
        for client, _ in clients:
            event = asyncio.Event()
            diagnostic_events[client.config.name] = event
            self._diagnostic_events.setdefault((client.config.name, uri), set()).add(
                event
            )
        pull_results: list[tuple[str, DocumentDiagnosticReport | None]] = []

        async def sync_and_pull(
            client: LSPClient, language_id: str
        ) -> tuple[str, DocumentDiagnosticReport | None]:
            key = (client.config.name, uri)
            previous_push = self._diagnostics.get(key)
            sync = await client.sync_document(resolved, language_id)
            if not client.supports_pull_diagnostics:
                if not client.supports_push_diagnostics:
                    return client.config.name, DocumentDiagnosticReport("full", [])
                if not sync.changed:
                    return client.config.name, DocumentDiagnosticReport("unchanged", [])
                if self._diagnostics.get(key) is previous_push:
                    self._diagnostics.pop(key, None)
                return (
                    client.config.name,
                    None,
                )
            return client.config.name, await client.pull_document_diagnostics(
                uri, self._diagnostic_result_ids.get(key)
            )

        try:
            settled = await asyncio.gather(
                *(sync_and_pull(client, language) for client, language in clients),
                return_exceptions=True,
            )
            failures: list[str] = []
            for (client, _), item in zip(clients, settled, strict=True):
                if isinstance(item, tuple):
                    pull_results.append(item)
                elif isinstance(item, BaseException):
                    failures.append(str(item)[:500])
                    if isinstance(item, LSPError) and not client.healthy:
                        await self._mark_broken(client, str(item))
            if failures and len(failures) == len(settled):
                raise LSPError("; ".join(failures))
            for name, report in pull_results:
                if report is None:
                    continue
                diagnostic_key = (name, uri)
                if report.result_id:
                    self._diagnostic_result_ids[diagnostic_key] = report.result_id
                if report.kind == "full":
                    self._store_diagnostics(name, uri, report.items)
            if any(report is None for _, report in pull_results):
                waiters = [
                    diagnostic_events[name].wait()
                    for name, report in pull_results
                    if report is None
                ]
                try:
                    await asyncio.wait_for(asyncio.gather(*waiters), timeout=2.0)
                except asyncio.TimeoutError:
                    pass
        finally:
            for name, event in diagnostic_events.items():
                self._remove_diagnostic_event(name, uri, event)
        combined: list[dict[str, Any]] = []
        seen: set[str] = set()
        for client, _ in clients:
            for diagnostic in self._diagnostics.get((client.config.name, uri), []):
                normalized = _normalize_diagnostic(diagnostic, client.config.name)
                encoded = json.dumps(normalized, sort_keys=True)
                if encoded not in seen:
                    seen.add(encoded)
                    combined.append(normalized)
                if len(combined) >= MAX_DIAGNOSTICS_PER_FILE:
                    return combined
        return combined

    async def formatting_for(
        self,
        file_path: str,
        *,
        server: str = "",
        tab_size: int = 4,
        insert_spaces: bool = True,
    ) -> LSPFormattingProposal:
        path = self.resolve_file(file_path)
        if server:
            config = self.configs.get(server)
            if config is None:
                raise LSPError(f"LSP server {server!r} is not configured")
            language_id = config.extensions.get(path.suffix.casefold())
            if language_id is None:
                raise LSPError(
                    f"LSP server {server!r} is not available for "
                    f"{path.suffix or path.name}"
                )
            client = await self._get_client(config, self._root_for(path, config))
            clients = [(client, language_id)]
        else:
            clients = await self.clients_for(path)
        supported = [
            (client, language_id)
            for client, language_id in clients
            if _has_capability(client, "formatting")
        ]
        if server and not supported:
            raise LSPError(
                f"LSP server {server!r} is not available with formatting support "
                f"for {path.name}"
            )
        elif len(supported) > 1:
            names = ", ".join(client.config.name for client, _ in supported)
            raise LSPError(
                "multiple language servers advertise formatting for this file; "
                f"specify server explicitly ({names})"
            )
        if not supported:
            raise LSPError(
                f"no configured LSP server advertises formatting for "
                f"{path.suffix or path.name}"
            )
        client, language_id = supported[0]
        await client.sync_document(path, language_id)
        uri = path.as_uri()
        value = await client.request(
            "textDocument/formatting",
            {
                "textDocument": {"uri": uri},
                "options": {
                    "tabSize": tab_size,
                    "insertSpaces": insert_spaces,
                },
            },
        )
        if value is None:
            edits: list[dict[str, Any]] = []
        else:
            normalized_edits = _normalize_formatting_edits(value)
            if normalized_edits is None:
                raise LSPError("language server returned invalid formatting edits")
            edits = normalized_edits
        return LSPFormattingProposal(
            server=client.config.name,
            position_encoding=client.position_encoding,
            document_text=client.document_text(uri),
            edits=tuple(edits),
        )

    async def clients_for(self, path: Path) -> list[tuple[LSPClient, str]]:
        resolved = self.resolve_file(str(path))
        matches: list[tuple[LSPClient, str]] = []
        errors: list[str] = []
        matched_config = False
        for config in self.configs.values():
            language_id = config.extensions.get(resolved.suffix.casefold())
            if language_id is None:
                continue
            matched_config = True
            root = self._root_for(resolved, config)
            try:
                client = await self._get_client(config, root)
            except (LSPError, OSError, asyncio.TimeoutError) as exc:
                errors.append(f"{config.name}: {str(exc)[:500]}")
                continue
            matches.append((client, language_id))
        if matched_config and not matches and errors:
            raise LSPError("; ".join(errors))
        return matches

    def resolve_file(self, value: str) -> Path:
        if not value:
            raise ValueError("file_path is required for this LSP operation")
        path = resolve_target_path(value, self.workspace)
        if not is_relative_to(path, self.workspace):
            raise ValueError("LSP file is outside the workspace")
        if not path.is_file():
            raise ValueError(f"LSP file does not exist: {value}")
        return path

    def status(self) -> list[LSPStatus]:
        values: list[LSPStatus] = []
        active_names: set[str] = set()
        for (name, root), client in sorted(
            self._clients.items(), key=lambda item: (item[0][0], str(item[0][1]))
        ):
            active_names.add(name)
            running = client.process is not None and client.process.returncode is None
            values.append(
                LSPStatus(name, str(root), "running" if running else "stopped")
            )
        for (name, root), failure in sorted(
            self._broken.items(), key=lambda item: (item[0][0], str(item[0][1]))
        ):
            active_names.add(name)
            retry = max(0.0, failure.retry_at - time.monotonic())
            retry_detail = (
                f"; retry in {retry:.1f}s"
                if failure.failures <= MAX_LSP_RESTART_ATTEMPTS
                else "; retry limit reached"
            )
            values.append(
                LSPStatus(
                    name,
                    str(root),
                    "error",
                    (failure.detail + retry_detail)[:500],
                )
            )
        for name, config in sorted(self.configs.items()):
            if name not in active_names:
                values.append(LSPStatus(name, "", "available", config.source))
        return values

    async def aclose(self) -> None:
        if self._close_task is None or (
            self._close_task.done() and self._clients
        ):
            self._close_task = asyncio.create_task(self._close())
        await _await_task_cancellation_safe(self._close_task)

    async def _close(self) -> None:
        async with self._lock:
            if self._closed and not self._clients and not self._starting:
                return
            self._closed = True
            starting = list(self._starting.values())
            clients = list(self._clients.values())
            self._starting.clear()
        for task in starting:
            task.cancel()
        await asyncio.gather(*starting, return_exceptions=True)
        outcomes = await asyncio.gather(
            *(client.aclose() for client in clients), return_exceptions=True
        )
        failed = {
            client
            for client, outcome in zip(clients, outcomes, strict=True)
            if isinstance(outcome, BaseException)
        }
        async with self._lock:
            for client, outcome in zip(clients, outcomes, strict=True):
                if not isinstance(outcome, BaseException):
                    key = (client.config.name, client.root)
                    if self._clients.get(key) is client:
                        self._clients.pop(key, None)
        if failed:
            raise LSPError(f"failed to close {len(failed)} language server(s)")

    async def _workspace_symbols(self, query: str) -> list[Any]:
        if len(query) > 512:
            raise ValueError("workspace symbol query exceeds 512 characters")
        if not self.configs:
            raise LSPError("no language servers are configured")
        clients: list[LSPClient] = []
        startup_errors: list[str] = []
        for config in self.configs.values():
            try:
                clients.append(await self._get_client(config, self.workspace))
            except (LSPError, OSError, asyncio.TimeoutError) as exc:
                startup_errors.append(f"{config.name}: {str(exc)[:500]}")
                continue
        if not clients and startup_errors:
            raise LSPError("; ".join(startup_errors))
        supported = [
            client for client in clients if _has_capability(client, "workspaceSymbol")
        ]
        if clients and not supported:
            raise LSPError(
                "configured language servers do not advertise workspace symbols"
            )
        results = await asyncio.gather(
            *(
                client.request("workspace/symbol", {"query": query})
                for client in supported
            ),
            return_exceptions=True,
        )
        merged: list[Any] = []
        errors: list[str] = []
        for client, result in zip(supported, results, strict=True):
            if isinstance(result, list):
                merged.extend(result[: max(0, MAX_LSP_RESULTS - len(merged))])
            elif isinstance(result, BaseException):
                errors.append(f"{client.config.name}: {str(result)[:500]}")
                if isinstance(result, LSPError) and not client.healthy:
                    await self._mark_broken(client, str(result))
        if not merged and errors:
            raise LSPError("; ".join(errors))
        return self._bounded_results(merged)

    async def _get_client(self, config: LSPServerConfig, root: Path) -> LSPClient:
        key = (config.name, root)
        if self._closed:
            raise LSPError("language-server manager is closed")
        async with self._lock:
            if self._closed:
                raise LSPError("language-server manager is closed")
            failure = self._broken.get(key)
            existing = self._clients.get(key)
            if failure is not None:
                if (
                    failure.failures > MAX_LSP_RESTART_ATTEMPTS
                    or time.monotonic() < failure.retry_at
                ):
                    raise LSPError(failure.detail)
                if existing is not None and not existing.healthy:
                    # A client retained after failed process cleanup still owns
                    # its process.  Never hand it back as a fresh, usable client.
                    raise LSPError(failure.detail)
                self._broken.pop(key, None)
            if existing is not None:
                if not existing.healthy:
                    detail = getattr(existing, "_channel_error", None)
                    raise LSPError(
                        str(detail)[:2000]
                        if detail is not None
                        else f"language server {config.name!r} is not healthy"
                    )
                return existing
            task = self._starting.get(key)
            if task is None:
                task = asyncio.create_task(self._start_client(config, root))
                self._starting[key] = task
        try:
            return await asyncio.shield(task)
        finally:
            if task.done():
                async with self._lock:
                    if self._starting.get(key) is task:
                        self._starting.pop(key, None)

    async def _start_client(self, config: LSPServerConfig, root: Path) -> LSPClient:
        async def diagnostics(uri: str, items: list[dict[str, Any]]) -> None:
            if client.has_document(uri) and _file_uri_in_workspace(uri, self.workspace):
                self._store_diagnostics(config.name, uri, items)
                for event in self._diagnostic_events.get((config.name, uri), ()):
                    event.set()

        client = LSPClient(config, root, diagnostics_callback=diagnostics)
        try:
            await client.start()
        except BaseException as exc:
            if not isinstance(exc, asyncio.CancelledError):
                self._record_failure((config.name, root), str(exc))
            raise
        async with self._lock:
            if self._closed:
                await client.aclose()
                raise LSPError("language-server manager is closed")
            existing = self._clients.get((config.name, root))
            if existing is not None:
                await client.aclose()
                return existing
            self._broken.pop((config.name, root), None)
            self._clients[(config.name, root)] = client
        return client

    async def _mark_broken(self, client: LSPClient, detail: str) -> None:
        key = (client.config.name, client.root)
        async with self._lock:
            self._record_failure(key, detail)
        try:
            await client.aclose()
        except BaseException as exc:
            async with self._lock:
                if self._clients.get(key) is not client:
                    self._clients[key] = client
                self._record_failure(
                    key,
                    f"{detail}; process-tree cleanup failed: {exc}",
                )
            raise
        async with self._lock:
            if self._clients.get(key) is client:
                self._clients.pop(key, None)

    def _record_failure(self, key: tuple[str, Path], detail: str) -> None:
        failures = self._failure_counts.get(key, 0) + 1
        self._failure_counts[key] = failures
        delay = min(30.0, float(2 ** (failures - 1)))
        self._broken[key] = LSPFailure(
            detail[:2000], failures, time.monotonic() + delay
        )

    def _root_for(self, path: Path, config: LSPServerConfig) -> Path:
        current = path.parent
        while is_relative_to(current, self.workspace):
            if any((current / marker).exists() for marker in config.root_markers):
                return current
            if current == self.workspace:
                break
            current = current.parent
        return self.workspace

    def _remove_diagnostic_event(
        self, name: str, uri: str, event: asyncio.Event
    ) -> None:
        key = (name, uri)
        events = self._diagnostic_events.get(key)
        if events is None:
            return
        events.discard(event)
        if not events:
            self._diagnostic_events.pop(key, None)

    def _store_diagnostics(
        self, name: str, uri: str, items: list[dict[str, Any]]
    ) -> None:
        key = (name, uri)
        if (
            key not in self._diagnostics
            and len(self._diagnostics) >= MAX_DIAGNOSTIC_CACHE_FILES
        ):
            oldest = next(iter(self._diagnostics))
            self._diagnostics.pop(oldest, None)
            self._diagnostic_result_ids.pop(oldest, None)
        self._diagnostics[key] = [
            _normalize_diagnostic(item, name)
            for item in items[:MAX_DIAGNOSTICS_PER_FILE]
        ]

    def _bounded_results(self, values: list[Any]) -> list[Any]:
        normalized: list[Any] = []
        encoded = 2
        for value in values[:MAX_LSP_RESULTS]:
            item = _sanitize_result(value, self.workspace)
            if item is None:
                continue
            item_bytes = len(json.dumps(item, default=str).encode("utf-8"))
            if encoded + item_bytes > MAX_LSP_RESULT_BYTES:
                break
            normalized.append(item)
            encoded += item_bytes
        return normalized


def _file_uri_in_workspace(uri: str, workspace: Path) -> bool:
    path = _path_from_file_uri(uri)
    return path is not None and is_relative_to(path, workspace)


def _has_capability(client: LSPClient, operation: str) -> bool:
    if operation == "prepareRename":
        value = client.capabilities.get("renameProvider")
        return isinstance(value, dict) and value.get("prepareProvider") is True
    capability = {
        "hover": "hoverProvider",
        "definition": "definitionProvider",
        "references": "referencesProvider",
        "formatting": "documentFormattingProvider",
        "implementation": "implementationProvider",
        "documentSymbol": "documentSymbolProvider",
        "workspaceSymbol": "workspaceSymbolProvider",
        "rename": "renameProvider",
        "codeAction": "codeActionProvider",
        "prepareCallHierarchy": "callHierarchyProvider",
        "incomingCalls": "callHierarchyProvider",
        "outgoingCalls": "callHierarchyProvider",
    }.get(operation)
    if capability is None:
        return True
    value = client.capabilities.get(capability)
    return value is True or isinstance(value, dict)


def _require_capability(client: LSPClient, operation: str) -> None:
    if not _has_capability(client, operation):
        raise LSPError(
            f"{client.config.name} does not advertise support for {operation}"
        )


def _path_from_file_uri(uri: str) -> Path | None:
    parsed = urlparse(uri)
    if parsed.scheme != "file" or parsed.netloc not in {"", "localhost"}:
        return None
    try:
        return Path(url2pathname(parsed.path)).resolve()
    except (OSError, ValueError):
        return None


def _sanitize_result(value: Any, workspace: Path) -> Any:
    sanitized = _sanitize_value(value, workspace, depth=0)
    return None if sanitized is _DROP else sanitized


def _sanitize_value(value: Any, workspace: Path, *, depth: int) -> Any:
    if depth > 32:
        return _DROP
    if isinstance(value, list):
        return [
            item
            for raw in value[:MAX_LSP_RESULTS]
            if (item := _sanitize_value(raw, workspace, depth=depth + 1)) is not _DROP
        ]
    if not isinstance(value, dict):
        if isinstance(value, str):
            return value[:MAX_LSP_STRING_CHARS]
        return (
            value
            if isinstance(value, int | float | bool) or value is None
            else str(value)[:MAX_LSP_STRING_CHARS]
        )
    result: dict[str, Any] = {}
    for key, raw in value.items():
        if key in {"uri", "targetUri", "oldUri", "newUri"} and isinstance(raw, str):
            path = _path_from_file_uri(raw)
            if path is None or not is_relative_to(path, workspace):
                return _DROP
            result[key] = path.relative_to(workspace).as_posix()
            continue
        item = _sanitize_value(raw, workspace, depth=depth + 1)
        if item is _DROP:
            return _DROP
        result[str(key)[:128]] = item
    return result


def _bound_advisory_results(values: list[Any]) -> list[Any]:
    """Bound already-normalized advisory LSP results without rewriting path keys."""

    bounded: list[Any] = []
    encoded = 2
    for value in values[:MAX_LSP_RESULTS]:
        try:
            item_bytes = len(
                json.dumps(value, ensure_ascii=True, separators=(",", ":")).encode(
                    "utf-8"
                )
            )
        except (TypeError, ValueError, OverflowError, RecursionError):
            continue
        if encoded + item_bytes > MAX_LSP_RESULT_BYTES:
            break
        bounded.append(value)
        encoded += item_bytes
    return bounded


def _workspace_relative_uri(uri: Any, workspace: Path) -> str | None:
    if not isinstance(uri, str):
        return None
    path = _path_from_file_uri(uri)
    if path is None or not is_relative_to(path, workspace):
        return None
    return path.relative_to(workspace).as_posix()


def _strict_position(value: Any) -> dict[str, int] | None:
    if not isinstance(value, dict):
        return None
    line = value.get("line")
    character = value.get("character")
    if (
        isinstance(line, bool)
        or not isinstance(line, int)
        or isinstance(character, bool)
        or not isinstance(character, int)
        or not 0 <= line <= 10_000_000
        or not 0 <= character <= 10_000_000
    ):
        return None
    return {"line": line, "character": character}


def _strict_range(value: Any) -> dict[str, dict[str, int]] | None:
    if not isinstance(value, dict):
        return None
    start = _strict_position(value.get("start"))
    end = _strict_position(value.get("end"))
    if start is None or end is None:
        return None
    return {"start": start, "end": end}


def _normalize_text_edit(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    new_text = value.get("newText")
    if not isinstance(new_text, str) or len(new_text) > MAX_LSP_STRING_CHARS:
        return None
    result: dict[str, Any] = {"newText": new_text}
    if "range" in value:
        edit_range = _strict_range(value.get("range"))
        if edit_range is None:
            return None
        result["range"] = edit_range
    elif "insert" in value and "replace" in value:
        insert = _strict_range(value.get("insert"))
        replace = _strict_range(value.get("replace"))
        if insert is None or replace is None:
            return None
        result["insert"] = insert
        result["replace"] = replace
    else:
        return None
    annotation_id = value.get("annotationId")
    if annotation_id is not None:
        if not isinstance(annotation_id, str) or len(annotation_id) > 512:
            return None
        result["annotationId"] = annotation_id
    return result


def _normalize_text_edits(value: Any) -> list[dict[str, Any]] | None:
    if not isinstance(value, list) or len(value) > MAX_LSP_RESULTS:
        return None
    edits: list[dict[str, Any]] = []
    for raw in value:
        edit = _normalize_text_edit(raw)
        if edit is None:
            return None
        edits.append(edit)
    return edits


def _normalize_formatting_edits(value: Any) -> list[dict[str, Any]] | None:
    edits = _normalize_text_edits(value)
    if edits is None:
        return None
    for edit in edits:
        if "range" not in edit or "insert" in edit or "replace" in edit:
            return None
        edit.pop("annotationId", None)
    if len(json.dumps(edits, ensure_ascii=True).encode("utf-8")) > MAX_LSP_RESULT_BYTES:
        return None
    return edits


def _normalize_workspace_edit(value: Any, workspace: Path) -> dict[str, Any] | None:
    """Normalize an LSP WorkspaceEdit into workspace-relative advisory data."""

    if not isinstance(value, dict):
        return None
    result: dict[str, Any] = {}
    if "changes" in value:
        raw_changes = value.get("changes")
        if not isinstance(raw_changes, dict) or len(raw_changes) > MAX_LSP_RESULTS:
            return None
        changes: dict[str, list[dict[str, Any]]] = {}
        for uri, raw_edits in raw_changes.items():
            path = _workspace_relative_uri(uri, workspace)
            edits = _normalize_text_edits(raw_edits)
            if path is None or edits is None or path in changes:
                return None
            changes[path] = edits
        result["changes"] = changes
    if "documentChanges" in value:
        raw_changes = value.get("documentChanges")
        if not isinstance(raw_changes, list) or len(raw_changes) > MAX_LSP_RESULTS:
            return None
        document_changes: list[dict[str, Any]] = []
        for raw in raw_changes:
            normalized = _normalize_document_change(raw, workspace)
            if normalized is None:
                return None
            document_changes.append(normalized)
        result["documentChanges"] = document_changes
    if "changeAnnotations" in value:
        raw_annotations = value.get("changeAnnotations")
        if not isinstance(raw_annotations, dict) or len(raw_annotations) > MAX_LSP_RESULTS:
            return None
        annotations: dict[str, Any] = {}
        for raw_key, raw_annotation in raw_annotations.items():
            if not isinstance(raw_key, str) or len(raw_key) > 512:
                return None
            sanitized = _sanitize_value(raw_annotation, workspace, depth=0)
            if sanitized is _DROP:
                return None
            annotations[raw_key] = sanitized
        result["changeAnnotations"] = annotations
    if not result and value:
        return None
    try:
        if len(json.dumps(result, ensure_ascii=True).encode("utf-8")) > MAX_LSP_RESULT_BYTES:
            return None
    except (TypeError, ValueError, OverflowError, RecursionError):
        return None
    return result


def _normalize_document_change(value: Any, workspace: Path) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    text_document = value.get("textDocument")
    if isinstance(text_document, dict):
        path = _workspace_relative_uri(text_document.get("uri"), workspace)
        edits = _normalize_text_edits(value.get("edits"))
        if path is None or edits is None:
            return None
        result: dict[str, Any] = {"textDocument": {"path": path}, "edits": edits}
        version = text_document.get("version")
        if version is not None:
            if isinstance(version, bool) or not isinstance(version, int):
                return None
            result["textDocument"]["version"] = version
        return result
    kind = value.get("kind")
    if kind == "create":
        path = _workspace_relative_uri(value.get("uri"), workspace)
        if path is None:
            return None
        result = {"kind": "create", "path": path}
    elif kind == "rename":
        old_path = _workspace_relative_uri(value.get("oldUri"), workspace)
        new_path = _workspace_relative_uri(value.get("newUri"), workspace)
        if old_path is None or new_path is None:
            return None
        result = {"kind": "rename", "oldPath": old_path, "newPath": new_path}
    elif kind == "delete":
        path = _workspace_relative_uri(value.get("uri"), workspace)
        if path is None:
            return None
        result = {"kind": "delete", "path": path}
    else:
        return None
    if "options" in value:
        options = _sanitize_value(value.get("options"), workspace, depth=0)
        if options is _DROP:
            return None
        result["options"] = options
    if "annotationId" in value:
        annotation_id = value.get("annotationId")
        if not isinstance(annotation_id, str) or len(annotation_id) > 512:
            return None
        result["annotationId"] = annotation_id
    return result


def _normalize_code_action(
    value: Any, workspace: Path, server_name: str
) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    title = value.get("title")
    if not isinstance(title, str) or not title or len(title) > 4096:
        return None
    result: dict[str, Any] = {"server": server_name, "title": title}
    kind = value.get("kind")
    if kind is not None:
        if not isinstance(kind, str) or len(kind) > 512:
            return None
        result["kind"] = kind
    preferred = value.get("isPreferred")
    if preferred is not None:
        if not isinstance(preferred, bool):
            return None
        result["isPreferred"] = preferred
    disabled = value.get("disabled")
    if disabled is not None:
        if not isinstance(disabled, dict) or not isinstance(disabled.get("reason"), str):
            return None
        result["disabled"] = {"reason": disabled["reason"][:4096]}
    if "edit" in value:
        edit = _normalize_workspace_edit(value.get("edit"), workspace)
        if edit is None:
            return None
        result["edit"] = edit
    command = value.get("command")
    if isinstance(command, dict):
        command_title = command.get("title")
        command_name = command.get("command")
        if (
            not isinstance(command_title, str)
            or not command_title
            or len(command_title) > 4096
            or not isinstance(command_name, str)
            or not command_name
            or len(command_name) > 1024
        ):
            return None
        result["command"] = {
            "title": command_title,
            "command": command_name,
            "execution": "not_performed",
        }
    elif isinstance(command, str):
        if not command or len(command) > 1024:
            return None
        result["command"] = {
            "title": title,
            "command": command,
            "execution": "not_performed",
        }
    elif command is not None:
        return None
    if "edit" not in result and "command" not in result and "disabled" not in result:
        return None
    return result


def _normalize_diagnostic(item: dict[str, Any], server_name: str) -> dict[str, Any]:
    message = str(item.get("message", ""))[:4000]
    result: dict[str, Any] = {
        "range": _normalize_range(item.get("range")),
        "severity": _bounded_integer(item.get("severity"), 1, 4),
        "message": message,
        "source": str(item.get("source") or server_name)[:128],
    }
    if "code" in item:
        result["code"] = str(item["code"])[:256]
    return result


def _normalize_range(value: Any) -> dict[str, dict[str, int]]:
    raw = value if isinstance(value, dict) else {}
    return {
        "start": _normalize_position(raw.get("start")),
        "end": _normalize_position(raw.get("end")),
    }


def _normalize_position(value: Any) -> dict[str, int]:
    raw = value if isinstance(value, dict) else {}
    return {
        "line": _bounded_integer(raw.get("line"), 0, 10_000_000) or 0,
        "character": _bounded_integer(raw.get("character"), 0, 10_000_000) or 0,
    }


def _bounded_integer(value: Any, minimum: int, maximum: int) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return min(maximum, max(minimum, value))


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
