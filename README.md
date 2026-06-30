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
ash permissions status --json    # inspect effective project rules
ash permissions deny write_file --exact 'file_path=".env"'
ash permissions allow run_command --command-prefix pytest
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
Tool approvals support once-only (`y`), exact session scope (`s`), broad
tool/session scope (`a`), persisted project scope (`p`), persisted scoped
denial (`x`), and an explicit argv-prefix project rule for simple commands
(`c`). Compound commands, redirection, and command substitution cannot match a
command-prefix rule and continue to require exact approval. Persisted
allow/ask/deny rules use stable IDs and can be listed or removed with
`ash permissions`; deny rules take precedence over ask and allow rules.
Use `ash plans list`, `ash plans show <sprint-id>`, and
`ash plans update <sprint-id> <item> <status>` to inspect or update persisted
checklists outside the REPL.
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
The setup wizard discovers models through the provider endpoint before writing
credentials, supports `b` to return and `c` to cancel, and offers explicit
retry or save-unverified choices when an endpoint is unavailable. API keys use
hidden input and related key/base/model settings are committed atomically.
Ollama setup discovers installed models and gives `ollama serve`/`ollama pull`
guidance without starting a download. In scripts, `ash setup
--non-interactive` validates an existing environment configuration without
prompting or making a network request.

Terminal behavior is configurable with `ASH_INPUT_MODE=vi`,
`ASH_NO_COLOR=true`, `ASH_REDUCED_MOTION=true`, and
`ASH_SHOW_TOKEN_METER=true`. Structured keybindings and sprint planning can be
set in `~/.ash/ash.toml`.

Interactive sessions use the responsive transcript viewport by default. Use
`PageUp`/`PageDown` to inspect history and `End` to resume following live
output. Set `ASH_TUI_MODE=inline` when native terminal scrollback is preferred
or a terminal does not support full-screen applications reliably.

Set `ASH_SCREEN_READER_MODE=true` for linear, non-rewriting terminal output.
This mode forces inline rendering, no color, reduced motion, and no token bar;
it also disables dynamic completion, autosuggestions, and the bottom toolbar
while retaining cancellable cross-platform prompt input and command history.

Desktop notifications are disabled by default. Enable terminal-aware desktop
notifications with `ASH_NOTIFICATION_METHOD=auto`, or choose `osc9` or `bel`
explicitly. The equivalent TOML configuration is:

```toml
notification_method = "auto" # off, auto, osc9, or bel
notification_events = ["turn_complete", "approval_required"]
notification_include_preview = false
```

`auto` uses OSC 9 for detected Ghostty, iTerm2, Kitty, Warp, and WezTerm
sessions and falls back to the terminal bell elsewhere. Output is TTY-only,
control characters are removed, and optional response previews are bounded.
Inside tmux, enable `allow-passthrough` in tmux for OSC notifications to reach
the host terminal.

First-party Anthropic and OpenAI prompt caching is enabled by default. It can
be configured in `~/.ash/ash.toml`:

```toml
prompt_cache_enabled = true
prompt_cache_retention = "memory" # memory or extended
```

`memory` selects Anthropic's default 5-minute cache or OpenAI's in-memory
cache. `extended` selects Anthropic's 1-hour TTL or OpenAI's 24-hour retention
where the chosen model supports it. Provider-specific cache controls are not
sent to custom OpenAI-compatible endpoints or local models. Cache reads,
writes, hit rate, and configured costs are visible through `/status`,
`/context`, JSON output, the SDK, HTTP, and JSON-RPC.

See [the production parity checklist](docs/PRODUCTION_HARNESS_PARITY.md) for
implemented and remaining release requirements.
