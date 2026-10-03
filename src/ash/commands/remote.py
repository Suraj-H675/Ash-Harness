"""First-party authenticated client for an already-running Ash HTTP server."""

from __future__ import annotations

import asyncio
import ipaddress
import json
import os
import sys
from collections.abc import AsyncIterator
from typing import Any
from urllib.parse import urlsplit

import httpx
from prompt_toolkit import PromptSession
from prompt_toolkit.patch_stdout import patch_stdout

from ash.core.redaction import redact_urls_in_text
from ash.ui.safe_text import terminal_safe_text


MAX_REMOTE_RESPONSE_BYTES = 8 * 1024 * 1024
MAX_REMOTE_SSE_EVENT_BYTES = 8 * 1024 * 1024
MAX_REMOTE_PROMPT_BYTES = 1_000_000
MAX_REMOTE_URL_CHARS = 4096


class RemoteClientError(RuntimeError):
    """The remote Ash control request failed safely."""


class RemoteAshClient:
    def __init__(
        self,
        base_url: str,
        *,
        bearer_token: str,
        timeout_seconds: float = 300.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.base_url = validate_remote_base_url(base_url)
        _validate_bearer_token(bearer_token)
        if not 1 <= timeout_seconds <= 3600:
            raise ValueError("remote timeout must be between 1 and 3600 seconds")
        self._http = httpx.AsyncClient(
            base_url=self.base_url,
            headers={"Authorization": f"Bearer {bearer_token}"},
            timeout=httpx.Timeout(timeout_seconds, connect=min(timeout_seconds, 15)),
            follow_redirects=False,
            trust_env=False,
            transport=transport,
        )
        self._selected_session_id: str | None = None

    async def close(self) -> None:
        await self._http.aclose()

    async def __aenter__(self) -> "RemoteAshClient":
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.close()

    async def status(self) -> dict[str, Any]:
        payload = await self._request_json(
            "POST",
            "/rpc",
            json_payload={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "status",
                "params": {},
            },
        )
        if not isinstance(payload, dict):
            raise RemoteClientError("remote Ash returned an invalid JSON-RPC response")
        error = payload.get("error")
        if isinstance(error, dict):
            message = terminal_safe_text(
                str(error.get("message", "error")),
                single_line=True,
            )
            raise RemoteClientError(f"remote Ash status failed: {message}")
        result = payload.get("result")
        if not isinstance(result, dict):
            raise RemoteClientError("remote Ash status response has no result object")
        session_id = result.get("session_id")
        if isinstance(session_id, str) and session_id:
            self._selected_session_id = session_id
        return result

    async def sessions(self, *, query: str = "", limit: int = 20) -> list[dict[str, Any]]:
        if not 1 <= limit <= 100:
            raise ValueError("remote session limit must be between 1 and 100")
        payload = await self._request_json(
            "GET",
            "/v1/sessions",
            params={"query": query, "limit": limit},
        )
        sessions = payload.get("sessions") if isinstance(payload, dict) else None
        if not isinstance(sessions, list) or not all(
            isinstance(item, dict) for item in sessions
        ):
            raise RemoteClientError("remote Ash returned an invalid session list")
        return sessions

    async def new_session(self) -> str:
        payload = await self._request_json("POST", "/v1/sessions")
        session_id = _session_id(payload)
        self._selected_session_id = session_id
        return session_id

    async def resume(self, session_id: str) -> str:
        if not session_id:
            raise ValueError("session_id is required")
        payload = await self._request_json(
            "POST",
            "/v1/sessions/resume",
            json_payload={"session_id": session_id},
        )
        resolved = _session_id(payload)
        self._selected_session_id = resolved
        return resolved

    async def steer(self, text: str) -> int:
        _validate_prompt(text, label="steering input")
        payload = await self._request_json(
            "POST",
            "/v1/turn/steer",
            json_payload={"input": text},
        )
        pending = payload.get("pending") if isinstance(payload, dict) else None
        if isinstance(pending, bool) or not isinstance(pending, int):
            raise RemoteClientError("remote Ash returned an invalid steering result")
        return pending

    async def approvals(self, *, wait_seconds: float = 0) -> list[dict[str, Any]]:
        if not 0 <= wait_seconds <= 30:
            raise ValueError("approval wait must be between 0 and 30 seconds")
        payload = await self._request_json(
            "GET",
            "/v1/approvals",
            params={"wait_seconds": wait_seconds},
            timeout=max(wait_seconds + 5, 10),
        )
        if not isinstance(payload, dict):
            raise RemoteClientError("remote Ash returned an invalid approval response")
        if payload.get("enabled") is not True:
            raise RemoteClientError("remote Ash does not expose live approvals")
        approvals = payload.get("approvals")
        if not isinstance(approvals, list) or not all(
            isinstance(item, dict) for item in approvals
        ):
            raise RemoteClientError("remote Ash returned invalid approval records")
        return approvals

    async def resolve_approval(self, request_id: str, *, approved: bool) -> None:
        if not request_id:
            raise ValueError("approval request ID is required")
        await self._request_json(
            "POST",
            f"/v1/approvals/{request_id}",
            json_payload={"approved": bool(approved)},
        )

    async def stream_turn(self, text: str) -> AsyncIterator[tuple[str, dict[str, Any]]]:
        _validate_prompt(text)
        payload: dict[str, Any] = {"input": text}
        if self._selected_session_id is not None:
            payload["session_id"] = self._selected_session_id
        try:
            async with self._http.stream(
                "POST",
                "/v1/turn/stream",
                json=payload,
            ) as response:
                if response.status_code != 200:
                    detail = await _bounded_response_text(response)
                    raise RemoteClientError(_http_failure(response.status_code, detail))
                content_type = response.headers.get("content-type", "").partition(";")[0]
                if content_type.casefold() != "text/event-stream":
                    raise RemoteClientError(
                        "remote Ash stream did not use text/event-stream"
                    )
                async for event, payload in _iter_sse_events(response):
                    yield event, payload
        except httpx.HTTPError as exc:
            raise RemoteClientError(_network_failure(exc)) from exc

    async def _request_json(
        self,
        method: str,
        path: str,
        *,
        json_payload: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        try:
            request_options: dict[str, Any] = {
                "json": json_payload,
                "params": params,
            }
            if timeout is not None:
                request_options["timeout"] = timeout
            async with self._http.stream(
                method,
                path,
                **request_options,
            ) as response:
                raw = await _bounded_response_bytes(response)
                if response.status_code < 200 or response.status_code >= 300:
                    detail = raw.decode("utf-8", errors="replace")
                    raise RemoteClientError(_http_failure(response.status_code, detail))
        except RemoteClientError:
            raise
        except httpx.HTTPError as exc:
            raise RemoteClientError(_network_failure(exc)) from exc
        try:
            payload = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RemoteClientError("remote Ash returned invalid JSON") from exc
        if not isinstance(payload, dict):
            raise RemoteClientError("remote Ash returned a non-object JSON response")
        return payload


async def run_remote(args: Any) -> int:
    token = os.environ.get(args.token_env, "")
    try:
        _validate_bearer_token(token)
    except ValueError as exc:
        raise ValueError(f"Set {args.token_env} to a valid Ash bearer token") from exc
    async with RemoteAshClient(
        args.url,
        bearer_token=token,
        timeout_seconds=args.timeout,
    ) as client:
        action = args.remote_action
        if action == "status":
            _print_payload(await client.status(), json_output=args.json)
            return 0
        if action == "sessions":
            sessions = await client.sessions(query=args.query, limit=args.limit)
            _print_payload({"sessions": sessions}, json_output=args.json)
            return 0
        if action == "new":
            _print_payload(
                {"session_id": await client.new_session()},
                json_output=args.json,
            )
            return 0
        if action == "resume":
            _print_payload(
                {"session_id": await client.resume(args.session_id)},
                json_output=args.json,
            )
            return 0
        if action == "steer":
            _print_payload(
                {"pending": await client.steer(args.text)},
                json_output=args.json,
            )
            return 0
        if action == "approvals":
            _print_payload(
                {"approvals": await client.approvals()},
                json_output=args.json,
            )
            return 0
        if action in {"approve", "deny"}:
            approved = action == "approve"
            await client.resolve_approval(args.request_id, approved=approved)
            _print_payload(
                {"resolved": True, "approved": approved},
                json_output=args.json,
            )
            return 0
        if action == "prompt":
            await client.status()
            return 0 if await _print_streamed_turn(client, args.prompt) else 1
        if action == "chat":
            if args.session_id:
                await client.resume(args.session_id)
            return await _interactive_chat(client)
    raise ValueError(f"unsupported remote action: {args.remote_action}")


async def _interactive_chat(client: RemoteAshClient) -> int:
    status = await client.status()
    print(
        "Connected to Ash "
        f"[{terminal_safe_text(str(status.get('model', 'unknown')), single_line=True)}] "
        f"session={terminal_safe_text(str(status.get('session_id') or 'none'), single_line=True)}"
    )
    print("Commands: /status /sessions [query] /new /resume ID /approvals /help /exit")
    prompt = PromptSession[str]()
    while True:
        try:
            with patch_stdout(raw=True):
                text = (await prompt.prompt_async("ash remote> ")).strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return 0
        if not text:
            continue
        if text in {"/exit", "/quit"}:
            return 0
        if text == "/help":
            print(
                "/status, /sessions [query], /new, /resume ID, /approvals, /exit. "
                "Use ash remote steer from another terminal to steer a live turn."
            )
            continue
        if text == "/status":
            _print_payload(await client.status(), json_output=False)
            continue
        if text.startswith("/sessions"):
            _, _, query = text.partition(" ")
            _print_payload(
                {"sessions": await client.sessions(query=query.strip())},
                json_output=False,
            )
            continue
        if text == "/new":
            print(f"Session: {await client.new_session()}")
            continue
        if text.startswith("/resume "):
            session_id = text.removeprefix("/resume ").strip()
            print(f"Session: {await client.resume(session_id)}")
            continue
        if text == "/approvals":
            _print_payload(
                {"approvals": await client.approvals()},
                json_output=False,
            )
            continue
        if text.startswith("/"):
            print("Unknown remote command. Use /help.")
            continue
        await _run_interactive_turn(client, prompt, text)


async def _run_interactive_turn(
    client: RemoteAshClient,
    prompt: PromptSession[str],
    text: str,
) -> None:
    stop = asyncio.Event()
    approvals = asyncio.create_task(
        _approval_watch(client, prompt, stop),
        name="ash-remote-approval-watch",
    )
    try:
        await _print_streamed_turn(client, text)
    finally:
        stop.set()
        approvals.cancel()
        await asyncio.gather(approvals, return_exceptions=True)


async def _approval_watch(
    client: RemoteAshClient,
    prompt: PromptSession[str],
    stop: asyncio.Event,
) -> None:
    seen: set[str] = set()
    while not stop.is_set():
        pending = await client.approvals(wait_seconds=30)
        for item in pending:
            request_id = item.get("id")
            tool = item.get("tool")
            arguments = item.get("arguments")
            if not isinstance(request_id, str) or request_id in seen:
                continue
            seen.add(request_id)
            safe_tool = terminal_safe_text(str(tool), single_line=True)
            safe_arguments = terminal_safe_text(
                json.dumps(arguments, ensure_ascii=False, sort_keys=True, default=str)
            )
            print(f"\nApproval required: {safe_tool}\n{safe_arguments}")
            with patch_stdout(raw=True):
                answer = (
                    await prompt.prompt_async("Approve once? [y/N] ")
                ).strip().casefold()
            await client.resolve_approval(
                request_id,
                approved=answer in {"y", "yes"},
            )


async def _print_streamed_turn(client: RemoteAshClient, text: str) -> bool:
    wrote_assistant = False
    terminal_seen = False
    succeeded = False
    async for event, payload in client.stream_turn(text):
        if event == "assistant.delta":
            delta = payload.get("text")
            if isinstance(delta, str):
                print(terminal_safe_text(delta), end="", flush=True)
                wrote_assistant = True
        elif event == "tool.requested":
            tool = terminal_safe_text(str(payload.get("tool", "tool")), single_line=True)
            print(f"\n[tool requested: {tool}]", flush=True)
        elif event == "tool.completed":
            tool = terminal_safe_text(str(payload.get("tool", "tool")), single_line=True)
            print(f"\n[tool completed: {tool}]", flush=True)
        elif event == "turn.completed":
            terminal_seen = True
            succeeded = True
        elif event in {"turn.error", "turn.cancelled"}:
            terminal_seen = True
            succeeded = False
            error = payload.get("error") or payload.get("reason") or event
            print(f"\n[{terminal_safe_text(str(error), single_line=True)}]", file=sys.stderr)
    if wrote_assistant:
        print()
    if not terminal_seen:
        print("[remote Ash stream ended without a terminal event]", file=sys.stderr)
    return succeeded


def validate_remote_base_url(value: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > MAX_REMOTE_URL_CHARS
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
        or any(character.isspace() for character in value)
    ):
        raise ValueError("remote Ash URL is invalid")
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as exc:
        raise ValueError("remote Ash URL is invalid") from exc
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.path not in {"", "/"}
        or (port is not None and not 0 < port <= 65535)
    ):
        raise ValueError(
            "remote Ash URL must be an HTTP(S) origin without credentials, path, query, or fragment"
        )
    if parsed.scheme == "http" and not _is_loopback_host(parsed.hostname):
        raise ValueError("remote Ash URL must use HTTPS except for loopback HTTP")
    return value.rstrip("/")


def _validate_bearer_token(value: str) -> None:
    if (
        len(value) < 16
        or not value.isascii()
        or any(character.isspace() for character in value)
    ):
        raise ValueError(
            "remote Ash bearer token must contain at least 16 "
            "non-whitespace ASCII characters"
        )


def _is_loopback_host(hostname: str) -> bool:
    host = hostname.casefold().rstrip(".")
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


async def _bounded_response_bytes(response: httpx.Response) -> bytes:
    length = response.headers.get("content-length")
    if length is not None:
        try:
            if int(length) > MAX_REMOTE_RESPONSE_BYTES:
                raise RemoteClientError("remote Ash response exceeds the client limit")
        except ValueError:
            pass
    chunks: list[bytes] = []
    total = 0
    async for chunk in response.aiter_bytes():
        total += len(chunk)
        if total > MAX_REMOTE_RESPONSE_BYTES:
            raise RemoteClientError("remote Ash response exceeds the client limit")
        chunks.append(chunk)
    return b"".join(chunks)


async def _bounded_response_text(response: httpx.Response) -> str:
    return (await _bounded_response_bytes(response)).decode("utf-8", errors="replace")


async def _iter_sse_events(
    response: httpx.Response,
) -> AsyncIterator[tuple[str, dict[str, Any]]]:
    event_name = "message"
    data_lines: list[str] = []
    event_bytes = 0
    line_buffer = bytearray()

    async def dispatch() -> tuple[str, dict[str, Any]] | None:
        nonlocal event_name, data_lines, event_bytes
        result: tuple[str, dict[str, Any]] | None = None
        if data_lines:
            raw = "\n".join(data_lines)
            try:
                payload = json.loads(raw)
            except json.JSONDecodeError as exc:
                raise RemoteClientError(
                    "remote Ash SSE event contains invalid JSON"
                ) from exc
            if not isinstance(payload, dict):
                raise RemoteClientError("remote Ash SSE payload must be an object")
            result = (event_name, payload)
        event_name = "message"
        data_lines = []
        event_bytes = 0
        return result

    async for chunk in response.aiter_bytes():
        cursor = 0
        while cursor < len(chunk):
            newline = chunk.find(b"\n", cursor)
            if newline < 0:
                tail = chunk[cursor:]
                if event_bytes + len(line_buffer) + len(tail) > MAX_REMOTE_SSE_EVENT_BYTES:
                    raise RemoteClientError(
                        "remote Ash SSE event exceeds the client limit"
                    )
                line_buffer.extend(tail)
                break

            segment = chunk[cursor:newline]
            if event_bytes + len(line_buffer) + len(segment) + 1 > MAX_REMOTE_SSE_EVENT_BYTES:
                raise RemoteClientError("remote Ash SSE event exceeds the client limit")
            line_buffer.extend(segment)
            event_bytes += len(line_buffer) + 1
            raw_line = bytes(line_buffer)
            line_buffer.clear()
            cursor = newline + 1
            if raw_line.endswith(b"\r"):
                raw_line = raw_line[:-1]
            try:
                line = raw_line.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise RemoteClientError("remote Ash SSE stream is not valid UTF-8") from exc

            if not line:
                item = await dispatch()
                if item is not None:
                    yield item
                continue
            if line.startswith(":"):
                continue
            field, separator, value = line.partition(":")
            if separator and value.startswith(" "):
                value = value[1:]
            if field == "event":
                event_name = value
            elif field == "data":
                data_lines.append(value)
    if line_buffer or data_lines:
        raise RemoteClientError("remote Ash SSE stream ended mid-event")


def _session_id(payload: dict[str, Any]) -> str:
    session_id = payload.get("session_id") if isinstance(payload, dict) else None
    if not isinstance(session_id, str) or not session_id:
        raise RemoteClientError("remote Ash returned an invalid session ID")
    return session_id


def _validate_prompt(text: str, *, label: str = "prompt") -> None:
    if not isinstance(text, str) or not text.strip():
        raise ValueError(f"{label} cannot be empty")
    if len(text.encode("utf-8")) > MAX_REMOTE_PROMPT_BYTES:
        raise ValueError(f"{label} exceeds {MAX_REMOTE_PROMPT_BYTES} bytes")


def _network_failure(exc: httpx.HTTPError) -> str:
    return "remote Ash network request failed: " + redact_urls_in_text(str(exc))


def _http_failure(status_code: int, detail: str) -> str:
    bounded = terminal_safe_text(detail[:4096], single_line=True)
    return f"remote Ash returned HTTP {status_code}: {bounded}"


def _print_payload(payload: dict[str, Any], *, json_output: bool) -> None:
    if json_output:
        print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
        return
    print(
        terminal_safe_text(
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True, default=str)
        )
    )
