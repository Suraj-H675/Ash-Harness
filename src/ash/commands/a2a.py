"""Lifecycle and validation for the optional A2A 1.0 server."""

from __future__ import annotations

import asyncio
import os
import json
import sys
from typing import Any
from urllib.parse import urlsplit
from uuid import uuid4

import httpx
import uvicorn
from a2a.client import A2ACardResolver, ClientConfig, ClientFactory
from a2a.server.request_handlers.response_helpers import agent_card_to_dict
from a2a.types.a2a_pb2 import (
    CancelTaskRequest,
    Message,
    Part,
    Role,
    SendMessageRequest,
    TaskState,
)
from a2a.utils.constants import TransportProtocol
from google.protobuf.json_format import MessageToDict

from ash.config import AshConfig
from ash.agents.a2a_remote import validate_agent_card_origins
from ash.safe_io import read_bounded_text
from ash.server.a2a import create_a2a_app


LOOPBACK_HOSTS = {"127.0.0.1", "::1", "localhost"}
MAX_A2A_CLIENT_INPUT_BYTES = 1_000_000
MAX_A2A_CLIENT_EVENTS = 10_000
MAX_A2A_CLIENT_OUTPUT_BYTES = 1_000_000
MAX_A2A_CLIENT_STATUS_BYTES = 64 * 1024
TERMINAL_STATES = {
    TaskState.TASK_STATE_COMPLETED,
    TaskState.TASK_STATE_CANCELED,
    TaskState.TASK_STATE_FAILED,
    TaskState.TASK_STATE_REJECTED,
}


async def serve_a2a(args) -> int:
    token = os.environ.get(args.token_env, "")
    if len(token) < 16:
        raise ValueError(
            f"Set {args.token_env} to a bearer token containing at least 16 characters"
        )
    if not 1 <= args.port <= 65535:
        raise ValueError("Port must be between 1 and 65535")
    if args.rate_limit < 1:
        raise ValueError("Rate limit must be positive")
    loopback = args.host in LOOPBACK_HOSTS
    if not loopback and not args.allow_remote:
        raise ValueError("Non-loopback A2A binding requires --allow-remote")
    if not loopback and not args.public_url:
        raise ValueError("Remote A2A binding requires an explicit --public-url")
    public_url = args.public_url or _local_public_url(args.host, args.port)
    if not loopback and urlsplit(public_url).scheme != "https":
        raise ValueError("Remote A2A public URL must use HTTPS")

    config = AshConfig.load()
    app = create_a2a_app(
        config,
        public_url=public_url,
        bearer_token=token,
        requests_per_minute=args.rate_limit,
    )
    server = uvicorn.Server(
        uvicorn.Config(
            app,
            host=args.host,
            port=args.port,
            log_level=args.log_level,
        )
    )
    await server.serve()
    return 0


async def inspect_a2a(args) -> int:
    url = _remote_url(args.url)
    async with _remote_http_client(args) as http:
        card = await A2ACardResolver(http, url).get_agent_card()
        print(json.dumps(agent_card_to_dict(card), indent=2))
    return 0


async def send_a2a(args) -> int:
    url = _remote_url(args.url)
    prompt = (
        read_bounded_text(
            sys.stdin,
            MAX_A2A_CLIENT_INPUT_BYTES,
            label="A2A prompt",
        )
        if args.prompt == "-"
        else args.prompt
    )
    if not prompt.strip() or len(prompt.encode("utf-8")) > MAX_A2A_CLIENT_INPUT_BYTES:
        raise ValueError("A2A prompt must be non-empty and no larger than 1 MB")
    if args.context_id and len(args.context_id.encode("utf-8")) > 512:
        raise ValueError("A2A context ID exceeds 512 bytes")

    async with _remote_http_client(args) as http:
        factory = ClientFactory(
            ClientConfig(
                httpx_client=http,
                streaming=True,
                supported_protocol_bindings=[TransportProtocol.JSONRPC],
                accepted_output_modes=["text/plain"],
            )
        )
        card = await A2ACardResolver(http, url).get_agent_card()
        validate_agent_card_origins(url, card.supported_interfaces)
        client = factory.create(card)
        task_id = ""
        events: list[dict[str, Any]] = []
        event_count = 0
        event_bytes = 0
        final_state: int | None = None
        immediate_message = False
        rendered_text = False
        status_message = ""
        rendered_bytes = 0
        try:
            message = Message(
                message_id=str(uuid4()),
                role=Role.ROLE_USER,
                parts=[Part(text=prompt)],
            )
            if args.context_id:
                message.context_id = args.context_id
            request = SendMessageRequest(message=message)
            async for event in client.send_message(request):
                event_count += 1
                if event_count > MAX_A2A_CLIENT_EVENTS:
                    raise RuntimeError("A2A response exceeded 10,000 events")
                if args.json:
                    event_bytes = _append_json_event(
                        events,
                        MessageToDict(event),
                        event_bytes,
                    )
                if event.HasField("task"):
                    task_id = event.task.id
                    final_state = event.task.status.state
                elif event.HasField("message"):
                    immediate_message = True
                    if not args.json:
                        for part in event.message.parts:
                            if part.WhichOneof("content") == "text":
                                rendered_bytes = _write_bounded_text(
                                    part.text,
                                    rendered_bytes,
                                )
                                rendered_text = True
                elif event.HasField("status_update"):
                    task_id = event.status_update.task_id
                    final_state = event.status_update.status.state
                    if event.status_update.status.HasField("message"):
                        status_message = _bounded_text_parts(
                            event.status_update.status.message.parts,
                            MAX_A2A_CLIENT_STATUS_BYTES,
                            "A2A status message",
                        )
                elif event.HasField("artifact_update"):
                    task_id = event.artifact_update.task_id
                    if not args.json:
                        for part in event.artifact_update.artifact.parts:
                            if part.WhichOneof("content") == "text":
                                rendered_bytes = _write_bounded_text(
                                    part.text,
                                    rendered_bytes,
                                )
                                rendered_text = True
            if args.json:
                print(
                    json.dumps(
                        {
                            "task_id": task_id or None,
                            "state": (
                                "MESSAGE"
                                if immediate_message
                                else _state_name(final_state)
                            ),
                            "events": events,
                        }
                    )
                )
            elif rendered_text:
                print()
            if (
                not args.json
                and not immediate_message
                and final_state != TaskState.TASK_STATE_COMPLETED
            ):
                suffix = f": {status_message}" if status_message else ""
                print(
                    f"A2A task ended in {_state_name(final_state) or 'UNKNOWN'}{suffix}",
                    file=sys.stderr,
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
    return (
        0 if immediate_message or final_state == TaskState.TASK_STATE_COMPLETED else 1
    )


def _local_public_url(host: str, port: int) -> str:
    rendered_host = f"[{host}]" if ":" in host and not host.startswith("[") else host
    return f"http://{rendered_host}:{port}"


def _append_json_event(
    events: list[dict[str, Any]],
    event: dict[str, Any],
    current_bytes: int,
) -> int:
    encoded = json.dumps(event, ensure_ascii=False, separators=(",", ":")).encode(
        "utf-8"
    )
    contribution = len(encoded) + (1 if events else 0)
    if current_bytes + contribution > MAX_A2A_CLIENT_OUTPUT_BYTES:
        raise RuntimeError(
            "A2A response exceeded "
            f"{MAX_A2A_CLIENT_OUTPUT_BYTES} bytes"
        )
    events.append(event)
    return current_bytes + contribution


def _write_bounded_text(text: str, current_bytes: int) -> int:
    encoded_bytes = len(text.encode("utf-8"))
    if current_bytes + encoded_bytes > MAX_A2A_CLIENT_OUTPUT_BYTES:
        raise RuntimeError(
            "A2A response exceeded "
            f"{MAX_A2A_CLIENT_OUTPUT_BYTES} bytes"
        )
    print(text, end="", flush=True)
    return current_bytes + encoded_bytes


def _bounded_text_parts(parts: Any, max_bytes: int, label: str) -> str:
    chunks: list[str] = []
    current_bytes = 0
    for part in parts:
        if part.WhichOneof("content") != "text":
            continue
        text = part.text
        encoded_bytes = len(text.encode("utf-8"))
        if current_bytes + encoded_bytes > max_bytes:
            raise RuntimeError(f"{label} exceeded {max_bytes} bytes")
        chunks.append(text)
        current_bytes += encoded_bytes
    return "".join(chunks)


def _remote_url(value: str) -> str:
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as exc:
        raise ValueError("invalid A2A agent URL") from exc
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
            "A2A agent URL must be HTTP(S) without credentials, query, or fragment"
        )
    return value.rstrip("/")


def _remote_http_client(args) -> httpx.AsyncClient:
    if not 1 <= args.timeout <= 3600:
        raise ValueError("A2A timeout must be between 1 and 3600 seconds")
    headers: dict[str, str] = {}
    token = os.environ.get(args.token_env, "")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return httpx.AsyncClient(
        headers=headers,
        timeout=httpx.Timeout(args.timeout, connect=min(args.timeout, 15)),
        follow_redirects=False,
    )


def _state_name(state: int | None) -> str | None:
    return TaskState.Name(state) if state is not None else None
