"""Shared OpenAI Responses protocol support and first-party API-key adapter."""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from typing import Any, AsyncGenerator

import openai  # type: ignore[import-not-found]

from ash.context.tokens import OpenAITokenCounter
from ash.providers.base import (
    ProviderABC,
    ProviderIncompleteStreamError,
    ProviderTerminalError,
    StreamChunk,
    TokenCounterLike,
    managed_async_stream,
)
from ash.providers.messages import CanonicalToolCall, MessageInput, normalize_messages
from ash.providers.openai import _owned_openai_http_client
from ash.providers.readiness import redact_provider_error
from ash.safe_io import strict_json_loads


MAX_OPENAI_RESPONSE_STATE_ITEMS = 64


def responses_tool_schema(tools: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    if not tools:
        return []
    converted: list[dict[str, Any]] = []
    for index, item in enumerate(tools):
        if not isinstance(item, dict) or item.get("type") != "function":
            raise ValueError(f"unsupported Responses tool declaration at index {index}")
        function = item.get("function")
        if not isinstance(function, dict):
            raise ValueError(f"invalid function tool declaration at index {index}")
        name = function.get("name")
        parameters = function.get("parameters")
        if not isinstance(name, str) or not name:
            raise ValueError(f"function tool at index {index} has no valid name")
        if not isinstance(parameters, dict):
            raise ValueError(f"function tool {name!r} has no JSON schema")
        converted.append(
            {
                "type": "function",
                "name": name,
                "description": str(function.get("description", "")),
                "parameters": parameters,
                "strict": False,
            }
        )
    return converted


def _responses_user_content(content: Any) -> Any:
    if not isinstance(content, list):
        return content
    result: list[dict[str, Any]] = []
    for block in content:
        if not isinstance(block, dict):
            continue
        if block.get("type") == "text":
            result.append(
                {
                    "type": "input_text",
                    "text": str(block.get("text", "")),
                }
            )
        elif block.get("type") == "image":
            media_type = str(block.get("media_type", ""))
            data = str(block.get("data", ""))
            result.append(
                {
                    "type": "input_image",
                    "detail": "auto",
                    "image_url": f"data:{media_type};base64,{data}",
                }
            )
    return result


def prepare_openai_responses_input(
    messages: Sequence[MessageInput],
) -> tuple[str, list[dict[str, Any]]]:
    """Translate canonical Ash history into stateless Responses input."""

    instructions: list[str] = []
    items: list[dict[str, Any]] = []
    for message in normalize_messages(messages):
        role = message["role"]
        content = message.get("content", "")
        if role == "system":
            if not isinstance(content, str):
                raise ValueError("Responses system instructions must be text")
            if content:
                instructions.append(content)
            continue
        if role == "user":
            items.append(
                {
                    "role": "user",
                    "content": _responses_user_content(content),
                }
            )
            continue
        if role == "assistant":
            for state_item in message.get("provider_state") or []:
                items.append(dict(state_item))
            if content:
                if not isinstance(content, str):
                    raise ValueError("assistant Responses replay content must be text")
                items.append({"role": "assistant", "content": content})
            for call in message.get("tool_calls") or []:
                items.append(
                    {
                        "type": "function_call",
                        "call_id": call["call_id"],
                        "name": call["name"],
                        "arguments": json.dumps(
                            call.get("arguments", {}),
                            ensure_ascii=False,
                            allow_nan=False,
                            separators=(",", ":"),
                        ),
                    }
                )
            continue
        if role == "tool":
            items.append(
                {
                    "type": "function_call_output",
                    "call_id": message["tool_call_id"],
                    "output": str(content),
                }
            )
            continue
        raise ValueError(f"unsupported Responses history role: {role}")
    return "\n\n".join(instructions), items


def _reasoning_replay_item(item: Any) -> dict[str, Any]:
    if getattr(item, "type", None) != "reasoning":
        raise ValueError("provider replay state item is not reasoning")
    item_id = str(getattr(item, "id", "") or "")
    encrypted_content = str(getattr(item, "encrypted_content", "") or "")
    if not item_id or not encrypted_content:
        raise RuntimeError("OpenAI stateless reasoning item was not replayable")
    summary_payload: list[dict[str, str]] = []
    for summary in getattr(item, "summary", None) or []:
        text = str(getattr(summary, "text", "") or "")
        if text:
            summary_payload.append({"type": "summary_text", "text": text})
    payload: dict[str, Any] = {
        "type": "reasoning",
        "id": item_id,
        "summary": summary_payload,
        "encrypted_content": encrypted_content,
    }
    status = str(getattr(item, "status", "") or "")
    if status in {"in_progress", "completed", "incomplete"}:
        payload["status"] = status
    return payload


def _usage_value(usage: Any, name: str) -> int:
    value = getattr(usage, name, 0) if usage is not None else 0
    return int(value or 0)


async def stream_openai_responses(
    client: Any,
    *,
    model_name: str,
    messages: Sequence[MessageInput],
    tools: list[dict[str, Any]] | None,
    request_options: Mapping[str, Any] | None = None,
    error_label: str = "OpenAI Responses",
    error_secrets: Sequence[str] = (),
    terminal_error_factory: Callable[[str], ProviderTerminalError] = ProviderTerminalError,
) -> AsyncGenerator[StreamChunk, None]:
    """Stream one stateless Responses request into Ash's canonical chunks."""

    instructions, response_input = prepare_openai_responses_input(messages)
    kwargs: dict[str, Any] = {
        "model": model_name,
        "input": response_input,
        "include": ["reasoning.encrypted_content"],
        "store": False,
        "stream": True,
    }
    if instructions:
        kwargs["instructions"] = instructions
    converted_tools = responses_tool_schema(tools)
    if converted_tools:
        kwargs["tools"] = converted_tools
    if request_options:
        kwargs.update(request_options)

    try:
        stream = await client.responses.create(**kwargs)
    except Exception as exc:  # noqa: BLE001 - normalize SDK/provider failures
        detail = redact_provider_error(str(exc), *error_secrets)
        raise RuntimeError(f"{error_label} error: {detail}") from exc

    completed = False
    provider_state: list[dict[str, Any]] = []
    tool_calls: list[CanonicalToolCall] = []
    async with managed_async_stream(stream, label='OpenAI Responses') as managed_stream:
        async for event in managed_stream:
            event_type = str(getattr(event, "type", "") or "")
            if event_type in {"response.output_text.delta", "response.refusal.delta"}:
                delta = str(getattr(event, "delta", "") or "")
                if delta:
                    yield StreamChunk(content=delta, model=model_name)
                continue
            if event_type == "response.output_item.done":
                item = getattr(event, "item", None)
                item_type = str(getattr(item, "type", "") or "")
                if item_type == "reasoning":
                    if len(provider_state) >= MAX_OPENAI_RESPONSE_STATE_ITEMS:
                        raise RuntimeError("OpenAI returned too many reasoning replay items")
                    provider_state.append(_reasoning_replay_item(item))
                elif item_type == "function_call":
                    raw_arguments = str(getattr(item, "arguments", "") or "")
                    try:
                        arguments = strict_json_loads(raw_arguments)
                    except (json.JSONDecodeError, ValueError, UnicodeError) as exc:
                        raise RuntimeError(
                            "OpenAI returned invalid function-call arguments"
                        ) from exc
                    if not isinstance(arguments, dict):
                        raise RuntimeError("OpenAI function-call arguments were not an object")
                    tool_calls.append(
                        CanonicalToolCall(
                            call_id=str(getattr(item, "call_id", "") or ""),
                            name=str(getattr(item, "name", "") or ""),
                            arguments=arguments,
                        )
                    )
                continue
            if event_type == "response.failed":
                error = getattr(getattr(event, "response", None), "error", None)
                code = str(getattr(error, "code", "") or "error")
                raise terminal_error_factory(code)
            if event_type == "response.incomplete":
                details = getattr(
                    getattr(event, "response", None),
                    "incomplete_details",
                    None,
                )
                reason = str(getattr(details, "reason", "") or "incomplete")
                raise ProviderTerminalError(reason)
            if event_type == "error":
                code = str(getattr(event, "code", "") or "error")
                raise terminal_error_factory(code)
            if event_type == "response.completed":
                response = getattr(event, "response", None)
                usage = getattr(response, "usage", None)
                input_details = getattr(usage, "input_tokens_details", None)
                yield StreamChunk(
                    is_done=True,
                    model=model_name,
                    prompt_tokens=_usage_value(usage, "input_tokens"),
                    completion_tokens=_usage_value(usage, "output_tokens"),
                    cache_read_tokens=_usage_value(input_details, "cached_tokens"),
                    cache_write_tokens=_usage_value(input_details, "cache_write_tokens"),
                    usage_source="provider" if usage is not None else "unavailable",
                    stop_reason="completed",
                    native_tool_calls=tool_calls or None,
                    provider_state=provider_state or None,
                )
                completed = True
                continue
    if not completed:
        raise ProviderIncompleteStreamError(
            "OpenAI Responses stream ended without response.completed"
        )


class OpenAIResponsesProvider(ProviderABC):
    """First-party OpenAI API-key adapter for current Responses-native models."""

    provider_family = "openai"

    def __init__(
        self,
        model_name: str,
        api_key: str,
        *,
        token_counter: TokenCounterLike | None = None,
        client: Any | None = None,
    ) -> None:
        if not api_key:
            raise ValueError("OpenAI API key is required")
        self._model_name = model_name
        self._api_key = api_key
        self._token_counter = token_counter or OpenAITokenCounter(model_name)
        self._client = client
        self._owns_client = client is None
        self._max_tokens: int | None = None
        self._prompt_cache_enabled = False
        self._prompt_cache_key = ""
        self._prompt_cache_retention = "memory"

    @property
    def model_name(self) -> str:
        return self._model_name

    def count_tokens(self, text: str) -> int:
        return self._token_counter.count(text)

    def _resolve_client(self) -> Any:
        if self._client is None:
            self._client = openai.AsyncOpenAI(
                api_key=self._api_key,
                max_retries=0,
                http_client=_owned_openai_http_client(),
            )
        return self._client

    def configure_max_tokens(self, max_tokens: int) -> None:
        if max_tokens < 1:
            raise ValueError("max_tokens must be positive")
        self._max_tokens = max_tokens

    def configure_prompt_cache(
        self,
        *,
        enabled: bool,
        cache_key: str = "",
        retention: str = "memory",
    ) -> None:
        if retention not in {"memory", "extended"}:
            raise ValueError("prompt cache retention must be memory or extended")
        if enabled and retention == "extended":
            raise ValueError(
                "current OpenAI Responses models do not support Ash's 24h "
                "extended prompt-cache retention"
            )
        self._prompt_cache_enabled = enabled
        self._prompt_cache_key = cache_key
        self._prompt_cache_retention = retention

    async def stream_chat(
        self,
        messages: Sequence[MessageInput],
        temperature: float = 0.0,
        tools: list[dict[str, Any]] | None = None,
    ) -> AsyncGenerator[StreamChunk, None]:
        if temperature != 0.0:
            raise ValueError(
                "current OpenAI Responses reasoning models do not support "
                "custom temperature"
            )
        request_options: dict[str, Any] = {}
        if self._max_tokens is not None:
            request_options["max_output_tokens"] = self._max_tokens
        if self._prompt_cache_enabled:
            request_options["prompt_cache_options"] = {
                "mode": "implicit",
                "ttl": "30m",
            }
            if self._prompt_cache_key:
                request_options["prompt_cache_key"] = self._prompt_cache_key
        else:
            request_options["prompt_cache_options"] = {"mode": "explicit"}

        client = self._resolve_client()
        async for chunk in stream_openai_responses(
            client,
            model_name=self._model_name,
            messages=messages,
            tools=tools,
            request_options=request_options,
            error_label="OpenAI API Responses",
            error_secrets=(self._api_key,),
        ):
            yield chunk

    async def aclose(self) -> None:
        client = self._client
        if self._owns_client and client is not None:
            await client.close()
            if self._client is client:
                self._client = None
