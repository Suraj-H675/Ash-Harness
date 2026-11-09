"""Bounded, content-free projections of persisted coding-agent activity."""

from __future__ import annotations

import re
from collections.abc import Sequence
from datetime import datetime
from typing import Any

from ash.core.redaction import redact_text
from ash.core.session import StoredRuntimeEvent


EDIT_TOOL_NAMES = frozenset(
    {
        "write_file",
        "replace_file_content",
        "replace_file_edits",
        "whole_edit",
        "apply_patch",
    }
)
METRIC_EVENT_FIELDS = {
    "turn.started": (),
    "context.usage": ("current", "maximum"),
    "model.request.started": ("attempt", "message_count", "tool_count", "native_tools"),
    "model.request.completed": (
        "attempt",
        "prompt_tokens",
        "completion_tokens",
        "cache_read_tokens",
        "cache_write_tokens",
        "stop_category",
    ),
    "model.request.error": ("attempt", "retriable", "failure_category"),
    "model.request.cancelled": ("attempt",),
    "tool.started": ("tool",),
    "tool.completed": ("tool", "success", "exit_code", "dispatched"),
    "tool.error": ("tool", "success", "exit_code", "ambiguous"),
    "tool.denied": ("tool",),
    "tool.skipped": ("tool", "success"),
    "turn.completed": ("stop_reason", "iterations"),
    "turn.cancelled": (),
    "turn.error": (),
}
MAX_TIMELINE_EVENTS = 300
_METRIC_NAME_PATTERN = re.compile(r"[a-zA-Z0-9_.-]{1,64}\Z")


def summarize_operational_events(
    events: Sequence[StoredRuntimeEvent],
) -> dict[str, Any]:
    """Project persisted events to bounded timings without model/tool content.

    Raw runtime events may include prompts, command output, paths, credentials,
    and tool arguments. Only explicitly listed non-content fields are retained.
    """

    origin: float | None = None
    request_starts: dict[str, float] = {}
    model_seconds = 0.0
    model_requests = 0
    completed_requests = 0
    last_successful_test: float | None = None
    last_successful_edit: float | None = None
    timeline: list[dict[str, Any]] = []
    event_counts: dict[str, int] = {}
    for stored in events:
        event = stored.event
        event_type = event.get("type")
        if not isinstance(event_type, str) or event_type not in METRIC_EVENT_FIELDS:
            continue
        timestamp = event.get("timestamp")
        if not isinstance(timestamp, str) or len(timestamp) > 64:
            continue
        try:
            parsed = datetime.fromisoformat(timestamp)
            if parsed.tzinfo is None:
                continue
            epoch = parsed.timestamp()
        except (ValueError, OverflowError):
            continue
        if origin is None:
            origin = epoch
        elapsed = max(0.0, round(epoch - origin, 3))
        entry: dict[str, Any] = {"type": event_type, "at_seconds": elapsed}
        for field in METRIC_EVENT_FIELDS[event_type]:
            value = event.get(field)
            if field in {"tool", "stop_category", "stop_reason", "failure_category"}:
                if isinstance(value, str) and _METRIC_NAME_PATTERN.fullmatch(value):
                    entry[field] = redact_text(value)
            elif type(value) in {int, bool}:
                entry[field] = value
        event_counts[event_type] = event_counts.get(event_type, 0) + 1
        if len(timeline) < MAX_TIMELINE_EVENTS:
            timeline.append(entry)
        request_id = event.get("operation_id")
        if event_type == "model.request.started":
            model_requests += 1
            if isinstance(request_id, str):
                request_starts[request_id] = epoch
        elif event_type in {
            "model.request.completed",
            "model.request.error",
            "model.request.cancelled",
        }:
            if event_type == "model.request.completed":
                completed_requests += 1
            if isinstance(request_id, str) and request_id in request_starts:
                model_seconds += max(0.0, epoch - request_starts.pop(request_id))
        elif event_type == "tool.completed" and entry.get("success") is True:
            if entry.get("tool") in EDIT_TOOL_NAMES:
                last_successful_edit = elapsed
            elif entry.get("tool") == "run_command":
                last_successful_test = elapsed

    return {
        "model_requests": model_requests,
        "model_requests_completed": completed_requests,
        "model_requests_still_open": len(request_starts),
        "completed_model_seconds": round(model_seconds, 3),
        "last_successful_edit_seconds": last_successful_edit,
        "last_successful_command_seconds": last_successful_test,
        "event_counts": event_counts,
        "timeline_truncated": sum(event_counts.values()) > len(timeline),
        "timeline": timeline,
    }
