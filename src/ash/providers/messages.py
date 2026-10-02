"""Provider-portable, validated chat message contracts."""

from __future__ import annotations

import base64
import binascii
import json
import re
from collections.abc import Mapping, Sequence
from typing import Annotated, Any, Literal, TypeAlias

from pydantic import (
    AliasChoices,
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

from ash.safe_io import strict_json_loads


MAX_CANONICAL_MESSAGES = 10_000
MAX_IMAGE_BASE64_CHARS = 14_000_000
MAX_CANONICAL_CONTENT_BLOCKS = 64
MAX_CANONICAL_CONTENT_BYTES = 16 * 1024 * 1024
MAX_TOOL_CALL_ID_BYTES = 512
MAX_TOOL_CALL_ARGUMENT_BYTES = 2 * 1024 * 1024
MAX_PROVIDER_TOOL_NAME_CHARS = 64
MAX_PROVIDER_STATE_ITEMS = 64
MAX_PROVIDER_STATE_BYTES = 4 * 1024 * 1024
MAX_PROVIDER_REASONING_SUMMARIES = 64
MAX_PROVIDER_REASONING_TEXT_BYTES = 256 * 1024
PROVIDER_TOOL_NAME = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
SUPPORTED_IMAGE_MEDIA_TYPES = frozenset(
    {"image/png", "image/jpeg", "image/gif", "image/webp"}
)


def _validate_tool_call_id(value: str) -> str:
    try:
        size = len(value.encode("utf-8"))
    except UnicodeEncodeError as exc:
        raise ValueError("tool call ID must be valid UTF-8 text") from exc
    if size > MAX_TOOL_CALL_ID_BYTES:
        raise ValueError(
            f"tool call ID exceeds {MAX_TOOL_CALL_ID_BYTES} UTF-8 bytes"
        )
    return value


def _validate_tool_call_argument_bytes(encoded: str) -> None:
    try:
        size = len(encoded.encode("utf-8"))
    except UnicodeEncodeError as exc:
        raise ValueError("tool-call arguments must be valid UTF-8 text") from exc
    if size > MAX_TOOL_CALL_ARGUMENT_BYTES:
        raise ValueError(
            "tool-call arguments exceed "
            f"{MAX_TOOL_CALL_ARGUMENT_BYTES} UTF-8 bytes"
        )


def validate_provider_tool_name(value: Any) -> str:
    if not isinstance(value, str) or not PROVIDER_TOOL_NAME.fullmatch(value):
        raise ValueError(
            "provider tool name must match [A-Za-z0-9_-]{1,64}"
        )
    return value


class TextContentBlock(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["text"] = "text"
    text: str


class ImageContentBlock(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["image"] = "image"
    media_type: str
    data: str = Field(..., min_length=1, max_length=MAX_IMAGE_BASE64_CHARS)

    @model_validator(mode="after")
    def validate_image(self) -> "ImageContentBlock":
        if self.media_type not in SUPPORTED_IMAGE_MEDIA_TYPES:
            raise ValueError(f"unsupported image media type: {self.media_type!r}")
        try:
            base64.b64decode(self.data, validate=True)
        except (ValueError, binascii.Error) as exc:
            raise ValueError("image data must be valid base64") from exc
        return self


ContentBlock: TypeAlias = Annotated[
    TextContentBlock | ImageContentBlock,
    Field(discriminator="type"),
]


class CanonicalToolCall(BaseModel):
    model_config = ConfigDict(extra="forbid")

    call_id: str = Field(
        ...,
        min_length=1,
        validation_alias=AliasChoices("call_id", "id"),
    )
    name: str = Field(..., min_length=1)
    arguments: dict[str, Any] = Field(default_factory=dict)

    @field_validator("call_id")
    @classmethod
    def validate_call_id(cls, value: str) -> str:
        return _validate_tool_call_id(value)

    @field_validator("name")
    @classmethod
    def validate_name(cls, value: str) -> str:
        return validate_provider_tool_name(value)

    @field_validator("arguments", mode="before")
    @classmethod
    def parse_arguments(cls, value: Any) -> Any:
        if not isinstance(value, str):
            return value
        _validate_tool_call_argument_bytes(value)
        try:
            parsed = strict_json_loads(value)
        except (json.JSONDecodeError, ValueError) as exc:
            raise ValueError("tool-call arguments contain invalid JSON") from exc
        if not isinstance(parsed, dict):
            raise ValueError("tool-call arguments JSON must decode to an object")
        return parsed

    @model_validator(mode="after")
    def validate_arguments(self) -> "CanonicalToolCall":
        try:
            encoded = json.dumps(
                self.arguments,
                allow_nan=False,
                separators=(",", ":"),
            )
        except (TypeError, ValueError) as exc:
            raise ValueError("tool-call arguments must be JSON serializable") from exc
        _validate_tool_call_argument_bytes(encoded)
        return self

    def to_wire(self) -> dict[str, Any]:
        return self.model_dump(mode="python")


class CanonicalMessage(BaseModel):
    """One provider-neutral message accepted by every Ash adapter."""

    model_config = ConfigDict(extra="forbid")

    role: Literal["system", "user", "assistant", "tool"]
    content: str | list[ContentBlock] = ""
    name: str | None = None
    tool_call_id: str | None = None
    tool_calls: list[CanonicalToolCall] | None = None
    provider_state: list[dict[str, Any]] | None = None

    @field_validator("content", mode="before")
    @classmethod
    def validate_content_size(cls, value: Any) -> Any:
        if isinstance(value, str):
            try:
                size = len(value.encode("utf-8"))
            except UnicodeEncodeError as exc:
                raise ValueError("message content must be valid UTF-8 text") from exc
            if size > MAX_CANONICAL_CONTENT_BYTES:
                raise ValueError(
                    "message content exceeds "
                    f"{MAX_CANONICAL_CONTENT_BYTES} UTF-8 bytes"
                )
            return value
        if not isinstance(value, list):
            return value
        if len(value) > MAX_CANONICAL_CONTENT_BLOCKS:
            raise ValueError(
                "message content blocks exceed "
                f"the limit of {MAX_CANONICAL_CONTENT_BLOCKS}"
            )
        total = 0
        for block in value:
            payload: Any
            if isinstance(block, TextContentBlock):
                payload = block.text
            elif isinstance(block, ImageContentBlock):
                payload = block.data
            elif isinstance(block, Mapping):
                block_type = block.get("type")
                payload = (
                    block.get("text")
                    if block_type == "text"
                    else block.get("data")
                    if block_type == "image"
                    else None
                )
            else:
                payload = None
            if not isinstance(payload, str):
                continue
            try:
                total += len(payload.encode("utf-8"))
            except UnicodeEncodeError as exc:
                raise ValueError("message content must be valid UTF-8 text") from exc
            if total > MAX_CANONICAL_CONTENT_BYTES:
                raise ValueError(
                    "message content exceeds "
                    f"{MAX_CANONICAL_CONTENT_BYTES} UTF-8 bytes"
                )
        return value

    @field_validator("tool_call_id")
    @classmethod
    def validate_tool_call_id(cls, value: str | None) -> str | None:
        return None if value is None else _validate_tool_call_id(value)

    @model_validator(mode="after")
    def validate_role_contract(self) -> "CanonicalMessage":
        if self.role == "tool":
            if not self.tool_call_id:
                raise ValueError("tool messages require tool_call_id")
            if not isinstance(self.content, str):
                raise ValueError("tool message content must be text")
        elif self.tool_call_id is not None:
            raise ValueError("tool_call_id is valid only on tool messages")
        if self.tool_calls and self.role != "assistant":
            raise ValueError("tool_calls are valid only on assistant messages")
        if self.provider_state is not None:
            if self.role != "assistant":
                raise ValueError("provider_state is valid only on assistant messages")
            if len(self.provider_state) > MAX_PROVIDER_STATE_ITEMS:
                raise ValueError(
                    f"provider_state exceeds {MAX_PROVIDER_STATE_ITEMS} items"
                )
            for item in self.provider_state:
                _validate_provider_state_item(item)
            try:
                encoded = json.dumps(
                    self.provider_state,
                    allow_nan=False,
                    separators=(",", ":"),
                ).encode("utf-8")
            except (TypeError, ValueError, UnicodeEncodeError) as exc:
                raise ValueError("provider_state must be bounded JSON data") from exc
            if len(encoded) > MAX_PROVIDER_STATE_BYTES:
                raise ValueError(
                    f"provider_state exceeds {MAX_PROVIDER_STATE_BYTES} UTF-8 bytes"
                )
        if isinstance(self.content, list):
            if self.role not in {"user", "assistant"}:
                raise ValueError("content blocks require a user or assistant role")
            if any(isinstance(block, ImageContentBlock) for block in self.content) and (
                self.role != "user"
            ):
                raise ValueError("image content is valid only on user messages")
        return self

    def to_wire(self) -> dict[str, Any]:
        return self.model_dump(mode="python", exclude_none=True)


def _validate_provider_state_item(item: Any) -> None:
    if not isinstance(item, dict):
        raise ValueError("provider_state items must be objects")
    if item.get("type") == "reasoning":
        _validate_provider_reasoning_state(item)
        return
    if item.get("type") == "sealed_provider_state":
        _validate_sealed_provider_state(item)
        return
    raise ValueError("provider_state item type is unsupported")


def _validate_provider_reasoning_state(item: dict[str, Any]) -> None:
    allowed = {"type", "id", "summary", "status", "encrypted_content"}
    unknown = set(item) - allowed
    if unknown:
        raise ValueError(
            "provider_state reasoning item contains unsupported field(s): "
            + ", ".join(sorted(str(key) for key in unknown))
        )
    item_id = item.get("id")
    encrypted = item.get("encrypted_content")
    summary = item.get("summary")
    status = item.get("status")
    if not isinstance(item_id, str) or not item_id or len(item_id) > 1024:
        raise ValueError("provider_state reasoning item has an invalid id")
    if not isinstance(encrypted, str) or not encrypted:
        raise ValueError(
            "provider_state reasoning item requires encrypted_content"
        )
    if not isinstance(summary, list) or len(summary) > MAX_PROVIDER_REASONING_SUMMARIES:
        raise ValueError("provider_state reasoning summary is invalid")
    for entry in summary:
        if (
            not isinstance(entry, dict)
            or set(entry) != {"type", "text"}
            or entry.get("type") != "summary_text"
            or not isinstance(entry.get("text"), str)
        ):
            raise ValueError("provider_state reasoning summary entry is invalid")
        try:
            size = len(entry["text"].encode("utf-8"))
        except UnicodeEncodeError as exc:
            raise ValueError(
                "provider_state reasoning summary must be valid UTF-8"
            ) from exc
        if size > MAX_PROVIDER_REASONING_TEXT_BYTES:
            raise ValueError("provider_state reasoning summary is too large")
    if status is not None and status not in {
        "in_progress",
        "completed",
        "incomplete",
    }:
        raise ValueError("provider_state reasoning status is invalid")


def _validate_sealed_provider_state(item: dict[str, Any]) -> None:
    allowed = {
        "type",
        "version",
        "provider",
        "kind",
        "nonce",
        "ciphertext",
    }
    unknown = set(item) - allowed
    if unknown:
        raise ValueError(
            "sealed provider_state contains unsupported field(s): "
            + ", ".join(sorted(str(key) for key in unknown))
        )
    if item.get("version") != 1:
        raise ValueError("sealed provider_state version is unsupported")
    for name in ("provider", "kind"):
        value = item.get(name)
        if (
            not isinstance(value, str)
            or not value
            or len(value) > 64
            or any(ord(char) < 33 or ord(char) > 126 for char in value)
        ):
            raise ValueError(f"sealed provider_state {name} is invalid")
    nonce = item.get("nonce")
    ciphertext = item.get("ciphertext")
    if not isinstance(nonce, str) or not nonce or len(nonce) > 64:
        raise ValueError("sealed provider_state nonce is invalid")
    if (
        not isinstance(ciphertext, str)
        or not ciphertext
        or len(ciphertext) > MAX_PROVIDER_STATE_BYTES
    ):
        raise ValueError("sealed provider_state ciphertext is invalid")
    try:
        decoded_nonce = base64.b64decode(nonce, validate=True)
        decoded_ciphertext = base64.b64decode(ciphertext, validate=True)
    except (ValueError, binascii.Error) as exc:
        raise ValueError("sealed provider_state encoding is invalid") from exc
    if len(decoded_nonce) != 12 or not decoded_ciphertext:
        raise ValueError("sealed provider_state encoding is invalid")


MessageInput: TypeAlias = CanonicalMessage | Mapping[str, Any]


def normalize_messages(messages: Sequence[MessageInput]) -> list[dict[str, Any]]:
    """Validate and serialize canonical messages before provider network I/O."""

    if len(messages) > MAX_CANONICAL_MESSAGES:
        raise ValueError(f"message count exceeds the limit of {MAX_CANONICAL_MESSAGES}")
    normalized: list[dict[str, Any]] = []
    for index, message in enumerate(messages):
        try:
            canonical = (
                message
                if isinstance(message, CanonicalMessage)
                else CanonicalMessage.model_validate(dict(message))
            )
        except (TypeError, ValueError) as exc:
            raise ValueError(
                f"invalid canonical message at index {index}: {exc}"
            ) from exc
        normalized.append(canonical.to_wire())
    return normalized
