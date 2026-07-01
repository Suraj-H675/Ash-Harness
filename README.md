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

Lightweight commands such as `ash --version` and `ash --help` lazy-load the
runtime stack; provider SDKs, the agent loop, repository parser, and TUI are
loaded only when a command needs them. Installed-wheel startup is covered by a
sub-second regression test while preserving historical `ash.tools.*` imports.

The default install includes the terminal harness, coding tools, repository
map, and supported API/local providers. Install optional features explicitly:

```bash
pip install 'ash[server]' # authenticated HTTP/SSE API
pip install 'ash[vector]' # ChromaDB and ONNX semantic memory
```

For development:

```bash
uv sync --group dev --extra server
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
ASH_SERVER_TOKEN=change-me-long-token ash serve # requires ash[server]
ash storage check --json         # read-only session DB integrity check
ash storage backup               # consistent timestamped backup
ash storage restore BACKUP --yes # validated, non-destructive restore
ash permissions status --json    # inspect effective project rules
ash permissions deny write_file --exact 'file_path=".env"'
ash permissions allow run_command --command-prefix pytest
ash sandbox status --json        # inspect effective command isolation
ash sandbox build                # explicitly build the optional baseline image
ash agents branches              # inspect isolated worker branches
ash agents apply ash-agent/ID    # cherry-pick a worker after review
ash agents discard ash-agent/ID --yes
ash extensions install ./my-plugin # local directory only; validates before install
ash extensions plugins --json      # inspect enabled and disabled plugins
ash extensions disable my-plugin
ash extensions enable my-plugin
ash extensions uninstall my-plugin --yes
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

Provider-backed subagents run bounded Ash loops with role-specific tools.
Researchers and reviewers are read-only; coders receive scoped edit tools;
testers receive shell execution only when a full OS sandbox is active. Coder
and tester roles default to locked Git worktrees and retain committed changes
on `ash-agent/*` branches without modifying the lead worktree. The lead must be
clean when a worktree is created or applied. Use `/agents`, `/agents stop ID`,
and `/agents resume ID` for live status and lifecycle control; queued steering
messages are consumed at model-iteration boundaries and marked delivered.

Plugins are self-contained local directories with a root `plugin.json`.
Manifests may declare path-based `skills`, `commands`, `agents`, `hooks`, and
`mcpServers`; omitted fields use the conventional `skills/`, `commands/`,
`agents/`, `hooks/hooks.json`, and `.mcp.json` locations. Plugin skills,
commands, agents, and MCP servers are namespaced to prevent collisions.
Dependencies are other installed plugins with optional PEP 440 version
constraints. Installation rejects links, path traversal, malformed or
oversized components, missing enabled dependencies, and invalid replacement
content before changing the active version. `/plugins` manages local plugins
inside a session and `/reload-plugins` atomically refreshes commands,
completion, skills, agents, hooks, and MCP servers without restarting Ash.
Project plugins remain disabled until their workspace is trusted.

Project-controlled config and extensions are disabled until the workspace is trusted.
API keys are stored in `~/.ash/.env` with restricted permissions. Custom
provider metadata is stored separately in `~/.ash/ash.toml`.
Network fetches use the `web_fetch` tool and require normal tool approval;
private, loopback, reserved, non-HTTP, oversized, and unsupported-content URLs
are refused before request content reaches the model.

Workspace file reads, attachments, writes, exact edits, and patches reject
symlink/junction mutation paths. On POSIX, Ash walks from an open workspace
descriptor with no-follow operations and performs atomic directory-anchored
writes; the cross-platform fallback revalidates path identity and content
before replacement. Concurrent no-overwrite creation and stale edits fail
without clobbering the other process's data.

Shell commands use a fail-closed workspace sandbox when one is available:
Bubblewrap on Linux, `sandbox-exec` on macOS, or a verified
`ash-sandbox:latest` Docker image (including Docker Desktop on Windows).
Network access is disabled inside isolated commands. Without one of those
backends, Ash clearly reports direct execution and keeps shell actions behind
the permission policy; `auto_approve` is refused unless
`ASH_ALLOW_UNSAFE_AUTO_APPROVE=true` is explicitly set. `/sandbox` and
`ash doctor` report the effective filesystem, network, backend readiness, and
fail-closed policy.

Sandbox selection is user-owned configuration and cannot be weakened by a
project `.ash/config.toml`:

```toml
sandbox_backend = "auto" # auto, native, docker, or direct
sandbox_network = false
sandbox_docker_image = "ash-sandbox:latest"
```

`auto` prefers Bubblewrap on Linux or `sandbox-exec` on macOS, then a locally
available Docker image. Windows uses the Docker fallback. `ash sandbox build`
builds the packaged Python, Node.js, Go, Rust, Java, and native-build baseline;
it is an explicit operation and Ash never downloads or builds it at startup.

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

Configuration resolves in this order, from highest to lowest priority:

1. Command-line overrides
2. Process environment (`ASH_*`)
3. Trusted `.ash/config.toml` files from the repository root to the current directory
4. User configuration in `~/.ash/ash.toml`
5. User credentials/settings in `~/.ash/.env`
6. Built-in defaults

Run `ash config explain` or `ash config explain --json` to inspect the selected
source for every field. Project configuration may tune model/runtime and TUI
behavior, but cannot set credentials, custom provider endpoints, workspace or
database paths, MCP servers, permission weakening, or outbound-domain policy.
Use `ash trust add <path>` to enable project layers and `ash trust remove
<path>` to disable them.

When setup finds the original project-root `ash.toml` format, it migrates all
recognized values without replacing newer user settings. Source and changed
destination files receive verified mode-0600 backups under `~/.ash/backups`;
an exact-content migration record prevents repeated prompts while retaining
recovery evidence.

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

Provider requests use one harness-level resilience policy. Only transient
connection, timeout, 408/409/425/429, and 5xx failures before the first stream
chunk are retried; authentication, validation, quota-exhaustion, and partial
stream failures are never replayed. `Retry-After` is honored within the
configured cap, otherwise Ash uses exponential backoff with jitter:

```toml
provider_max_attempts = 3
provider_retry_base_delay = 0.5
provider_retry_max_delay = 8.0
provider_circuit_failure_threshold = 5
provider_circuit_cooldown_seconds = 30.0
```

Exhausted transient requests open a provider circuit after the configured
threshold. `/status` reports circuit state; cooldown permits a half-open probe,
and a successful request resets it. Retry reasons are redacted in logs and
structured events.

See [the production parity checklist](docs/PRODUCTION_HARNESS_PARITY.md) for
implemented and remaining release requirements.
