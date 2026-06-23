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
ash --ci -p "inspect only"          # no prompts; stream-json by default
ash -p "summarize" --output-format json
ash --session SESSION_ID         # resume a durable session
ash mcp add local -- python server.py
ash trust add .                  # allow project ASH.md, skills, hooks, MCP
ash update                       # explicitly check GitHub releases
ASH_SERVER_TOKEN=change-me-long-token ash serve
ash storage check --json         # read-only session DB integrity check
ash storage backup               # consistent timestamped backup
ash storage restore BACKUP --yes # validated, non-destructive restore
```

The authenticated server exposes synchronous turns at `/v1/turn` and live
SSE turn events at `/v1/turn/stream`; non-loopback binding requires
`--allow-remote`.

Inside the terminal, `/help` lists session, context, model, diff, review,
permissions, sandbox, skills, plugins, export, and diagnostic commands.
`/review` supports worktree, staged, commit, and branch-versus-base scopes.
`/plan on` enables editable sprint plans for multi-step requests; type `e` at
the plan prompt to revise the generated contract in `$VISUAL` or `$EDITOR`,
then `y` to approve and execute it.
`Alt+Enter` or `Ctrl+J` inserts a newline; prompt history is stored under
`~/.ash`. Type `@path` or `@"path with spaces"` to attach bounded workspace
text or a directory listing to a prompt; secret, binary, oversized, and
out-of-workspace paths are rejected.

Project-controlled extensions are disabled until the workspace is trusted.
API keys are stored in `~/.ash/.env` with restricted permissions. Custom
provider metadata is stored separately in `~/.ash/ash.toml`.
Network fetches use the `web_fetch` tool and require normal tool approval;
private, loopback, reserved, non-HTTP, oversized, and unsupported-content URLs
are refused before request content reaches the model.

## Configuration

The selected model uses `provider/model` form, for example:

```text
anthropic/claude-sonnet-4-5
openai/gpt-5
ollama/qwen3-coder
```

Run `ash setup` to configure a provider and `ash doctor --connect` to test its
endpoint. Run `ash doctor --json` for machine-readable diagnostics.

Terminal behavior is configurable with `ASH_INPUT_MODE=vi`,
`ASH_NO_COLOR=true`, `ASH_REDUCED_MOTION=true`, and
`ASH_SHOW_TOKEN_METER=true`. Structured keybindings and sprint planning can be
set in `~/.ash/ash.toml`.

See [the production parity checklist](docs/PRODUCTION_HARNESS_PARITY.md) for
implemented and remaining release requirements.
