"""Non-interactive event UI for scripts and CI."""

from __future__ import annotations

import json
import sys
from contextlib import nullcontext
from typing import Any, Callable, TextIO

from rich.console import Console


class HeadlessUI:
    """Consume loop UI events without terminal control sequences."""

    def __init__(
        self,
        *,
        output_format: str = "text",
        stream: TextIO | None = None,
    ) -> None:
        if output_format not in {"text", "json", "stream-json"}:
            raise ValueError(f"Unsupported output format: {output_format}")
        self.output_format = output_format
        self.stream = stream or sys.stdout
        self.console = Console(file=sys.stderr, force_terminal=False, no_color=True)
        self._listeners: set[Callable[[dict[str, Any]], None]] = set()

    @property
    def has_approval_callback(self) -> bool:
        return False

    def begin_turn(self):
        return nullcontext()

    def finalize_turn(self) -> None:
        return None

    def print_token(self, text: str) -> None:
        if text:
            self._notify({"type": "assistant.delta", "text": text})
        if self.output_format == "stream-json" and text:
            self._emit({"type": "assistant.delta", "text": text})

    def print_thought(self, text: str) -> None:
        if text:
            self._notify({"type": "reasoning.delta", "text": text})
        if self.output_format == "stream-json" and text:
            self._emit({"type": "reasoning.delta", "text": text})

    def update_token_count(self, current: int, maximum: int | None = None) -> None:
        self._notify({"type": "context.usage", "current": current, "maximum": maximum})
        if self.output_format == "stream-json":
            self._emit(
                {"type": "context.usage", "current": current, "maximum": maximum}
            )

    def request_tool_approval(self, tool_name: str, arguments: dict[str, Any]) -> bool:
        return False

    def show_plan(self, execution: Any) -> bool:
        if self.output_format == "stream-json":
            self._emit(
                {
                    "type": "plan.denied",
                    "plan_id": execution.contract.contract_id,
                    "reason": "headless mode cannot approve a generated plan",
                }
            )
        return False

    def emit_result(self, payload: dict[str, Any]) -> None:
        if self.output_format in {"json", "stream-json"}:
            event = {"type": "turn.completed", **payload}
            self._emit(event)
        else:
            print(payload["response"], file=self.stream, flush=True)

    def emit_error(self, payload: dict[str, Any]) -> None:
        event = {"type": "error", "error": payload}
        self._notify(event)
        if self.output_format in {"json", "stream-json"}:
            self._emit(event)
        else:
            message = payload.get("message") or "Unknown error"
            category = payload.get("category") or "internal"
            remedy = payload.get("remedy")
            print(f"Error [{category}]: {message}", file=sys.stderr)
            if remedy:
                print(f"Remedy: {remedy}", file=sys.stderr)

    def emit_event(self, payload: dict[str, Any]) -> None:
        self._notify(payload)
        if self.output_format == "stream-json":
            self._emit(payload)

    def _emit(self, payload: dict[str, Any]) -> None:
        print(
            json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
            file=self.stream,
            flush=True,
        )

    def subscribe(
        self, listener: Callable[[dict[str, Any]], None]
    ) -> Callable[[], None]:
        """Subscribe to runtime events and return an idempotent unsubscribe callback."""

        self._listeners.add(listener)

        def unsubscribe() -> None:
            self._listeners.discard(listener)

        return unsubscribe

    def _notify(self, payload: dict[str, Any]) -> None:
        for listener in tuple(self._listeners):
            listener(dict(payload))
