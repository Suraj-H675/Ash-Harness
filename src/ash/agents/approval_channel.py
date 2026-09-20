"""Ephemeral authenticated approval transport for foreground subagents."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import secrets
import uuid
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from ash.safe_io import strict_json_loads
from ash.safety.grants import PermissionGrantError, PermissionRule


APPROVAL_PROTOCOL_VERSION = 1
MAX_APPROVAL_MESSAGE_BYTES = 10 * 1024 * 1024
MAX_APPROVAL_RULE_DELTAS = 64
MAX_APPROVAL_CONNECTIONS = 8
APPROVAL_AUTH_TIMEOUT_SECONDS = 5.0
_APPROVAL_CATEGORIES = frozenset({"session_rules", "persistent_rules"})


class ApprovalChannelError(RuntimeError):
    """Raised when the ephemeral approval channel cannot be trusted."""


@dataclass(frozen=True)
class ApprovalEndpoint:
    host: str
    port: int
    token: str
    task_id: str
    agent_id: str
    attempt: int

    def as_payload(self) -> dict[str, Any]:
        return {
            "version": APPROVAL_PROTOCOL_VERSION,
            "host": self.host,
            "port": self.port,
            "token": self.token,
            "task_id": self.task_id,
            "agent_id": self.agent_id,
            "attempt": self.attempt,
        }

    @classmethod
    def from_payload(cls, raw: Any) -> "ApprovalEndpoint":
        if not isinstance(raw, dict) or raw.get("version") != APPROVAL_PROTOCOL_VERSION:
            raise ApprovalChannelError("unsupported subagent approval endpoint")
        host = raw.get("host")
        port = raw.get("port")
        token = raw.get("token")
        task_id = raw.get("task_id")
        agent_id = raw.get("agent_id")
        attempt = raw.get("attempt")
        if host != "127.0.0.1":
            raise ApprovalChannelError("subagent approval endpoint must use loopback")
        if type(port) is not int or not 1 <= port <= 65535:
            raise ApprovalChannelError("subagent approval endpoint port is invalid")
        if not isinstance(token, str) or len(token) != 64:
            raise ApprovalChannelError("subagent approval endpoint token is invalid")
        try:
            bytes.fromhex(token)
        except ValueError as exc:
            raise ApprovalChannelError(
                "subagent approval endpoint token is invalid"
            ) from exc
        if not isinstance(task_id, str) or not task_id:
            raise ApprovalChannelError("subagent approval task id is invalid")
        if not isinstance(agent_id, str) or not agent_id:
            raise ApprovalChannelError("subagent approval agent id is invalid")
        if type(attempt) is not int or attempt < 1:
            raise ApprovalChannelError("subagent approval attempt is invalid")
        return cls(host, port, token, task_id, agent_id, attempt)


@dataclass(frozen=True)
class ApprovalRuleDelta:
    category: str
    rule: PermissionRule


@dataclass(frozen=True)
class ApprovalDecision:
    approved: bool
    feedback: str = ""
    rules: tuple[ApprovalRuleDelta, ...] = ()


ApprovalHandler = Callable[[str, dict[str, Any]], Awaitable[ApprovalDecision]]


def approval_arguments_sha256(arguments: dict[str, Any]) -> str:
    try:
        canonical = json.dumps(
            arguments,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise ApprovalChannelError(
            "subagent approval arguments are not JSON serializable"
        ) from exc
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _encode_message(payload: dict[str, Any]) -> bytes:
    try:
        encoded = (
            json.dumps(
                payload,
                ensure_ascii=True,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8")
            + b"\n"
        )
    except (TypeError, ValueError) as exc:
        raise ApprovalChannelError("subagent approval message is invalid") from exc
    if len(encoded) > MAX_APPROVAL_MESSAGE_BYTES:
        raise ApprovalChannelError("subagent approval message exceeds the protocol limit")
    return encoded


class ForegroundApprovalServer:
    """Loopback-only server carrying exact foreground approval arguments."""

    def __init__(
        self,
        *,
        task_id: str,
        agent_id: str,
        attempt: int,
        handler: ApprovalHandler,
    ) -> None:
        self._task_id = task_id
        self._agent_id = agent_id
        self._attempt = attempt
        self._handler = handler
        self._token = secrets.token_hex(32)
        self._server: asyncio.AbstractServer | None = None
        self._seen_request_ids: set[str] = set()
        self._request_lock = asyncio.Lock()
        self._connections: set[asyncio.StreamWriter] = set()
        self._handler_tasks: set[asyncio.Task[Any]] = set()

    async def start(self) -> ApprovalEndpoint:
        if self._server is not None:
            raise ApprovalChannelError("subagent approval server is already running")
        self._server = await asyncio.start_server(
            self._handle_connection,
            host="127.0.0.1",
            port=0,
            limit=MAX_APPROVAL_MESSAGE_BYTES + 1,
        )
        sockets = self._server.sockets or ()
        if len(sockets) != 1:
            await self.aclose()
            raise ApprovalChannelError("subagent approval server did not bind exactly once")
        port = sockets[0].getsockname()[1]
        if type(port) is not int or not 1 <= port <= 65535:
            await self.aclose()
            raise ApprovalChannelError("subagent approval server bound an invalid port")
        return ApprovalEndpoint(
            host="127.0.0.1",
            port=port,
            token=self._token,
            task_id=self._task_id,
            agent_id=self._agent_id,
            attempt=self._attempt,
        )

    async def aclose(self) -> None:
        server = self._server
        self._server = None
        if server is not None:
            server.close()
        writers = list(self._connections)
        for writer in writers:
            writer.close()
        current = asyncio.current_task()
        handlers = [
            task
            for task in self._handler_tasks
            if task is not current and not task.done()
        ]
        for task in handlers:
            task.cancel()
        if handlers:
            await asyncio.gather(*handlers, return_exceptions=True)
        if writers:
            await asyncio.sleep(0)
        if server is not None:
            await server.wait_closed()

    async def _handle_connection(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        if len(self._connections) >= MAX_APPROVAL_CONNECTIONS:
            writer.close()
            return
        task = asyncio.current_task()
        if task is not None:
            self._handler_tasks.add(task)
        self._connections.add(writer)
        try:
            try:
                line = await asyncio.wait_for(
                    reader.readline(),
                    timeout=APPROVAL_AUTH_TIMEOUT_SECONDS,
                )
            except (TimeoutError, ValueError, asyncio.LimitOverrunError):
                return
            if not line or len(line) > MAX_APPROVAL_MESSAGE_BYTES or not line.endswith(b"\n"):
                return
            try:
                raw = strict_json_loads(line)
            except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
                return
            if not isinstance(raw, dict) or raw.get("version") != APPROVAL_PROTOCOL_VERSION:
                return
            token = raw.get("token")
            if not isinstance(token, str) or not hmac.compare_digest(token, self._token):
                return
            if (
                raw.get("task_id") != self._task_id
                or raw.get("agent_id") != self._agent_id
                or raw.get("attempt") != self._attempt
            ):
                return
            request_id = raw.get("request_id")
            tool_name = raw.get("tool_name")
            arguments = raw.get("arguments")
            digest = raw.get("arguments_sha256")
            if (
                not isinstance(request_id, str)
                or len(request_id) != 32
                or not isinstance(tool_name, str)
                or not tool_name
                or not isinstance(arguments, dict)
                or not isinstance(digest, str)
                or len(digest) != 64
            ):
                return
            if approval_arguments_sha256(arguments) != digest:
                return
            async with self._request_lock:
                if request_id in self._seen_request_ids:
                    return
                self._seen_request_ids.add(request_id)
                decision = await self._handler(tool_name, arguments)
            response = {
                "version": APPROVAL_PROTOCOL_VERSION,
                "request_id": request_id,
                "approved": decision.approved,
                "feedback": decision.feedback[:500],
                "rules": [
                    {"category": delta.category, "rule": delta.rule.as_payload()}
                    for delta in decision.rules
                ],
            }
            writer.write(_encode_message(response))
            await writer.drain()
        except (ApprovalChannelError, ConnectionError, OSError):
            return
        except Exception:
            return
        finally:
            self._connections.discard(writer)
            if task is not None:
                self._handler_tasks.discard(task)
            writer.close()


async def request_foreground_approval(
    endpoint: ApprovalEndpoint,
    *,
    tool_name: str,
    arguments: dict[str, Any],
) -> ApprovalDecision:
    digest = approval_arguments_sha256(arguments)
    request_id = uuid.uuid4().hex
    request = {
        "version": APPROVAL_PROTOCOL_VERSION,
        "token": endpoint.token,
        "task_id": endpoint.task_id,
        "agent_id": endpoint.agent_id,
        "attempt": endpoint.attempt,
        "request_id": request_id,
        "tool_name": tool_name,
        "arguments_sha256": digest,
        "arguments": arguments,
    }
    try:
        reader, writer = await asyncio.open_connection(
            endpoint.host,
            endpoint.port,
            limit=MAX_APPROVAL_MESSAGE_BYTES + 1,
        )
    except OSError as exc:
        raise ApprovalChannelError("subagent approval channel is unavailable") from exc
    try:
        writer.write(_encode_message(request))
        await writer.drain()
        try:
            line = await reader.readline()
        except (ValueError, asyncio.LimitOverrunError) as exc:
            raise ApprovalChannelError(
                "subagent approval response exceeds the protocol limit"
            ) from exc
        if not line:
            raise ApprovalChannelError("subagent approval channel closed without a response")
        if len(line) > MAX_APPROVAL_MESSAGE_BYTES or not line.endswith(b"\n"):
            raise ApprovalChannelError("subagent approval response is invalid")
        try:
            raw = strict_json_loads(line)
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
            raise ApprovalChannelError("subagent approval response is malformed") from exc
        if (
            not isinstance(raw, dict)
            or raw.get("version") != APPROVAL_PROTOCOL_VERSION
            or raw.get("request_id") != request_id
            or type(raw.get("approved")) is not bool
        ):
            raise ApprovalChannelError("subagent approval response correlation failed")
        feedback = raw.get("feedback", "")
        if not isinstance(feedback, str):
            raise ApprovalChannelError("subagent approval feedback is invalid")
        raw_rules = raw.get("rules", [])
        if (
            not isinstance(raw_rules, list)
            or len(raw_rules) > MAX_APPROVAL_RULE_DELTAS
        ):
            raise ApprovalChannelError("subagent approval rule delta is invalid")
        rules: list[ApprovalRuleDelta] = []
        for item in raw_rules:
            if not isinstance(item, dict):
                raise ApprovalChannelError("subagent approval rule delta is invalid")
            category = item.get("category")
            if category not in _APPROVAL_CATEGORIES:
                raise ApprovalChannelError("subagent approval rule category is invalid")
            try:
                rule = PermissionRule.from_payload(item.get("rule"))
            except PermissionGrantError as exc:
                raise ApprovalChannelError("subagent approval rule is invalid") from exc
            if not rule.matches(tool_name, arguments):
                raise ApprovalChannelError(
                    "subagent approval rule does not match the approved request"
                )
            rules.append(ApprovalRuleDelta(category, rule))
        return ApprovalDecision(
            approved=raw["approved"],
            feedback=feedback[:500],
            rules=tuple(rules),
        )
    except (BrokenPipeError, ConnectionResetError) as exc:
        raise ApprovalChannelError("subagent approval channel was interrupted") from exc
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except (ConnectionError, OSError):
            pass
