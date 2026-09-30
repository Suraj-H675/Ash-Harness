"""Redacted human logging plus opt-in rotating structured debug logs."""

from __future__ import annotations

import json
import math
import os
import stat
import sys
import threading
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
from typing import Any

from loguru import logger as _loguru_logger

from ash.core.redaction import redact_text, redact_value
from ash.safety.anchored_fs import AnchoredDirectory, AnchoredFilesystemError
from ash.ui.safe_text import terminal_safe_text


_LOG_FILENAME = "ash.jsonl"
_LOG_LOCK_FILENAME = ".ash-log.lock"
_LOG_MAX_BYTES = 2 * 1024 * 1024
_LOG_BACKUP_COUNT = 3
_LOG_RECORD_MAX_BYTES = 64 * 1024
_LOG_TEXT_MAX_CHARS = 8 * 1024
_LOG_COLLECTION_MAX_ITEMS = 64
_LOG_VALUE_MAX_DEPTH = 5

_logger: Any = None
_file_sink: _RotatingJsonlSink | None = None
_configuration_lock = threading.RLock()
_active_no_color = False
_active_debug = False
_active_log_directory: Path | None = None
_active_max_file_bytes = _LOG_MAX_BYTES
_active_backup_count = _LOG_BACKUP_COUNT
_log_context: ContextVar[dict[str, str]] = ContextVar(
    "ash_log_context",
    default={},
)


def _debug_enabled_from_environment() -> bool:
    return os.environ.get("ASH_DEBUG", "").strip().casefold() in {
        "1",
        "true",
        "yes",
        "on",
    }


def _bounded_log_text(value: object, *, limit: int = _LOG_TEXT_MAX_CHARS) -> str:
    rendered = terminal_safe_text(redact_text(str(value)), single_line=True)
    if len(rendered) <= limit:
        return rendered
    return rendered[:limit] + "… [truncated]"


def _json_safe_log_value(value: Any, *, depth: int = 0) -> Any:
    if depth >= _LOG_VALUE_MAX_DEPTH:
        return "[truncated]"
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else str(value)
    if isinstance(value, str):
        return _bounded_log_text(value)
    if isinstance(value, dict):
        items = list(value.items())[:_LOG_COLLECTION_MAX_ITEMS]
        dict_rendered: dict[str, Any] = {
            _bounded_log_text(key, limit=256): _json_safe_log_value(
                item,
                depth=depth + 1,
            )
            for key, item in items
        }
        if len(value) > len(items):
            dict_rendered["_truncated"] = True
        return dict_rendered
    if isinstance(value, (list, tuple)):
        items = list(value)[:_LOG_COLLECTION_MAX_ITEMS]
        list_rendered: list[Any] = [
            _json_safe_log_value(item, depth=depth + 1) for item in items
        ]
        if len(value) > len(items):
            list_rendered.append("[truncated]")
        return list_rendered
    return _bounded_log_text(value)


def _safe_log_extra(extra: dict[str, Any]) -> dict[str, Any]:
    try:
        redacted = redact_value(extra)
    except (RecursionError, TypeError, ValueError):
        return {"unavailable": "[unserializable log context]"}
    if not isinstance(redacted, dict):
        return {"unavailable": "[invalid log context]"}
    rendered = _json_safe_log_value(redacted)
    return rendered if isinstance(rendered, dict) else {}


def _patch_record(record: Any) -> None:
    record["message"] = _bounded_log_text(record.get("message", ""))
    extra = record.setdefault("extra", {})
    if isinstance(extra, dict):
        extra.update(_log_context.get())


def _structured_record(record: dict[str, Any]) -> bytes:
    extra = _safe_log_extra(dict(record.get("extra") or {}))
    component = str(extra.pop("name", record.get("name") or "ash"))
    session_id = extra.pop("session_id", None)
    turn_id = extra.pop("turn_id", None)
    operation_id = extra.pop("operation_id", None)
    exception = record.get("exception")
    exception_payload: dict[str, str] | None = None
    if exception is not None:
        exception_type = getattr(exception, "type", None)
        exception_value = getattr(exception, "value", None)
        exception_payload = {
            "type": _bounded_log_text(
                getattr(exception_type, "__name__", exception_type or "Exception"),
                limit=256,
            ),
            "message": _bounded_log_text(exception_value or "", limit=4096),
        }
    timestamp = record.get("time")
    isoformat = getattr(timestamp, "isoformat", None)
    timestamp_text = str(isoformat()) if callable(isoformat) else str(timestamp)
    payload: dict[str, Any] = {
        "schema_version": 1,
        "timestamp": timestamp_text,
        "level": str(record.get("level") or "INFO"),
        "component": _bounded_log_text(component, limit=512),
        "function": _bounded_log_text(record.get("function") or "", limit=512),
        "line": int(record.get("line") or 0),
        "message": _bounded_log_text(record.get("message") or ""),
        "session_id": session_id,
        "turn_id": turn_id,
        "operation_id": operation_id,
        "extra": extra,
    }
    if exception_payload is not None:
        payload["exception"] = exception_payload
    encoded = (
        json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")
    if len(encoded) <= _LOG_RECORD_MAX_BYTES:
        return encoded
    payload["message"] = _bounded_log_text(payload["message"], limit=4096)
    payload["extra"] = {"truncated": True}
    payload["record_truncated"] = True
    if "exception" in payload:
        payload["exception"] = {
            "type": payload["exception"]["type"],
            "message": _bounded_log_text(
                payload["exception"]["message"],
                limit=1024,
            ),
        }
    return (
        json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")


class _RotatingJsonlSink:
    """Small descriptor-anchored rotating JSONL sink for opt-in debug logging."""

    def __init__(self, directory: Path, *, max_bytes: int, backup_count: int) -> None:
        if max_bytes < _LOG_RECORD_MAX_BYTES:
            raise ValueError("structured log max_bytes is too small")
        if backup_count < 1:
            raise ValueError("structured log backup_count must be positive")
        self._directory = AnchoredDirectory.open(
            directory,
            create=True,
            private=True,
            pin_path=True,
        )
        self._max_bytes = max_bytes
        self._backup_count = backup_count
        self._disabled = False

    def __call__(self, message: Any) -> None:
        if self._disabled:
            return
        try:
            line = _structured_record(message.record)
            self._write(line)
        except (AnchoredFilesystemError, OSError, TypeError, ValueError):
            # Logging must never become an application availability dependency.
            self._disabled = True

    def _write(self, line: bytes) -> None:
        with self._directory.lock(_LOG_LOCK_FILENAME):
            metadata = self._directory.stat(_LOG_FILENAME)
            if metadata is not None and not stat.S_ISREG(metadata.st_mode):
                raise AnchoredFilesystemError("structured log path is not a regular file")
            if metadata is not None and metadata.st_size + len(line) > self._max_bytes:
                self._rotate(metadata)
                metadata = None
            if metadata is None:
                descriptor = self._directory.create_file(_LOG_FILENAME, mode=0o600)
            else:
                descriptor = self._directory.open_file(
                    _LOG_FILENAME,
                    os.O_WRONLY | os.O_APPEND,
                    expected=metadata,
                    expected_type=stat.S_IFREG,
                )
                os.fchmod(descriptor, 0o600)
            try:
                view = memoryview(line)
                while view:
                    written = os.write(descriptor, view)
                    if written <= 0:
                        raise OSError("short write while writing structured log")
                    view = view[written:]
            finally:
                os.close(descriptor)

    def _rotate(self, current: os.stat_result) -> None:
        oldest = f"{_LOG_FILENAME}.{self._backup_count}"
        oldest_metadata = self._directory.stat(oldest)
        if oldest_metadata is not None:
            if not stat.S_ISREG(oldest_metadata.st_mode):
                raise AnchoredFilesystemError("structured log backup is not regular")
            self._directory.unlink(oldest, expected=oldest_metadata)
        for index in range(self._backup_count - 1, 0, -1):
            source = f"{_LOG_FILENAME}.{index}"
            metadata = self._directory.stat(source)
            if metadata is None:
                continue
            if not stat.S_ISREG(metadata.st_mode):
                raise AnchoredFilesystemError("structured log backup is not regular")
            self._directory.rename(
                source,
                f"{_LOG_FILENAME}.{index + 1}",
                expected_source=metadata,
            )
        self._directory.rename(
            _LOG_FILENAME,
            f"{_LOG_FILENAME}.1",
            expected_source=current,
        )

    def close(self) -> None:
        self._directory.close()


def _write_stderr(message: Any) -> None:
    """Write through the current stderr instead of retaining a replaced stream."""

    sys.stderr.write(str(message))
    sys.stderr.flush()


def _apply_configuration(*, level_override: str | None = None) -> Any:
    global _file_sink, _logger
    if _file_sink is not None:
        _file_sink.close()
        _file_sink = None
    _loguru_logger.remove()
    _loguru_logger.configure(patcher=_patch_record)
    stderr_level = level_override or ("DEBUG" if _active_debug else "INFO")
    _loguru_logger.add(
        _write_stderr,
        format=(
            "<level>{time:YYYY-MM-DD HH:mm:ss}</level> | "
            "<level>{level: <8}</level> | "
            "<level>{name}</level>:<level>{function}</level> — "
            "<level>{message}</level>"
        ),
        level=stderr_level,
        colorize=not _active_no_color,
    )
    if _active_debug:
        directory = _active_log_directory or (Path.home() / ".ash" / "logs")
        try:
            sink = _RotatingJsonlSink(
                directory,
                max_bytes=_active_max_file_bytes,
                backup_count=_active_backup_count,
            )
        except (AnchoredFilesystemError, OSError, ValueError):
            sink = None
        if sink is not None:
            _file_sink = sink
            _loguru_logger.add(
                sink,
                format="{message}",
                level=level_override or "DEBUG",
                colorize=False,
                catch=True,
            )
    _logger = _loguru_logger
    return _logger


def _configure(
    *,
    no_color: bool = False,
    debug: bool | None = None,
    log_directory: Path | None = None,
    max_file_bytes: int = _LOG_MAX_BYTES,
    backup_count: int = _LOG_BACKUP_COUNT,
) -> Any:
    global _active_backup_count, _active_debug, _active_log_directory
    global _active_max_file_bytes, _active_no_color
    with _configuration_lock:
        _active_no_color = no_color
        _active_debug = _debug_enabled_from_environment() if debug is None else bool(debug)
        _active_log_directory = log_directory
        _active_max_file_bytes = max_file_bytes
        _active_backup_count = backup_count
        return _apply_configuration()


def configure_logging(
    *,
    no_color: bool,
    debug: bool | None = None,
    log_directory: Path | None = None,
    max_file_bytes: int = _LOG_MAX_BYTES,
    backup_count: int = _LOG_BACKUP_COUNT,
) -> Any:
    """Apply Ash's human and optional structured-debug logging policy."""

    return _configure(
        no_color=no_color,
        debug=debug,
        log_directory=log_directory,
        max_file_bytes=max_file_bytes,
        backup_count=backup_count,
    )


def get_logger(name: str) -> Any:
    """Return a logger scoped to ``name`` (typically a module name)."""

    global _logger
    if _logger is None:
        _logger = _configure()
    return _logger.bind(name=name)


def current_log_context() -> dict[str, str]:
    """Return a copy of the active correlation context."""

    return dict(_log_context.get())


def replace_log_context(context: dict[str, str]) -> None:
    """Replace the active correlation context with a trusted runtime snapshot."""

    _log_context.set({str(key): str(value) for key, value in context.items() if value})


def set_log_context(**values: str | None) -> None:
    """Merge session/turn/operation identifiers into the current log context."""

    context = current_log_context()
    for key, value in values.items():
        if value is None:
            context.pop(key, None)
        else:
            context[key] = str(value)
    _log_context.set(context)


@contextmanager
def log_context(**values: str | None):
    """Temporarily add correlation identifiers to logs in this context/task."""

    previous = current_log_context()
    set_log_context(**values)
    try:
        yield
    finally:
        replace_log_context(previous)


@contextmanager
def temporary_level(level: str) -> Any:
    """Temporarily rebuild Ash's sinks at a different minimum log level."""

    global _logger
    if _logger is None:
        get_logger("ash")
    with _configuration_lock:
        _apply_configuration(level_override=level)
    try:
        yield _logger
    finally:
        with _configuration_lock:
            _apply_configuration()
