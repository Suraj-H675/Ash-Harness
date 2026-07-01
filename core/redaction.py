"""Conservative secret redaction for user-visible exports and diagnostics."""

from __future__ import annotations

import re
from typing import Any


_SECRET_PATTERNS = (
    re.compile(r"(?i)(api[_-]?key|token|secret|password)(\s*[=:]\s*)([^\s,;]+)"),
    re.compile(r"\b(sk-(?:ant-|proj-)?[A-Za-z0-9_-]{12,})\b"),
    re.compile(r"\b(gsk_[A-Za-z0-9_-]{12,})\b"),
    re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]{12,}"),
)


def redact_text(value: str) -> str:
    redacted = value
    redacted = _SECRET_PATTERNS[0].sub(r"\1\2[REDACTED]", redacted)
    for pattern in _SECRET_PATTERNS[1:]:
        redacted = pattern.sub("[REDACTED]", redacted)
    return redacted


class StreamingRedactor:
    """Redact complete tokens while retaining chunk-split secret candidates."""

    def __init__(self, *, max_token_characters: int = 8192) -> None:
        if max_token_characters < 256:
            raise ValueError("max_token_characters must be at least 256")
        self.max_token_characters = max_token_characters
        self._buffer = ""
        self._withholding_long_token = False

    def feed(self, value: str) -> str:
        if not value:
            return ""
        self._buffer += value
        if self._withholding_long_token:
            boundary = _last_whitespace_boundary(self._buffer)
            if boundary is None:
                self._buffer = self._buffer[-1:]
                return ""
            self._withholding_long_token = False
            self._buffer = self._buffer[boundary:]
            return ""

        boundary = _last_whitespace_boundary(self._buffer)
        if boundary is not None:
            complete = self._buffer[:boundary]
            self._buffer = self._buffer[boundary:]
            return redact_text(complete)
        if len(self._buffer) > self.max_token_characters:
            self._buffer = ""
            self._withholding_long_token = True
            return "[long unbroken output token withheld]"
        return ""

    def finish(self) -> str:
        if self._withholding_long_token:
            self._buffer = ""
            self._withholding_long_token = False
            return ""
        remaining = redact_text(self._buffer)
        self._buffer = ""
        return remaining


def _last_whitespace_boundary(value: str) -> int | None:
    for index in range(len(value) - 1, -1, -1):
        if value[index].isspace():
            return index + 1
    return None


def redact_value(value: Any) -> Any:
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, dict):
        return {
            key: "[REDACTED]"
            if any(
                term in str(key).casefold()
                for term in ("key", "token", "secret", "password")
            )
            else redact_value(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact_value(item) for item in value]
    return value
