"""Safe text projections for unattended automation diagnostics."""

from __future__ import annotations

import re
from urllib.parse import unquote, urlsplit

from ash.automation.models import AutomationJob
from ash.core.redaction import redact_text
from ash.ui.safe_text import terminal_safe_text

_TRUNCATION_MARKER = "…[truncated]"
_MAX_PERCENT_DECODE_ROUNDS = 4


def automation_error_excerpt(
    value: str | None,
    *,
    job: AutomationJob,
    max_characters: int | None = None,
) -> str | None:
    """Return a redacted, single-line error without the job prompt or URL."""

    if max_characters is not None and (
        type(max_characters) is not int or max_characters < 1
    ):
        raise ValueError("max_characters must be a positive integer")
    if not value:
        return None
    redacted = value
    for _ in range(_MAX_PERCENT_DECODE_ROUNDS):
        decoded = unquote(redacted)
        if decoded == redacted:
            break
        redacted = decoded
    redacted = redacted.replace("\\/", "/")
    redacted = _redact_prompt_material(redacted, job.prompt)
    if job.webhook_url:
        redacted = _redact_webhook_material(redacted, job.webhook_url)
    excerpt = terminal_safe_text(redact_text(redacted), single_line=True)
    if max_characters is None or len(excerpt) <= max_characters:
        return excerpt
    if max_characters <= len(_TRUNCATION_MARKER):
        return _TRUNCATION_MARKER[:max_characters]
    return (
        excerpt[: max_characters - len(_TRUNCATION_MARKER)].rstrip()
        + _TRUNCATION_MARKER
    )


def _redact_prompt_material(value: str, prompt: str) -> str:
    value = re.sub(re.escape(prompt), "[REDACTED]", value, flags=re.IGNORECASE)
    terms = {term.casefold() for term in re.findall(r"\w+", prompt)}
    if terms:
        value = re.sub(
            r"\w+",
            lambda match: (
                "[REDACTED]" if match.group().casefold() in terms else match.group()
            ),
            value,
            flags=re.UNICODE,
        )
    return value


def _redact_webhook_material(value: str, webhook_url: str) -> str:
    value = re.sub(re.escape(webhook_url), "[REDACTED]", value, flags=re.IGNORECASE)
    parsed = urlsplit(webhook_url)
    if parsed.netloc:
        target_pattern = re.compile(
            rf"(?<![\w.-])(?:https?://)?{re.escape(parsed.netloc)}[^\s<>\"'`]*",
            flags=re.IGNORECASE,
        )
        value = target_pattern.sub("[REDACTED]", value)
    for segment in parsed.path.split("/"):
        for _ in range(_MAX_PERCENT_DECODE_ROUNDS):
            decoded = unquote(segment)
            if decoded == segment:
                break
            segment = decoded
        if not segment:
            continue
        if len(segment) >= 4 or any(character.isdigit() for character in segment):
            pattern = re.escape(segment)
        else:
            # Keep short, common path words useful in diagnostics unless they
            # appear as a path component (for example, ``/abc``).
            pattern = rf"(?<![\w.-])/{re.escape(segment)}(?![\w.-])"
        value = re.sub(pattern, "[REDACTED]", value, flags=re.IGNORECASE)
    return value
