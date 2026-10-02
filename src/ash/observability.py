"""Opt-in, content-free OpenTelemetry export for Ash runtime events."""

from __future__ import annotations

import math
import os
import threading
import time
from dataclasses import dataclass
from importlib.util import find_spec
from typing import Any, Callable
from urllib.parse import urlsplit

from ash import __version__


class ObservabilityError(RuntimeError):
    """Operator-actionable observability configuration or capability failure."""


@dataclass(frozen=True)
class ObservabilityStatus:
    enabled: bool
    available: bool
    traces_endpoint: str
    metrics_endpoint: str
    sample_rate: float
    content_capture: bool = False


@dataclass
class _OpenSpan:
    span: Any
    started_at: float


_GEN_AI_PROVIDER_NAMES = {
    "anthropic": "anthropic",
    "deepseek": "deepseek",
    "google": "gcp.gemini",
    "groq": "groq",
    "mistral": "mistral_ai",
    "openai": "openai",
    "xai": "x_ai",
}


def _module_available(name: str) -> bool:
    try:
        return find_spec(name) is not None
    except (ImportError, ModuleNotFoundError, ValueError):
        return False


def _gen_ai_provider_name(value: str) -> str:
    normalized = value.strip().casefold()
    return _GEN_AI_PROVIDER_NAMES.get(normalized, normalized or "custom")


def _validated_endpoint(value: str, *, label: str) -> str:
    normalized = value.strip()
    if not normalized:
        return ""
    try:
        parsed = urlsplit(normalized)
        port = parsed.port
    except ValueError as exc:
        raise ObservabilityError(f"{label} must be a valid URL") from exc
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise ObservabilityError(
            f"{label} must be a credential-free HTTP(S) URL without query or fragment"
        )
    if port is not None and not 1 <= port <= 65535:
        raise ObservabilityError(f"{label} port is invalid")
    return normalized.rstrip("/")


def _signal_endpoint(base: str, signal: str) -> str:
    suffix = f"/v1/{signal}"
    if base.endswith(suffix):
        return base
    for existing in ("/v1/traces", "/v1/metrics"):
        if base.endswith(existing):
            base = base[: -len(existing)]
            break
    return f"{base.rstrip('/')}{suffix}"


def resolve_observability_endpoints(config: Any) -> tuple[str, str]:
    """Resolve user-owned OTLP/HTTP endpoints only after explicit Ash opt-in."""

    configured = str(getattr(config, "observability_otlp_endpoint", "") or "").strip()
    if configured:
        base = _validated_endpoint(configured, label="observability_otlp_endpoint")
        return _signal_endpoint(base, "traces"), _signal_endpoint(base, "metrics")

    base = _validated_endpoint(
        os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT", ""),
        label="OTEL_EXPORTER_OTLP_ENDPOINT",
    )
    traces = _validated_endpoint(
        os.environ.get("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT", ""),
        label="OTEL_EXPORTER_OTLP_TRACES_ENDPOINT",
    )
    metrics = _validated_endpoint(
        os.environ.get("OTEL_EXPORTER_OTLP_METRICS_ENDPOINT", ""),
        label="OTEL_EXPORTER_OTLP_METRICS_ENDPOINT",
    )
    if not traces and base:
        traces = _signal_endpoint(base, "traces")
    if not metrics and base:
        metrics = _signal_endpoint(base, "metrics")
    if not traces or not metrics:
        raise ObservabilityError(
            "observability is enabled but OTLP endpoints are incomplete; set "
            "ASH_OBSERVABILITY_OTLP_ENDPOINT or the standard OTEL exporter endpoints"
        )
    return traces, metrics


def observability_status(config: Any) -> ObservabilityStatus:
    enabled = bool(getattr(config, "observability_enabled", False))
    available = (
        _module_available("opentelemetry.sdk")
        and _module_available("opentelemetry.exporter.otlp.proto.http.trace_exporter")
        and _module_available("opentelemetry.exporter.otlp.proto.http.metric_exporter")
    )
    traces = ""
    metrics = ""
    if enabled:
        try:
            traces, metrics = resolve_observability_endpoints(config)
        except ObservabilityError:
            pass
    sample_rate = float(getattr(config, "observability_sample_rate", 1.0))
    return ObservabilityStatus(
        enabled=enabled,
        available=available,
        traces_endpoint=traces,
        metrics_endpoint=metrics,
        sample_rate=sample_rate,
    )


class OpenTelemetryEventObserver:
    """Translate a strict safe subset of runtime events into OTel signals."""

    def __init__(
        self,
        *,
        tracer_provider: Any,
        meter_provider: Any,
        close_callback: Callable[[], None] | None = None,
    ) -> None:
        from opentelemetry import trace

        self._trace = trace
        self._tracer_provider = tracer_provider
        self._meter_provider = meter_provider
        self._close_callback = close_callback
        self._tracer = tracer_provider.get_tracer("ash.runtime", __version__)
        self._meter = meter_provider.get_meter("ash.runtime", __version__)
        self._turns = self._meter.create_counter(
            "ash.turns",
            unit="{turn}",
            description="Ash agent turns by terminal outcome.",
        )
        self._turn_duration = self._meter.create_histogram(
            "ash.turn.duration",
            unit="s",
            description="Wall-clock duration of Ash turns.",
        )
        self._model_requests = self._meter.create_counter(
            "ash.model.requests",
            unit="{request}",
            description="Provider model request attempts by outcome.",
        )
        self._model_duration = self._meter.create_histogram(
            "ash.model.request.duration",
            unit="s",
            description="Wall-clock duration of provider model request attempts.",
        )
        self._model_tokens = self._meter.create_counter(
            "ash.model.tokens",
            unit="{token}",
            description="Provider model token usage by token type.",
        )
        self._tool_calls = self._meter.create_counter(
            "ash.tool.calls",
            unit="{call}",
            description="Ash tool calls by terminal outcome.",
        )
        self._tool_duration = self._meter.create_histogram(
            "ash.tool.duration",
            unit="s",
            description="Wall-clock duration of Ash tool calls.",
        )
        self._context_tokens = self._meter.create_histogram(
            "ash.context.tokens",
            unit="{token}",
            description="Ash input-context usage at model assembly time.",
        )
        self._provider_retries = self._meter.create_counter(
            "ash.provider.retries",
            unit="{retry}",
            description="Provider request retries.",
        )
        self._circuit_opened = self._meter.create_counter(
            "ash.provider.circuit_opened",
            unit="{event}",
            description="Provider circuit-breaker openings.",
        )
        self._turn_spans: dict[str, _OpenSpan] = {}
        self._model_spans: dict[str, _OpenSpan] = {}
        self._tool_spans: dict[str, _OpenSpan] = {}
        self._closed = False
        self._lock = threading.RLock()

    def _parent_context(self, turn_id: str) -> Any | None:
        parent = self._turn_spans.get(turn_id)
        if parent is None:
            return None
        return self._trace.set_span_in_context(parent.span)

    @staticmethod
    def _safe_string(value: Any, *, maximum: int = 512) -> str:
        if not isinstance(value, str):
            return ""
        return value[:maximum]

    @staticmethod
    def _safe_int(value: Any) -> int:
        if isinstance(value, bool) or not isinstance(value, int):
            return 0
        return max(0, value)

    def on_event(self, event: dict[str, Any]) -> None:
        if self._closed:
            return
        event_type = self._safe_string(event.get("type"), maximum=128)
        turn_id = self._safe_string(event.get("turn_id"), maximum=256)
        operation_id = self._safe_string(event.get("operation_id"), maximum=256)
        now = time.monotonic()
        with self._lock:
            if event_type == "turn.started" and turn_id:
                if turn_id in self._turn_spans:
                    return
                session_id = self._safe_string(event.get("session_id"), maximum=256)
                span = self._tracer.start_span(
                    "invoke_agent ash",
                    attributes={
                        "gen_ai.operation.name": "invoke_agent",
                        "ash.turn.id": turn_id,
                        **({"ash.session.id": session_id} if session_id else {}),
                        "ash.runtime.event_schema_version": self._safe_int(
                            event.get("schema_version")
                        ),
                    },
                )
                self._turn_spans[turn_id] = _OpenSpan(span=span, started_at=now)
                return

            if event_type == "model.request.started" and operation_id:
                if operation_id in self._model_spans:
                    return
                provider = _gen_ai_provider_name(
                    self._safe_string(event.get("provider"))
                )
                model = self._safe_string(event.get("model"))
                attributes: dict[str, Any] = {
                    "gen_ai.operation.name": "chat",
                    "gen_ai.provider.name": provider or "custom",
                    "gen_ai.request.model": model or "unknown",
                    "ash.operation.id": operation_id,
                    "ash.model.attempt": self._safe_int(event.get("attempt")),
                    "ash.model.native_tools": bool(event.get("native_tools", False)),
                    "ash.model.message_count": self._safe_int(
                        event.get("message_count")
                    ),
                    "ash.model.tool_count": self._safe_int(event.get("tool_count")),
                }
                span = self._tracer.start_span(
                    f"chat {model or 'model'}",
                    context=self._parent_context(turn_id),
                    attributes=attributes,
                    kind=self._trace.SpanKind.CLIENT,
                )
                self._model_spans[operation_id] = _OpenSpan(span=span, started_at=now)
                return

            if event_type == "tool.requested" and operation_id:
                if operation_id in self._tool_spans:
                    return
                tool = self._safe_string(event.get("tool"))
                span = self._tracer.start_span(
                    f"execute_tool {tool or 'tool'}",
                    context=self._parent_context(turn_id),
                    attributes={
                        "gen_ai.operation.name": "execute_tool",
                        "gen_ai.tool.name": tool or "unknown",
                        "gen_ai.tool.type": "function",
                        "ash.operation.id": operation_id,
                    },
                )
                self._tool_spans[operation_id] = _OpenSpan(span=span, started_at=now)
                return

            if event_type == "context.usage":
                current = self._safe_int(event.get("current"))
                maximum = self._safe_int(event.get("maximum"))
                context_attributes = (
                    {"ash.context.maximum_tokens": maximum} if maximum else {}
                )
                self._context_tokens.record(current, attributes=context_attributes)
                return

            if event_type == "provider.retrying":
                status_code = event.get("status_code")
                retry_attributes: dict[str, Any] = {}
                if isinstance(status_code, int) and not isinstance(status_code, bool):
                    retry_attributes["http.response.status_code"] = status_code
                self._provider_retries.add(1, attributes=retry_attributes)
                return

            if event_type == "provider.circuit_opened":
                self._circuit_opened.add(1)
                return

            if event_type in {
                "model.request.completed",
                "model.request.error",
                "model.request.cancelled",
            }:
                self._finish_model(event_type, event, operation_id, now)
                return

            if event_type in {
                "tool.completed",
                "tool.error",
                "tool.denied",
                "tool.skipped",
            }:
                self._finish_tool(event_type, event, operation_id, now)
                return

            if event_type in {
                "turn.completed",
                "goal.step.completed",
                "turn.error",
                "turn.cancelled",
            }:
                self._finish_turn(event_type, event, turn_id, now)

    def _finish_model(
        self,
        event_type: str,
        event: dict[str, Any],
        operation_id: str,
        now: float,
    ) -> None:
        outcome = {
            "model.request.completed": "success",
            "model.request.error": "error",
            "model.request.cancelled": "cancelled",
        }[event_type]
        provider = _gen_ai_provider_name(self._safe_string(event.get("provider")))
        model = self._safe_string(event.get("model")) or "unknown"
        attrs = {
            "outcome": outcome,
            "gen_ai.provider.name": provider,
            "gen_ai.request.model": model,
        }
        self._model_requests.add(1, attributes=attrs)
        state = self._model_spans.pop(operation_id, None)
        if state is not None:
            state.span.set_attribute("ash.outcome", outcome)
            state.span.set_attribute("gen_ai.provider.name", provider or "custom")
            state.span.set_attribute("gen_ai.response.model", model)
            if outcome == "error":
                error_type = self._safe_string(event.get("error_type"), maximum=256)
                if error_type:
                    state.span.set_attribute("error.type", error_type)
                state.span.set_status(self._trace.Status(self._trace.StatusCode.ERROR))
            elif outcome == "cancelled":
                state.span.set_attribute("ash.cancelled", True)
            if event_type == "model.request.completed":
                self._record_model_usage(event, attrs)
                state.span.set_attribute(
                    "gen_ai.usage.input_tokens",
                    self._safe_int(event.get("prompt_tokens")),
                )
                state.span.set_attribute(
                    "gen_ai.usage.output_tokens",
                    self._safe_int(event.get("completion_tokens")),
                )
            state.span.end()
            self._model_duration.record(
                max(0.0, now - state.started_at),
                attributes=attrs,
            )
        elif event_type == "model.request.completed":
            self._record_model_usage(event, attrs)

    def _record_model_usage(
        self,
        event: dict[str, Any],
        base_attributes: dict[str, Any],
    ) -> None:
        for field, token_type in (
            ("prompt_tokens", "input"),
            ("completion_tokens", "output"),
            ("cache_read_tokens", "cache_read"),
            ("cache_write_tokens", "cache_write"),
        ):
            count = self._safe_int(event.get(field))
            if count:
                self._model_tokens.add(
                    count,
                    attributes={**base_attributes, "gen_ai.token.type": token_type},
                )

    def _finish_tool(
        self,
        event_type: str,
        event: dict[str, Any],
        operation_id: str,
        now: float,
    ) -> None:
        outcome = {
            "tool.completed": "success" if bool(event.get("success", True)) else "error",
            "tool.error": "error",
            "tool.denied": "denied",
            "tool.skipped": "skipped",
        }[event_type]
        tool = self._safe_string(event.get("tool")) or "unknown"
        attrs = {"outcome": outcome, "gen_ai.tool.name": tool}
        self._tool_calls.add(1, attributes=attrs)
        state = self._tool_spans.pop(operation_id, None)
        if state is None:
            return
        state.span.set_attribute("ash.outcome", outcome)
        if outcome == "error":
            state.span.set_status(self._trace.Status(self._trace.StatusCode.ERROR))
        elif outcome == "denied":
            state.span.set_attribute("ash.tool.denied", True)
        state.span.end()
        self._tool_duration.record(
            max(0.0, now - state.started_at),
            attributes=attrs,
        )

    def _finish_turn(
        self,
        event_type: str,
        event: dict[str, Any],
        turn_id: str,
        now: float,
    ) -> None:
        outcome = {
            "turn.completed": "success",
            "goal.step.completed": "success",
            "turn.error": "error",
            "turn.cancelled": "cancelled",
        }[event_type]
        attrs = {"outcome": outcome}
        self._turns.add(1, attributes=attrs)
        state = self._turn_spans.pop(turn_id, None)
        if state is None:
            return
        state.span.set_attribute("ash.outcome", outcome)
        if outcome == "error":
            state.span.set_status(self._trace.Status(self._trace.StatusCode.ERROR))
        elif outcome == "cancelled":
            state.span.set_attribute("ash.cancelled", True)
        state.span.end()
        self._turn_duration.record(
            max(0.0, now - state.started_at),
            attributes=attrs,
        )

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            for collection in (
                self._model_spans,
                self._tool_spans,
                self._turn_spans,
            ):
                for state in collection.values():
                    state.span.set_attribute("ash.outcome", "shutdown")
                    state.span.end()
                collection.clear()
        if self._close_callback is not None:
            self._close_callback()


def build_observability_observer(config: Any) -> OpenTelemetryEventObserver | None:
    """Build the optional OTLP/HTTP observer only after explicit user opt-in."""

    if not bool(getattr(config, "observability_enabled", False)):
        return None
    try:
        from opentelemetry.exporter.otlp.proto.http.metric_exporter import (
            OTLPMetricExporter,
        )
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
            OTLPSpanExporter,
        )
        from opentelemetry.sdk.metrics import MeterProvider
        from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
        from opentelemetry.sdk.trace.sampling import ParentBased, TraceIdRatioBased
    except ImportError as exc:
        raise ObservabilityError(
            "observability is enabled but the optional OpenTelemetry pack is "
            "not installed; install Ash with the 'observability' extra"
        ) from exc

    traces_endpoint, metrics_endpoint = resolve_observability_endpoints(config)
    sample_rate = float(getattr(config, "observability_sample_rate", 1.0))
    if not math.isfinite(sample_rate) or not 0.0 <= sample_rate <= 1.0:
        raise ObservabilityError("observability_sample_rate must be between 0 and 1")
    timeout = float(getattr(config, "observability_export_timeout_seconds", 5.0))
    interval = float(
        getattr(config, "observability_export_interval_seconds", 15.0)
    )
    resource = Resource.create(
        {
            "service.name": "ash",
            "service.version": __version__,
        }
    )
    span_exporter = OTLPSpanExporter(endpoint=traces_endpoint, timeout=timeout)
    metric_exporter = OTLPMetricExporter(endpoint=metrics_endpoint, timeout=timeout)
    metric_reader = PeriodicExportingMetricReader(
        metric_exporter,
        export_interval_millis=interval * 1000.0,
        export_timeout_millis=timeout * 1000.0,
    )
    meter_provider = MeterProvider(
        resource=resource,
        metric_readers=[metric_reader],
        shutdown_on_exit=False,
    )
    tracer_provider = TracerProvider(
        sampler=ParentBased(TraceIdRatioBased(sample_rate)),
        resource=resource,
        shutdown_on_exit=False,
        meter_provider=meter_provider,
    )
    tracer_provider.add_span_processor(
        BatchSpanProcessor(
            span_exporter,
            schedule_delay_millis=min(5000.0, interval * 1000.0),
            export_timeout_millis=timeout * 1000.0,
        )
    )

    def close() -> None:
        # Traces first so completed spans are queued before metric shutdown.
        tracer_provider.shutdown()
        meter_provider.shutdown()

    return OpenTelemetryEventObserver(
        tracer_provider=tracer_provider,
        meter_provider=meter_provider,
        close_callback=close,
    )
