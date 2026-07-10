"""Validated JSON-RPC 2.0 adapter for the asynchronous Ash SDK."""

from __future__ import annotations

import asyncio
from typing import Any, Awaitable, Callable

from ash.sdk import AshClient
from core.events import EVENT_SCHEMA_VERSION


class JSONRPCServer:
    def __init__(self, client: AshClient) -> None:
        self.client = client
        self._pending: dict[str | int, asyncio.Future[Any]] = {}
        self._turn_lock = asyncio.Lock()
        self._methods: dict[str, Callable[[dict[str, Any]], Awaitable[Any]]] = {
            "initialize": self._initialize,
            "turn/run": self._run_turn,
            "session/new": self._new_session,
            "session/resume": self._resume_session,
            "session/list": self._list_sessions,
            "session/fork": self._fork_session,
            "session/tree": self._session_tree,
            "event/list": self._list_events,
            "status": self._status,
        }

    async def handle_request(self, request: dict[str, Any]) -> dict[str, Any] | None:
        request_id = request.get("id")
        if request.get("jsonrpc") != "2.0" or not isinstance(
            request.get("method"), str
        ):
            return _error(request_id, -32600, "Invalid Request")
        method = request["method"]
        params = request.get("params", {})
        if not isinstance(params, dict):
            return _error(request_id, -32602, "Params must be an object")
        if method == "$/cancelRequest":
            cancelled = self.cancel(params.get("id"))
            return None if request_id is None else _result(request_id, cancelled)
        handler = self._methods.get(method)
        if handler is None:
            return (
                None
                if request_id is None
                else _error(request_id, -32601, f"Method not found: {method}")
            )
        if request_id is None:
            asyncio.ensure_future(handler(params))
            return None
        task: asyncio.Future[Any] = asyncio.ensure_future(handler(params))
        self._pending[request_id] = task
        try:
            value = await task
            return _result(request_id, value)
        except asyncio.CancelledError:
            return _error(request_id, -32800, "Request cancelled")
        except (KeyError, TypeError, ValueError) as exc:
            return _error(request_id, -32602, str(exc))
        except Exception as exc:  # noqa: BLE001
            return _error(request_id, -32603, "Internal error", {"detail": str(exc)})
        finally:
            self._pending.pop(request_id, None)

    def cancel(self, request_id: Any) -> bool:
        task = self._pending.get(request_id)
        if task is None or task.done():
            return False
        task.cancel()
        return True

    async def close(self) -> None:
        tasks = list(self._pending.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await self.client.close()

    async def _initialize(self, params: dict[str, Any]) -> dict[str, Any]:
        return {
            "protocol_version": 1,
            "server": {"name": "ash", "version": "0.1.0"},
            "capabilities": {
                "turns": True,
                "sessions": True,
                "cancellation": True,
                "event_schema_version": EVENT_SCHEMA_VERSION,
                "event_replay": True,
                "session_tree": True,
            },
        }

    async def _run_turn(self, params: dict[str, Any]) -> dict[str, Any]:
        text = params.get("input")
        if not isinstance(text, str) or not text.strip():
            raise ValueError("input must be a non-empty string")
        async with self._turn_lock:
            result = await self.client.prompt(text)
        return {
            "response": result.response,
            "session_id": result.session_id,
            "model": result.model,
            "context_tokens": result.context_tokens,
            "usage": result.usage,
        }

    async def _new_session(self, params: dict[str, Any]) -> dict[str, str]:
        return {"session_id": await self.client.new_session()}

    async def _resume_session(self, params: dict[str, Any]) -> dict[str, str]:
        session_id = params.get("session_id")
        if not isinstance(session_id, str) or not session_id:
            raise ValueError("session_id is required")
        return {"session_id": await self.client.resume(session_id)}

    async def _list_sessions(self, params: dict[str, Any]) -> list[dict[str, Any]]:
        query = str(params.get("query", ""))
        limit = int(params.get("limit", 20))
        return [
            item.model_dump(mode="json")
            for item in self.client.sessions(query=query, limit=limit)
        ]

    async def _fork_session(self, params: dict[str, Any]) -> dict[str, str]:
        session_id = params.get("session_id")
        if session_id is not None and not isinstance(session_id, str):
            raise ValueError("session_id must be a string")
        message_count = params.get("message_count")
        if message_count is not None and (
            not isinstance(message_count, int)
            or isinstance(message_count, bool)
            or message_count < 0
        ):
            raise ValueError("message_count must be a non-negative integer")
        branch_name = params.get("branch_name", "")
        branch_summary = params.get("branch_summary", "")
        if not isinstance(branch_name, str) or not isinstance(branch_summary, str):
            raise ValueError("branch_name and branch_summary must be strings")
        return {
            "session_id": await self.client.fork(
                session_id,
                message_count=message_count,
                branch_name=branch_name,
                branch_summary=branch_summary,
            )
        }

    async def _session_tree(self, params: dict[str, Any]) -> list[dict[str, Any]]:
        session_id = params.get("session_id")
        if session_id is not None and not isinstance(session_id, str):
            raise ValueError("session_id must be a string")
        return [
            item.model_dump(mode="json")
            for item in self.client.session_tree(session_id)
        ]

    async def _list_events(self, params: dict[str, Any]) -> dict[str, Any]:
        session_id = params.get("session_id")
        if session_id is not None and not isinstance(session_id, str):
            raise ValueError("session_id must be a string")
        records = self.client.events(
            session_id,
            after_sequence=int(params.get("after_sequence", 0)),
            turn_id=params.get("turn_id"),
            limit=int(params.get("limit", 1000)),
        )
        return {
            "schema_version": EVENT_SCHEMA_VERSION,
            "events": [
                {"sequence": item.sequence, "event": item.event.to_wire()}
                for item in records
            ],
            "next_sequence": (
                records[-1].sequence
                if records
                else int(params.get("after_sequence", 0))
            ),
        }

    async def _status(self, params: dict[str, Any]) -> dict[str, Any]:
        session = self.client.loop.current_session
        return {
            "model": self.client.config.model,
            "mode": self.client.loop.permission_policy.mode.value,
            "workspace": str(self.client.loop.project_root),
            "session_id": session.session_id if session else None,
            "context_tokens": self.client.loop._last_context_tokens,
            "usage": self.client.loop.last_turn_usage,
        }


def _result(request_id: Any, value: Any) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "result": value}


def _error(
    request_id: Any, code: int, message: str, data: Any | None = None
) -> dict[str, Any]:
    error: dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        error["data"] = data
    return {"jsonrpc": "2.0", "id": request_id, "error": error}
