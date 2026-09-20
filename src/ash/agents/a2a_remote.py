"""Configured A2A 1.0 remote-agent discovery and delegation tools."""

from __future__ import annotations

import asyncio
import ipaddress
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from collections.abc import Awaitable, Callable
from typing import Any
from urllib.parse import urlsplit
from uuid import uuid4

import httpx
from pydantic import BaseModel, Field

from ash.agents.a2a_tasks import RemoteTaskStore
from ash.core.redaction import redact_text
from ash.safe_io import strict_json_loads
from ash.safety.guard import SafetyGuard
from ash.safe_io import read_bounded_bytes
from ash.tools.base import BaseTool, ToolResult, count_output_tokens


MAX_A2A_CONFIG_BYTES = 256 * 1024
MAX_A2A_REMOTE_RESPONSE_BYTES = 1_000_000
MAX_A2A_REMOTE_EVENTS = 10_000
A2A_AGENT_NAME = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
A2A_ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


async def _settle_cleanup_task_after_cancellation(
    task: asyncio.Task[Any],
) -> BaseException | None:
    """Wait for a shielded A2A cleanup task despite repeated cancellation."""

    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            current = asyncio.current_task()
            if current is not None:
                current.uncancel()
        except BaseException:
            # The cleanup task has normally reached a terminal state when its
            # exception is observed.  Read the result below without allowing a
            # cleanup failure to replace caller cancellation.
            if not task.done():
                raise
    try:
        task.result()
    except BaseException as exc:
        return exc
    return None


@dataclass(frozen=True)
class RemoteAgentConfig:
    name: str
    url: str
    description: str = ""
    token_env: str = "ASH_A2A_TOKEN"
    timeout_seconds: float = 300.0


@dataclass(frozen=True)
class RemoteAgentResult:
    response: str
    task_id: str
    context_id: str
    state: str


class ListRemoteAgentsArgs(BaseModel):
    query: str = Field(default="", max_length=256)


class DelegateRemoteAgentArgs(BaseModel):
    agent: str = Field(..., min_length=1, max_length=64)
    prompt: str = Field(..., min_length=1, max_length=1_000_000)
    context_id: str = Field(default="", max_length=512)


class RemoteAgentTaskArgs(BaseModel):
    agent: str = Field(..., min_length=1, max_length=64)
    task_id: str = Field(..., min_length=1, max_length=512)


class ListRemoteAgentTasksArgs(BaseModel):
    agent: str = Field(default="", max_length=64)
    limit: int = Field(default=100, ge=1, le=500)


class RecoverRemoteAgentTaskArgs(BaseModel):
    agent: str = Field(..., min_length=1, max_length=64)
    context_id: str = Field(..., min_length=1, max_length=512)


RemoteTaskObserver = Callable[[RemoteAgentResult], Awaitable[None]]
RemoteRequestObserver = Callable[[str], Awaitable[None]]


class _RemoteTaskStoreTool(BaseTool):
    def __init__(
        self,
        safety_guard: SafetyGuard,
        agents: dict[str, RemoteAgentConfig],
        task_store: RemoteTaskStore | None,
    ) -> None:
        super().__init__(safety_guard)
        self.agents = dict(agents)
        self.task_store = task_store

    async def aclose(self) -> None:
        if self.task_store is not None:
            self.task_store.close()

    async def _check_binding(self, config: RemoteAgentConfig, task_id: str) -> None:
        if self.task_store is None:
            return
        try:
            conflict = await asyncio.to_thread(
                self.task_store.conflicting_endpoint,
                agent=config.name,
                endpoint=config.url,
                task_id=task_id,
            )
        except Exception as exc:
            raise RuntimeError("could not read durable A2A remote task state") from exc
        if conflict is not None:
            raise ValueError(
                f"remote task {task_id!r} for agent {config.name!r} is bound to "
                "a different configured endpoint"
            )

    async def _remember(
        self,
        config: RemoteAgentConfig,
        result: RemoteAgentResult,
    ) -> None:
        if self.task_store is None or not result.task_id:
            return
        try:
            await asyncio.to_thread(
                self.task_store.save,
                agent=config.name,
                endpoint=config.url,
                task_id=result.task_id,
                context_id=result.context_id,
                state=result.state,
            )
        except Exception as exc:
            raise RuntimeError("could not persist A2A remote task handle") from exc

    async def _remember_intent(
        self,
        config: RemoteAgentConfig,
        context_id: str,
    ) -> None:
        if self.task_store is None:
            return
        try:
            await asyncio.to_thread(
                self.task_store.save_intent,
                agent=config.name,
                endpoint=config.url,
                context_id=context_id,
            )
        except Exception as exc:
            raise RuntimeError("could not persist pending A2A remote task") from exc

    async def _forget_intent(
        self,
        config: RemoteAgentConfig,
        context_id: str,
    ) -> None:
        if self.task_store is None or not context_id:
            return
        try:
            await asyncio.to_thread(
                self.task_store.delete_intent,
                agent=config.name,
                endpoint=config.url,
                context_id=context_id,
            )
        except Exception as exc:
            raise RuntimeError("could not update pending A2A remote task") from exc

    async def _check_intent_binding(
        self,
        config: RemoteAgentConfig,
        context_id: str,
    ) -> bool:
        if self.task_store is None:
            return False
        try:
            endpoint = await asyncio.to_thread(
                self.task_store.get_intent_endpoint,
                agent=config.name,
                context_id=context_id,
            )
        except Exception as exc:
            raise RuntimeError("could not read pending A2A remote task state") from exc
        if endpoint is None:
            return False
        if endpoint != config.url:
            raise ValueError(
                f"pending remote context {context_id!r} for agent {config.name!r} "
                "is bound to a different configured endpoint"
            )
        return True


class ListRemoteAgentsTool(BaseTool):
    name = "list_remote_agents"
    description = (
        "List explicitly configured A2A remote agents available for delegation."
    )
    args_schema = ListRemoteAgentsArgs

    def __init__(
        self, safety_guard: SafetyGuard, agents: dict[str, RemoteAgentConfig]
    ) -> None:
        super().__init__(safety_guard)
        self.agents = dict(agents)

    async def run(self, **kwargs: Any) -> ToolResult:
        args = self.validate_args(**kwargs)
        assert isinstance(args, ListRemoteAgentsArgs)
        query = args.query.casefold()
        values = [
            agent
            for agent in self.agents.values()
            if not query
            or query in agent.name.casefold()
            or query in agent.description.casefold()
        ]
        output = json.dumps(
            [
                {
                    "name": agent.name,
                    "description": agent.description,
                    "credential_configured": bool(os.environ.get(agent.token_env)),
                }
                for agent in sorted(values, key=lambda item: item.name)
            ]
        )
        return ToolResult(
            success=True,
            output=output,
            token_count=count_output_tokens(output),
        )


class DelegateRemoteAgentTool(_RemoteTaskStoreTool):
    name = "delegate_remote_agent"
    description = (
        "Delegate a bounded text task to an explicitly configured A2A remote agent; "
        "reuse the returned context_id for follow-up work."
    )
    args_schema = DelegateRemoteAgentArgs

    def __init__(
        self,
        safety_guard: SafetyGuard,
        agents: dict[str, RemoteAgentConfig],
        task_store: RemoteTaskStore | None = None,
    ) -> None:
        super().__init__(safety_guard, agents, task_store)

    async def run(self, **kwargs: Any) -> ToolResult:
        args = self.validate_args(**kwargs)
        assert isinstance(args, DelegateRemoteAgentArgs)
        config = self.agents.get(args.agent)
        if config is None:
            return ToolResult(
                success=False,
                output="",
                error=f"unknown remote agent: {args.agent}",
            )
        pending_context = ""
        try:
            async def remember_request(context_id: str) -> None:
                nonlocal pending_context
                pending_context = context_id
                await self._remember_intent(config, context_id)

            async def remember(result: RemoteAgentResult) -> None:
                await self._remember(config, result)

            result = await send_remote_agent(
                config,
                args.prompt,
                context_id=args.context_id,
                request_observer=(
                    remember_request if self.task_store is not None else None
                ),
                task_observer=remember,
            )
            if pending_context and (result.task_id or result.state == "MESSAGE"):
                await self._forget_intent(config, pending_context)
        except ModuleNotFoundError as exc:
            if exc.name == "a2a" or (exc.name or "").startswith("a2a."):
                from ash.install import pipx_install_command

                return ToolResult(
                    success=False,
                    output="",
                    error=(f"A2A support requires `{pipx_install_command('a2a')}`."),
                )
            raise
        except (httpx.HTTPError, RuntimeError, ValueError) as exc:
            return ToolResult(
                success=False,
                output="",
                error=redact_text(str(exc)),
            )
        except Exception as exc:
            from a2a.utils.errors import A2AError

            if not isinstance(exc, A2AError):
                raise
            return ToolResult(
                success=False,
                output="",
                error=redact_text(str(exc)),
            )
        payload = json.dumps(
            {
                "agent": config.name,
                "task_id": result.task_id or None,
                "context_id": result.context_id or None,
                "state": result.state,
                "response": result.response,
            }
        )
        success = result.state in {"TASK_STATE_COMPLETED", "MESSAGE"}
        return ToolResult(
            success=success,
            output=payload if success else "",
            error=None if success else f"remote task ended in {result.state}",
            token_count=count_output_tokens(payload) if success else 0,
        )


class RemoteAgentTaskStatusTool(_RemoteTaskStoreTool):
    name = "remote_agent_task_status"
    description = "Fetch the current state and bounded text output of an A2A task."
    args_schema = RemoteAgentTaskArgs

    def __init__(
        self,
        safety_guard: SafetyGuard,
        agents: dict[str, RemoteAgentConfig],
        task_store: RemoteTaskStore | None = None,
    ) -> None:
        super().__init__(safety_guard, agents, task_store)

    async def run(self, **kwargs: Any) -> ToolResult:
        args = self.validate_args(**kwargs)
        assert isinstance(args, RemoteAgentTaskArgs)
        config = self.agents.get(args.agent)
        if config is None:
            return ToolResult(
                success=False,
                output="",
                error=f"unknown remote agent: {args.agent}",
            )
        try:
            await self._check_binding(config, args.task_id)
            result = await get_remote_agent_task(config, args.task_id)
            await self._remember(config, result)
        except ModuleNotFoundError as exc:
            if exc.name == "a2a" or (exc.name or "").startswith("a2a."):
                from ash.install import pipx_install_command

                return ToolResult(
                    success=False,
                    output="",
                    error=(f"A2A support requires `{pipx_install_command('a2a')}`."),
                )
            raise
        except (httpx.HTTPError, RuntimeError, ValueError) as exc:
            return ToolResult(
                success=False,
                output="",
                error=redact_text(str(exc)),
            )
        except Exception as exc:
            from a2a.utils.errors import A2AError

            if not isinstance(exc, A2AError):
                raise
            return ToolResult(
                success=False,
                output="",
                error=redact_text(str(exc)),
            )
        payload = json.dumps(
            {
                "agent": config.name,
                "task_id": result.task_id,
                "context_id": result.context_id or None,
                "state": result.state,
                "response": result.response,
            }
        )
        return ToolResult(
            success=True,
            output=payload,
            token_count=count_output_tokens(payload),
        )


class RemoteAgentTaskCancelTool(_RemoteTaskStoreTool):
    name = "remote_agent_task_cancel"
    description = "Cancel a known A2A remote task and return its resulting state."
    args_schema = RemoteAgentTaskArgs

    def __init__(
        self,
        safety_guard: SafetyGuard,
        agents: dict[str, RemoteAgentConfig],
        task_store: RemoteTaskStore | None = None,
    ) -> None:
        super().__init__(safety_guard, agents, task_store)

    async def run(self, **kwargs: Any) -> ToolResult:
        args = self.validate_args(**kwargs)
        assert isinstance(args, RemoteAgentTaskArgs)
        config = self.agents.get(args.agent)
        if config is None:
            return ToolResult(
                success=False,
                output="",
                error=f"unknown remote agent: {args.agent}",
            )
        try:
            await self._check_binding(config, args.task_id)
            result = await cancel_remote_agent_task(config, args.task_id)
            await self._remember(config, result)
        except ModuleNotFoundError as exc:
            if exc.name == "a2a" or (exc.name or "").startswith("a2a."):
                from ash.install import pipx_install_command

                return ToolResult(
                    success=False,
                    output="",
                    error=(f"A2A support requires `{pipx_install_command('a2a')}`."),
                )
            raise
        except (httpx.HTTPError, RuntimeError, ValueError) as exc:
            return ToolResult(
                success=False,
                output="",
                error=redact_text(str(exc)),
            )
        except Exception as exc:
            from a2a.utils.errors import A2AError

            if not isinstance(exc, A2AError):
                raise
            return ToolResult(
                success=False,
                output="",
                error=redact_text(str(exc)),
            )
        payload = json.dumps(
            {
                "agent": config.name,
                "task_id": result.task_id,
                "context_id": result.context_id or None,
                "state": result.state,
            }
        )
        return ToolResult(
            success=True,
            output=payload,
            token_count=count_output_tokens(payload),
        )


class ListRemoteAgentTasksTool(_RemoteTaskStoreTool):
    name = "list_remote_agent_tasks"
    description = (
        "List durable outbound A2A task handles for this workspace without network use."
    )
    args_schema = ListRemoteAgentTasksArgs

    async def run(self, **kwargs: Any) -> ToolResult:
        args = self.validate_args(**kwargs)
        assert isinstance(args, ListRemoteAgentTasksArgs)
        if self.task_store is None:
            rows: list[Any] = []
            intents: list[Any] = []
        else:
            try:
                rows = await asyncio.to_thread(
                    self.task_store.list,
                    agent=args.agent or None,
                    limit=args.limit,
                )
                intents = await asyncio.to_thread(
                    self.task_store.list_intents,
                    agent=args.agent or None,
                    limit=args.limit,
                )
            except Exception as exc:
                return ToolResult(
                    success=False,
                    output="",
                    error=redact_text(
                        f"could not read durable A2A remote task state: {exc}"
                    ),
                )
        payload: list[tuple[str, dict[str, Any]]] = []
        for row in rows:
            configured = self.agents.get(row.agent)
            binding = (
                "current"
                if configured is not None and configured.url == row.endpoint
                else "agent_unconfigured"
                if configured is None
                else "endpoint_changed"
            )
            payload.append(
                (
                    row.updated_at,
                    {
                        "agent": row.agent,
                        "task_id": row.task_id,
                        "context_id": row.context_id or None,
                        "state": row.state,
                        "binding": binding,
                        "updated_at": row.updated_at,
                    },
                )
            )
        for row in intents:
            configured = self.agents.get(row.agent)
            binding = (
                "current"
                if configured is not None and configured.url == row.endpoint
                else "agent_unconfigured"
                if configured is None
                else "endpoint_changed"
            )
            payload.append(
                (
                    row.updated_at,
                    {
                        "agent": row.agent,
                        "task_id": None,
                        "context_id": row.context_id,
                        "state": "PENDING_REMOTE_ACCEPTANCE",
                        "binding": binding,
                        "updated_at": row.updated_at,
                    },
                )
            )
        payload.sort(key=lambda item: item[0], reverse=True)
        output = json.dumps([item for _, item in payload[: args.limit]])
        return ToolResult(
            success=True,
            output=output,
            token_count=count_output_tokens(output),
        )


class RecoverRemoteAgentTaskTool(_RemoteTaskStoreTool):
    name = "recover_remote_agent_task"
    description = (
        "Resolve a pending durable A2A context to its remote task after an "
        "interrupted new delegation."
    )
    args_schema = RecoverRemoteAgentTaskArgs

    async def run(self, **kwargs: Any) -> ToolResult:
        args = self.validate_args(**kwargs)
        assert isinstance(args, RecoverRemoteAgentTaskArgs)
        config = self.agents.get(args.agent)
        if config is None:
            return ToolResult(
                success=False,
                output="",
                error=f"unknown remote agent: {args.agent}",
            )
        try:
            known = await self._check_intent_binding(config, args.context_id)
            if not known:
                return ToolResult(
                    success=False,
                    output="",
                    error=(
                        f"unknown pending remote context for agent {args.agent}: "
                        f"{args.context_id}"
                    ),
                )
            result = await recover_remote_agent_task(config, args.context_id)
            if result is not None:
                await self._remember(config, result)
        except ModuleNotFoundError as exc:
            if exc.name == "a2a" or (exc.name or "").startswith("a2a."):
                from ash.install import pipx_install_command

                return ToolResult(
                    success=False,
                    output="",
                    error=(f"A2A support requires `{pipx_install_command('a2a')}`."),
                )
            raise
        except (httpx.HTTPError, RuntimeError, ValueError) as exc:
            return ToolResult(
                success=False,
                output="",
                error=redact_text(str(exc)),
            )
        except Exception as exc:
            from a2a.utils.errors import A2AError

            if not isinstance(exc, A2AError):
                raise
            return ToolResult(
                success=False,
                output="",
                error=redact_text(str(exc)),
            )
        payload = json.dumps(
            {
                "agent": config.name,
                "task_id": result.task_id if result is not None else None,
                "context_id": args.context_id,
                "state": (
                    result.state if result is not None else "PENDING_REMOTE_ACCEPTANCE"
                ),
            }
        )
        return ToolResult(
            success=True,
            output=payload,
            token_count=count_output_tokens(payload),
        )


def load_remote_agent_configs(
    workspace: Path,
    *,
    include_project: bool,
) -> dict[str, RemoteAgentConfig]:
    paths: list[tuple[Path, Path | None]] = [
        (Path.home() / ".ash" / "a2a.json", None)
    ]
    if include_project:
        paths.append((workspace / ".ash" / "a2a.json", workspace))
    agents: dict[str, RemoteAgentConfig] = {}
    for path, trusted_root in paths:
        if not path.is_file():
            continue
        try:
            raw_bytes = read_bounded_bytes(
                path,
                MAX_A2A_CONFIG_BYTES,
                label="A2A config",
                trusted_root=trusted_root,
            )
            payload = strict_json_loads(raw_bytes)
        except (OSError, ValueError) as exc:
            raise ValueError(f"invalid A2A config {path}: {exc}") from exc
        if not isinstance(payload, dict) or set(payload) != {"agents"}:
            raise ValueError(f"A2A config {path} must contain only an agents object")
        raw_agents = payload["agents"]
        if not isinstance(raw_agents, dict) or len(raw_agents) > 64:
            raise ValueError(
                f"A2A config {path} agents must be an object of at most 64 entries"
            )
        for name, raw in raw_agents.items():
            if not isinstance(name, str) or not A2A_AGENT_NAME.fullmatch(name):
                raise ValueError(f"invalid A2A agent name in {path}: {name!r}")
            if name in agents:
                raise ValueError(f"duplicate A2A agent name: {name}")
            agents[name] = _parse_agent_config(name, raw, path)
    return agents


async def send_remote_agent(
    config: RemoteAgentConfig,
    prompt: str,
    *,
    context_id: str = "",
    transport: httpx.AsyncBaseTransport | None = None,
    request_observer: RemoteRequestObserver | None = None,
    task_observer: RemoteTaskObserver | None = None,
) -> RemoteAgentResult:
    from a2a.client import A2ACardResolver, ClientConfig, ClientFactory
    from a2a.types.a2a_pb2 import (
        CancelTaskRequest,
        Message,
        Part,
        Role,
        SendMessageRequest,
        TaskState,
    )
    from a2a.utils.constants import TransportProtocol

    _validate_remote_url(config.url)
    if not prompt.strip() or len(prompt.encode("utf-8")) > 1_000_000:
        raise ValueError("remote-agent prompt must be non-empty and at most 1 MB")
    if context_id and len(context_id.encode("utf-8")) > 512:
        raise ValueError("remote-agent context ID exceeds 512 bytes")
    generated_context = not context_id and request_observer is not None
    resolved_context = str(uuid4()) if generated_context else context_id
    token = os.environ.get(config.token_env, "")
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    async with httpx.AsyncClient(
        headers=headers,
        timeout=httpx.Timeout(
            config.timeout_seconds,
            connect=min(config.timeout_seconds, 15),
        ),
        follow_redirects=False,
        transport=transport,
    ) as http:
        card = await A2ACardResolver(http, config.url).get_agent_card()
        validate_agent_card_origins(config.url, card.supported_interfaces)
        client = ClientFactory(
            ClientConfig(
                httpx_client=http,
                streaming=True,
                supported_protocol_bindings=[TransportProtocol.JSONRPC],
                accepted_output_modes=["text/plain"],
            )
        ).create(card)
        task_id = ""
        state = ""
        chunks: list[str] = []
        output_bytes = 0
        event_count = 0
        client_closed = False
        try:
            if generated_context and request_observer is not None:
                await request_observer(resolved_context)
            message = Message(
                message_id=str(uuid4()),
                role=Role.ROLE_USER,
                parts=[Part(text=prompt)],
            )
            if resolved_context:
                message.context_id = resolved_context
            async for event in client.send_message(SendMessageRequest(message=message)):
                event_count += 1
                if event_count > MAX_A2A_REMOTE_EVENTS:
                    raise RuntimeError("remote A2A response exceeded 10,000 events")
                if event.HasField("task"):
                    task_id = event.task.id
                    resolved_context = event.task.context_id
                    state = TaskState.Name(event.task.status.state)
                elif event.HasField("message"):
                    task_id = event.message.task_id
                    resolved_context = event.message.context_id
                    state = "MESSAGE"
                    output_bytes = _append_text_parts(
                        event.message.parts, chunks, output_bytes
                    )
                elif event.HasField("status_update"):
                    task_id = event.status_update.task_id
                    resolved_context = event.status_update.context_id
                    state = TaskState.Name(event.status_update.status.state)
                elif event.HasField("artifact_update"):
                    task_id = event.artifact_update.task_id
                    resolved_context = event.artifact_update.context_id
                    output_bytes = _append_text_parts(
                        event.artifact_update.artifact.parts,
                        chunks,
                        output_bytes,
                    )
                if task_id and task_observer is not None:
                    try:
                        await task_observer(
                            RemoteAgentResult(
                                response="",
                                task_id=task_id,
                                context_id=resolved_context,
                                state=state or "UNKNOWN",
                            )
                        )
                    except Exception as observer_error:
                        cancel_task = asyncio.create_task(
                            client.cancel_task(CancelTaskRequest(id=task_id))
                        )
                        cleanup_error = await _settle_cleanup_task_after_cancellation(
                            cancel_task
                        )
                        if cleanup_error is not None:
                            observer_error.add_note(
                                "remote task cancellation after durable-state failure "
                                f"also failed: {cleanup_error}"
                            )
                        raise
        except asyncio.CancelledError:
            if task_id:
                cancel_task = asyncio.create_task(
                    client.cancel_task(CancelTaskRequest(id=task_id))
                )
                await _settle_cleanup_task_after_cancellation(cancel_task)
            close_task = asyncio.create_task(client.close())
            await _settle_cleanup_task_after_cancellation(close_task)
            client_closed = True
            raise
        finally:
            if not client_closed:
                await client.close()
    return RemoteAgentResult(
        response="".join(chunks),
        task_id=task_id,
        context_id=resolved_context,
        state=state or "UNKNOWN",
    )


async def recover_remote_agent_task(
    config: RemoteAgentConfig,
    context_id: str,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
) -> RemoteAgentResult | None:
    from a2a.types.a2a_pb2 import ListTasksRequest, TaskState

    _validate_remote_context_id(context_id)
    http, client = await _open_remote_agent_client(config, transport=transport)
    closed = False
    try:
        response = await client.list_tasks(
            ListTasksRequest(
                context_id=context_id,
                page_size=2,
                history_length=0,
                include_artifacts=False,
            )
        )
        tasks = list(response.tasks)
        if not tasks:
            return None
        if len(tasks) != 1 or response.next_page_token:
            raise RuntimeError(
                "pending A2A context resolved to multiple remote tasks; refusing to guess"
            )
        task = tasks[0]
        _validate_remote_task_id(task.id)
        if task.context_id != context_id:
            raise RuntimeError("remote A2A task recovery returned a mismatched context")
        return RemoteAgentResult(
            response="",
            task_id=task.id,
            context_id=task.context_id,
            state=TaskState.Name(task.status.state),
        )
    except asyncio.CancelledError:
        close_task = asyncio.create_task(client.close())
        await _settle_cleanup_task_after_cancellation(close_task)
        closed = True
        raise
    finally:
        if not closed:
            await client.close()
        await http.aclose()


async def get_remote_agent_task(
    config: RemoteAgentConfig,
    task_id: str,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
) -> RemoteAgentResult:
    from a2a.types.a2a_pb2 import GetTaskRequest, TaskState

    _validate_remote_task_id(task_id)
    http, client = await _open_remote_agent_client(config, transport=transport)
    closed = False
    try:
        task = await client.get_task(GetTaskRequest(id=task_id))
        chunks: list[str] = []
        output_bytes = 0
        for artifact in task.artifacts:
            output_bytes = _append_text_parts(
                artifact.parts,
                chunks,
                output_bytes,
            )
        return RemoteAgentResult(
            response="".join(chunks),
            task_id=task.id,
            context_id=task.context_id,
            state=TaskState.Name(task.status.state),
        )
    except asyncio.CancelledError:
        close_task = asyncio.create_task(client.close())
        await _settle_cleanup_task_after_cancellation(close_task)
        closed = True
        raise
    finally:
        if not closed:
            await client.close()
        await http.aclose()


async def cancel_remote_agent_task(
    config: RemoteAgentConfig,
    task_id: str,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
) -> RemoteAgentResult:
    from a2a.types.a2a_pb2 import CancelTaskRequest, TaskState

    _validate_remote_task_id(task_id)
    http, client = await _open_remote_agent_client(config, transport=transport)
    closed = False
    try:
        task = await client.cancel_task(CancelTaskRequest(id=task_id))
        return RemoteAgentResult(
            response="",
            task_id=task.id,
            context_id=task.context_id,
            state=TaskState.Name(task.status.state),
        )
    except asyncio.CancelledError:
        close_task = asyncio.create_task(client.close())
        await _settle_cleanup_task_after_cancellation(close_task)
        closed = True
        raise
    finally:
        if not closed:
            await client.close()
        await http.aclose()


async def _open_remote_agent_client(
    config: RemoteAgentConfig,
    *,
    transport: httpx.AsyncBaseTransport | None,
) -> tuple[httpx.AsyncClient, Any]:
    from a2a.client import A2ACardResolver, ClientConfig, ClientFactory
    from a2a.utils.constants import TransportProtocol

    _validate_remote_url(config.url)
    token = os.environ.get(config.token_env, "")
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    http = httpx.AsyncClient(
        headers=headers,
        timeout=httpx.Timeout(
            config.timeout_seconds,
            connect=min(config.timeout_seconds, 15),
        ),
        follow_redirects=False,
        transport=transport,
    )
    try:
        card = await A2ACardResolver(http, config.url).get_agent_card()
        validate_agent_card_origins(config.url, card.supported_interfaces)
        client = ClientFactory(
            ClientConfig(
                httpx_client=http,
                streaming=True,
                supported_protocol_bindings=[TransportProtocol.JSONRPC],
                accepted_output_modes=["text/plain"],
            )
        ).create(card)
    except BaseException:
        await http.aclose()
        raise
    return http, client


def _validate_remote_task_id(task_id: str) -> None:
    if not task_id or len(task_id.encode("utf-8")) > 512:
        raise ValueError("remote-agent task ID must be non-empty and at most 512 bytes")


def _validate_remote_context_id(context_id: str) -> None:
    if not context_id or len(context_id.encode("utf-8")) > 512:
        raise ValueError(
            "remote-agent context ID must be non-empty and at most 512 bytes"
        )


def _parse_agent_config(name: str, raw: Any, path: Path) -> RemoteAgentConfig:
    allowed = {"url", "description", "token_env", "timeout_seconds"}
    if not isinstance(raw, dict) or not set(raw) <= allowed:
        raise ValueError(f"invalid A2A agent {name!r} in {path}")
    url = raw.get("url")
    description = raw.get("description", "")
    token_env = raw.get("token_env", "ASH_A2A_TOKEN")
    timeout = raw.get("timeout_seconds", 300.0)
    if not isinstance(url, str):
        raise ValueError(f"A2A agent {name!r} requires a URL")
    _validate_remote_url(url)
    if not isinstance(description, str) or len(description) > 512:
        raise ValueError(f"A2A agent {name!r} description is invalid")
    if not isinstance(token_env, str) or not A2A_ENV_NAME.fullmatch(token_env):
        raise ValueError(f"A2A agent {name!r} token_env is invalid")
    if (
        isinstance(timeout, bool)
        or not isinstance(timeout, int | float)
        or not 1 <= float(timeout) <= 3600
    ):
        raise ValueError(f"A2A agent {name!r} timeout_seconds must be 1..3600")
    return RemoteAgentConfig(
        name, url.rstrip("/"), description, token_env, float(timeout)
    )


def _validate_remote_url(value: str) -> None:
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as exc:
        raise ValueError("invalid A2A remote URL") from exc
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or (port is not None and not 0 < port <= 65535)
    ):
        raise ValueError(
            "A2A remote URL must be HTTP(S) without credentials, query, or fragment"
        )
    if parsed.scheme == "http" and not _is_loopback_host(parsed.hostname):
        raise ValueError("A2A remote URL must use HTTPS except for loopback HTTP")


def _is_loopback_host(hostname: str) -> bool:
    host = hostname.casefold().rstrip(".")
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def validate_agent_card_origins(configured_url: str, interfaces: Any) -> None:
    expected = _origin(configured_url)
    jsonrpc_interfaces = [
        interface for interface in interfaces if interface.protocol_binding == "JSONRPC"
    ]
    if not jsonrpc_interfaces:
        raise ValueError("remote Agent Card does not declare a JSON-RPC interface")
    for interface in jsonrpc_interfaces:
        _validate_remote_url(interface.url)
        if _origin(interface.url) != expected:
            raise ValueError("remote Agent Card interface changed origin")


def _origin(value: str) -> tuple[str, str, int | None]:
    parsed = urlsplit(value)
    port = parsed.port
    if port is None:
        port = 443 if parsed.scheme == "https" else 80
    return parsed.scheme, parsed.hostname or "", port


def _append_text_parts(parts: Any, chunks: list[str], current_bytes: int) -> int:
    for part in parts:
        if part.WhichOneof("content") != "text":
            continue
        encoded = part.text.encode("utf-8")
        current_bytes += len(encoded)
        if current_bytes > MAX_A2A_REMOTE_RESPONSE_BYTES:
            raise RuntimeError("remote A2A text response exceeded 1 MB")
        chunks.append(part.text)
    return current_bytes
