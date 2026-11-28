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
    "context.usage": (
        "context_id",
        "assembly_purpose",
        "estimate_source",
        "current",
        "maximum",
        "combined_limit",
        "response_limit",
        "input_limit",
        "message_estimate_tokens",
        "serialized_message_tokens",
        "native_tool_schema_tokens",
        "deferred_catalog_omitted_count",
        "original_tool_schema_tokens",
        "estimator_route",
        "estimator_count_mode",
        "system_tokens",
        "tool_schema_tokens",
        "history_tokens",
        "repo_map_tokens",
        "memory_tokens",
        "history_message_count",
        "tool_message_count",
        "tool_message_chars",
        "assistant_text_chars",
        "assistant_tool_call_argument_chars",
        "serialized_tool_result_chars",
        "unattributed_tool_call_arguments",
        "non_string_tool_result_messages",
        "image_count",
        "image_estimate_tokens",
        "opaque_transport_estimate_enabled",
        "opaque_transport_estimate_tokens",
        "provider_replay_state_preserved",
    ),
    "context.request.rejected": (
        "context_id",
        "cause",
        "scope",
        "dispatch_status",
        "required_input_tokens",
        "input_limit",
        "protected_current_turn",
        "opaque_provider_replay_state_preserved",
        "required_history_summary",
        "turn_tokens_used",
        "turn_token_limit",
        "native_tool_schema_tokens",
        "required_total_input_tokens",
        "total_input_limit",
        "combined_limit",
        "response_limit",
        "message_estimate_tokens",
        "serialized_message_tokens",
        "image_count",
        "image_estimate_tokens",
        "opaque_transport_estimate_tokens",
        "opaque_transport_estimate_enabled",
        "assistant_text_chars",
        "assistant_tool_call_argument_chars",
        "serialized_tool_result_chars",
        "unattributed_tool_call_arguments",
        "non_string_tool_result_messages",
    ),
    "model.request.started": (
        "context_id",
        "attempt",
        "message_count",
        "tool_count",
        "native_tools",
        "requested_completion_limit",
        "requested_route",
        "effective_route",
    ),
    "model.request.first_output": ("context_id", "attempt"),
    "model.request.completed": (
        "attempt",
        "prompt_tokens",
        "completion_tokens",
        "cache_read_tokens",
        "cache_write_tokens",
        "usage_source",
        "stop_category",
        "context_id",
        "requested_route",
        "effective_route",
    ),
    "model.request.error": (
        "context_id",
        "attempt",
        "retriable",
        "failure_category",
        "requested_route",
        "effective_route",
    ),
    "model.request.cancelled": (
        "context_id",
        "attempt",
        "requested_route",
        "effective_route",
    ),
    "provider.retrying": (
        "context_id",
        "attempt",
        "max_attempts",
        "delay_seconds",
        "status_code",
        "failure_category",
    ),
    "turn.usage": (
        "prompt_tokens",
        "completion_tokens",
        "cache_read_tokens",
        "cache_write_tokens",
        "usage_source",
        "estimated_prompt_tokens",
        "estimated_completion_tokens",
    ),
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
_METRIC_ROUTE_PATTERN = re.compile(r"[a-zA-Z0-9_.:/+-]{1,180}\Z")
_CORRELATION_ID_PATTERN = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\Z"
)


def summarize_context_diagnostics(
    events: Sequence[StoredRuntimeEvent], *, session_id: str
) -> dict[str, Any]:
    """Join a bounded session tail by explicit correlation IDs, never time."""

    recent = sorted(events[-MAX_TIMELINE_EVENTS:], key=lambda item: item.sequence)
    scoped = [
        (stored.sequence, stored.event)
        for stored in recent
        if stored.event.get("session_id") == session_id
    ]
    latest_assembly: dict[str, Any] | None = None
    latest_rejection: dict[str, Any] | None = None
    latest_turn_usage: dict[str, Any] | None = None
    latest_start: tuple[int, dict[str, Any]] | None = None
    terminal_turns: dict[str, int] = {}
    outcomes: dict[str, tuple[int, dict[str, Any]]] = {}
    assemblies_by_context: dict[str, tuple[int, dict[str, Any]]] = {}

    for sequence, event in scoped:
        event_type = event.get("type")
        context_id = event.get("context_id")
        valid_context_id = (
            isinstance(context_id, str)
            and _CORRELATION_ID_PATTERN.fullmatch(context_id) is not None
        )
        operation_id = event.get("operation_id")
        valid_operation_id = (
            isinstance(operation_id, str)
            and _CORRELATION_ID_PATTERN.fullmatch(operation_id) is not None
        )
        if (
            event_type == "context.usage"
            and event.get("assembly_purpose") == "request"
            and valid_context_id
            and isinstance(context_id, str)
        ):
            latest_assembly = {
                "sequence": sequence,
                "context_id": context_id,
                "current": _nonnegative_int(event.get("current")),
                "maximum": _nonnegative_int(event.get("maximum")),
                "combined_limit": _nonnegative_int(event.get("combined_limit")),
                "response_limit": _nonnegative_int(event.get("response_limit")),
                "input_limit": _nonnegative_int(event.get("input_limit")),
                "message_estimate_tokens": _nonnegative_int(
                    event.get("message_estimate_tokens")
                ),
                "serialized_message_tokens": _nonnegative_int(
                    event.get("serialized_message_tokens")
                ),
                "native_tool_schema_tokens": _nonnegative_int(
                    event.get("native_tool_schema_tokens")
                ),
                "deferred_catalog_omitted_count": _nonnegative_int(
                    event.get("deferred_catalog_omitted_count")
                ),
                "original_tool_schema_tokens": _nonnegative_int(
                    event.get("original_tool_schema_tokens")
                ),
                "estimator_route": _safe_route(event.get("estimator_route")),
                "estimate_source": _safe_name(event.get("estimate_source")),
                "estimator_count_mode": _safe_name(
                    event.get("estimator_count_mode")
                ),
                "assistant_text_chars": _nonnegative_int(
                    event.get("assistant_text_chars")
                ),
                "assistant_tool_call_argument_chars": _nonnegative_int(
                    event.get("assistant_tool_call_argument_chars")
                ),
                "serialized_tool_result_chars": _nonnegative_int(
                    event.get("serialized_tool_result_chars")
                ),
                "unattributed_tool_call_arguments": _nonnegative_int(
                    event.get("unattributed_tool_call_arguments")
                ),
                "non_string_tool_result_messages": _nonnegative_int(
                    event.get("non_string_tool_result_messages")
                ),
                "image_count": _nonnegative_int(event.get("image_count")),
                "image_estimate_tokens": _nonnegative_int(
                    event.get("image_estimate_tokens")
                ),
                "opaque_transport_estimate_enabled": _optional_bool(
                    event.get("opaque_transport_estimate_enabled")
                ),
                "opaque_transport_estimate_tokens": _nonnegative_int(
                    event.get("opaque_transport_estimate_tokens")
                ),
                "provider_replay_state_preserved": (
                    event.get("provider_replay_state_preserved")
                    if type(event.get("provider_replay_state_preserved")) is bool
                    else None
                ),
                "recorded_at": _safe_timestamp(event.get("timestamp")),
            }
            assemblies_by_context[context_id] = (sequence, latest_assembly)
        elif event_type == "context.request.rejected" and valid_context_id:
            latest_rejection = {
                "sequence": sequence,
                "context_id": context_id,
                "cause": _safe_name(event.get("cause")),
                "scope": _safe_name(event.get("scope")),
                "dispatch_status": _safe_name(event.get("dispatch_status")),
                "required_input_tokens": _nonnegative_int(
                    event.get("required_input_tokens")
                ),
                "input_limit": _nonnegative_int(event.get("input_limit")),
                "protected_current_turn": _optional_bool(
                    event.get("protected_current_turn")
                ),
                "opaque_provider_replay_state_preserved": _optional_bool(
                    event.get("opaque_provider_replay_state_preserved")
                ),
                "required_history_summary": _optional_bool(
                    event.get("required_history_summary")
                ),
                **{
                    field: _nonnegative_int(event.get(field))
                    for field in (
                        "native_tool_schema_tokens",
                        "required_total_input_tokens",
                        "total_input_limit",
                        "combined_limit",
                        "response_limit",
                        "message_estimate_tokens",
                        "serialized_message_tokens",
                        "image_count",
                        "image_estimate_tokens",
                        "opaque_transport_estimate_tokens",
                        "assistant_text_chars",
                        "assistant_tool_call_argument_chars",
                        "serialized_tool_result_chars",
                        "unattributed_tool_call_arguments",
                        "non_string_tool_result_messages",
                    )
                },
                "opaque_transport_estimate_enabled": _optional_bool(
                    event.get("opaque_transport_estimate_enabled")
                ),
                "turn_tokens_used": _nonnegative_int(
                    event.get("turn_tokens_used")
                ),
                "turn_token_limit": _nonnegative_int(
                    event.get("turn_token_limit")
                ),
                "recorded_at": _safe_timestamp(event.get("timestamp")),
            }
        elif event_type == "turn.usage":
            latest_turn_usage = {
                key: _nonnegative_int(event.get(key))
                for key in (
                    "prompt_tokens",
                    "completion_tokens",
                    "cache_read_tokens",
                    "cache_write_tokens",
                    "estimated_prompt_tokens",
                    "estimated_completion_tokens",
                )
            }
            latest_turn_usage["usage_source"] = _safe_name(
                event.get("usage_source")
            )
            prompt_tokens = _nonnegative_int(event.get("prompt_tokens"))
            cache_read_tokens = _nonnegative_int(event.get("cache_read_tokens"))
            latest_turn_usage["cache_hit_rate"] = (
                cache_read_tokens / prompt_tokens
                if prompt_tokens and cache_read_tokens is not None
                else 0.0
            )
            latest_turn_usage["turn_id"] = (
                event.get("turn_id")
                if isinstance(event.get("turn_id"), str)
                and len(event["turn_id"]) <= 64
                else None
            )
            latest_turn_usage["recorded_at"] = _safe_timestamp(
                event.get("timestamp")
            )
        elif event_type in {"turn.completed", "turn.error", "turn.cancelled"}:
            turn_id = event.get("turn_id")
            if isinstance(turn_id, str) and len(turn_id) <= 64:
                terminal_turns[turn_id] = sequence

        if event_type == "model.request.started" and valid_context_id and valid_operation_id:
            if latest_start is None or sequence >= latest_start[0]:
                latest_start = (sequence, event)
        elif (
            event_type
            in {
                "model.request.completed",
                "model.request.error",
                "model.request.cancelled",
            }
            and valid_operation_id
            and valid_context_id
            and isinstance(operation_id, str)
        ):
            outcomes[operation_id] = (sequence, event)

    latest_request: dict[str, Any] | None = None
    if latest_start is not None:
        start_sequence, start = latest_start
        start_context_id = start["context_id"]
        operation_id = start["operation_id"]
        terminal = outcomes.get(operation_id)
        outcome_event: dict[str, Any] | None = None
        if terminal is not None:
            terminal_sequence, candidate = terminal
            if (
                terminal_sequence >= start_sequence
                and candidate.get("context_id") == start_context_id
            ):
                outcome_event = candidate
        state = "pending"
        if outcome_event is not None:
            outcome_type = outcome_event.get("type")
            state = {
                "model.request.completed": "completed",
                "model.request.error": "failed",
                "model.request.cancelled": "cancelled",
            }.get(str(outcome_type), "missing")
        else:
            turn_id = start.get("turn_id")
            if (
                isinstance(turn_id, str)
                and terminal_turns.get(turn_id, -1) > start_sequence
            ):
                state = "missing"
        requested_route = _safe_route(start.get("requested_route"))
        effective_route = (
            _safe_route(outcome_event.get("effective_route"))
            if outcome_event is not None
            else "unknown"
        )
        parity = "unknown"
        if requested_route not in {None, "unknown"} and effective_route not in {
            None,
            "unknown",
        }:
            parity = "match" if requested_route == effective_route else "mismatch"
        matched_assembly = assemblies_by_context.get(start_context_id)
        if matched_assembly is not None and matched_assembly[0] > start_sequence:
            matched_assembly = None
        latest_request = {
            "sequence": start_sequence,
            "turn_id": (
                start.get("turn_id")
                if isinstance(start.get("turn_id"), str)
                and len(start["turn_id"]) <= 64
                else None
            ),
            "context_id": start_context_id,
            "operation_id": operation_id,
            "attempt": _nonnegative_int(start.get("attempt")),
            "requested_completion_limit": _nonnegative_int(
                start.get("requested_completion_limit")
            ),
            "requested_route": requested_route or "unknown",
            "effective_route": effective_route or "unknown",
            "route_parity": parity,
            "state": state,
            "assembly": matched_assembly[1] if matched_assembly is not None else None,
            "usage_source": (
                (
                    _safe_name(outcome_event.get("usage_source")) or "unavailable"
                )
                if outcome_event is not None
                and outcome_event.get("type") == "model.request.completed"
                else "unavailable"
            ),
            "prompt_tokens": (
                _nonnegative_int(outcome_event.get("prompt_tokens"))
                if outcome_event is not None
                else None
            ),
            "completion_tokens": (
                _nonnegative_int(outcome_event.get("completion_tokens"))
                if outcome_event is not None
                else None
            ),
            "cache_read_tokens": (
                _nonnegative_int(outcome_event.get("cache_read_tokens"))
                if outcome_event is not None
                else None
            ),
            "cache_write_tokens": (
                _nonnegative_int(outcome_event.get("cache_write_tokens"))
                if outcome_event is not None
                else None
            ),
            "recorded_at": _safe_timestamp(
                outcome_event.get("timestamp")
                if outcome_event is not None
                else start.get("timestamp")
            ),
        }

    return {
        "assembly": latest_assembly,
        "request": latest_request,
        "rejection": latest_rejection,
        "turn_usage": latest_turn_usage,
        "events_scanned": len(scoped),
    }


def _nonnegative_int(value: Any) -> int | None:
    if type(value) is int and 0 <= value <= 2_147_483_647:
        return value
    return None


def _optional_bool(value: Any) -> bool | None:
    return value if type(value) is bool else None


def _safe_name(value: Any) -> str | None:
    if isinstance(value, str) and _METRIC_NAME_PATTERN.fullmatch(value):
        return redact_text(value)
    return None


def _safe_route(value: Any) -> str | None:
    if isinstance(value, str) and _METRIC_ROUTE_PATTERN.fullmatch(value):
        return redact_text(value)
    return None


def _safe_timestamp(value: Any) -> str | None:
    if isinstance(value, str) and len(value) <= 64:
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError:
            return None
        return value if parsed.tzinfo is not None else None
    return None


def summarize_operational_events(
    events: Sequence[StoredRuntimeEvent],
) -> dict[str, Any]:
    """Project persisted events to bounded timings without model/tool content.

    Raw runtime events may include prompts, command output, paths, credentials,
    and tool arguments. Only explicitly listed non-content fields are retained.
    """

    origin: float | None = None
    request_starts: dict[str, float] = {}
    first_output_delays: dict[str, float] = {}
    tool_starts: dict[str, float] = {}
    model_seconds = 0.0
    model_first_output_seconds = 0.0
    model_after_first_output_seconds = 0.0
    completed_requests_with_first_output = 0
    unsuccessful_model_seconds = 0.0
    completed_tool_seconds = 0.0
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
            if field in {"context_id"}:
                if isinstance(value, str) and _CORRELATION_ID_PATTERN.fullmatch(value):
                    entry[field] = value
                continue
            if field in {"requested_route", "effective_route", "estimator_route"}:
                if isinstance(value, str) and _METRIC_ROUTE_PATTERN.fullmatch(value):
                    entry[field] = value
                continue
            if field in {
                "tool",
                "usage_source",
                "stop_category",
                "stop_reason",
                "failure_category",
                "assembly_purpose",
                "estimator_count_mode",
                "cause",
                "scope",
                "dispatch_status",
            }:
                if isinstance(value, str) and _METRIC_NAME_PATTERN.fullmatch(value):
                    entry[field] = redact_text(value)
            elif type(value) in {int, bool}:
                entry[field] = value
        request_id = event.get("operation_id")
        if (
            isinstance(request_id, str)
            and _CORRELATION_ID_PATTERN.fullmatch(request_id)
        ):
            entry["operation_id"] = request_id
        event_counts[event_type] = event_counts.get(event_type, 0) + 1
        if len(timeline) < MAX_TIMELINE_EVENTS:
            timeline.append(entry)
        request_id = event.get("operation_id")
        if event_type == "model.request.started":
            model_requests += 1
            if isinstance(request_id, str):
                request_starts[request_id] = epoch
        elif event_type == "model.request.first_output":
            if (
                isinstance(request_id, str)
                and request_id in request_starts
                and request_id not in first_output_delays
            ):
                delay = round(max(0.0, epoch - request_starts[request_id]), 3)
                first_output_delays[request_id] = delay
                entry["since_start_seconds"] = delay
        elif event_type in {
            "model.request.completed",
            "model.request.error",
            "model.request.cancelled",
        }:
            if isinstance(request_id, str) and request_id in request_starts:
                duration = round(max(0.0, epoch - request_starts.pop(request_id)), 3)
                entry["duration_seconds"] = duration
                if event_type == "model.request.completed":
                    model_seconds += duration
                    if request_id in first_output_delays:
                        first_output_seconds = min(
                            duration, first_output_delays[request_id]
                        )
                        after_first_output = round(duration - first_output_seconds, 3)
                        entry["first_output_seconds"] = first_output_seconds
                        entry["after_first_output_seconds"] = after_first_output
                        model_first_output_seconds += first_output_seconds
                        model_after_first_output_seconds += after_first_output
                        completed_requests_with_first_output += 1
                else:
                    unsuccessful_model_seconds += duration
            if isinstance(request_id, str):
                first_output_delays.pop(request_id, None)
            if event_type == "model.request.completed":
                completed_requests += 1
        elif event_type == "tool.started":
            if isinstance(request_id, str):
                tool_starts[request_id] = epoch
        elif event_type in {"tool.completed", "tool.error", "tool.skipped"}:
            if isinstance(request_id, str) and request_id in tool_starts:
                duration = round(max(0.0, epoch - tool_starts.pop(request_id)), 3)
                entry["duration_seconds"] = duration
                if event_type == "tool.completed":
                    completed_tool_seconds += duration
            if event_type == "tool.completed" and entry.get("success") is True:
                if entry.get("tool") in EDIT_TOOL_NAMES:
                    last_successful_edit = elapsed
                elif entry.get("tool") == "run_command":
                    last_successful_test = elapsed

    return {
        "model_requests": model_requests,
        "model_requests_completed": completed_requests,
        "model_requests_still_open": len(request_starts),
        "completed_model_seconds": round(model_seconds, 3),
        "completed_requests_with_first_output": completed_requests_with_first_output,
        "completed_model_first_output_seconds": round(model_first_output_seconds, 3),
        "completed_model_after_first_output_seconds": round(
            model_after_first_output_seconds, 3
        ),
        "unsuccessful_model_seconds": round(unsuccessful_model_seconds, 3),
        "completed_tool_seconds": round(completed_tool_seconds, 3),
        "tool_calls_still_open": len(tool_starts),
        "last_successful_edit_seconds": last_successful_edit,
        "last_successful_command_seconds": last_successful_test,
        "event_counts": event_counts,
        "timeline_truncated": sum(event_counts.values()) > len(timeline),
        "timeline": timeline,
    }
