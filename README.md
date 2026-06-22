# Ash

Ash is a terminal-native coding agent harness for Linux, macOS, and Windows.
It supports API-key providers, custom OpenAI-compatible endpoints, and local
Ollama models. OAuth and subscription-based authentication are intentionally
out of scope.

## Install

Python 3.12 or newer is required.

```bash
pipx install .
ash setup
ash doctor
ash
```

For development:

```bash
uv sync --extra dev
uv run ash --help
uv run pytest -q
```

## Core Usage

```bash
ash                              # interactive terminal session
ash -p "inspect and test this repo"  # one-shot prompt
ash -p "summarize" --output-format json
ash --session SESSION_ID         # resume a durable session
ash mcp add local -- python server.py
ash trust add .                  # allow project ASH.md, skills, hooks, MCP
```

Inside the terminal, `/help` lists session, context, model, diff, permissions,
sandbox, skills, plugins, export, and diagnostic commands. `Alt+Enter` or
`Ctrl+J` inserts a newline; prompt history is stored under `~/.ash`.

Project-controlled extensions are disabled until the workspace is trusted.
API keys are stored in `~/.ash/.env` with restricted permissions. Custom
provider metadata is stored separately in `~/.ash/ash.toml`.

## Configuration

The selected model uses `provider/model` form, for example:

```text
anthropic/claude-sonnet-4-5
openai/gpt-5
ollama/qwen3-coder
```

Run `ash setup` to configure a provider and `ash doctor --connect` to test its
endpoint. Run `ash doctor --json` for machine-readable diagnostics.

See [the production parity checklist](docs/PRODUCTION_HARNESS_PARITY.md) for
implemented and remaining release requirements.
