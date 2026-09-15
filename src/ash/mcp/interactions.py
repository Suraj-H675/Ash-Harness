"""Human-reviewed MCP sampling and elicitation boundaries."""

from __future__ import annotations

import inspect
import json
import math
import re
from collections.abc import Awaitable, Callable, Mapping
from typing import Any

from jsonschema import Draft202012Validator, FormatChecker  # type: ignore[import-untyped]

from ash.mcp.client import MCPProtocolError
from ash.providers.base import (
    CompletionStopCategory,
    ProviderABC,
    ProviderCapabilityError,
    completion_stop_category,
)
from ash.providers.messages import CanonicalMessage

SamplingReview = Callable[
    [str, str, dict[str, Any]], bool | Awaitable[bool]
]
ElicitationCallback = Callable[
    [str, str, dict[str, Any]], dict[str, Any] | Awaitable[dict[str, Any]]
]
ProviderFactory = Callable[[], ProviderABC]

MAX_MCP_SAMPLING_MESSAGES = 128
MAX_MCP_SAMPLING_REQUEST_BYTES = 512 * 1024
MAX_MCP_SAMPLING_RESPONSE_CHARS = 1_000_000
MAX_MCP_SYSTEM_PROMPT_CHARS = 64_000
MAX_MCP_ELICITATION_MESSAGE_CHARS = 4_000
MAX_MCP_ELICITATION_SCHEMA_BYTES = 128 * 1024
MAX_MCP_ELICITATION_PROPERTIES = 64
MAX_MCP_ELICITATION_RESPONSE_BYTES = 128 * 1024
_ALLOWED_STRING_FORMATS = frozenset({"uri", "email", "date", "date-time"})
_SENSITIVE_FIELD_MARKERS = frozenset(
    {
        "password",
        "passcode",
        "secret",
        "credential",
        "credentials",
        "apikey",
        "apitoken",
        "accesstoken",
        "refreshtoken",
        "authtoken",
        "bearertoken",
        "privatekey",
        "otp",
        "onetimepassword",
        "securitycode",
        "cvv",
        "pin",
    }
)


async def _maybe_await(value: Any) -> Any:
    return await value if inspect.isawaitable(value) else value


def _invalid(message: str, *, data: dict[str, Any] | None = None) -> MCPProtocolError:
    return MCPProtocolError(message, code=-32602, **({"data": data} if data else {}))


def _json_size(value: Any, *, label: str, maximum: int) -> int:
    try:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, OverflowError, RecursionError) as exc:
        raise _invalid(f"{label} must be valid JSON") from exc
    if len(encoded) > maximum:
        raise _invalid(f"{label} exceeds {maximum} bytes")
    return len(encoded)


def _model_id(provider: ProviderABC) -> str:
    family = str(getattr(provider, "provider_family", "custom")).strip() or "custom"
    model = provider.model_name.strip()
    return f"{family}/{model}"


def _normalize_sampling_content(
    role: str,
    value: Any,
) -> list[dict[str, Any]]:
    blocks = value if isinstance(value, list) else [value]
    if not blocks:
        raise _invalid("sampling message content must not be empty")
    normalized: list[dict[str, Any]] = []
    for block in blocks:
        if not isinstance(block, Mapping):
            raise _invalid("sampling message content blocks must be objects")
        block_type = block.get("type")
        if block_type == "text":
            text = block.get("text")
            if not isinstance(text, str):
                raise _invalid("sampling text content must contain text")
            normalized.append({"type": "text", "text": text})
            continue
        if block_type == "image" and role == "user":
            data = block.get("data")
            mime_type = block.get("mimeType")
            if not isinstance(data, str) or not isinstance(mime_type, str):
                raise _invalid("sampling image content is malformed")
            normalized.append(
                {"type": "image", "media_type": mime_type, "data": data}
            )
            continue
        raise _invalid(
            f"sampling content type {block_type!r} is unsupported without tool/context capabilities"
        )
    return normalized


def _normalize_sampling_request(
    params: dict[str, Any],
    *,
    maximum_tokens: int,
) -> tuple[list[CanonicalMessage], float, int, dict[str, Any]]:
    _json_size(
        params,
        label="MCP sampling request",
        maximum=MAX_MCP_SAMPLING_REQUEST_BYTES,
    )
    if "task" in params:
        raise _invalid("task-augmented MCP sampling is not supported")
    if "tools" in params or "toolChoice" in params:
        raise _invalid("MCP sampling tools are not supported by this client capability")
    include_context = params.get("includeContext", "none")
    if include_context not in {None, "none"}:
        raise _invalid("MCP sampling context inclusion is not supported")
    stop_sequences = params.get("stopSequences")
    if stop_sequences is not None and stop_sequences != [] and stop_sequences != ():
        raise _invalid("MCP sampling stop sequences are not supported")

    raw_max_tokens = params.get("maxTokens")
    if (
        isinstance(raw_max_tokens, bool)
        or not isinstance(raw_max_tokens, int)
        or raw_max_tokens < 1
    ):
        raise _invalid("MCP sampling maxTokens must be a positive integer")
    max_tokens = min(raw_max_tokens, maximum_tokens)

    raw_temperature = params.get("temperature", 0.0)
    if (
        isinstance(raw_temperature, bool)
        or not isinstance(raw_temperature, (int, float))
        or not math.isfinite(raw_temperature)
        or not 0 <= raw_temperature <= 1
    ):
        raise _invalid("MCP sampling temperature must be between 0 and 1")
    temperature = float(raw_temperature)

    model_preferences = params.get("modelPreferences")
    if model_preferences is not None and not isinstance(model_preferences, dict):
        raise _invalid("MCP sampling modelPreferences must be an object")
    metadata = params.get("metadata")
    if metadata is not None and not isinstance(metadata, dict):
        raise _invalid("MCP sampling metadata must be an object")

    messages_value = params.get("messages")
    if (
        not isinstance(messages_value, list)
        or not messages_value
        or len(messages_value) > MAX_MCP_SAMPLING_MESSAGES
    ):
        raise _invalid(
            f"MCP sampling messages must contain 1-{MAX_MCP_SAMPLING_MESSAGES} entries"
        )

    canonical: list[CanonicalMessage] = []
    system_prompt = params.get("systemPrompt")
    if system_prompt is not None:
        if not isinstance(system_prompt, str):
            raise _invalid("MCP sampling systemPrompt must be a string")
        if len(system_prompt) > MAX_MCP_SYSTEM_PROMPT_CHARS:
            raise _invalid(
                f"MCP sampling systemPrompt exceeds {MAX_MCP_SYSTEM_PROMPT_CHARS} characters"
            )
        if system_prompt:
            canonical.append(CanonicalMessage(role="system", content=system_prompt))

    review_messages: list[dict[str, Any]] = []
    for raw_message in messages_value:
        if not isinstance(raw_message, Mapping):
            raise _invalid("MCP sampling messages must be objects")
        role = raw_message.get("role")
        if role not in {"user", "assistant"}:
            raise _invalid("MCP sampling message role must be user or assistant")
        content = _normalize_sampling_content(str(role), raw_message.get("content"))
        try:
            canonical.append(
                CanonicalMessage.model_validate({"role": role, "content": content})
            )
        except ValueError as exc:
            raise _invalid(f"MCP sampling message is invalid: {exc}") from exc
        review_messages.append({"role": role, "content": content})

    preview = {
        "systemPrompt": system_prompt or "",
        "messages": review_messages,
        "requestedMaxTokens": raw_max_tokens,
        "maxTokens": max_tokens,
        "temperature": temperature,
        "modelPreferences": model_preferences or {},
    }
    return canonical, temperature, max_tokens, preview


def _sampling_stop_reason(reason: str | None) -> str:
    normalized = (reason or "").strip().casefold().replace("-", "_")
    if normalized in {"length", "max_tokens", "max_output_tokens", "token_limit"}:
        return "maxTokens"
    if normalized in {"stop_sequence", "stopsequence"}:
        return "stopSequence"
    if normalized in {"tool_use", "tool_calls", "function_call"}:
        return "toolUse"
    if not normalized or normalized in {
        "stop",
        "complete",
        "completed",
        "done",
        "end",
        "end_turn",
        "eos",
    }:
        return "endTurn"
    return reason or "endTurn"


def _field_text(schema: Mapping[str, Any], key: str) -> str:
    value = schema.get(key, "")
    return value if isinstance(value, str) else ""


def _sensitive_field(name: str, schema: Mapping[str, Any]) -> bool:
    combined = " ".join(
        (name, _field_text(schema, "title"), _field_text(schema, "description"))
    )
    lowered = combined.casefold()
    compact = re.sub(r"[^a-z0-9]+", "", lowered)
    words = set(re.findall(r"[a-z0-9]+", lowered))
    return bool(
        words & _SENSITIVE_FIELD_MARKERS
        or any(marker in compact for marker in _SENSITIVE_FIELD_MARKERS if len(marker) >= 4)
    )


def _enum_values(schema: Mapping[str, Any]) -> list[str] | None:
    enum = schema.get("enum")
    if isinstance(enum, list) and enum and all(isinstance(item, str) for item in enum):
        return list(enum)
    one_of = schema.get("oneOf")
    if isinstance(one_of, list) and one_of:
        values: list[str] = []
        for item in one_of:
            if not isinstance(item, Mapping) or not isinstance(item.get("const"), str):
                return None
            values.append(str(item["const"]))
        return values
    return None


def _multi_enum_values(schema: Mapping[str, Any]) -> list[str] | None:
    items = schema.get("items")
    if not isinstance(items, Mapping):
        return None
    enum = items.get("enum")
    if isinstance(enum, list) and enum and all(isinstance(item, str) for item in enum):
        return list(enum)
    any_of = items.get("anyOf")
    if isinstance(any_of, list) and any_of:
        values: list[str] = []
        for item in any_of:
            if not isinstance(item, Mapping) or not isinstance(item.get("const"), str):
                return None
            values.append(str(item["const"]))
        return values
    return None


def _normalize_elicitation_property(
    name: str,
    raw: Any,
) -> dict[str, Any]:
    if not isinstance(raw, Mapping):
        raise _invalid(f"elicitation property {name!r} must be an object")
    if _sensitive_field(name, raw):
        raise _invalid(
            f"elicitation property {name!r} appears sensitive; use URL-mode elicitation"
        )
    field_type = raw.get("type")
    common = {key: raw[key] for key in ("title", "description") if key in raw}
    if any(not isinstance(value, str) for value in common.values()):
        raise _invalid(f"elicitation property {name!r} has invalid display metadata")

    if field_type == "string":
        enum_values = _enum_values(raw)
        if enum_values is not None:
            allowed = {"type", "title", "description", "enum", "enumNames", "oneOf", "default"}
            if set(raw) - allowed:
                raise _invalid(f"elicitation enum property {name!r} has unsupported keywords")
            normalized: dict[str, Any] = {"type": "string", "enum": enum_values, **common}
            if "default" in raw:
                normalized["default"] = raw["default"]
            return normalized
        allowed = {"type", "title", "description", "minLength", "maxLength", "format", "default"}
        if set(raw) - allowed:
            raise _invalid(f"elicitation string property {name!r} has unsupported keywords")
        normalized = {"type": "string", **common}
        for key in ("minLength", "maxLength"):
            if key in raw:
                value = raw[key]
                if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                    raise _invalid(f"elicitation {name!r}.{key} must be non-negative")
                normalized[key] = value
        if (
            "minLength" in normalized
            and "maxLength" in normalized
            and normalized["minLength"] > normalized["maxLength"]
        ):
            raise _invalid(f"elicitation property {name!r} has minLength above maxLength")
        if "format" in raw:
            if raw["format"] not in _ALLOWED_STRING_FORMATS:
                raise _invalid(f"elicitation property {name!r} has unsupported format")
            normalized["format"] = raw["format"]
        if "default" in raw:
            normalized["default"] = raw["default"]
        return normalized

    if field_type in {"number", "integer"}:
        allowed = {"type", "title", "description", "minimum", "maximum", "default"}
        if set(raw) - allowed:
            raise _invalid(f"elicitation numeric property {name!r} has unsupported keywords")
        normalized = {"type": field_type, **common}
        for key in ("minimum", "maximum"):
            if key in raw:
                value = raw[key]
                if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
                    raise _invalid(f"elicitation {name!r}.{key} must be finite numeric")
                normalized[key] = value
        if (
            "minimum" in normalized
            and "maximum" in normalized
            and normalized["minimum"] > normalized["maximum"]
        ):
            raise _invalid(f"elicitation property {name!r} has minimum above maximum")
        if "default" in raw:
            normalized["default"] = raw["default"]
        return normalized

    if field_type == "boolean":
        allowed = {"type", "title", "description", "default"}
        if set(raw) - allowed:
            raise _invalid(f"elicitation boolean property {name!r} has unsupported keywords")
        normalized = {"type": "boolean", **common}
        if "default" in raw:
            normalized["default"] = raw["default"]
        return normalized

    if field_type == "array":
        values = _multi_enum_values(raw)
        if values is None:
            raise _invalid(f"elicitation array property {name!r} must be a string enum")
        allowed = {"type", "title", "description", "items", "minItems", "maxItems", "default"}
        if set(raw) - allowed:
            raise _invalid(f"elicitation multi-select {name!r} has unsupported keywords")
        normalized = {
            "type": "array",
            "items": {"type": "string", "enum": values},
            "uniqueItems": True,
            **common,
        }
        for key in ("minItems", "maxItems"):
            if key in raw:
                value = raw[key]
                if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                    raise _invalid(f"elicitation {name!r}.{key} must be non-negative")
                normalized[key] = value
        if (
            "minItems" in normalized
            and "maxItems" in normalized
            and normalized["minItems"] > normalized["maxItems"]
        ):
            raise _invalid(f"elicitation property {name!r} has minItems above maxItems")
        if "default" in raw:
            normalized["default"] = raw["default"]
        return normalized

    raise _invalid(f"elicitation property {name!r} uses unsupported type {field_type!r}")


def _normalize_elicitation_schema(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping) or value.get("type") != "object":
        raise _invalid("elicitation requestedSchema must be a flat object schema")
    _json_size(
        value,
        label="MCP elicitation schema",
        maximum=MAX_MCP_ELICITATION_SCHEMA_BYTES,
    )
    allowed_root = {"$schema", "type", "properties", "required"}
    if set(value) - allowed_root:
        raise _invalid("elicitation requestedSchema contains unsupported root keywords")
    properties = value.get("properties")
    if not isinstance(properties, Mapping) or len(properties) > MAX_MCP_ELICITATION_PROPERTIES:
        raise _invalid(
            f"elicitation requestedSchema properties must contain at most {MAX_MCP_ELICITATION_PROPERTIES} fields"
        )
    normalized_properties: dict[str, Any] = {}
    for name, schema in properties.items():
        if not isinstance(name, str) or not name or len(name) > 128:
            raise _invalid("elicitation property names must be 1-128 characters")
        normalized_properties[name] = _normalize_elicitation_property(name, schema)

    required = value.get("required", [])
    if (
        not isinstance(required, list)
        or not all(isinstance(item, str) for item in required)
        or len(required) != len(set(required))
        or any(item not in normalized_properties for item in required)
    ):
        raise _invalid("elicitation required must contain unique declared property names")

    normalized: dict[str, Any] = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
        "properties": normalized_properties,
        "required": list(required),
        "additionalProperties": False,
    }
    try:
        Draft202012Validator.check_schema(normalized)
    except Exception as exc:  # pragma: no cover - defensive after strict normalization
        raise _invalid("elicitation requestedSchema is invalid") from exc
    return normalized


def _validate_elicitation_response(
    response: Any,
    schema: dict[str, Any],
) -> dict[str, Any]:
    if not isinstance(response, Mapping):
        raise _invalid("elicitation response must be an object")
    action = response.get("action")
    if action not in {"accept", "decline", "cancel"}:
        raise _invalid("elicitation response action must be accept, decline, or cancel")
    if action != "accept":
        if "content" in response and response.get("content") not in {None, {}}:
            raise _invalid("declined or cancelled elicitation must not include content")
        return {"action": str(action)}
    content = response.get("content")
    if not isinstance(content, dict):
        raise _invalid("accepted elicitation response must include content")
    _json_size(
        content,
        label="MCP elicitation response",
        maximum=MAX_MCP_ELICITATION_RESPONSE_BYTES,
    )
    validator = Draft202012Validator(schema, format_checker=FormatChecker())
    errors = sorted(validator.iter_errors(content), key=lambda error: list(error.path))
    if errors:
        first = errors[0]
        path = ".".join(str(part) for part in first.path)
        where = f" at {path}" if path else ""
        raise _invalid(f"elicitation response is invalid{where}: {first.message}")
    return {"action": "accept", "content": dict(content)}


class MCPInteractionController:
    """Validate, review, and service optional MCP client-side interactions."""

    def __init__(
        self,
        *,
        sampling_enabled: bool,
        elicitation_enabled: bool,
        provider_factory: ProviderFactory | None = None,
        sampling_review: SamplingReview | None = None,
        elicitation_callback: ElicitationCallback | None = None,
        sampling_max_tokens: int = 4096,
    ) -> None:
        if sampling_max_tokens < 1:
            raise ValueError("MCP sampling max tokens must be positive")
        self.sampling_enabled = sampling_enabled
        self.elicitation_enabled = elicitation_enabled
        self.provider_factory = provider_factory
        self.sampling_review = sampling_review
        self.elicitation_callback = elicitation_callback
        self.sampling_max_tokens = sampling_max_tokens
        self._sampling_lock = __import__("asyncio").Lock()
        self._elicitation_lock = __import__("asyncio").Lock()

    @property
    def supports_sampling(self) -> bool:
        return bool(
            self.sampling_enabled
            and self.provider_factory is not None
            and self.sampling_review is not None
        )

    @property
    def supports_elicitation(self) -> bool:
        return bool(self.elicitation_enabled and self.elicitation_callback is not None)

    async def handle_sampling(
        self,
        server: str,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        if not self.supports_sampling:
            raise MCPProtocolError("MCP sampling is not enabled", code=-32601)
        if self._sampling_lock.locked():
            raise MCPProtocolError("another MCP sampling request is already active", code=-32000)
        async with self._sampling_lock:
            messages, temperature, max_tokens, preview = _normalize_sampling_request(
                params,
                maximum_tokens=self.sampling_max_tokens,
            )
            review = self.sampling_review
            assert review is not None
            approved = await _maybe_await(
                review(server, "request", dict(preview))
            )
            if not approved:
                raise MCPProtocolError("User rejected sampling request", code=-1)

            assert self.provider_factory is not None
            provider = self.provider_factory()
            try:
                detect = getattr(provider, "detect_capabilities", None)
                if callable(detect):
                    try:
                        await detect()
                    except ProviderCapabilityError:
                        raise
                    except Exception:
                        pass
                if any(
                    isinstance(message.content, list)
                    and any(block.type == "image" for block in message.content)
                    for message in messages
                ) and not provider.capabilities.vision:
                    raise _invalid("configured sampling model does not support image input")
                provider.configure_max_tokens(max_tokens)
                chunks: list[str] = []
                response_chars = 0
                stop_reason: str | None = None
                terminal_seen = False
                async for chunk in provider.stream_chat(
                    messages,
                    temperature=temperature,
                    tools=None,
                ):
                    if terminal_seen:
                        raise MCPProtocolError(
                            "sampling provider emitted output after terminal completion",
                            code=-32603,
                        )
                    if chunk.native_tool_calls or chunk.tool_call_delta:
                        raise MCPProtocolError(
                            "sampling model attempted unsupported tool use",
                            code=-32603,
                        )
                    if chunk.content:
                        response_chars += len(chunk.content)
                        if response_chars > MAX_MCP_SAMPLING_RESPONSE_CHARS:
                            raise MCPProtocolError(
                                "MCP sampling response exceeded the client limit",
                                code=-32603,
                            )
                        chunks.append(chunk.content)
                    if chunk.is_done:
                        terminal_seen = True
                        stop_reason = chunk.stop_reason
                        category = completion_stop_category(stop_reason)
                        if category in {
                            CompletionStopCategory.FILTERED,
                            CompletionStopCategory.ERROR,
                        }:
                            raise MCPProtocolError(
                                "sampling provider reported an unsuccessful terminal outcome",
                                code=-32603,
                                data={"stopReason": stop_reason or "error"},
                            )
                if not terminal_seen:
                    raise MCPProtocolError(
                        "sampling provider ended without a terminal completion",
                        code=-32603,
                    )
                text = "".join(chunks)
                model = _model_id(provider)
                result = {
                    "role": "assistant",
                    "content": {"type": "text", "text": text},
                    "model": model,
                    "stopReason": _sampling_stop_reason(stop_reason),
                }
                approved_response = await _maybe_await(
                    review(
                        server,
                        "response",
                        {
                            "model": model,
                            "response": text,
                            "stopReason": result["stopReason"],
                        },
                    )
                )
                if not approved_response:
                    raise MCPProtocolError("User rejected sampling response", code=-1)
                return result
            finally:
                await provider.aclose()

    async def handle_elicitation(
        self,
        server: str,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        if not self.supports_elicitation:
            raise MCPProtocolError("MCP elicitation is not enabled", code=-32601)
        if self._elicitation_lock.locked():
            raise MCPProtocolError("another MCP elicitation request is already active", code=-32000)
        async with self._elicitation_lock:
            if "task" in params:
                raise _invalid("task-augmented MCP elicitation is not supported")
            mode = params.get("mode", "form")
            if mode != "form":
                raise _invalid("only form-mode MCP elicitation is supported")
            message = params.get("message")
            if (
                not isinstance(message, str)
                or not message.strip()
                or len(message) > MAX_MCP_ELICITATION_MESSAGE_CHARS
            ):
                raise _invalid(
                    f"elicitation message must contain 1-{MAX_MCP_ELICITATION_MESSAGE_CHARS} characters"
                )
            schema = _normalize_elicitation_schema(params.get("requestedSchema"))
            callback = self.elicitation_callback
            assert callback is not None
            response = await _maybe_await(callback(server, message, schema))
            return _validate_elicitation_response(response, schema)
