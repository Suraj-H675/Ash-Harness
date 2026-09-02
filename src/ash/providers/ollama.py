"""Ollama local model provider for Ash."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Sequence
from typing import Any, AsyncGenerator

import httpx

from ash.context.tokens import AnthropicTokenCounter
from ash.providers.capabilities import ProviderCapabilities
from ash.providers.base import ProviderABC, StreamChunk, TokenCounterLike
from ash.providers.messages import (
    CanonicalToolCall,
    MessageInput,
    normalize_messages,
)
from ash.providers.retry import ProviderHTTPError

MAX_OLLAMA_METADATA_BYTES = 1_000_000
MAX_OLLAMA_STREAM_LINE_BYTES = 1_000_000
MAX_OLLAMA_STREAM_BYTES = 16_000_000
MAX_OLLAMA_ERROR_BYTES = 64 * 1024


class OllamaProvider(ProviderABC):
    provider_family = "ollama"
    _dynamic_capabilities: ProviderCapabilities | None = None

    def __init__(
        self,
        model_name: str = "llama3",
        base_url: str = "http://localhost:11434",
        *,
        token_counter: TokenCounterLike | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._model_name = model_name
        self._base_url = base_url.rstrip("/")
        self._token_counter = token_counter or AnthropicTokenCounter()
        self._client = (
            client if client is not None else httpx.AsyncClient(timeout=60.0)
        )
        self._owns_client = client is None
        self._dynamic_capabilities = None

    async def detect_capabilities(
        self, *, refresh: bool = False
    ) -> ProviderCapabilities:
        """Probe model metadata once and map supported tool capabilities."""

        if self._dynamic_capabilities is not None and not refresh:
            return self._dynamic_capabilities
        self._dynamic_capabilities = None
        try:
            async with self._client.stream(
                "POST",
                f"{self._base_url}/api/show",
                json={"model": self._model_name},
                timeout=5.0,
            ) as response:
                if response.status_code == 200:
                    raw_payload = await _read_bounded_response(
                        response,
                        max_bytes=MAX_OLLAMA_METADATA_BYTES,
                        label="Ollama metadata response",
                    )
                    payload = json.loads(raw_payload)
                    if not isinstance(payload, dict):
                        raise ValueError("metadata payload must be an object")
                    details = payload.get("details")
                    families = (
                        details.get("families", [])
                        if isinstance(details, dict)
                        else []
                    )
                    if not isinstance(families, list):
                        families = []
                    template = str(payload.get("template", "")).casefold()
                    supports_tools = any(
                        family in {"tools", "function-calling"}
                        for family in families
                    ) or any(
                        marker in template
                        for marker in ("tool_call", "tools", "function_call")
                    )
                    model_info = payload.get("model_info")
                    context_window = (
                        model_info.get("general.context_length")
                        if isinstance(model_info, dict)
                        else None
                    )
                    self._dynamic_capabilities = ProviderCapabilities(
                        native_tools=bool(supports_tools),
                        local=True,
                        context_window=(
                            int(context_window)
                            if isinstance(context_window, int)
                            and not isinstance(context_window, bool)
                            and context_window > 0
                            else None
                        ),
                    )
        except Exception:  # noqa: BLE001 - capability probing is best-effort
            pass
        if self._dynamic_capabilities is None:
            self._dynamic_capabilities = ProviderCapabilities(local=True)
        return self._dynamic_capabilities

    @property
    def model_name(self) -> str:
        return self._model_name

    @property
    def capabilities(self) -> ProviderCapabilities:
        """Expose negotiated metadata while retaining a local-only fallback."""

        return self._dynamic_capabilities or ProviderCapabilities(local=True)

    def count_tokens(self, text: str) -> int:
        return self._token_counter.count(text)

    def configure_max_tokens(self, max_tokens: int) -> None:
        if max_tokens < 1:
            raise ValueError("max_tokens must be positive")
        self._max_tokens = max_tokens

    async def stream_chat(
        self,
        messages: Sequence[MessageInput],
        temperature: float = 0.0,
        tools: list[dict[str, Any]] | None = None,
    ) -> AsyncGenerator[StreamChunk, None]:
        options: dict[str, Any] = {"temperature": temperature}
        if hasattr(self, "_max_tokens"):
            options["num_predict"] = self._max_tokens
        payload = {
            "model": self._model_name,
            "messages": normalize_messages(messages),
            "stream": True,
            "options": options,
        }
        if tools:
            payload["tools"] = tools

        pending_tool_calls: dict[str, CanonicalToolCall] = {}
        try:
            async with self._client.stream(
                "POST", f"{self._base_url}/api/chat", json=payload
            ) as resp:
                if resp.status_code != 200:
                    text = await _read_bounded_error(resp)
                    suffix = f": {text}" if text else ""
                    raise ProviderHTTPError(
                        f"Ollama API error {resp.status_code}{suffix}",
                        status_code=resp.status_code,
                        headers=resp.headers,
                    )
                async for line in _iter_bounded_lines(resp):
                    if not line.strip():
                        continue
                    data = _parse_stream_message(line)
                    message = data.get("message", {})
                    if not isinstance(message, dict):
                        raise RuntimeError(
                            "Ollama stream contained an invalid message object"
                        )
                    content = message.get("content", "")
                    if not isinstance(content, str):
                        raise RuntimeError(
                            "Ollama stream contained non-text message content"
                        )
                    is_done = data.get("done", False)
                    if not isinstance(is_done, bool):
                        raise RuntimeError(
                            "Ollama stream contained a non-boolean done flag"
                        )
                    for tool_call in _parse_native_tool_calls(message):
                        pending_tool_calls[tool_call.call_id] = tool_call
                    # Extract token usage from final chunk.
                    prompt_tokens = 0
                    completion_tokens = 0
                    stop_reason = None
                    if is_done:
                        prompt_tokens = _usage_count(data.get("prompt_eval_count"))
                        completion_tokens = _usage_count(data.get("eval_count"))
                        done_reason = data.get("done_reason", "")
                        if not isinstance(done_reason, str):
                            done_reason = ""
                        if done_reason.strip():
                            stop_reason = done_reason
                    yield StreamChunk(
                        content=content,
                        is_done=is_done,
                        model=self._model_name,
                        prompt_tokens=prompt_tokens,
                        completion_tokens=completion_tokens,
                        usage_source=(
                            "provider"
                            if is_done
                            and ("prompt_eval_count" in data or "eval_count" in data)
                            else "unavailable"
                        ),
                        stop_reason=stop_reason,
                        native_tool_calls=(
                            list(pending_tool_calls.values()) if is_done else None
                        ),
                    )
        except httpx.HTTPError as exc:
            raise RuntimeError(f"Ollama connection error: {exc}") from exc

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()


async def _read_bounded_response(
    response: httpx.Response,
    *,
    max_bytes: int,
    label: str,
) -> bytes:
    content_length = response.headers.get("content-length")
    if content_length is not None:
        try:
            declared_length = int(content_length)
        except (TypeError, ValueError) as exc:
            raise RuntimeError(f"{label} had an invalid Content-Length") from exc
        if declared_length < 0 or declared_length > max_bytes:
            raise RuntimeError(f"{label} exceeded {max_bytes} bytes")

    chunks: list[bytes] = []
    total = 0
    async for chunk in response.aiter_bytes():
        total += len(chunk)
        if total > max_bytes:
            raise RuntimeError(f"{label} exceeded {max_bytes} bytes")
        chunks.append(chunk)
    return b"".join(chunks)


async def _read_bounded_error(response: httpx.Response) -> str:
    try:
        raw = await _read_bounded_response(
            response,
            max_bytes=MAX_OLLAMA_ERROR_BYTES,
            label="Ollama error response",
        )
    except Exception:  # noqa: BLE001 - error details are best-effort
        return ""
    return raw.decode("utf-8", errors="replace").strip()


async def _iter_bounded_lines(response: httpx.Response) -> AsyncIterator[str]:
    buffer = bytearray()
    total = 0
    async for chunk in response.aiter_bytes():
        total += len(chunk)
        if total > MAX_OLLAMA_STREAM_BYTES:
            raise RuntimeError(
                f"Ollama stream exceeded {MAX_OLLAMA_STREAM_BYTES} bytes"
            )
        buffer.extend(chunk)
        while True:
            newline = buffer.find(b"\n")
            if newline < 0:
                break
            line = bytes(buffer[:newline])
            del buffer[: newline + 1]
            yield _decode_stream_line(line)
        if len(buffer) > MAX_OLLAMA_STREAM_LINE_BYTES:
            raise RuntimeError(
                "Ollama stream line exceeded "
                f"{MAX_OLLAMA_STREAM_LINE_BYTES} bytes"
            )
    if buffer:
        if len(buffer) > MAX_OLLAMA_STREAM_LINE_BYTES:
            raise RuntimeError(
                "Ollama stream line exceeded "
                f"{MAX_OLLAMA_STREAM_LINE_BYTES} bytes"
            )
        yield _decode_stream_line(bytes(buffer))


def _decode_stream_line(line: bytes) -> str:
    try:
        return line.rstrip(b"\r").decode("utf-8")
    except UnicodeDecodeError as exc:
        raise RuntimeError("Ollama stream contained invalid UTF-8") from exc


def _parse_stream_message(line: str) -> dict[str, Any]:
    try:
        data = json.loads(line)
    except (json.JSONDecodeError, TypeError, ValueError) as exc:
        raise RuntimeError("Ollama stream contained invalid JSON") from exc
    if not isinstance(data, dict):
        raise RuntimeError("Ollama stream message must be an object")
    return data


def _parse_native_tool_calls(message: dict[str, Any]) -> list[CanonicalToolCall]:
    raw_tool_calls = message.get("tool_calls", [])
    if raw_tool_calls is None:
        return []
    if not isinstance(raw_tool_calls, list):
        raise RuntimeError("Ollama stream contained invalid tool_calls")

    parsed: list[CanonicalToolCall] = []
    for index, raw_call in enumerate(raw_tool_calls):
        if not isinstance(raw_call, dict):
            raise RuntimeError("Ollama stream contained an invalid tool call")
        function = raw_call.get("function")
        if not isinstance(function, dict):
            raise RuntimeError("Ollama stream contained an invalid tool function")
        call_id = raw_call.get("id") or raw_call.get("call_id") or f"call_{index}"
        try:
            parsed.append(
                CanonicalToolCall.model_validate(
                    {
                        "call_id": call_id,
                        "name": function.get("name"),
                        "arguments": function.get("arguments", {}),
                    }
                )
            )
        except (TypeError, ValueError) as exc:
            raise RuntimeError("Ollama stream contained an invalid tool call") from exc
    return parsed


def _usage_count(value: object) -> int:
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return value
    return 0
