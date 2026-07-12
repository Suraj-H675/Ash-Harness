"""Custom exceptions and user-facing error classification for Ash."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any


class ErrorCategory(str, Enum):
    """Stable top-level categories exposed to CLI and automation callers."""

    CONFIG = "config"
    PROVIDER = "provider"
    TOOL = "tool"
    POLICY = "policy"
    SANDBOX = "sandbox"
    CONTEXT = "context"
    STORAGE = "storage"
    SESSION = "session"
    OUTPUT = "output"
    IO = "io"
    INTERNAL = "internal"


@dataclass(frozen=True)
class ErrorInfo:
    """Structured error payload safe to show to users and scripts."""

    category: ErrorCategory
    message: str
    remedy: str
    exit_code: int = 1
    retriable: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "category": self.category.value,
            "message": self.message,
            "remedy": self.remedy,
            "exit_code": self.exit_code,
            "retriable": self.retriable,
        }


class AshError(Exception):
    """Base exception for Ash errors with optional structured metadata."""

    category = ErrorCategory.INTERNAL
    remedy = "Retry with --debug for more detail, or report this as an Ash bug."
    exit_code = 1
    retriable = False

    def __init__(
        self,
        message: str,
        *,
        category: ErrorCategory | None = None,
        remedy: str | None = None,
        exit_code: int | None = None,
        retriable: bool | None = None,
    ) -> None:
        super().__init__(message)
        if category is not None:
            self.category = category
        if remedy is not None:
            self.remedy = remedy
        if exit_code is not None:
            self.exit_code = exit_code
        if retriable is not None:
            self.retriable = retriable


class AshConfigError(AshError):
    """Raised when Ash configuration cannot be loaded or validated."""

    category = ErrorCategory.CONFIG
    remedy = "Run `ash setup` or edit ~/.ash/ash.toml, then retry."
    exit_code = 2


def classify_exception(exc: BaseException) -> ErrorInfo:
    """Classify arbitrary runtime exceptions into stable Ash error groups."""

    if isinstance(exc, AshError):
        return ErrorInfo(
            category=exc.category,
            message=_message(exc),
            remedy=exc.remedy,
            exit_code=exc.exit_code,
            retriable=exc.retriable,
        )

    module = type(exc).__module__
    name = type(exc).__name__
    message = _message(exc)
    lowered = message.casefold()

    if _is_pydantic_validation_error(exc) or module == "ash.config":
        return ErrorInfo(
            ErrorCategory.CONFIG,
            message,
            "Run `ash doctor`, then fix the reported config value or rerun `ash setup`.",
            exit_code=2,
        )

    if name == "SessionStorageError" or "database" in lowered or "sqlite" in lowered:
        return ErrorInfo(
            ErrorCategory.STORAGE,
            message,
            "Run `ash storage check`; if needed, create a backup and restore a known-good sessions database.",
            retriable=False,
        )

    if isinstance(exc, KeyError) and "session" in lowered:
        return ErrorInfo(
            ErrorCategory.SESSION,
            message.strip("'"),
            "List sessions with `/sessions` or start a new session without --session.",
        )

    if name in {"SafetyViolation", "PermissionDeniedError"} or module.startswith(
        "ash.safety."
    ):
        return ErrorInfo(
            ErrorCategory.POLICY,
            message,
            "Review the requested path, command, or permission mode before retrying.",
            exit_code=2,
        )

    if name == "SandboxBackendUnavailable" or module.startswith("ash.sandbox."):
        return ErrorInfo(
            ErrorCategory.SANDBOX,
            message,
            "Install an available sandbox backend, choose a less privileged safety mode, or configure Ash for this host.",
            retriable=True,
        )

    if _is_provider_error(module, lowered):
        return ErrorInfo(
            ErrorCategory.PROVIDER,
            message,
            "Check the selected model, API key, base URL, local provider process, and network connectivity.",
            retriable=_looks_transient(lowered),
        )

    if module.startswith("ash.tools.") or name.endswith(
        ("ToolError", "SkillParseError")
    ):
        return ErrorInfo(
            ErrorCategory.TOOL,
            message,
            "Inspect the tool arguments and workspace state, then retry the command.",
        )

    if module.startswith("ash.context.") or "context" in lowered or "token" in lowered:
        return ErrorInfo(
            ErrorCategory.CONTEXT,
            message,
            "Reduce the prompt, attach fewer files, compact the session, or switch to a larger-context model.",
        )

    if name in {"PlannerError", "SprintTransitionError"}:
        return ErrorInfo(
            ErrorCategory.CONTEXT,
            message,
            "Review the generated plan or sprint state, then retry the request.",
        )

    if "json schema" in lowered or "structured output" in lowered:
        return ErrorInfo(
            ErrorCategory.OUTPUT,
            message,
            "Fix the JSON schema or ask the model to regenerate output that matches it.",
        )

    if isinstance(exc, (OSError, PermissionError)):
        return ErrorInfo(
            ErrorCategory.IO,
            message,
            "Check filesystem permissions, disk space, and the path referenced in the error.",
            retriable=isinstance(exc, TimeoutError),
        )

    return ErrorInfo(
        ErrorCategory.INTERNAL,
        message,
        "Retry with --debug for more detail, or report this as an Ash bug.",
    )


def format_error(info: ErrorInfo) -> str:
    """Render a concise human-readable error with an actionable next step."""

    return f"Error [{info.category.value}]: {info.message}\nRemedy: {info.remedy}"


def _message(exc: BaseException) -> str:
    message = str(exc).strip()
    return message or type(exc).__name__


def _is_pydantic_validation_error(exc: BaseException) -> bool:
    try:
        from pydantic import ValidationError
    except Exception:  # pragma: no cover - pydantic is a runtime dependency
        return False
    return isinstance(exc, ValidationError)


def _is_provider_error(module: str, lowered_message: str) -> bool:
    if module.startswith("ash.providers."):
        return True
    provider_markers = (
        "api key",
        "api error",
        "connection error",
        "rate limit",
        "ollama",
        "openai",
        "anthropic",
        "deepseek",
        "groq",
        "configured providers failed",
    )
    return any(marker in lowered_message for marker in provider_markers)


def _looks_transient(lowered_message: str) -> bool:
    transient_markers = (
        "timeout",
        "temporarily",
        "connection",
        "rate limit",
        "429",
        "500",
        "502",
        "503",
        "504",
    )
    return any(marker in lowered_message for marker in transient_markers)
