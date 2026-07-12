"""Agent Client Protocol v1 adapter backed by isolated Ash SDK sessions."""

from __future__ import annotations

import asyncio
import html
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from importlib.metadata import version
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
from uuid import uuid4

from acp import PROTOCOL_VERSION, RequestError, run_agent
from acp.helpers import (
    start_tool_call,
    update_agent_message_text,
    update_agent_thought_text,
    update_tool_call,
    update_user_message_text,
)
from acp.interfaces import Client as ACPClient
from acp.schema import (
    AcpMcpServer,
    AgentCapabilities,
    AllowedOutcome,
    AudioContentBlock,
    AuthenticateResponse,
    ClientCapabilities,
    CloseSessionResponse,
    EmbeddedResourceContentBlock,
    ForkSessionResponse,
    HttpMcpServer,
    ImageContentBlock,
    Implementation,
    InitializeResponse,
    ListSessionsResponse,
    LoadSessionResponse,
    McpCapabilities,
    McpServerStdio,
    NewSessionResponse,
    PermissionOption,
    PromptCapabilities,
    PromptResponse,
    ResourceContentBlock,
    ResumeSessionResponse,
    SessionCapabilities,
    SessionCloseCapabilities,
    SessionInfo,
    SessionListCapabilities,
    SseMcpServer,
    SetSessionConfigOptionResponse,
    SetSessionModeResponse,
    TextContentBlock,
    ToolCallLocation,
    ToolCallStatus,
    ToolCallUpdate,
    ToolKind,
    UsageUpdate,
)

from ash.sdk import AshClient, AshEvent
from config import AshConfig
from core.redaction import redact_text, redact_value
from core.session import Message, SessionStore, ToolCallRecord, normalize_project_path
from mcp.server import MCP_SERVER_NAME, MCPServerConfig


MAX_ACP_SESSIONS = 16
MAX_ACP_PROMPT_BLOCKS = 64
MAX_ACP_PROMPT_BYTES = 1_000_000
MAX_ACP_MCP_SERVERS = 32
MAX_ACP_MCP_VALUES = 64
MAX_ACP_MCP_FIELD_BYTES = 64 * 1024
MAX_ACP_MCP_TOTAL_BYTES = 1_000_000
MAX_ACP_STDIO_BYTES = 16 * 1024 * 1024
SESSION_PAGE_SIZE = 50
MAX_SESSION_CURSOR = 10_000

ACPClientFactory = Callable[
    [Path, str | None, dict[str, MCPServerConfig], Callable[[str, dict], Awaitable[bool]]],
    Awaitable[AshClient],
]


@dataclass
class _ACPSession:
    workspace: Path
    client: AshClient
    prompt_task: asyncio.Task[Any] | None = None


class AshACPAgent:
    """Expose one bounded set of Ash runtimes over an ACP connection."""

    def __init__(
        self,
        *,
        client_factory: ACPClientFactory | None = None,
        max_sessions: int = MAX_ACP_SESSIONS,
    ) -> None:
        if max_sessions < 1:
            raise ValueError("ACP max_sessions must be positive")
        self._client_factory = client_factory or _create_ash_client
        self._max_sessions = max_sessions
        self._sessions: dict[str, _ACPSession] = {}
        self._pending_sessions = 0
        self._lock = asyncio.Lock()
        self._connection: ACPClient | None = None

    def on_connect(self, conn: ACPClient) -> None:
        self._connection = conn

    async def initialize(
        self,
        protocol_version: int,
        client_capabilities: ClientCapabilities | None = None,
        client_info: Implementation | None = None,
        **kwargs: Any,
    ) -> InitializeResponse:
        return InitializeResponse(
            protocol_version=(
                protocol_version
                if protocol_version == PROTOCOL_VERSION
                else PROTOCOL_VERSION
            ),
            agent_capabilities=AgentCapabilities(
                load_session=True,
                prompt_capabilities=PromptCapabilities(
                    image=False,
                    audio=False,
                    embedded_context=False,
                ),
                mcp_capabilities=McpCapabilities(http=True, sse=True, acp=False),
                session_capabilities=SessionCapabilities(
                    list=SessionListCapabilities(),
                    close=SessionCloseCapabilities(),
                ),
            ),
            auth_methods=[],
            agent_info=Implementation(
                name="ash",
                title="Ash",
                version=version("ash-ai"),
            ),
        )

    async def new_session(
        self,
        cwd: str,
        additional_directories: list[str] | None = None,
        mcp_servers: list[Any] | None = None,
        **kwargs: Any,
    ) -> NewSessionResponse:
        workspace = _workspace(cwd)
        _reject_additional_directories(additional_directories)
        configs = _mcp_configs(mcp_servers or [])
        await self._reserve_session()
        client: AshClient | None = None
        session_id = ""
        try:
            approval = self._approval_callback(lambda: session_id)
            client = await self._client_factory(workspace, None, configs, approval)
            if client.loop.current_session is None:
                raise RuntimeError("Ash did not create an ACP session")
            session_id = client.loop.current_session.session_id
            async with self._lock:
                if session_id in self._sessions:
                    raise RuntimeError("Ash returned a duplicate session ID")
                self._sessions[session_id] = _ACPSession(workspace, client)
            return NewSessionResponse(session_id=session_id)
        except Exception:
            if client is not None:
                await client.close()
            raise
        finally:
            await self._release_reservation()

    async def load_session(
        self,
        cwd: str,
        session_id: str,
        mcp_servers: list[Any] | None = None,
        additional_directories: list[str] | None = None,
        **kwargs: Any,
    ) -> LoadSessionResponse:
        workspace = _workspace(cwd)
        _reject_additional_directories(additional_directories)
        configs = _mcp_configs(mcp_servers or [])
        await self._reserve_session(session_id=session_id)
        client: AshClient | None = None
        try:
            config = AshConfig.load(workspace_root=workspace)
            stored = SessionStore(config.db_directory / "sessions.db").load_session(
                session_id
            )
            if normalize_project_path(stored.project_path) != normalize_project_path(
                workspace
            ):
                raise RequestError.invalid_params(
                    {"sessionId": "session belongs to a different workspace"}
                )
            approval = self._approval_callback(lambda: session_id)
            client = await self._client_factory(
                workspace, session_id, configs, approval
            )
            async with self._lock:
                self._sessions[session_id] = _ACPSession(workspace, client)
            await self._replay(session_id, stored.messages, stored.tool_calls)
            return LoadSessionResponse()
        except KeyError as exc:
            if client is not None:
                await client.close()
            raise RequestError.resource_not_found(session_id) from exc
        except Exception:
            if client is not None:
                await client.close()
            async with self._lock:
                self._sessions.pop(session_id, None)
            raise
        finally:
            await self._release_reservation()

    async def list_sessions(
        self,
        cwd: str | None = None,
        cursor: str | None = None,
        **kwargs: Any,
    ) -> ListSessionsResponse:
        workspace = _workspace(cwd) if cwd is not None else None
        offset = _decode_cursor(cursor)
        config = (
            AshConfig.load(workspace_root=workspace)
            if workspace is not None
            else AshConfig.load()
        )
        store = SessionStore(config.db_directory / "sessions.db")
        items = store.list_sessions(
            project_path=str(workspace) if workspace is not None else None,
            limit=offset + SESSION_PAGE_SIZE + 1,
        )[offset:]
        page = items[:SESSION_PAGE_SIZE]
        next_cursor = (
            _encode_cursor(offset + SESSION_PAGE_SIZE)
            if len(items) > SESSION_PAGE_SIZE
            else None
        )
        return ListSessionsResponse(
            sessions=[
                SessionInfo(
                    session_id=item.session_id,
                    cwd=str(Path(item.project_path).resolve()),
                    title=item.title or item.branch_name or None,
                    updated_at=item.updated_at.isoformat(),
                )
                for item in page
            ],
            next_cursor=next_cursor,
        )

    async def prompt(
        self,
        session_id: str,
        prompt: list[
            TextContentBlock
            | ImageContentBlock
            | AudioContentBlock
            | ResourceContentBlock
            | EmbeddedResourceContentBlock
        ],
        **kwargs: Any,
    ) -> PromptResponse:
        state = await self._session(session_id)
        text = _prompt_text(prompt)
        current = asyncio.current_task()
        if current is None:
            raise RequestError.internal_error()
        async with self._lock:
            if state.prompt_task is not None and not state.prompt_task.done():
                raise RequestError.invalid_request(
                    {"sessionId": session_id, "reason": "turn already running"}
                )
            state.prompt_task = current

        sent_text = False
        failure = ""
        cancelled = False
        try:
            async for event in state.client.stream_prompt(text):
                if event.type == "assistant.delta" and event.data.get("text"):
                    sent_text = True
                if event.type == "turn.error":
                    failure = redact_text(str(event.data.get("error", "turn failed")))
                if event.type == "turn.cancelled":
                    cancelled = True
                await self._send_event(session_id, event)
                if event.type == "turn.completed" and not sent_text:
                    response = str(event.data.get("response", ""))
                    if response:
                        await self._notify(
                            session_id, update_agent_message_text(response)
                        )
        except asyncio.CancelledError:
            cancelled = True
        finally:
            async with self._lock:
                if state.prompt_task is current:
                    state.prompt_task = None
        if cancelled:
            return PromptResponse(stop_reason="cancelled")
        if failure:
            raise RequestError(-32001, "Ash turn failed", {"message": failure})
        return PromptResponse(stop_reason="end_turn")

    async def cancel(self, session_id: str, **kwargs: Any) -> None:
        state = await self._session(session_id)
        task = state.prompt_task
        if task is not None and not task.done():
            task.cancel()

    async def close_session(
        self, session_id: str, **kwargs: Any
    ) -> CloseSessionResponse:
        state = await self._pop_session(session_id)
        if state.prompt_task is not None and not state.prompt_task.done():
            state.prompt_task.cancel()
            await asyncio.gather(state.prompt_task, return_exceptions=True)
        await state.client.close()
        return CloseSessionResponse()

    async def set_session_mode(
        self, session_id: str, mode_id: str, **kwargs: Any
    ) -> SetSessionModeResponse | None:
        raise RequestError.method_not_found("session/set_mode")

    async def set_config_option(
        self,
        config_id: str,
        session_id: str,
        value: str | bool,
        **kwargs: Any,
    ) -> SetSessionConfigOptionResponse | None:
        raise RequestError.method_not_found("session/set_config_option")

    async def authenticate(
        self, method_id: str, **kwargs: Any
    ) -> AuthenticateResponse | None:
        raise RequestError.method_not_found("authenticate")

    async def fork_session(
        self,
        session_id: str,
        cwd: str,
        additional_directories: list[str] | None = None,
        mcp_servers: list[Any] | None = None,
        **kwargs: Any,
    ) -> ForkSessionResponse:
        raise RequestError.method_not_found("session/fork")

    async def resume_session(
        self,
        session_id: str,
        cwd: str,
        additional_directories: list[str] | None = None,
        mcp_servers: list[Any] | None = None,
        **kwargs: Any,
    ) -> ResumeSessionResponse:
        raise RequestError.method_not_found("session/resume")

    async def ext_method(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        raise RequestError.method_not_found(f"_{method}")

    async def ext_notification(self, method: str, params: dict[str, Any]) -> None:
        return None

    async def aclose(self) -> None:
        async with self._lock:
            states = list(self._sessions.values())
            self._sessions.clear()
        prompt_tasks = [
            state.prompt_task
            for state in states
            if state.prompt_task is not None and not state.prompt_task.done()
        ]
        for task in prompt_tasks:
            task.cancel()
        await asyncio.gather(*prompt_tasks, return_exceptions=True)
        await asyncio.gather(
            *(state.client.close() for state in states), return_exceptions=True
        )

    async def _send_event(self, session_id: str, event: AshEvent) -> None:
        data = event.data
        if event.type == "assistant.delta":
            text = str(data.get("text", ""))
            if text:
                await self._notify(session_id, update_agent_message_text(text))
            return
        if event.type == "reasoning.delta":
            text = str(data.get("text", ""))
            if text:
                await self._notify(session_id, update_agent_thought_text(text))
            return
        if event.type == "tool.requested":
            await self._notify(
                session_id,
                start_tool_call(
                    str(data.get("call_id", "")),
                    str(data.get("tool", "tool")),
                    kind=_tool_kind(str(data.get("tool", ""))),
                    status="pending",
                    raw_input=data.get("arguments"),
                    locations=_tool_locations(data.get("arguments")),
                ),
            )
            return
        if event.type in {"tool.started", "tool.completed", "tool.denied", "tool.error"}:
            status: ToolCallStatus = {
                "tool.started": "in_progress",
                "tool.completed": "completed" if data.get("success", True) else "failed",
                "tool.denied": "failed",
                "tool.error": "failed",
            }[event.type]
            raw_output = (
                data.get("output")
                if event.type == "tool.completed"
                else data.get("error") or data.get("reason")
            )
            await self._notify(
                session_id,
                update_tool_call(
                    str(data.get("call_id", "")),
                    title=str(data.get("tool", "tool")),
                    kind=_tool_kind(str(data.get("tool", ""))),
                    status=status,
                    raw_output=raw_output,
                ),
            )
            return
        if event.type == "context.usage":
            current = data.get("current")
            maximum = data.get("maximum")
            if isinstance(current, int) and isinstance(maximum, int):
                await self._notify(
                    session_id,
                    UsageUpdate(session_update="usage_update", used=current, size=maximum),
                )

    async def _notify(self, session_id: str, update: Any) -> None:
        if self._connection is None:
            raise RuntimeError("ACP client connection is unavailable")
        await self._connection.session_update(session_id, update)

    async def _replay(
        self,
        session_id: str,
        messages: list[Message],
        tool_calls: list[ToolCallRecord],
    ) -> None:
        timeline: list[tuple[str, Message | ToolCallRecord]] = sorted(
            [
                *(("message", item) for item in messages),
                *(("tool", item) for item in tool_calls),
            ],
            key=lambda entry: entry[1].timestamp,
        )
        for item_type, item in timeline:
            if item_type == "tool":
                tool = item
                assert isinstance(tool, ToolCallRecord)
                await self._notify(
                    session_id,
                    start_tool_call(
                        tool.call_id,
                        tool.tool_name,
                        kind=_tool_kind(tool.tool_name),
                        status="pending",
                        raw_input=redact_value(tool.arguments),
                        locations=_tool_locations(tool.arguments),
                    ),
                )
                status: ToolCallStatus = (
                    "completed" if tool.executed and not tool.error else "failed"
                )
                await self._notify(
                    session_id,
                    update_tool_call(
                        tool.call_id,
                        title=tool.tool_name,
                        kind=_tool_kind(tool.tool_name),
                        status=status,
                        raw_output=redact_text(tool.error or tool.result or ""),
                    ),
                )
                continue
            message = item
            assert isinstance(message, Message)
            if message.role == "user":
                await self._notify(
                    session_id, update_user_message_text(message.content)
                )
            elif message.role == "assistant":
                await self._notify(
                    session_id, update_agent_message_text(message.content)
                )

    def _approval_callback(
        self, session_id: Callable[[], str]
    ) -> Callable[[str, dict], Awaitable[bool]]:
        async def approve(tool_name: str, arguments: dict) -> bool:
            if self._connection is None:
                return False
            permission_id = f"permission-{uuid4()}"
            tool_call = ToolCallUpdate(
                tool_call_id=permission_id,
                title=tool_name,
                kind=_tool_kind(tool_name),
                status="pending",
                raw_input=redact_value(arguments),
                locations=_tool_locations(arguments),
            )
            options = [
                PermissionOption(
                    option_id="allow_once", name="Allow once", kind="allow_once"
                ),
                PermissionOption(
                    option_id="reject_once", name="Reject", kind="reject_once"
                ),
            ]
            try:
                response = await self._connection.request_permission(
                    session_id(), tool_call, options
                )
            except Exception:
                return False
            return isinstance(response.outcome, AllowedOutcome) and (
                response.outcome.option_id == "allow_once"
            )

        return approve

    async def _session(self, session_id: str) -> _ACPSession:
        async with self._lock:
            state = self._sessions.get(session_id)
        if state is None:
            raise RequestError.resource_not_found(session_id)
        return state

    async def _pop_session(self, session_id: str) -> _ACPSession:
        async with self._lock:
            state = self._sessions.pop(session_id, None)
        if state is None:
            raise RequestError.resource_not_found(session_id)
        return state

    async def _reserve_session(self, *, session_id: str = "") -> None:
        async with self._lock:
            if session_id and session_id in self._sessions:
                raise RequestError.invalid_request(
                    {"sessionId": session_id, "reason": "session already loaded"}
                )
            if len(self._sessions) + self._pending_sessions >= self._max_sessions:
                raise RequestError(
                    -32003,
                    "ACP session limit reached",
                    {"maximum": self._max_sessions},
                )
            self._pending_sessions += 1

    async def _release_reservation(self) -> None:
        async with self._lock:
            self._pending_sessions = max(0, self._pending_sessions - 1)


async def run_acp_agent() -> None:
    agent = AshACPAgent()
    try:
        await run_agent(agent, stdio_buffer_limit_bytes=MAX_ACP_STDIO_BYTES)
    finally:
        await agent.aclose()


async def _create_ash_client(
    workspace: Path,
    session_id: str | None,
    mcp_configs: dict[str, MCPServerConfig],
    approval_callback: Callable[[str, dict], Awaitable[bool]],
) -> AshClient:
    config = AshConfig.load(workspace_root=workspace)
    return await AshClient.create(
        config=config,
        approval_callback=approval_callback,
        session_id=session_id,
        additional_mcp_configs=mcp_configs,
        run_maintenance=False,
    )


def _workspace(value: str) -> Path:
    if not value or len(value) > 4096:
        raise RequestError.invalid_params({"cwd": "missing or too long"})
    path = Path(value).expanduser()
    if not path.is_absolute():
        raise RequestError.invalid_params({"cwd": "must be an absolute path"})
    try:
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise RequestError.invalid_params({"cwd": "does not exist"}) from exc
    if not resolved.is_dir():
        raise RequestError.invalid_params({"cwd": "must be a directory"})
    return resolved


def _reject_additional_directories(values: list[str] | None) -> None:
    if values:
        raise RequestError.invalid_params(
            {"additionalDirectories": "Ash does not advertise this capability"}
        )


def _mcp_configs(servers: list[Any]) -> dict[str, MCPServerConfig]:
    if len(servers) > MAX_ACP_MCP_SERVERS:
        raise RequestError.invalid_params({"mcpServers": "too many servers"})
    configs: dict[str, MCPServerConfig] = {}
    total_bytes = 0
    for server in servers:
        name = str(getattr(server, "name", ""))
        if not MCP_SERVER_NAME.fullmatch(name) or name in configs:
            raise RequestError.invalid_params({"mcpServers": "invalid or duplicate name"})
        if isinstance(server, AcpMcpServer):
            raise RequestError.invalid_params(
                {"mcpServers": "ACP-transport MCP is not supported"}
            )
        if isinstance(server, McpServerStdio):
            if len(server.env) > MAX_ACP_MCP_VALUES or len(server.args) > 256:
                raise RequestError.invalid_params({"mcpServers": "stdio values exceed limits"})
            if not server.command or len({item.name for item in server.env}) != len(
                server.env
            ):
                raise RequestError.invalid_params(
                    {"mcpServers": "stdio command is empty or env names are duplicated"}
                )
            fields = [server.command, *server.args]
            fields.extend(value for item in server.env for value in (item.name, item.value))
            config = MCPServerConfig(
                name=name,
                command=server.command,
                args=list(server.args),
                env={item.name: item.value for item in server.env},
                transport="stdio",
            )
        elif isinstance(server, (HttpMcpServer, SseMcpServer)):
            if len(server.headers) > MAX_ACP_MCP_VALUES:
                raise RequestError.invalid_params({"mcpServers": "too many headers"})
            if len({item.name.casefold() for item in server.headers}) != len(
                server.headers
            ):
                raise RequestError.invalid_params(
                    {"mcpServers": "header names are duplicated"}
                )
            _validate_mcp_url(server.url)
            fields = [server.url]
            fields.extend(
                value for item in server.headers for value in (item.name, item.value)
            )
            config = MCPServerConfig(
                name=name,
                command="",
                args=[],
                env={},
                transport="http" if isinstance(server, HttpMcpServer) else "sse",
                url=server.url,
                headers={item.name: item.value for item in server.headers},
            )
        else:
            raise RequestError.invalid_params({"mcpServers": "unknown server type"})
        encoded_sizes = [len(value.encode("utf-8")) for value in fields]
        if any(size > MAX_ACP_MCP_FIELD_BYTES for size in encoded_sizes):
            raise RequestError.invalid_params({"mcpServers": "a value exceeds 64 KiB"})
        total_bytes += sum(encoded_sizes)
        if total_bytes > MAX_ACP_MCP_TOTAL_BYTES:
            raise RequestError.invalid_params({"mcpServers": "values exceed 1 MB total"})
        configs[name] = config
    return configs


def _validate_mcp_url(value: str) -> None:
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as exc:
        raise RequestError.invalid_params({"mcpServers": "invalid server URL"}) from exc
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or port is not None
        and not 0 < port <= 65535
    ):
        raise RequestError.invalid_params(
            {"mcpServers": "server URL must be HTTP(S) without credentials"}
        )


def _prompt_text(blocks: list[Any]) -> str:
    if not blocks or len(blocks) > MAX_ACP_PROMPT_BLOCKS:
        raise RequestError.invalid_params({"prompt": "must contain 1..64 blocks"})
    rendered: list[str] = []
    for block in blocks:
        if isinstance(block, TextContentBlock):
            rendered.append(block.text)
        elif isinstance(block, ResourceContentBlock):
            if len(block.uri) > 4096 or len(block.name) > 512:
                raise RequestError.invalid_params({"prompt": "resource link is too long"})
            rendered.append(
                '<resource_link name="'
                + html.escape(block.name, quote=True)
                + '" uri="'
                + html.escape(block.uri, quote=True)
                + '" />'
            )
        else:
            raise RequestError.invalid_params(
                {"prompt": f"unsupported content type: {getattr(block, 'type', '')}"}
            )
    text = "\n\n".join(rendered).strip()
    if not text or len(text.encode("utf-8")) > MAX_ACP_PROMPT_BYTES:
        raise RequestError.invalid_params({"prompt": "content is empty or exceeds 1 MB"})
    return text


def _decode_cursor(cursor: str | None) -> int:
    if cursor is None:
        return 0
    prefix = "ash-v1:"
    if not cursor.startswith(prefix) or not cursor[len(prefix) :].isdigit():
        raise RequestError.invalid_params({"cursor": "invalid cursor"})
    offset = int(cursor[len(prefix) :])
    if offset > MAX_SESSION_CURSOR:
        raise RequestError.invalid_params({"cursor": "cursor exceeds limit"})
    return offset


def _encode_cursor(offset: int) -> str:
    return f"ash-v1:{offset}"


def _tool_kind(tool_name: str) -> ToolKind:
    if tool_name in {"read_file", "list_directory", "glob_files", "find_symbol"}:
        return "read"
    if tool_name in {
        "write_file",
        "edit_file",
        "replace_file_content",
        "replace_file_edits",
        "apply_patch",
    }:
        return "edit"
    if tool_name in {"search_text", "find_references", "search_tools"}:
        return "search"
    if tool_name in {"web_fetch", "web_search", "browser_navigate"}:
        return "fetch"
    if tool_name in {"run_command", "background_process"}:
        return "execute"
    return "other"


def _tool_locations(arguments: Any) -> list[ToolCallLocation] | None:
    if not isinstance(arguments, dict):
        return None
    for key in ("file_path", "path"):
        value = arguments.get(key)
        if isinstance(value, str) and value:
            return [ToolCallLocation(path=value[:4096])]
    return None
