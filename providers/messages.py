"""Provider-portable, validated chat message contracts."""

from __future__ import annotations

import base64
import binascii
import json
from collections.abc import Mapping, Sequence
from typing import Annotated, Any, Literal, TypeAlias

from pydantic import BaseModel, ConfigDict, Field, model_validator


MAX_CANONICAL_MESSAGES = 10_000
MAX_IMAGE_BASE64_CHARS = 14_000_000
SUPPORTED_IMAGE_MEDIA_TYPES = frozenset(
    {"image/png", "image/jpeg", "image/gif", "image/webp"}
)


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

    call_id: str = Field(..., min_length=1)
    name: str = Field(..., min_length=1)
    arguments: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_arguments(self) -> "CanonicalToolCall":
        try:
            json.dumps(self.arguments, allow_nan=False)
        except (TypeError, ValueError) as exc:
            raise ValueError("tool-call arguments must be JSON serializable") from exc
        return self


class CanonicalMessage(BaseModel):
    """One provider-neutral message accepted by every Ash adapter."""

    model_config = ConfigDict(extra="forbid")

    role: Literal["system", "user", "assistant", "tool"]
    content: str | list[ContentBlock] = ""
    name: str | None = None
    tool_call_id: str | None = None
    tool_calls: list[CanonicalToolCall] | None = None

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
        raise ValueError(
            f"message count exceeds the limit of {MAX_CANONICAL_MESSAGES}"
        )
    normalized: list[dict[str, Any]] = []
    for index, message in enumerate(messages):
        try:
            canonical = (
                message
                if isinstance(message, CanonicalMessage)
                else CanonicalMessage.model_validate(dict(message))
            )
        except (TypeError, ValueError) as exc:
            raise ValueError(f"invalid canonical message at index {index}: {exc}") from exc
        normalized.append(canonical.to_wire())
    return normalized
