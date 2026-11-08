"""Fixed offline worker entry point for one isolated coworker AshLoop turn."""

from __future__ import annotations

import asyncio
import json
import sys
from collections.abc import AsyncGenerator, Sequence
from pathlib import Path
from typing import Any
from uuid import UUID

from ash.core.session import CoworkerWorkerSessionBinding, SessionStore
from ash.providers.base import ProviderABC, StreamChunk
from ash.providers.messages import MessageInput


WORKER_REQUEST_PATH = Path("/workspace/request.json")
WORKER_WORKSPACE_DIRECTORY = Path("/workspace")
WORKER_SESSION_DIRECTORY = Path("/worker-state")
WORKER_MODEL = "custom/ash-offline-coworker-v1"
WORKER_RESPONSE = "Ash offline coworker completed one supervised turn."
WORKER_MAX_REQUEST_BYTES = 16 * 1024
WORKER_MAX_PROMPT_BYTES = 8 * 1024
WORKER_MAX_RESPONSE_BYTES = 1024
WORKER_TURN_TIMEOUT_SECONDS = 30


class _OfflineCoworkerProvider(ProviderABC):
    """A fixed, stateless provider that never interprets prompt contents."""

    @property
    def model_name(self) -> str:
        return WORKER_MODEL.partition("/")[2]

    def count_tokens(self, text: str) -> int:
        return max(1, len(text.split()))

    async def stream_chat(
        self,
        messages: Sequence[MessageInput],
        temperature: float = 0.0,
        tools: list[dict[str, Any]] | None = None,
    ) -> AsyncGenerator[StreamChunk, None]:
        del messages, temperature
        if tools:
            raise RuntimeError("offline coworker provider does not accept tools")
        yield StreamChunk(
            content=WORKER_RESPONSE,
            is_done=True,
            model=self.model_name,
        )


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate protocol field")
        result[key] = value
    return result


def _canonical_uuid(value: Any) -> str:
    if not isinstance(value, str):
        raise ValueError("invalid protocol identifier")
    parsed = UUID(value)
    if str(parsed) != value:
        raise ValueError("invalid protocol identifier")
    return value


def _read_request() -> tuple[str, str, str]:
    raw = WORKER_REQUEST_PATH.read_bytes()
    if len(raw) > WORKER_MAX_REQUEST_BYTES:
        raise ValueError("worker request is too large")
    request = json.loads(raw, object_pairs_hook=_unique_object)
    if not isinstance(request, dict) or set(request) != {
        "version",
        "request_id",
        "session_id",
        "prompt",
    }:
        raise ValueError("invalid worker request fields")
    if type(request["version"]) is not int or request["version"] != 1:
        raise ValueError("unsupported worker request version")
    request_id = _canonical_uuid(request["request_id"])
    session_id = _canonical_uuid(request["session_id"])
    prompt = request["prompt"]
    if not isinstance(prompt, str) or not prompt.strip() or "\0" in prompt:
        raise ValueError("invalid worker prompt")
    try:
        prompt_bytes = prompt.encode("utf-8", errors="strict")
    except UnicodeEncodeError as exc:
        raise ValueError("invalid worker prompt") from exc
    if len(prompt_bytes) > WORKER_MAX_PROMPT_BYTES:
        raise ValueError("worker prompt is too large")
    return request_id, session_id, prompt


async def _run_turn(
    request_id: str,
    session_id: str,
    prompt: str,
    *,
    session_store: SessionStore,
    binding: CoworkerWorkerSessionBinding,
) -> dict[str, Any]:
    from ash.config import AshConfig
    from ash.sdk import AshClient

    config = AshConfig.model_validate(
        {
            "workspace_root": binding.runtime_project_root,
            "db_directory": Path(session_store.db_path).parent,
            "model": WORKER_MODEL,
            "fallback_models": [],
            "safety_tier": "interactive",
            "session_retention_days": 0,
        }
    )
    client = await AshClient._create_coworker_turn(
        config=config,
        provider=_OfflineCoworkerProvider(),
        session_id=session_id,
        session_store=session_store,
        binding=binding,
    )
    try:
        result = await client.prompt(
            prompt,
            user_metadata={"coworker_request_id": request_id},
        )
        if result.session_id != session_id or result.response != WORKER_RESPONSE:
            raise RuntimeError("offline AshLoop turn returned an invalid result")
        return {
            "version": 1,
            "request_id": request_id,
            "session_id": session_id,
            "response": result.response,
        }
    finally:
        await client.close()


def main() -> int:
    """Run exactly one validated request and emit one bounded JSON response."""

    try:
        request_id, session_id, prompt = _read_request()
        session_store = SessionStore(WORKER_SESSION_DIRECTORY / "sessions.db")
        binding = session_store._bind_coworker_worker_session(
            session_id,
            WORKER_WORKSPACE_DIRECTORY,
        )

        async def execute() -> dict[str, Any]:
            async with asyncio.timeout(WORKER_TURN_TIMEOUT_SECONDS):
                return await _run_turn(
                    request_id,
                    session_id,
                    prompt,
                    session_store=session_store,
                    binding=binding,
                )

        response = asyncio.run(execute())
        encoded = json.dumps(response, separators=(",", ":"), ensure_ascii=True)
        if len(encoded.encode("utf-8")) > WORKER_MAX_RESPONSE_BYTES:
            raise RuntimeError("offline AshLoop response exceeded its limit")
    except BaseException as exc:
        if isinstance(exc, (KeyboardInterrupt, SystemExit)):
            raise
        sys.stderr.write("offline coworker worker failed\n")
        return 70
    sys.stdout.write(encoded)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
