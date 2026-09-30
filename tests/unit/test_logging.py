from __future__ import annotations

import io
import json
import sys
from pathlib import Path

import pytest

from ash.config import AshConfig
from ash.core.loop import AshLoop
from ash.core.session import SessionStore
from ash.logging import (
    configure_logging,
    get_logger,
    log_context,
    temporary_level,
)
from ash.providers.base import ProviderABC, StreamChunk
from ash.providers.capabilities import ProviderCapabilities
from ash.safety.guard import SafetyGuard
from ash.tools.base import BaseTool, ToolResult
from ash.ui.terminal import TerminalUI


def _reset_logging() -> None:
    configure_logging(no_color=True, debug=False)


def test_structured_debug_log_redacts_controls_and_correlation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured = io.StringIO()
    monkeypatch.setattr(sys, "stderr", captured)
    log_directory = tmp_path / "logs"
    secret = "sk-proj-" + "A" * 32
    try:
        configure_logging(
            no_color=True,
            debug=True,
            log_directory=log_directory,
        )
        with log_context(
            session_id="session-1",
            turn_id="turn-1",
            operation_id="call-1",
        ):
            get_logger("test.logging").warning(
                "token={} control=\x1b[2J",
                secret,
            )

        payload = json.loads(
            (log_directory / "ash.jsonl").read_text(encoding="utf-8").splitlines()[-1]
        )
        assert secret not in captured.getvalue()
        assert secret not in json.dumps(payload)
        assert "\x1b[2J" not in captured.getvalue()
        assert "\\x1b[2J" in captured.getvalue()
        assert payload["session_id"] == "session-1"
        assert payload["turn_id"] == "turn-1"
        assert payload["operation_id"] == "call-1"
        assert payload["component"] == "test.logging"
        assert payload["schema_version"] == 1
        assert oct((log_directory / "ash.jsonl").stat().st_mode & 0o777) == "0o600"
    finally:
        _reset_logging()


def test_structured_debug_log_rotates_with_bounded_retention(tmp_path: Path) -> None:
    log_directory = tmp_path / "logs"
    try:
        configure_logging(
            no_color=True,
            debug=True,
            log_directory=log_directory,
            max_file_bytes=64 * 1024,
            backup_count=2,
        )
        logger = get_logger("rotation-test")
        for index in range(90):
            logger.warning("record {} {}", index, "x" * 900)

        assert (log_directory / "ash.jsonl").is_file()
        assert (log_directory / "ash.jsonl.1").is_file()
        assert not (log_directory / "ash.jsonl.3").exists()
    finally:
        _reset_logging()


def test_structured_debug_log_is_strict_json_for_non_finite_extras(
    tmp_path: Path,
) -> None:
    log_directory = tmp_path / "logs"
    try:
        configure_logging(
            no_color=True,
            debug=True,
            log_directory=log_directory,
        )
        get_logger("strict-json").bind(
            nan_value=float("nan"),
            positive_infinity=float("inf"),
            negative_infinity=float("-inf"),
        ).warning("non-finite metrics")

        raw = (log_directory / "ash.jsonl").read_text(encoding="utf-8")

        def reject_constant(value: str):
            raise ValueError(f"non-standard JSON constant: {value}")

        payload = json.loads(raw, parse_constant=reject_constant)
        assert payload["extra"] == {
            "nan_value": "nan",
            "negative_infinity": "-inf",
            "positive_infinity": "inf",
        }
    finally:
        _reset_logging()


def test_structured_debug_log_refuses_symlinked_log_directory(
    tmp_path: Path,
) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    log_directory = tmp_path / "logs"
    try:
        log_directory.symlink_to(outside, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"symlinks are unavailable: {exc}")
    try:
        configure_logging(
            no_color=True,
            debug=True,
            log_directory=log_directory,
        )
        get_logger("symlink-test").warning("must not escape")
        assert list(outside.iterdir()) == []
    finally:
        _reset_logging()


def test_debug_disabled_does_not_create_structured_log(tmp_path: Path) -> None:
    log_directory = tmp_path / "logs"
    try:
        configure_logging(
            no_color=True,
            debug=False,
            log_directory=log_directory,
        )
        get_logger("disabled-test").warning("stderr only")
        assert not log_directory.exists()
    finally:
        _reset_logging()


def test_temporary_level_enables_debug_and_restores_threshold(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured = io.StringIO()
    monkeypatch.setattr(sys, "stderr", captured)
    try:
        configure_logging(no_color=True, debug=False)
        logger = get_logger("temporary-level")
        logger.debug("before")
        with temporary_level("DEBUG"):
            logger.debug("inside")
        logger.debug("after")

        rendered = captured.getvalue()
        assert "before" not in rendered
        assert "inside" in rendered
        assert "after" not in rendered
    finally:
        _reset_logging()


@pytest.mark.asyncio
async def test_ash_loop_structured_logs_carry_actual_turn_context(
    tmp_path: Path,
) -> None:
    class PlainProvider(ProviderABC):
        model_name = "logging-integration"
        _ash_declared_capabilities = ProviderCapabilities(native_tools=True)

        def count_tokens(self, text):
            return len(str(text))

        async def stream_chat(self, messages, temperature=0.0, tools=None):
            del messages, temperature, tools
            yield StreamChunk(content="done", is_done=True, stop_reason="stop")

    log_directory = tmp_path / "logs"
    config = AshConfig(
        workspace_root=tmp_path,
        db_directory=tmp_path / "db",
        memory_backend="off",
        repo_map_enabled=False,
    )
    loop = AshLoop(
        SessionStore(config.db_directory / "sessions.db"),
        PlainProvider(),
        SafetyGuard(tmp_path),
        TerminalUI(safety_tier="auto_approve"),
        tmp_path,
        config=config,
    )
    try:
        configure_logging(
            no_color=True,
            debug=True,
            log_directory=log_directory,
        )
        assert await loop.run_turn("hello") == "done"
        assert loop.current_session is not None
        assert loop.turn_context is not None
        session_id = loop.current_session.session_id
        turn_id = loop.turn_context.turn_id
        await loop.aclose()

        records = [
            json.loads(line)
            for line in (log_directory / "ash.jsonl").read_text(
                encoding="utf-8"
            ).splitlines()
        ]
        turn_records = [
            item
            for item in records
            if item["message"].startswith("turn ")
        ]
        assert {item["message"] for item in turn_records} >= {
            "turn started",
            "turn complete, 4 chars returned",
        }
        assert all(item["session_id"] == session_id for item in turn_records)
        assert all(item["turn_id"] == turn_id for item in turn_records)
    finally:
        await loop.aclose()
        _reset_logging()


@pytest.mark.asyncio
async def test_ash_loop_tool_logs_carry_actual_operation_id(tmp_path: Path) -> None:
    class ToolProvider(ProviderABC):
        model_name = "logging-tool-integration"
        _ash_declared_capabilities = ProviderCapabilities(native_tools=True)

        def __init__(self) -> None:
            self.calls = 0

        def count_tokens(self, text):
            return len(str(text))

        async def stream_chat(self, messages, temperature=0.0, tools=None):
            del messages, temperature, tools
            self.calls += 1
            if self.calls == 1:
                yield StreamChunk(
                    is_done=True,
                    native_tool_calls=[
                        {
                            "id": "provider-call-1",
                            "name": "logging_tool",
                            "arguments": "{}",
                        }
                    ],
                )
            else:
                yield StreamChunk(content="done", is_done=True, stop_reason="stop")

    class LoggingTool(BaseTool):
        name = "logging_tool"
        args_schema = None

        async def run(self, **kwargs):
            del kwargs
            get_logger("test.logging.tool").warning("inside tool")
            return ToolResult(success=True, output="ok")

    log_directory = tmp_path / "logs"
    config = AshConfig(
        workspace_root=tmp_path,
        db_directory=tmp_path / "db",
        memory_backend="off",
        repo_map_enabled=False,
        safety_tier="auto_approve",
    )
    store = SessionStore(config.db_directory / "sessions.db")
    guard = SafetyGuard(tmp_path)
    loop = AshLoop(
        store,
        ToolProvider(),
        guard,
        TerminalUI(safety_tier="auto_approve"),
        tmp_path,
        tools={"logging_tool": LoggingTool(guard)},
        config=config,
        safety_tier="auto_approve",
    )
    try:
        configure_logging(
            no_color=True,
            debug=True,
            log_directory=log_directory,
        )
        assert await loop.run_turn("use the tool") == "done"
        assert loop.current_session is not None
        assert loop.turn_context is not None
        stored = store.load_session(loop.current_session.session_id)
        assert len(stored.tool_calls) == 1
        operation_id = stored.tool_calls[0].call_id

        records = [
            json.loads(line)
            for line in (log_directory / "ash.jsonl").read_text(
                encoding="utf-8"
            ).splitlines()
        ]
        record = next(
            item
            for item in records
            if item["component"] == "test.logging.tool"
            and item["message"] == "inside tool"
        )
        assert record["session_id"] == loop.current_session.session_id
        assert record["turn_id"] == loop.turn_context.turn_id
        assert record["operation_id"] == operation_id
    finally:
        await loop.aclose()
        _reset_logging()
