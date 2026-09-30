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
