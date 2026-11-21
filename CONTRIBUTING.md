# Contributing to Ash

Ash is an AI harness with security-sensitive filesystem, process, network, and
credential boundaries. Changes must preserve real runtime behavior, not only
unit-test expectations.

## Development Setup

Python 3.12 through 3.14 and **uv 0.12.17** are required for Ash development.
The exact uv version is enforced by `pyproject.toml`; a different installed
version will refuse to run project commands, even if it is newer. **Linux is
the primary target**, with Arch Linux as the main local development environment;
hosted CI exercises Ubuntu. **macOS remains a supported secondary native host**
with important regression and packaging checks. On Windows, WSL2 is an
**experimental, not-yet-verified** way to reuse the Linux implementation;
native Windows is deferred and unsupported.

With uv 0.12.17 already on your `PATH`:

```bash
uv sync --locked --group dev --extra server --extra acp --extra a2a
uv run --locked ash --help
```

If your installed uv has a different version, use `uvx` to invoke the pinned
version **without replacing your system uv**:

```bash
uvx --from 'uv==0.12.17' uv sync --locked --group dev --extra server --extra acp --extra a2a
uvx --from 'uv==0.12.17' uv run --locked ash --help
```

Use the same `uvx --from 'uv==0.12.17' uv` prefix for verification and build
commands below while your system uv differs. `uvx` acquires the pinned tool if
it is not cached; `uvx --offline --from 'uv==0.12.17' uv` also works if that
version is already cached. Offline mode cannot fetch a missing tool or package.
Do not loosen the repository's version pin to work around a local mismatch.

Implementation code belongs under `src/ash`; tests mirror the affected layer
under `tests/unit`, `tests/integration`, or `tests/e2e`. Import project code
through `ash.*`. Do not add new top-level Python packages.

## Verification

Prioritize real Linux coding workflows, permissions, sandboxing, interruption,
recovery, and meaningful performance evaluations. Retain existing macOS checks
for shared contracts and supported behavior; do not claim WSL2 verification
from Linux CI alone. Run checks proportionate to the change, and the full local
gate for major or cross-cutting work:

```bash
uv run ruff check src tests
uv run mypy src/ash
uv run pytest -q
uv build --sdist --out-dir dist --clear
uv build --wheel "$(printf '%s\n' dist/*.tar.gz)" --out-dir dist
```

Build release wheels from the freshly generated sdist rather than directly
from the checkout. Setuptools reuses its ignored local `build/` directory, so a
direct source-tree wheel build can otherwise retain files that were deleted
from `src/ash`. Packaging changes must also be tested by installing the built
wheel into a clean environment and running
`tests/packaging/smoke_minimal_install.py` from outside the repository. Tests
involving optional external services must remain explicitly opt-in and
document their prerequisites.

For real coding-agent performance, use the local-model, independently scored
[coding evaluation](docs/guides/CODING_EVALUATION.md). It runs disposable
workspaces and never uses personal inference credentials. Treat a small number
of passing tasks as a smoke baseline, not a statistical reliability claim.

The development test group includes Hypothesis for generated durable-state
sequences and pyte for real-terminal screen assertions. The Toxiproxy OpenAI
transport test uses only a synthetic loopback server and is opt-in. Install an
official Toxiproxy server binary separately, verify its release checksum, then
run:

```bash
ASH_RUN_TOXIPROXY_TESTS=1 \
ASH_TOXIPROXY_SERVER=/absolute/path/to/toxiproxy-server \
uv run --locked pytest -q tests/e2e/test_toxiproxy_openai_provider.py
```

This test starts and stops its own proxy; it does not require Docker or a paid
model provider. It is skipped in ordinary test runs.

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
