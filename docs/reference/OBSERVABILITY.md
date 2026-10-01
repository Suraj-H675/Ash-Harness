# Observability

Ash provides optional, content-free OpenTelemetry tracing and metrics for
operators who explicitly enable outbound telemetry.

## Safety and ownership

- Observability is **off by default**.
- The collector configuration is user-owned. Project configuration cannot
  enable telemetry, change its endpoint, or change its sampling policy.
- The runtime sends only an allowlisted operational projection of Ash events to
  observers. Prompts, model responses, tool arguments/results, file paths,
  denial/error text, and credential material never cross that observer
  boundary.
- Runtime exporter failures are fail-open for coding work: an exporter failure
  disables that observer for the current runtime rather than crashing the turn.

## Installation

The OpenTelemetry SDK/exporter is an optional package extra named
`observability`. Include that extra when installing Ash from the package
source you use. Repository development environments can install it with:

```bash
uv sync --extra observability
```

## Configuration

In the user-owned Ash configuration:

```toml
observability_enabled = true
observability_otlp_endpoint = "http://127.0.0.1:4318"
observability_sample_rate = 1.0
observability_export_interval_seconds = 15
observability_export_timeout_seconds = 5
```

Equivalent `ASH_OBSERVABILITY_*` environment variables are supported.

When `observability_otlp_endpoint` is empty, an explicitly enabled runtime may
use the standard OpenTelemetry environment variables:

- `OTEL_EXPORTER_OTLP_ENDPOINT`
- `OTEL_EXPORTER_OTLP_TRACES_ENDPOINT`
- `OTEL_EXPORTER_OTLP_METRICS_ENDPOINT`

A base endpoint is resolved to `/v1/traces` and `/v1/metrics` for OTLP/HTTP.
Collector URLs must be credential-free HTTP(S) URLs. Authentication headers, if
required by the collector, should be supplied through the standard
OpenTelemetry exporter environment rather than embedded in the URL.

## Signals

Ash exports:

- agent-turn spans and turn outcome/duration metrics;
- model-request spans with provider/model, attempt, token usage, outcome, and
  duration;
- tool spans with tool name, outcome, and duration;
- provider retry and circuit-open counters;
- context-token usage;
- input/output/cache token counters.

Internal session, turn, and operation IDs are included on spans so exported
traces can be correlated with Ash's local runtime-event/session records.

Ash deliberately does **not** enable GenAI prompt/completion content capture.

## Verify

```bash
ash setup status
ash doctor
```

`ash doctor` reports whether observability is off, missing its optional
runtime, missing an endpoint, or ready. It does not print collector URLs or
credential headers.
