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


def redact_value(value: Any) -> Any:
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, dict):
        return {
            key: "[REDACTED]"
            if any(term in str(key).casefold() for term in ("key", "token", "secret", "password"))
            else redact_value(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact_value(item) for item in value]
    return value
