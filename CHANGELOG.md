# Changelog

All notable changes to Ash are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and versioning follows
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Fixed
- Routed interactive REPL command and turn failures through the shared error
  taxonomy so users receive stable categories and actionable remedies.
- Recorded subagent token and cost usage in one durable transaction,
  restored explicit pricing precedence over built-in defaults, and exposed
  background graph cost aggregates.
- Prevented approved-sprint input substitution from applying to turns that
  did not create a new sprint.
- Preserved case-correct Windows system environment variables in scrubbed
  command environments and bounded CI tests at 120 seconds with verbose test
  names so platform hangs identify the exact case instead of blocking the
  workflow.
- Restored cross-platform CI correctness for Windows-only type analysis and a
  Linux-specific Bubblewrap regression test.

### Added
- Tool-specific permission grammar with safe `path_prefix` workspace matching
  and hostname-based `domain` matching for URLs/domain arguments, exposed via
  `ash permissions`.
- Structured provider-input provenance reporting through `/context
  --provenance`, including source, trust class, token/truncation accounting,
  content digest, and untrusted-content policy metadata.
- Live dynamic model discovery through `/models --refresh`, rendering endpoint
  catalogs alongside static/configured models with explicit failure handling.
- Dynamic Ollama tool-capability detection from model metadata, cached per
  provider instance, with context-length reporting and fail-closed local fallback.
- `ash ollama pull` executes local model downloads through strict validation, a
  scrubbed environment, bounded output, timeout cleanup, and process-tree
  termination.
- Extended lifecycle hooks for context compaction, runtime configuration
  changes, and permission rule/mode changes with redacted bounded payloads and
  event-loop-safe dispatch.
- Submission-time expansion for `@symbol` and `@mcp` mentions through the
  bounded attachment pipeline, including fail-closed resolution, provenance
  marking, and token-budget enforcement.
- Extended interactive `@` completion for fuzzy workspace symbols and live
  connected MCP resources.
- Explicit prompt-injection isolation: every provider request includes an
  untrusted-content boundary, and tool responses are labeled as data with an
  embedded-instruction warning.
- Structured bounded diagnostics for compiler, lint, and pytest-style failures
  returned by `run_command`, with model-visible path, line, symbol, code, and
  message fields.
- Model catalog improvements: configured custom-provider models appear in
  `/models`, and `/model` displays known capabilities plus context/output
  budgets before switching.
- Deny-with-feedback approval flow that records bounded user feedback in the
  durable tool-call ledger and returns it to the model for corrective retry.
- Selectable approval diff layouts with persisted `unified`/`side-by-side`
  configuration, bounded side-by-side rendering, interactive controller wiring,
  and CLI switching through `ash diff-mode`.
- Selectable dark/light terminal themes with validated configuration,
  project-layer selection, Rich panel styling, prompt-toolkit viewport styling,
  and explicit screen-reader fallback behavior.
- Local-only aggregate model usage metrics through `ash metrics`, with no
  network telemetry.
- Plugin manifest schema v2 with explicit compatibility policy: schema 1 is
  accepted with a deprecation notice, schema 0 and future schemas fail closed.
- Searchable session summaries: session lookup now matches redacted context
  summaries and exposes them in summary metadata.
- Opt-in automatic project-memory indexing with bounded file counts, file
  sizes, trusted-project gating, deterministic shutdown, and exclusion-aware
  selection.
- `ash storage debug-bundle` produces a bounded, redacted structured JSON
  diagnostics file covering runtime, configuration, and database health.
- Tamper-evident permission-mode decision auditing, including schema v11
  migration and regression coverage for chained non-tool decisions.
- Bounded, redacted semantic-memory export for in-memory and FTS-backed
  indexes through `/memory export`.
- Full interactive `/agents --full` status view with durable task identity,
  token budgets, and USD cost usage alongside live worker state.
- Durable graph-consolidation artifacts for delegated agent results, with
  inspectable path evidence and conflict records.
- Regression coverage proving independent read-only tool calls execute
  concurrently, preserve deterministic result order, and preserve durable
  dispatch intent under cancellation.
- Evidence-linked result consolidation for foreground agent DAGs, including
  successful/failed task summaries, path-scoped evidence, and conflict
  detection.
- Durable graph-wide USD cost ceilings for parallel agent DAGs, atomic
  exceedance handling, aggregate cost inspection, and provider-backed
  subagent cost reporting.
- Live persisted sprint-plan state in runtime model context, including
  checklist progress, notes, and bounded budget accounting.
- Built-in USD-per-million-token pricing defaults for current major
  Anthropic, OpenAI, DeepSeek, and Groq models, while preserving explicit
  user overrides.
- Durable graph-wide token ceilings for parallel agent DAGs, atomic usage
  accounting across tasks, terminal failure on overrun, and aggregate budget
  inspection.
- Managed enterprise permission policy from platform administrator files.
  Managed deny and ask rules cannot be overridden by user, project, or session
  rules, while invalid policy fails closed at startup.

### Fixed
- Unified provider readiness: setup, doctor, and runtime construction resolve
  the same endpoint and authentication path for every provider.
- `ash doctor --connect` validates the selected model against the exact
  configured endpoint rather than probing vendor defaults.
- Custom OpenAI-compatible endpoints explicitly declare `auth_mode = "bearer"`
  or `auth_mode = "none"`; anonymous mode never inherits `OPENAI_API_KEY`.
- Fresh interactive runs default to provider setup instead of entering an
  unconfigured REPL.

### Security
- Prevented credential leakage by ensuring doctor probes match the runtime
  base URL exactly; API keys are never sent to unintended endpoints.

### Packaging
- Added package license metadata, project links, classifiers, and a changelog.
- Fixed duplicate `project.urls` tables so the distribution builds with modern
  `setuptools`; expanded CI across Python 3.11 and 3.12.
