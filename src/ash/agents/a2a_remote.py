"""Configured A2A 1.0 remote-agent discovery and delegation tools."""

from __future__ import annotations

import asyncio
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
from uuid import uuid4

import httpx
from pydantic import BaseModel, Field

from ash.core.redaction import redact_text
from ash.safety.guard import SafetyGuard
from ash.tools.base import BaseTool, ToolResult, count_output_tokens


MAX_A2A_CONFIG_BYTES = 256 * 1024
MAX_A2A_REMOTE_RESPONSE_BYTES = 1_000_000
MAX_A2A_REMOTE_EVENTS = 10_000
A2A_AGENT_NAME = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
A2A_ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


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


class DelegateRemoteAgentTool(BaseTool):
    name = "delegate_remote_agent"
    description = (
        "Delegate a bounded text task to an explicitly configured A2A remote agent; "
        "reuse the returned context_id for follow-up work."
    )
    args_schema = DelegateRemoteAgentArgs

    def __init__(
        self, safety_guard: SafetyGuard, agents: dict[str, RemoteAgentConfig]
    ) -> None:
        super().__init__(safety_guard)
        self.agents = dict(agents)

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
        try:
            result = await send_remote_agent(
                config,
                args.prompt,
                context_id=args.context_id,
            )
        except ModuleNotFoundError as exc:
            if exc.name == "a2a" or (exc.name or "").startswith("a2a."):
                from ash.install import pipx_install_command

                return ToolResult(
                    success=False,
                    output="",
                    error=(
                        "A2A support requires "
                        f"`{pipx_install_command('a2a')}`."
                    ),
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


def load_remote_agent_configs(
    workspace: Path,
    *,
    include_project: bool,
) -> dict[str, RemoteAgentConfig]:
    paths = [Path.home() / ".ash" / "a2a.json"]
    if include_project:
        paths.append(workspace / ".ash" / "a2a.json")
    agents: dict[str, RemoteAgentConfig] = {}
    for path in paths:
        if not path.is_file():
            continue
        try:
            raw_bytes = path.read_bytes()
            if len(raw_bytes) > MAX_A2A_CONFIG_BYTES:
                raise ValueError(f"A2A config exceeds 256 KiB: {path}")
            payload = json.loads(raw_bytes.decode("utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
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

    if not prompt.strip() or len(prompt.encode("utf-8")) > 1_000_000:
        raise ValueError("remote-agent prompt must be non-empty and at most 1 MB")
    if context_id and len(context_id.encode("utf-8")) > 512:
        raise ValueError("remote-agent context ID exceeds 512 bytes")
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
        resolved_context = context_id
        state = ""
        chunks: list[str] = []
        output_bytes = 0
        event_count = 0
        try:
            message = Message(
                message_id=str(uuid4()),
                role=Role.ROLE_USER,
                parts=[Part(text=prompt)],
            )
            if context_id:
                message.context_id = context_id
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
        except asyncio.CancelledError:
            if task_id:
                try:
                    await asyncio.shield(
                        client.cancel_task(CancelTaskRequest(id=task_id))
                    )
                except Exception:  # noqa: BLE001 - cancellation remains primary
                    pass
            raise
        finally:
            await client.close()
    return RemoteAgentResult(
        response="".join(chunks),
        task_id=task_id,
        context_id=resolved_context,
        state=state or "UNKNOWN",
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
