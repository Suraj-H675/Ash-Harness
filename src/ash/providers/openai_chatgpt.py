"""OpenAI Responses adapter backed by an eligible ChatGPT plan."""

from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from typing import Any, AsyncGenerator

import openai  # type: ignore[import-not-found]

from ash.context.tokens import OpenAITokenCounter
from ash.providers.base import (
    ProviderABC,
    ProviderIncompleteStreamError,
    ProviderTerminalError,
    StreamChunk,
    TokenCounterLike,
)
from ash.providers.messages import (
    CanonicalToolCall,
    MessageInput,
    normalize_messages,
)
from ash.providers.openai import _owned_openai_http_client
from ash.providers.openai_chatgpt_auth import (
    CHATGPT_RESOURCE,
    ChatGPTAuthSession,
)
from ash.providers.readiness import redact_provider_error
from ash.safe_io import strict_json_loads


MAX_CHATGPT_RESPONSE_STATE_ITEMS = 64
_RETRYABLE_CHATGPT_RESPONSE_FAILURES = frozenset(
    {
        "subscription_sharing_usage_unavailable",
        "subscription_sharing_user_unavailable",
    }
)


class ChatGPTResponsesTerminalError(ProviderTerminalError):
    """Responses failure with preview-specific safe retry classification."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.retriable = code in _RETRYABLE_CHATGPT_RESPONSE_FAILURES


def _responses_tool_schema(tools: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
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


def prepare_chatgpt_responses_input(
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
                payload: dict[str, Any] = {
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
                items.append(payload)
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
        raise RuntimeError(
            "OpenAI stateless reasoning item was not replayable"
        )
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


class OpenAIChatGPTProvider(ProviderABC):
    """Stateless Responses provider using OAuth plan credentials."""

    provider_family = "openai"

    def __init__(
        self,
        model_name: str,
        *,
        auth_session: ChatGPTAuthSession | None = None,
        token_counter: TokenCounterLike | None = None,
        client_factory: Callable[[str], Any] | None = None,
    ) -> None:
        if not model_name.strip():
            raise ValueError("ChatGPT plan model name cannot be empty")
        self._model_name = model_name
        self._auth_session = auth_session or ChatGPTAuthSession()
        self._token_counter = token_counter or OpenAITokenCounter(model_name)
        self._client_factory = client_factory

    @property
    def model_name(self) -> str:
        return self._model_name

    def count_tokens(self, text: str) -> int:
        return self._token_counter.count(text)

    def configure_max_tokens(self, max_tokens: int) -> None:
        # ChatGPT-plan preview currently rejects max_output_tokens. Retain
        # validation because the Ash loop calls this provider-neutral hook.
        if max_tokens < 1:
            raise ValueError("max_tokens must be positive")

    def configure_prompt_cache(
        self,
        *,
        enabled: bool,
        cache_key: str = "",
        retention: str = "memory",
    ) -> None:
        del enabled, cache_key
        if retention not in {"memory", "extended"}:
            raise ValueError("prompt cache retention must be memory or extended")

    def _new_client(self, access_token: str) -> Any:
        if self._client_factory is not None:
            return self._client_factory(access_token)
        return openai.AsyncOpenAI(
            api_key=access_token,
            base_url=CHATGPT_RESOURCE,
            max_retries=0,
            http_client=_owned_openai_http_client(),
        )

    async def stream_chat(
        self,
        messages: Sequence[MessageInput],
        temperature: float = 0.0,
        tools: list[dict[str, Any]] | None = None,
    ) -> AsyncGenerator[StreamChunk, None]:
        del temperature
        instructions, response_input = prepare_chatgpt_responses_input(messages)
        access_token = await self._auth_session.access_token()
        client = self._new_client(access_token)
        completed = False
        provider_state: list[dict[str, Any]] = []
        tool_calls: list[CanonicalToolCall] = []
        owns_client = self._client_factory is None
        try:
            kwargs: dict[str, Any] = {
                "model": self._model_name,
                "input": response_input,
                "include": ["reasoning.encrypted_content"],
                "store": False,
                "stream": True,
            }
            if instructions:
                kwargs["instructions"] = instructions
            converted_tools = _responses_tool_schema(tools)
            if converted_tools:
                kwargs["tools"] = converted_tools
            try:
                stream = await client.responses.create(**kwargs)
            except Exception as exc:  # noqa: BLE001
                detail = redact_provider_error(str(exc), access_token)
                raise RuntimeError(f"OpenAI ChatGPT Responses error: {detail}") from exc

            async for event in stream:
                event_type = str(getattr(event, "type", "") or "")
                if event_type == "response.output_text.delta":
                    delta = str(getattr(event, "delta", "") or "")
                    if delta:
                        yield StreamChunk(content=delta, model=self._model_name)
                    continue
                if event_type == "response.refusal.delta":
                    delta = str(getattr(event, "delta", "") or "")
                    if delta:
                        yield StreamChunk(content=delta, model=self._model_name)
                    continue
                if event_type == "response.output_item.done":
                    item = getattr(event, "item", None)
                    item_type = str(getattr(item, "type", "") or "")
                    if item_type == "reasoning":
                        if len(provider_state) >= MAX_CHATGPT_RESPONSE_STATE_ITEMS:
                            raise RuntimeError(
                                "OpenAI returned too many reasoning replay items"
                            )
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
                            raise RuntimeError(
                                "OpenAI function-call arguments were not an object"
                            )
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
                    raise ChatGPTResponsesTerminalError(code)
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
                    raise ChatGPTResponsesTerminalError(code)
                if event_type == "response.completed":
                    response = getattr(event, "response", None)
                    usage = getattr(response, "usage", None)
                    input_details = getattr(usage, "input_tokens_details", None)
                    yield StreamChunk(
                        is_done=True,
                        model=self._model_name,
                        prompt_tokens=_usage_value(usage, "input_tokens"),
                        completion_tokens=_usage_value(usage, "output_tokens"),
                        cache_read_tokens=_usage_value(input_details, "cached_tokens"),
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
        finally:
            if owns_client:
                close = getattr(client, "close", None)
                aclose = getattr(client, "aclose", None)
                if callable(aclose):
                    await aclose()
                elif callable(close):
                    result = close()
                    if hasattr(result, "__await__"):
                        await result
