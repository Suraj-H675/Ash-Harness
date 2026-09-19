"""Bounded, redacted diagnostics at MCP user-facing boundaries."""

from __future__ import annotations

from collections.abc import Mapping
import json

from ash.core.redaction import redact_text, redact_value
from ash.ui.safe_text import terminal_safe_text


MAX_MCP_DIAGNOSTIC_CHARS = 512
MAX_MCP_DIAGNOSTICS = 16


def safe_mcp_diagnostic(
    value: object,
    *,
    max_chars: int = MAX_MCP_DIAGNOSTIC_CHARS,
) -> str:
    """Render one MCP diagnostic without exposing secrets or unbounded text."""

    if max_chars < 1:
        raise ValueError("max_chars must be positive")
    rendered = redact_text(str(value)).strip() or type(value).__name__
    rendered = terminal_safe_text(rendered, single_line=True)
    if len(rendered) > max_chars:
        return rendered[: max_chars - 3] + "..."
    return rendered


def safe_mcp_error_map(
    errors: Mapping[object, object],
    *,
    max_errors: int = MAX_MCP_DIAGNOSTICS,
) -> dict[str, str]:
    """Render a bounded error mapping for logs, summaries, or CLI output."""

    if max_errors < 1:
        raise ValueError("max_errors must be positive")
    return {
        safe_mcp_diagnostic(name): safe_mcp_diagnostic(error)
        for name, error in sorted(errors.items(), key=lambda item: str(item[0]))[
            :max_errors
        ]
    }


def safe_mcp_error_payload(value: object) -> object:
    """Redact and bound structured data attached to an MCP error."""

    sanitized = redact_value(value)
    try:
        encoded = json.dumps(
            sanitized,
            ensure_ascii=False,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError):
        return safe_mcp_diagnostic(sanitized)
    if len(encoded) > MAX_MCP_DIAGNOSTIC_CHARS:
        return "[diagnostic truncated]"
    return sanitized
