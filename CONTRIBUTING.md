# Contributing to Ash

Ash is an AI harness with security-sensitive filesystem, process, network, and
credential boundaries. Changes must preserve real runtime behavior, not only
unit-test expectations.

## Development Setup

Python 3.11 or newer and `uv` are required.

```bash
uv sync --group dev --extra server --extra acp --extra a2a
uv run ash --help
```

Implementation code belongs under `src/ash`; tests mirror the affected layer
under `tests/unit`, `tests/integration`, or `tests/e2e`. Import project code
through `ash.*`. Do not add new top-level Python packages.

## Verification

Run the checks that match the change, then run the complete local gate before
committing:

```bash
uv run ruff check src tests
uv run mypy src/ash
uv run pytest -q
uv build
```

Packaging changes must also be tested by installing the built wheel into a
clean environment and running `tests/packaging/smoke_minimal_install.py` from
outside the repository. Tests involving optional external services must remain
explicitly opt-in and document their prerequisites.

## Change Standards

- Keep changes scoped and preserve established public `ash.*` APIs.
- Treat model output, repository content, plugins, MCP servers, hooks, and
  language servers as untrusted input.
- Keep secrets out of logs, events, persisted transcripts, fixtures, and
  commits.
- Add regression coverage for failure paths and verify the user-facing flow.
- Update maintained documentation when behavior or configuration changes.
- Never commit local comparison repositories under `ref/`.

Use focused commit messages without generated co-author trailers.
