"""Conservative secret redaction for user-visible exports and diagnostics."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any


_SECRET_VALUE_ASSIGNMENT = re.compile(
    r"""(?ix)
    (?P<field_quote>(?:\\?["'])?)
    (?P<field>(?:[a-z0-9]+[_-])*(?:api[_-]?key|access[_-]?token|auth[_-]?token|secret|password|token))\b
    (?P=field_quote)
    (?P<separator>\s*[=:]\s*)
    (?:
        (?P<escaped_double>\\"(?:\\.|[^"\\])*?(?<!\\)\\")
        |
        (?P<double>"(?:\\.|[^"\\])*")
        |
        (?P<single>'(?:\\.|[^'\\])*')
        |
        (?P<unquoted>(?!\\?["'])[^\s,;"']+)
    )
    """
)
_UNTERMINATED_SECRET_VALUE_ASSIGNMENT = re.compile(
    r"""(?ix)
    (?P<field_quote>(?:\\?["'])?)
    (?P<field>(?:[a-z0-9]+[_-])*(?:api[_-]?key|access[_-]?token|auth[_-]?token|secret|password|token))\b
    (?P=field_quote)
    (?P<separator>\s*[=:]\s*)
    (?:
        (?P<escaped_double>\\"(?:\\.|[^"\\])*)
        |
        (?P<double>"(?:\\.|[^"\\])*)
        |
        (?P<single>'(?:\\.|[^'\\])*)
    )$
    """
)
_SENSITIVE_HEADER_VALUE = re.compile(
    r"""(?ixm)
    (?P<prefix>
        (?<![a-z0-9_-])
        (?P<field_quote>[\"']?)
        (?P<field>authorization|proxy-authorization|cookie|set-cookie)
        (?P=field_quote)
        [ \t]*:[ \t]*
    )
    (?:(?P<value_quote>[\"'])(?P<quoted_value>[^\"\r\n]*)(?P=value_quote)|(?P<unquoted_value>[^\r\n]*))
    """
)
_SECRET_PATTERNS = (
    _SECRET_VALUE_ASSIGNMENT,
    re.compile(r"\b(sk-(?:ant-|proj-)?[A-Za-z0-9_-]{12,})\b"),
    re.compile(r"\b(gsk_[A-Za-z0-9_-]{12,})\b"),
    re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]{12,}"),
)
_SECRET_CANDIDATE_PATTERNS = (
    (
        "private key",
        re.compile(r"-----BEGIN (?:[A-Z0-9 ]+ )?PRIVATE KEY-----"),
    ),
    ("AWS access key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    (
        "GitHub token",
        re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{40,})\b"),
    ),
    ("Slack token", re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{20,}\b")),
    ("Stripe live key", re.compile(r"\bsk_live_[A-Za-z0-9]{16,}\b")),
    (
        "provider API key",
        re.compile(
            r"\b(?:sk-(?:ant-|proj-)?[A-Za-z0-9_-]{12,}|gsk_[A-Za-z0-9_-]{12,})\b"
        ),
    ),
)
_SECRET_ASSIGNMENT = re.compile(
    r"""(?ix)
    \b(?:[a-z0-9]+[_-])*
    (api[_-]?key|access[_-]?token|auth[_-]?token|secret|password)\b
    \s*[=:]\s*
    (?:
        "([^"\r\n]{8,})"
        |
        '([^'\r\n]{8,})'
        |
        ([A-Za-z0-9_./+=-]{12,})
    )
    """
)
_PLACEHOLDER_TERMS = (
    "changeme",
    "dummy",
    "example",
    "placeholder",
    "redacted",
    "sample",
    "test",
    "your_",
    "your-",
)


@dataclass(frozen=True)
class SecretFinding:
    kind: str
    line_number: int


def redact_text(value: str) -> str:
    redacted = _SENSITIVE_HEADER_VALUE.sub(_redact_sensitive_header, value)
    redacted = _UNTERMINATED_SECRET_VALUE_ASSIGNMENT.sub(
        lambda match: _redact_unterminated_if_incomplete(match, value),
        redacted,
    )
    redacted = _SECRET_PATTERNS[0].sub(
        _redact_secret_assignment,
        redacted,
    )
    for pattern in _SECRET_PATTERNS[1:]:
        redacted = pattern.sub("[REDACTED]", redacted)
    return redacted


def _redact_sensitive_header(match: re.Match[str]) -> str:
    field = match.group("field").casefold()
    raw_value = match.group("quoted_value")
    if raw_value is None:
        raw_value = match.group("unquoted_value") or ""

    if field in {"authorization", "proxy-authorization"}:
        scheme_match = re.match(r"(?is)(basic|bearer)(?:[ \t]+|$)", raw_value)
        replacement = (
            f"{scheme_match.group(1)} [REDACTED]"
            if scheme_match is not None
            else "[REDACTED]"
        )
    else:
        replacement = "[REDACTED]"

    quote = match.group("value_quote") or ""
    return f"{match.group('prefix')}{quote}{replacement}{quote}"


def _redact_unterminated_if_incomplete(
    match: re.Match[str],
    source: str,
) -> str:
    if _SECRET_VALUE_ASSIGNMENT.match(source, match.start()) is not None:
        return match.group(0)
    return _redact_unterminated_secret_assignment(match)


def _redact_secret_assignment(match: re.Match[str]) -> str:
    value = (
        match.group("escaped_double")
        or match.group("double")
        or match.group("single")
        or match.group("unquoted")
        or ""
    )
    field_quote = match.group("field_quote")
    if field_quote.startswith("\\"):
        return (
            f"{field_quote}{match.group('field')}{field_quote}"
            f"{match.group('separator')}"
            r'\"[REDACTED]\"'
        )
    if value.startswith('\\"') and value.endswith('\\"'):
        return (
            f"{match.group('field_quote')}{match.group('field')}"
            f"{match.group('field_quote')}{match.group('separator')}"
            r'\"[REDACTED]\"'
        )
    quote = value[0] if value[:1] in {'"', "'"} else ""
    closing_quote = quote if quote else ""
    return (
        f"{match.group('field_quote')}{match.group('field')}"
        f"{match.group('field_quote')}{match.group('separator')}"
        f"{quote}[REDACTED]{closing_quote}"
    )


def _redact_unterminated_secret_assignment(match: re.Match[str]) -> str:
    value = (
        match.group("escaped_double")
        or match.group("double")
        or match.group("single")
        or ""
    )
    quote = value[:1]
    field_quote = match.group("field_quote")
    if value.startswith("\\"):
        return (
            f"{field_quote}{match.group('field')}{field_quote}"
            f"{match.group('separator')}"
            r'\"[REDACTED]'
        )
    return (
        f"{field_quote}{match.group('field')}"
        f"{field_quote}{match.group('separator')}"
        f"{quote}[REDACTED]"
    )


def find_secret_candidates(value: str) -> tuple[SecretFinding, ...]:
    """Return high-confidence secret candidates without exposing their values."""

    findings: list[SecretFinding] = []
    seen: set[tuple[str, int]] = set()
    for line_number, line in enumerate(value.splitlines(), start=1):
        for kind, pattern in _SECRET_CANDIDATE_PATTERNS:
            if pattern.search(line):
                key = (kind, line_number)
                if key not in seen:
                    findings.append(SecretFinding(kind, line_number))
                    seen.add(key)
        for match in _SECRET_ASSIGNMENT.finditer(line):
            candidate = next(
                (group for group in match.groups()[1:] if group is not None),
                "",
            )
            if _looks_like_placeholder(candidate):
                continue
            key = ("secret assignment", line_number)
            if key not in seen:
                findings.append(SecretFinding(*key))
                seen.add(key)
    return tuple(findings)


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

        incomplete_start = _incomplete_secret_assignment_start(self._buffer)
        header_start = _incomplete_sensitive_header_start(self._buffer)
        if header_start is not None:
            incomplete_start = (
                header_start
                if incomplete_start is None
                else min(incomplete_start, header_start)
            )
        if incomplete_start is not None:
            if incomplete_start:
                complete = self._buffer[:incomplete_start]
                self._buffer = self._buffer[incomplete_start:]
                return redact_text(complete)
            if len(self._buffer) > self.max_token_characters:
                self._buffer = ""
                self._withholding_long_token = True
                return "[long unbroken output token withheld]"
            return ""

        boundary = _last_whitespace_boundary(self._buffer)
        if boundary is not None:
            boundary = _boundary_before_quoted_secret(self._buffer, boundary)
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


_SECRET_ASSIGNMENT_START = re.compile(
    r"""(?ix)
    (?P<field_quote>(?:\\?["'])?)
    (?:[a-z0-9]+[_-])*(?:api[_-]?key|access[_-]?token|auth[_-]?token|secret|password|token)\b
    (?P=field_quote)
    \s*[=:]\s*
    (?P<value_escape>\\?)(?P<value_quote>["'])
    """
)
_SECRET_ASSIGNMENT_PREFIX = re.compile(
    r"""(?ix)
    (?P<field_quote>(?:\\?["'])?)
    (?:[a-z0-9]+[_-])*(?:api[_-]?key|access[_-]?token|auth[_-]?token|secret|password|token)\b
    (?P=field_quote)
    (?:\s*[=:]\s*\\?)?
    $
    """
)
_SECRET_FIELD_PREFIX = re.compile(
    r"""(?ix)
    (?P<field_quote>\\?["']?)
    (?P<field>[a-z0-9_-]+)
    $
    """
)
_SECRET_FIELD_COMPONENTS = (
    "apikey",
    "access",
    "authtoken",
    "secret",
    "password",
    "token",
)

_SENSITIVE_HEADER_PREFIX = re.compile(
    r"""(?ixm)
    (?<![a-z0-9_-])
    [\"']?(?:authorization|proxy-authorization|cookie|set-cookie)[\"']?
    [ \t]*:[ \t]*
    """
)


def _incomplete_secret_assignment_start(value: str) -> int | None:
    starts: list[int] = []
    for match in _SECRET_ASSIGNMENT_START.finditer(value):
        if _quoted_secret_end(value, match) is None:
            starts.append(match.start())
    prefix = _SECRET_ASSIGNMENT_PREFIX.search(value)
    if prefix is not None:
        starts.append(prefix.start())
    field_prefix = _SECRET_FIELD_PREFIX.search(value)
    if field_prefix is not None:
        field = field_prefix.group("field").replace("_", "").replace("-", "")
        if len(field) >= 3 and any(
            component.startswith(field) or field.endswith(component)
            for component in _SECRET_FIELD_COMPONENTS
        ):
            starts.append(field_prefix.start())
    return min(starts) if starts else None


def _incomplete_sensitive_header_start(value: str) -> int | None:
    """Withhold a sensitive header until its line is complete."""

    starts: list[int] = []
    for match in _SENSITIVE_HEADER_PREFIX.finditer(value):
        remainder = value[match.end() :]
        if "\n" not in remainder and "\r" not in remainder:
            starts.append(match.start())
    return min(starts) if starts else None


def _boundary_before_quoted_secret(value: str, boundary: int) -> int | None:
    for match in _SECRET_ASSIGNMENT_START.finditer(value):
        end = _quoted_secret_end(value, match)
        if end is not None and match.start() < boundary <= end:
            return _last_whitespace_boundary(value[: match.start()])
    return boundary


def _quoted_secret_end(value: str, match: re.Match[str]) -> int | None:
    quote = match.group("value_quote")
    if match.group("value_escape"):
        for offset in range(match.end(), len(value)):
            if value[offset] != quote:
                continue
            slash_count = 0
            preceding = offset - 1
            while preceding >= match.end() - 1 and value[preceding] == "\\":
                slash_count += 1
                preceding -= 1
            if slash_count == 1:
                return offset + 1
        return None
    escaped = False
    for offset, character in enumerate(value[match.end() :], start=match.end()):
        if escaped:
            escaped = False
        elif character == "\\":
            escaped = True
        elif character == quote:
            return offset
    return None


def _looks_like_placeholder(value: str) -> bool:
    normalized = value.strip().casefold()
    if not normalized:
        return True
    if any(term in normalized for term in _PLACEHOLDER_TERMS):
        return True
    if normalized.startswith(("${", "$env", "env.", "os.environ", "process.env")):
        return True
    return value.isidentifier() and value.upper() == value


def redact_value(value: Any) -> Any:
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, dict):
        return {
            key: "[REDACTED]"
            if _is_secret_field(key, item)
            else redact_value(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact_value(item) for item in value]
    return value


_NON_SECRET_USAGE_FIELDS = frozenset(
    {
        "prompt_tokens",
        "completion_tokens",
        "cache_read_tokens",
        "cache_write_tokens",
        "estimated_prompt_tokens",
        "estimated_completion_tokens",
        "total_tokens",
        "total_prompt_tokens",
        "total_completion_tokens",
        "max_tokens",
        "max_context_tokens",
        "max_completion_tokens",
        "max_turn_total_tokens",
        "max_tool_result_tokens",
        "max_attachment_tokens",
        "agent_token_budget",
    }
)
_USAGE_TOKEN_FIELD = re.compile(
    r"^(?:(?:prompt|completion|input|output|total|estimated|cached|cache|"
    r"context|max|agent|reasoning|turn|attachment|tool|used|added|before|after|"
    r"current|remaining|reserved|original|minimum|candidate|uncached|graph|"
    r"record|marker|usage)(?:_[a-z0-9]+)*)_"
    r"(?:tokens?|token_count|token_budget|token_limit|token_total|token_used|"
    r"tokens_used)$|^token_count$"
)
_ADDITIONAL_SECRET_FIELD_COMPONENTS = frozenset(
    {
        "authorization",
        "cookie",
        "cookies",
    }
)


def _normalize_field_name(key: object) -> str:
    raw = str(key)
    raw = re.sub(r"(?<=[A-Z])(?=[A-Z][a-z])", "_", raw)
    raw = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", raw)
    return re.sub(r"[^a-z0-9]+", "_", raw.casefold()).strip("_")


def _is_secret_field(key: object, value: Any) -> bool:
    normalized = _normalize_field_name(key)
    is_numeric_usage = (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and (
            normalized in _NON_SECRET_USAGE_FIELDS
            or _USAGE_TOKEN_FIELD.fullmatch(normalized) is not None
        )
    )
    if is_numeric_usage:
        return False

    # Preserve the pre-existing fail-closed structured-field behavior for
    # key/token/secret/password names.  The usage exemption above is deliberately
    # narrow so machine accounting stays typed without opening credential fields.
    if any(
        term in str(key).casefold()
        for term in ("key", "token", "secret", "password")
    ):
        return True

    components = normalized.split("_")
    if any(
        component in _ADDITIONAL_SECRET_FIELD_COMPONENTS
        for component in components
    ):
        return True
    return normalized in {"credential", "credentials"}
