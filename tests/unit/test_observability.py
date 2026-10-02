from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import threading
from types import SimpleNamespace

import pytest

from ash.observability import (
    ObservabilityError,
    OpenTelemetryEventObserver,
    _gen_ai_provider_name,
    build_observability_observer,
    observability_status,
    resolve_observability_endpoints,
)


class _Instrument:
    def __init__(self) -> None:
        self.points: list[tuple[float, dict[str, object]]] = []

    def add(self, value, *, attributes=None) -> None:
        self.points.append((float(value), dict(attributes or {})))

    def record(self, value, *, attributes=None) -> None:
        self.points.append((float(value), dict(attributes or {})))


class _Meter:
    def __init__(self) -> None:
        self.instruments: dict[str, _Instrument] = {}

    def create_counter(self, name, **_kwargs):
        instrument = _Instrument()
        self.instruments[name] = instrument
        return instrument

    def create_histogram(self, name, **_kwargs):
        instrument = _Instrument()
        self.instruments[name] = instrument
        return instrument


class _MeterProvider:
    def __init__(self) -> None:
        self.meter = _Meter()

    def get_meter(self, *_args, **_kwargs):
        return self.meter


class _Span:
    def __init__(self, name, *, context=None, attributes=None, kind=None) -> None:
        self.name = name
        self.context = context
        self.attributes = dict(attributes or {})
        self.kind = kind
        self.status = None
        self.ended = False

    def set_attribute(self, key, value) -> None:
        self.attributes[key] = value

    def set_status(self, status) -> None:
        self.status = status

    def end(self) -> None:
        self.ended = True


class _Tracer:
    def __init__(self) -> None:
        self.spans: list[_Span] = []

    def start_span(self, name, *, context=None, attributes=None, kind=None):
        span = _Span(name, context=context, attributes=attributes, kind=kind)
        self.spans.append(span)
        return span


class _TracerProvider:
    def __init__(self) -> None:
        self.tracer = _Tracer()

    def get_tracer(self, *_args, **_kwargs):
        return self.tracer


class _Trace:
    class SpanKind:
        CLIENT = "client"

    class StatusCode:
        ERROR = "error"

    class Status:
        def __init__(self, code) -> None:
            self.code = code

    @staticmethod
    def set_span_in_context(span):
        return ("parent", span)


def _event(event_type: str, **values):
    return {
        "schema_version": 1,
        "event_id": f"event-{event_type}",
        "timestamp": "2026-10-01T00:00:00+00:00",
        "source": {"type": "runtime", "id": "ash"},
        "session_id": "session-1",
        "turn_id": "turn-1",
        "operation_id": None,
        "parent_event_id": None,
        "type": event_type,
        **values,
    }


def test_observability_endpoint_resolution_uses_safe_otlp_http_paths(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT", raising=False)
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_METRICS_ENDPOINT", raising=False)
    config = SimpleNamespace(
        observability_otlp_endpoint="https://otel.example/collector"
    )

    assert resolve_observability_endpoints(config) == (
        "https://otel.example/collector/v1/traces",
        "https://otel.example/collector/v1/metrics",
    )
    config.observability_otlp_endpoint = "https://otel.example/collector/v1/traces"
    assert resolve_observability_endpoints(config) == (
        "https://otel.example/collector/v1/traces",
        "https://otel.example/collector/v1/metrics",
    )

    config.observability_otlp_endpoint = ""
    monkeypatch.setenv(
        "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT",
        "https://otel.example/custom-traces",
    )
    monkeypatch.setenv(
        "OTEL_EXPORTER_OTLP_METRICS_ENDPOINT",
        "https://otel.example/custom-metrics",
    )
    assert resolve_observability_endpoints(config) == (
        "https://otel.example/custom-traces",
        "https://otel.example/custom-metrics",
    )


def test_observability_endpoint_resolution_rejects_credentials_and_partial_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with pytest.raises(ObservabilityError, match="credential-free"):
        resolve_observability_endpoints(
            SimpleNamespace(
                observability_otlp_endpoint="https://user:secret@otel.example"
            )
        )

    monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
    monkeypatch.setenv(
        "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT",
        "https://otel.example/v1/traces",
    )
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_METRICS_ENDPOINT", raising=False)
    with pytest.raises(ObservabilityError, match="endpoints are incomplete"):
        resolve_observability_endpoints(
            SimpleNamespace(observability_otlp_endpoint="")
        )


def test_observability_status_is_dormant_when_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("ash.observability._module_available", lambda _name: False)
    status = observability_status(
        SimpleNamespace(
            observability_enabled=False,
            observability_sample_rate=0.25,
        )
    )

    assert status.enabled is False
    assert status.available is False
    assert status.traces_endpoint == ""
    assert status.metrics_endpoint == ""
    assert status.sample_rate == 0.25
    assert status.content_capture is False


def test_genai_provider_names_follow_semantic_conventions() -> None:
    assert _gen_ai_provider_name("google") == "gcp.gemini"
    assert _gen_ai_provider_name("mistral") == "mistral_ai"
    assert _gen_ai_provider_name("xai") == "x_ai"
    assert _gen_ai_provider_name("openai") == "openai"
    assert _gen_ai_provider_name("custom-gateway") == "custom-gateway"


def test_observer_exports_content_free_correlated_spans_and_metrics() -> None:
    tracer_provider = _TracerProvider()
    meter_provider = _MeterProvider()
    closed: list[bool] = []
    observer = OpenTelemetryEventObserver(
        tracer_provider=tracer_provider,
        meter_provider=meter_provider,
        close_callback=lambda: closed.append(True),
    )
    observer._trace = _Trace()  # type: ignore[assignment]

    observer.on_event(_event("turn.started"))
    observer.on_event(
        _event(
            "model.request.started",
            operation_id="request-1",
            provider="google",
            model="gemini-test",
            attempt=1,
            message_count=4,
            tool_count=1,
            native_tools=True,
            prompt="SECRET-PROMPT",
        )
    )
    observer.on_event(
        _event(
            "tool.requested",
            operation_id="call-1",
            tool="read_file",
            arguments={"path": "SECRET-PATH"},
        )
    )
    observer.on_event(
        _event(
            "tool.completed",
            operation_id="call-1",
            tool="read_file",
            success=True,
            output="SECRET-TOOL-RESULT",
        )
    )
    observer.on_event(
        _event(
            "model.request.completed",
            operation_id="request-1",
            provider="google",
            model="gemini-test",
            prompt_tokens=11,
            completion_tokens=7,
            cache_read_tokens=3,
            cache_write_tokens=2,
            response="SECRET-RESPONSE",
        )
    )
    observer.on_event(_event("context.usage", current=123, maximum=1000))
    observer.on_event(_event("turn.completed", response="SECRET-FINAL"))

    spans = tracer_provider.tracer.spans
    assert [span.name for span in spans] == [
        "invoke_agent ash",
        "chat gemini-test",
        "execute_tool read_file",
    ]
    turn, model, tool = spans
    assert turn.attributes["ash.session.id"] == "session-1"
    assert turn.attributes["ash.turn.id"] == "turn-1"
    assert model.context == ("parent", turn)
    assert tool.context == ("parent", turn)
    assert model.attributes["gen_ai.provider.name"] == "gcp.gemini"
    assert model.attributes["ash.operation.id"] == "request-1"
    assert tool.attributes["ash.operation.id"] == "call-1"
    assert all(span.ended for span in spans)

    exported = repr(
        {
            "spans": [
                {"name": span.name, "attributes": span.attributes}
                for span in spans
            ],
            "metrics": {
                name: instrument.points
                for name, instrument in meter_provider.meter.instruments.items()
            },
        }
    )
    assert "SECRET-" not in exported
    assert meter_provider.meter.instruments["ash.model.tokens"].points
    assert meter_provider.meter.instruments["ash.context.tokens"].points == [
        (123.0, {"ash.context.maximum_tokens": 1000})
    ]

    observer.close()
    observer.close()
    assert closed == [True]


def test_observer_records_error_type_without_error_message() -> None:
    tracer_provider = _TracerProvider()
    meter_provider = _MeterProvider()
    observer = OpenTelemetryEventObserver(
        tracer_provider=tracer_provider,
        meter_provider=meter_provider,
    )
    observer._trace = _Trace()  # type: ignore[assignment]

    observer.on_event(_event("turn.started"))
    observer.on_event(
        _event(
            "model.request.started",
            operation_id="request-1",
            provider="openai",
            model="gpt-test",
            attempt=1,
        )
    )
    observer.on_event(
        _event(
            "model.request.error",
            operation_id="request-1",
            provider="openai",
            model="gpt-test",
            error_type="TimeoutError",
            error="SECRET provider failure",
        )
    )

    model = tracer_provider.tracer.spans[-1]
    assert model.attributes["error.type"] == "TimeoutError"
    assert "SECRET" not in repr(model.attributes)
    assert model.status is not None


def test_goal_step_completion_closes_its_turn_span() -> None:
    tracer_provider = _TracerProvider()
    meter_provider = _MeterProvider()
    observer = OpenTelemetryEventObserver(
        tracer_provider=tracer_provider,
        meter_provider=meter_provider,
    )
    observer._trace = _Trace()  # type: ignore[assignment]

    observer.on_event(_event("turn.started"))
    observer.on_event(_event("goal.step.completed"))

    (turn,) = tracer_provider.tracer.spans
    assert turn.ended is True
    assert meter_provider.meter.instruments["ash.turns"].points == [
        (1.0, {"outcome": "success"})
    ]


def test_real_otlp_http_export_is_content_free() -> None:
    received: list[tuple[str, bytes]] = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802 - stdlib handler API
            length = int(self.headers.get("Content-Length", "0"))
            body = self.rfile.read(length)
            received.append((self.path, body))
            self.send_response(200)
            self.send_header("Content-Type", "application/x-protobuf")
            self.end_headers()

        def log_message(self, _format: str, *_args) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    endpoint = f"http://127.0.0.1:{server.server_port}"
    observer = build_observability_observer(
        SimpleNamespace(
            observability_enabled=True,
            observability_otlp_endpoint=endpoint,
            observability_sample_rate=1.0,
            observability_export_interval_seconds=60.0,
            observability_export_timeout_seconds=2.0,
        )
    )
    assert observer is not None
    try:
        observer.on_event(_event("turn.started"))
        observer.on_event(
            _event(
                "model.request.started",
                operation_id="request-1",
                provider="openai",
                model="gpt-test",
                attempt=1,
                message_count=1,
                tool_count=0,
                native_tools=True,
                prompt="SECRET-PROMPT",
            )
        )
        observer.on_event(
            _event(
                "model.request.completed",
                operation_id="request-1",
                provider="openai",
                model="gpt-test",
                prompt_tokens=12,
                completion_tokens=4,
                cache_read_tokens=0,
                cache_write_tokens=0,
                response="SECRET-RESPONSE",
            )
        )
        observer.on_event(_event("turn.completed", response="SECRET-FINAL"))
        observer.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)

    paths = {path for path, _body in received}
    assert "/v1/traces" in paths
    assert "/v1/metrics" in paths
    payload = b"".join(body for _path, body in received)
    assert b"SECRET-" not in payload
    assert b"gpt-test" in payload
    assert b"ash" in payload


def test_model_span_records_actual_response_identity_after_failover() -> None:
    tracer_provider = _TracerProvider()
    meter_provider = _MeterProvider()
    observer = OpenTelemetryEventObserver(
        tracer_provider=tracer_provider,
        meter_provider=meter_provider,
    )
    observer._trace = _Trace()  # type: ignore[assignment]

    observer.on_event(
        _event(
            "model.request.started",
            operation_id="request-1",
            provider="anthropic",
            model="anthropic/primary",
            attempt=1,
        )
    )
    observer.on_event(
        _event(
            "model.request.completed",
            operation_id="request-1",
            provider="openai",
            model="openai/backup",
            prompt_tokens=10,
            completion_tokens=2,
        )
    )

    (span,) = tracer_provider.tracer.spans
    assert span.attributes["gen_ai.request.model"] == "anthropic/primary"
    assert span.attributes["gen_ai.provider.name"] == "openai"
    assert span.attributes["gen_ai.response.model"] == "openai/backup"
