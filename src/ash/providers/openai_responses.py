"""Shared OpenAI Responses protocol support and first-party API-key adapter."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
from collections.abc import Callable, Mapping, Sequence
from typing import Any, AsyncGenerator

import openai  # type: ignore[import-not-found]

from ash.context.tokens import OpenAITokenCounter
from ash.providers.base import (
    ProviderABC,
    ProviderIncompleteStreamError,
    ProviderMalformedToolArgumentsError,
    ProviderTerminalError,
    StreamChunk,
    TokenCounterLike,
    managed_async_stream,
)
from ash.providers.messages import (
    MAX_PROVIDER_STATE_BYTES,
    CanonicalToolCall,
    MessageInput,
    normalize_messages,
)
from ash.providers.openai import (
    _api_key_replay_identity,
    _owned_openai_http_client,
    _sdk_replay_transport_scope,
)
from ash.providers.readiness import (
    normalize_provider_base_url,
    redact_provider_error,
    require_secure_provider_transport,
)
from ash.providers.replay_state import ProviderReplayCredentialIdentity
from ash.providers.replay_affinity import verify_provider_replay_identity
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
                if state_item["type"] == "responses_compacted_input":
                    items.extend(state_item["output"])
                elif state_item["type"] == "responses_server_compaction_output":
                    checkpoint_output = state_item["output"]
                    anchor_index = max(
                        index
                        for index, entry in enumerate(checkpoint_output)
                        if entry.get("type") == "compaction"
                    )
                    for checkpoint_item in checkpoint_output[anchor_index:]:
                        if checkpoint_item.get("type") == "compaction":
                            # Server output has an output-only `created_by`.
                            # Retain it durably, but omit it from provider input.
                            items.append(
                                {
                                    key: value
                                    for key, value in checkpoint_item.items()
                                    if key != "created_by"
                                }
                            )
                        else:
                            items.append(checkpoint_item)
                else:
                    items.append(dict(state_item))
            if any(
                state_item["type"]
                in {"responses_compacted_input", "responses_server_compaction_output"}
                for state_item in message.get("provider_state") or []
            ):
                # The stored provider output is the canonical replay of this
                # assistant response. Its Ash text/tool representation remains
                # durable for audit, but must not be appended a second time.
                continue
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
            output: Any = str(content)
            tool_result_images = message.get("tool_result_images")
            if isinstance(tool_result_images, list) and tool_result_images:
                output_blocks = (
                    [{"type": "text", "text": str(content)}]
                    if content
                    else []
                )
                output_blocks.extend(_responses_user_content(tool_result_images))
                output = output_blocks
            items.append(
                {
                    "type": "function_call_output",
                    "call_id": message["tool_call_id"],
                    "output": output,
                }
            )
            continue
        raise ValueError(f"unsupported Responses history role: {role}")
    return "\n\n".join(instructions), items


def _response_output_item_payload(item: Any) -> dict[str, Any]:
    if isinstance(item, Mapping):
        payload = dict(item)
    else:
        dump = getattr(item, "model_dump", None)
        if not callable(dump):
            raise ValueError("Responses output item is not a typed object")
        payload = dump(mode="json", exclude_none=True)
    if not isinstance(payload, dict):
        raise ValueError("Responses output item is not an object")
    try:
        encoded = json.dumps(
            payload,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeEncodeError, OverflowError):
        raise ValueError("Responses output item is not bounded JSON") from None
    if len(encoded) > MAX_PROVIDER_STATE_BYTES:
        raise ValueError("Responses output item exceeds the replay bound")
    return payload


def _validate_server_compaction_output(
    output: Any,
    *,
    streamed_items: Sequence[dict[str, Any]],
    streamed_text: Mapping[tuple[int, int], str],
    tool_calls: Sequence[CanonicalToolCall],
) -> list[dict[str, Any]]:
    if not isinstance(output, list) or not output:
        raise ValueError("server-side compaction output is invalid")
    try:
        payload = [_response_output_item_payload(item) for item in output]
        if payload != list(streamed_items):
            raise ValueError("completed output differs from streamed output")
        anchor_indices = [
            index
            for index, item in enumerate(payload)
            if item.get("type") == "compaction"
        ]
        if not anchor_indices:
            raise ValueError("server-side compaction output has no anchor")
        expected_text: dict[tuple[int, int], str] = {}
        for output_index, item in enumerate(payload):
            if item.get("type") != "message":
                continue
            content = item.get("content")
            if not isinstance(content, list):
                raise ValueError("server-side Responses message content is invalid")
            for content_index, block in enumerate(content):
                if not isinstance(block, dict):
                    raise ValueError("server-side Responses text block is invalid")
                block_type = block.get("type")
                if block_type == "output_text":
                    expected_text[(output_index, content_index)] = block["text"]
                elif block_type == "refusal":
                    expected_text[(output_index, content_index)] = block["refusal"]
        if dict(streamed_text) != expected_text:
            raise ValueError("completed text differs from streamed text")
        anchor_index = anchor_indices[-1]
        anchor = payload[anchor_index]
        if not isinstance(anchor.get("id"), str) or not anchor["id"]:
            raise ValueError("server-side compaction anchor has no identifier")
        suffix_calls = [
            item
            for item in payload[anchor_index:]
            if item.get("type") == "function_call"
        ]
        if len(suffix_calls) != len(tool_calls):
            raise ValueError("server-side compaction would orphan a tool call")
        for item, call in zip(suffix_calls, tool_calls, strict=True):
            if (
                item.get("call_id") != call.call_id
                or item.get("name") != call.name
                or strict_json_loads(str(item.get("arguments", "")))
                != call.arguments
            ):
                raise ValueError("server-side compaction tool replay is inconsistent")
        state = [{"type": "responses_server_compaction_output", "output": payload}]
        from ash.providers.messages import CanonicalMessage

        validated = CanonicalMessage.model_validate(
            {"role": "assistant", "content": "", "provider_state": state}
        )
        return validated.provider_state or []
    except (TypeError, ValueError, KeyError, UnicodeError, OverflowError):
        raise RuntimeError(
            "OpenAI returned a malformed or unsafe server-side compaction checkpoint"
        ) from None


async def _server_compaction_stream_events(stream: Any) -> AsyncGenerator[Any, None]:
    """Hide SDK parser and transport details for opaque-state requests."""

    try:
        iterator = stream.__aiter__()
    except asyncio.CancelledError:
        raise
    except Exception:
        raise ProviderIncompleteStreamError(
            "OpenAI Responses server-side compaction stream failed before completion"
        ) from None
    while True:
        try:
            event = await iterator.__anext__()
        except StopAsyncIteration:
            return
        except asyncio.CancelledError:
            raise
        except Exception:
            raise ProviderIncompleteStreamError(
                "OpenAI Responses server-side compaction stream failed before completion"
            ) from None
        yield event


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
    capture_server_side_compaction: bool = False,
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
    streamed_output: list[dict[str, Any]] = []
    streamed_output_bytes = 0
    streamed_text: dict[tuple[int, int], str] = {}
    streamed_text_bytes = 0
    output_capture_failed = False
    compaction_item_seen = False
    async with managed_async_stream(stream, label='OpenAI Responses') as managed_stream:
        event_stream = (
            _server_compaction_stream_events(managed_stream)
            if capture_server_side_compaction
            else managed_stream
        )
        async for event in event_stream:
            event_type = str(getattr(event, "type", "") or "")
            if event_type in {"response.output_text.delta", "response.refusal.delta"}:
                delta = str(getattr(event, "delta", "") or "")
                if delta:
                    if capture_server_side_compaction:
                        output_index = getattr(event, "output_index", None)
                        content_index = getattr(event, "content_index", None)
                        if (
                            type(output_index) is not int
                            or output_index < 0
                            or type(content_index) is not int
                            or content_index < 0
                        ):
                            output_capture_failed = True
                        elif not output_capture_failed:
                            key = (output_index, content_index)
                            try:
                                delta_bytes = len(delta.encode("utf-8"))
                            except UnicodeEncodeError:
                                output_capture_failed = True
                            else:
                                if streamed_text_bytes + delta_bytes > MAX_PROVIDER_STATE_BYTES:
                                    output_capture_failed = True
                                else:
                                    streamed_text[key] = streamed_text.get(key, "") + delta
                                    streamed_text_bytes += delta_bytes
                    yield StreamChunk(content=delta, model=model_name)
                continue
            if event_type == "response.output_item.done":
                item = getattr(event, "item", None)
                item_type = str(getattr(item, "type", "") or "")
                if capture_server_side_compaction:
                    output_index = getattr(event, "output_index", None)
                    try:
                        payload = _response_output_item_payload(item)
                        encoded_size = len(
                            json.dumps(
                                payload,
                                ensure_ascii=False,
                                allow_nan=False,
                                separators=(",", ":"),
                            ).encode("utf-8")
                        )
                    except (TypeError, ValueError, UnicodeError, OverflowError):
                        output_capture_failed = True
                    else:
                        if (
                            type(output_index) is not int
                            or output_index != len(streamed_output)
                            or len(streamed_output) >= MAX_OPENAI_RESPONSE_STATE_ITEMS
                            or streamed_output_bytes + encoded_size
                            > MAX_PROVIDER_STATE_BYTES
                        ):
                            output_capture_failed = True
                        elif not output_capture_failed:
                            streamed_output.append(payload)
                            streamed_output_bytes += encoded_size
                    if item_type == "compaction":
                        compaction_item_seen = True
                        if output_capture_failed:
                            raise RuntimeError(
                                "OpenAI server-side compaction output exceeded its "
                                "verified replay bounds"
                            ) from None
                if item_type == "reasoning":
                    if len(provider_state) >= MAX_OPENAI_RESPONSE_STATE_ITEMS:
                        raise RuntimeError("OpenAI returned too many reasoning replay items")
                    provider_state.append(_reasoning_replay_item(item))
                elif item_type == "function_call":
                    raw_arguments = str(getattr(item, "arguments", "") or "")
                    malformed = False
                    try:
                        arguments = strict_json_loads(raw_arguments)
                    except (json.JSONDecodeError, ValueError, UnicodeError):
                        malformed = True
                    # Raise *outside* the parser's exception handler. `from None`
                    # only hides `__context__` in formatted tracebacks; the
                    # original JSON error may retain private arguments in `doc`.
                    if malformed:
                        raise ProviderMalformedToolArgumentsError(
                            "OpenAI returned invalid native function-call arguments"
                        )
                    if not isinstance(arguments, dict):
                        raise ProviderMalformedToolArgumentsError(
                            "OpenAI returned non-object native function-call arguments"
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
                if capture_server_side_compaction:
                    raise ProviderTerminalError(
                        "OpenAI Responses server-side compaction request failed"
                    )
                error = getattr(getattr(event, "response", None), "error", None)
                code = str(getattr(error, "code", "") or "error")
                raise terminal_error_factory(code)
            if event_type == "response.incomplete":
                if capture_server_side_compaction:
                    raise ProviderTerminalError(
                        "OpenAI Responses server-side compaction stream was incomplete"
                    )
                details = getattr(
                    getattr(event, "response", None),
                    "incomplete_details",
                    None,
                )
                reason = str(getattr(details, "reason", "") or "incomplete")
                raise ProviderTerminalError(reason)
            if event_type == "error":
                if capture_server_side_compaction:
                    raise ProviderTerminalError(
                        "OpenAI Responses server-side compaction request failed"
                    )
                code = str(getattr(event, "code", "") or "error")
                raise terminal_error_factory(code)
            if event_type == "response.completed":
                response = getattr(event, "response", None)
                if capture_server_side_compaction:
                    response_output = getattr(response, "output", None)
                    response_has_compaction = isinstance(response_output, list) and any(
                        str(getattr(item, "type", "") or "") == "compaction"
                        for item in response_output
                    )
                    if response_has_compaction and output_capture_failed:
                        raise RuntimeError(
                            "OpenAI server-side compaction output exceeded its "
                            "verified replay bounds"
                        ) from None
                    if response_has_compaction != compaction_item_seen:
                        raise RuntimeError(
                            "OpenAI completed output did not match its streamed "
                            "compaction items"
                        ) from None
                    if compaction_item_seen:
                        provider_state = _validate_server_compaction_output(
                            response_output,
                            streamed_items=streamed_output,
                            streamed_text=streamed_text,
                            tool_calls=tool_calls,
                        )
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


async def compact_openai_responses_history(
    client: Any,
    *,
    model_name: str,
    messages: Sequence[MessageInput],
) -> list[dict[str, Any]]:
    """Capture the *complete* compacted Responses input without rewriting it.

    This requires an API-key-backed Responses client with compaction access.
    The session owner is responsible for installing the checkpoint atomically
    and must not change durable history on transport/validation failure.
    """
    _, input_items = prepare_openai_responses_input(messages)
    if not input_items:
        raise ValueError("cannot compact an empty Responses conversation")
    # The SDK currently decodes the documented retained user-message items
    # through a response-output union intended for assistant messages.
    # Capture the raw HTTP JSON instead: model_dump() can otherwise coerce or
    # discard fields when the generated response schema and wire schema differ.
    result = await client.responses.with_raw_response.compact(
        model=model_name,
        input=input_items,
        timeout=30,
    )
    raw_bytes = result.content
    if not isinstance(raw_bytes, bytes) or len(raw_bytes) > MAX_PROVIDER_STATE_BYTES:
        raise RuntimeError("native compaction returned an oversized response")
    try:
        response_data = strict_json_loads(raw_bytes.decode("utf-8"))
    except (UnicodeError, ValueError):
        # Raw provider data can include credentials or opaque reasoning. Do not
        # chain a parsing exception that quotes untrusted input into tracebacks.
        raise RuntimeError("native compaction returned invalid JSON") from None
    if (
        not isinstance(response_data, dict)
        or response_data.get("object") != "response.compaction"
    ):
        raise RuntimeError("native compaction response has an unexpected object type")
    outputs = response_data.get("output")
    if not isinstance(outputs, list) or not outputs:
        raise RuntimeError("native compaction returned an invalid replacement window")
    payload: dict[str, Any] = {
        "type": "responses_compacted_input",
        "output": outputs,
    }
    from ash.providers.messages import CanonicalMessage

    try:
        CanonicalMessage.model_validate(
            {"role": "assistant", "content": "", "provider_state": [payload]}
        )
    except (TypeError, ValueError):
        raise RuntimeError(
            "native compaction returned a malformed or oversized checkpoint"
        ) from None
    return [payload]


class OpenAIResponsesProvider(ProviderABC):
    """First-party OpenAI API-key adapter for current Responses-native models."""

    provider_family = "openai"
    # The complete replacement-window serializer has synthetic coverage, but
    # the installed SDK has not passed a real API compaction response fixture.
    # Keep the optional native action unavailable until verified end-to-end.
    supports_native_compaction = False

    def __init__(
        self,
        model_name: str,
        api_key: str,
        *,
        base_url: str | None = None,
        token_counter: TokenCounterLike | None = None,
        client: Any | None = None,
    ) -> None:
        if not api_key:
            raise ValueError("OpenAI API key is required")
        if base_url:
            base_url = normalize_provider_base_url(base_url, provider="openai")
            require_secure_provider_transport(base_url, provider="openai")
        self._model_name = model_name
        self._api_key = api_key
        self._base_url = base_url
        self._token_counter = token_counter or OpenAITokenCounter(model_name)
        self._client = client
        self._owns_client = client is None
        self._max_tokens: int | None = None
        self._prompt_cache_enabled = False
        self._prompt_cache_key = ""
        self._prompt_cache_retention = "memory"
        self._server_side_compaction_enabled = False
        self._server_side_compaction_threshold: int | None = None

    @property
    def model_name(self) -> str:
        return self._model_name

    def replay_credential_identity(self) -> ProviderReplayCredentialIdentity | None:
        """Bind to the SDK's effective, cached endpoint and tenant selection."""

        if not self._owns_client:
            return None
        client_scope = _sdk_replay_transport_scope(
            self._resolve_client(), provider_family=self.provider_family
        )
        if client_scope is None:
            return None
        base_url, organization, project = client_scope
        return _api_key_replay_identity(
            self._api_key,
            provider_family=self.provider_family,
            base_url=base_url,
            extra_scope=(organization, project),
        )

    @property
    def server_side_compaction_enabled(self) -> bool:
        return self._server_side_compaction_enabled

    @property
    def server_side_compaction_route_binding(self) -> str:
        if not self._server_side_compaction_enabled:
            raise RuntimeError("server-side compaction is not enabled on this route")
        route_values = "\0".join(
            (
                self._api_key,
                os.environ.get("OPENAI_ORG_ID", ""),
                os.environ.get("OPENAI_PROJECT_ID", ""),
            )
        )
        return hashlib.sha256(route_values.encode("utf-8")).hexdigest()

    def enable_server_side_compaction(self) -> None:
        if self._base_url is not None:
            raise ValueError(
                "server-side compaction requires the first-party default endpoint"
            )
        self._server_side_compaction_enabled = True

    def configure_server_side_compaction_threshold(self, threshold: int) -> None:
        if not self._server_side_compaction_enabled:
            raise RuntimeError("server-side compaction is not enabled on this route")
        if type(threshold) is not int or threshold < 1:
            raise ValueError("server-side compaction threshold must be positive")
        self._server_side_compaction_threshold = threshold

    def count_tokens(self, text: str) -> int:
        return self._token_counter.count(text)

    def _resolve_client(self) -> Any:
        if self._client is None:
            self._client = openai.AsyncOpenAI(
                api_key=self._api_key,
                base_url=self._base_url,
                max_retries=0,
                http_client=_owned_openai_http_client(
                    trust_env=not getattr(
                        self, "_ash_disable_ambient_transport", False
                    )
                ),
            )
        return self._client

    async def compact_history(
        self, messages: Sequence[MessageInput]
    ) -> list[dict[str, Any]]:
        """Create a native reasoning checkpoint (API key access required)."""
        verify_provider_replay_identity(
            self.replay_credential_identity(),
            provider_family=self.provider_family,
            model_name=self.model_name,
        )
        try:
            return await compact_openai_responses_history(
                self._resolve_client(),
                model_name=self._model_name,
                messages=messages,
            )
        except Exception as exc:
            if isinstance(exc, ValueError):
                raise RuntimeError(
                    "OpenAI native compaction failed checkpoint validation"
                ) from None
            detail = redact_provider_error(str(exc), self._api_key)
            # The original SDK exception may include request headers or
            # opaque provider output. Redacting only the wrapper message is
            # insufficient if the original exception is still chained.
            raise RuntimeError(f"OpenAI native compaction failed: {detail}") from None

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
        verify_provider_replay_identity(
            self.replay_credential_identity(),
            provider_family=self.provider_family,
            model_name=self.model_name,
        )
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
        if self.configured_reasoning_effort is not None:
            request_options["reasoning"] = {
                "effort": self.configured_reasoning_effort
            }
        if self._server_side_compaction_enabled:
            threshold = self._server_side_compaction_threshold
            if threshold is None:
                raise RuntimeError(
                    "server-side compaction has no resolved request threshold"
                )
            request_options["context_management"] = [
                {"type": "compaction", "compact_threshold": threshold}
            ]

        client = self._resolve_client()
        async for chunk in stream_openai_responses(
            client,
            model_name=self._model_name,
            messages=messages,
            tools=tools,
            request_options=request_options,
            error_label="OpenAI API Responses",
            error_secrets=(self._api_key,),
            capture_server_side_compaction=self._server_side_compaction_enabled,
        ):
            yield chunk

    async def aclose(self) -> None:
        client = self._client
        if self._owns_client and client is not None:
            await client.close()
            if self._client is client:
                self._client = None
