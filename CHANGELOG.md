# Changelog

All notable changes to Ash are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and versioning follows
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Fixed
- Preserved case-correct Windows system environment variables in scrubbed
  command environments and bounded CI tests at 120 seconds with verbose test
  names so platform hangs identify the exact case instead of blocking the
  workflow.
- Restored cross-platform CI correctness for Windows-only type analysis and a
  Linux-specific Bubblewrap regression test.

### Added
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
