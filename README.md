# Ash

Ash is a terminal-native coding agent harness for Linux, macOS, and Windows.
It supports API-key providers, custom OpenAI-compatible endpoints, and local
Ollama models. Subscription-based model-provider login is intentionally out of
scope; protected remote MCP servers support the MCP OAuth 2.1 flow.

## Install

Python 3.11 or newer is required. Install the latest repository version with
one command:

```bash
pipx install 'git+https://github.com/Suraj-H675/Ash-Harness.git'
```

Then configure and verify the runtime:

```bash
ash setup
ash doctor
ash
```

Lightweight commands such as `ash --version` and `ash --help` lazy-load the
runtime stack; provider SDKs, the agent loop, repository parser, and TUI are
loaded only when a command needs them. Installed-wheel startup is covered by a
sub-second regression test. Public Python APIs use the canonical `ash.*`
namespace.

The default install includes the terminal harness, coding tools, repository
map, and supported API/local providers. Install optional features explicitly:

```bash
pipx install --force 'ash-ai[server] @ git+https://github.com/Suraj-H675/Ash-Harness.git'
pipx install --force 'ash-ai[vector] @ git+https://github.com/Suraj-H675/Ash-Harness.git'
pipx install --force 'ash-ai[acp] @ git+https://github.com/Suraj-H675/Ash-Harness.git'
pipx install --force 'ash-ai[a2a] @ git+https://github.com/Suraj-H675/Ash-Harness.git'
```

Choose the extras needed by one installation, for example
`ash-ai[server,vector]`. The direct Git reference is required until an
`ash-ai` release is published to PyPI; `--force` also upgrades an existing
base `pipx` installation into the selected capability pack.

For development:

```bash
pipx install .
uv sync --group dev
uv run ash --help
uv run pytest -q
```

## Repository Layout

```text
src/ash/           installable harness and public Python package
tests/             unit, integration, end-to-end, and wheel smoke tests
docs/architecture/ current architecture and parity analysis
docs/guides/       operational and workflow guides
docs/reference/    versioned protocol and extension contracts
docs/archive/      historical plans and completed verification reports
```

Start with the [documentation index](docs/README.md) for maintained design and
extension references. Development standards are in
[CONTRIBUTING.md](CONTRIBUTING.md).

## Core Usage

```bash
ash                              # interactive terminal session
ash -p "inspect and test this repo"  # one-shot prompt
ash --ci -p "inspect only"          # no prompts; stream-json by default
ash -p "summarize" --output-format json
ash -c                          # continue this project's latest session
ash -r                          # choose from searchable project sessions
ash -r SESSION_ID_OR_NAME       # resume an exact durable session
ash -c --fork-session           # branch latest history under a new ID
ash sessions tree               # inspect the latest conversation tree
ash sessions tree --session NAME --json
ash --session SESSION_ID        # legacy explicit-ID compatibility
ash mcp add local -- python server.py
ash mcp add remote --transport http --url https://mcp.example/rpc --auth oauth
ash mcp login remote            # explicit browser authorization
ash trust add .                  # allow project instructions, extensions, MCP, LSP
ash update                       # explicitly check GitHub releases
ASH_SERVER_TOKEN=change-me-long-token ash serve # requires ash-ai[server]
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
ash lsp status --json              # inspect servers without starting them
ash lsp diagnostics src/app.py
ash lsp query definition src/app.py --line 12 --character 8
ash lsp query workspaceSymbol --query App
ash cron add nightly --prompt 'Review open risks' --cron '0 2 * * mon-fri' --timezone UTC
ash cron status                  # includes worker liveness
ash cron worker                  # execute due schedules for this workspace
```

The authenticated server exposes synchronous turns at `/v1/turn`, live SSE
turn events at `/v1/turn/stream`, and durable branching at
`/v1/sessions/{session_id}/fork` and `/v1/sessions/{session_id}/tree`;
non-loopback binding requires `--allow-remote`. See
[durable session branching](docs/guides/SESSION_BRANCHING.md) for integrity and
retention behavior.

### Editor integration (ACP v1)

Install the capability pack, configure a provider once, and verify the agent:

```bash
pipx install --force 'ash-ai[acp] @ git+https://github.com/Suraj-H675/Ash-Harness.git'
ash setup
ash acp --check
```

Point an ACP-compatible editor's custom-agent command at `ash` with argument
`acp` (for example, Zed accepts that command/argument pair). Ash uses the
editor's workspace as the session boundary and supports new, load, list,
prompt, cancel, close, text/resource-link content, tool progress, usage, and
HTTP/SSE/stdio MCP servers. Durable loads replay messages and redacted tool
activity. Each editor connection is limited to 16 live sessions and one active
turn per session. Image, audio, embedded-resource, extra-directory, ACP-MCP,
mode, fork, and resume capabilities are not advertised until their full
protocol behavior is implemented. Standard output is reserved for ACP JSONL;
diagnostics go to standard error.

### Remote agents (A2A 1.0)

Install and verify the optional capability pack, then expose the current
project on loopback with an operator-owned token:

```bash
pipx install --force 'ash-ai[a2a] @ git+https://github.com/Suraj-H675/Ash-Harness.git'
ash a2a check
export ASH_A2A_TOKEN='replace-with-at-least-16-characters'
ash a2a serve
```

The public Agent Card is at `/.well-known/agent-card.json`; JSON-RPC is at
`/a2a`, and the official HTTP+JSON routes are also mounted under `/a2a`.
Operational routes require the bearer token and are rate-limited. A remote
binding additionally requires `--allow-remote` and an explicit HTTPS
`--public-url`; terminate TLS in a production reverse proxy.

Discover or call another A2A agent with the same out-of-band token convention:

```bash
ash a2a inspect https://agent.example.com
ash a2a send https://agent.example.com 'Review the authentication flow'
ash a2a send https://agent.example.com 'Continue' --context-id CONTEXT --json
```

To let models delegate through `list_remote_agents` and
`delegate_remote_agent`, configure explicit endpoints in `~/.ash/a2a.json` or,
for a trusted workspace, `.ash/a2a.json`:

```json
{
  "agents": {
    "review": {
      "url": "https://review.example.com",
      "description": "Independent code reviewer",
      "token_env": "REVIEW_A2A_TOKEN",
      "timeout_seconds": 300
    }
  }
}
```

Tokens are read from the named environment variable and never stored in the
file or exposed as model arguments. Delegation remains subject to Ash's normal
approval policy. A2A tasks and context-to-Ash session mappings persist in
separate SQLite databases; text streaming, polling, task get/list,
cancellation, and context continuation are supported. Push notifications,
files/data modalities, extended cards, gRPC, and signed-card trust policy are
not advertised.

### Managed language servers (LSP 3.18)

Managed LSP support is included in the base install. Ash never downloads or
installs a language-server binary. It detects installed basedpyright/pyright,
typescript-language-server, gopls, rust-analyzer, clangd, and
lua-language-server processes. A server starts lazily for the nearest project
root and stops with its owning Ash runtime.

The `lsp` tool and `ash lsp` commands support diagnostics, hover, definition,
references, implementation, document/workspace symbols, and call hierarchy.
Input line and character coordinates are 1-based; returned protocol ranges are
0-based. Successful write, replace, and patch tools may append advisory
diagnostics. A language-server failure never changes whether the edit itself
succeeded. Rename and code actions are not currently exposed.

Add custom servers or override detected ones in `~/.ash/lsp.json`. A trusted
workspace may also use `.ash/lsp.json`; project entries take precedence over
user entries. Set `disabled` to remove a detected or inherited server.

```json
{
  "servers": {
    "example": {
      "command": ["example-language-server", "--stdio"],
      "extensions": {".example": "example"},
      "root_markers": ["example.toml", ".git"],
      "env": {"EXAMPLE_MODE": "strict"},
      "initialization_options": {},
      "settings": {"example": {"diagnostics": true}},
      "disabled": false
    }
  }
}
```

Server commands execute directly without a shell and receive a scrubbed
environment plus explicit `env` overrides. They are host processes, not
network-isolated sandboxes. For that reason, project LSP configuration and
workspace `node_modules/.bin` discovery are disabled until `ash trust add` is
run. Ash rejects server-requested workspace edits and discards semantic results
that reference files outside the workspace. Set `lsp_enabled = false` in the
user config, or `ASH_LSP_ENABLED=false`, to disable the capability globally;
trusted project config cannot override that user-owned control.

### Durable automation

Ash can persist one-shot, interval, and five-field cron prompts in SQLite and
execute them through the ordinary trusted runtime:

```bash
cd /path/to/project
ash trust add .
ash cron add dependency-review \
  --prompt 'Review dependency changes, run focused checks, and report risks' \
  --cron '30 9 * * mon-fri' --timezone Asia/Kolkata
ash cron worker
```

Jobs do not run inside the interactive CLI process. Keep one `ash cron worker`
under a process supervisor for each workspace, or invoke `ash cron worker
--once` from an external scheduler. `ash cron status` and `ash doctor` warn
when enabled jobs have no fresh worker heartbeat. Unattended tool calls use the
normal permission rules: read-only calls proceed, persistent allows proceed,
and actions that still require a prompt fail closed. Revoking workspace trust
stops new claims.

See the [durable automation guide](docs/guides/DURABLE_AUTOMATION.md) for
schedule, crash-recovery, DST, supervision, SDK, and cancellation contracts.

Inside an interactive terminal, `/help` opens a full-screen searchable command
reference; redirected input and screen-reader mode keep the linear text output.
It covers session, context, model, diff, review, permissions, sandbox, skills,
plugins, export, and diagnostic commands.
Prefix a command with `!` to run it through the same permission and sandbox
policy as model tool calls while streaming bounded stdout and stderr live.
`@path` attachments are capped at 25% of usable model input context by default;
set `max_attachment_tokens` (or `ASH_MAX_ATTACHMENT_TOKENS`) for an explicit cap.
File reads and edits support UTF-8 plus BOM-tagged UTF-8/16/32 and preserve the
detected encoding during atomic overwrites.
`/resume` opens the project-scoped session picker; type to filter, use arrow
keys to navigate, `Space` to preview, and `Enter` to resume. `/resume NAME`
resolves an exact persisted title, while `/rename TITLE` sets that title.
`/review` supports worktree, staged, commit, and branch-versus-base scopes.
`/diff --turn` shows the latest Ash checkpoint diff and refuses if files changed
after Ash's edit; `/diff --staged` and `/diff path` keep showing Git diffs.
`/rewind COUNT` truncates complete persisted turns at a message boundary.
Add `--files` to restore all checkpointed direct file edits from the removed
turns as well. Combined rewind preflights every file hash, handles repeated
edits newest-first, adjusts persisted token/cost totals, and rolls files back
to their current state if the database update fails. It refuses split-turn
boundaries, post-edit conflicts, incomplete checkpoints, and legacy transcript
records that predate durable turn IDs.
When a resumed session contains a turn interrupted during tool execution, Ash
uses the persisted approved-call intent and per-call checkpoint to compensate
only that in-flight direct file edit. It restores a file only when its hash is
still the recorded post-edit or pre-edit state. Changed/incomplete files and
non-file tools such as commands are never guessed: they are marked as needing
attention, persisted in the recovery report, emitted as `session.recovery`, and
shown by `/status`. A successful recovery is idempotent across another crash.
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
Ash auto-commit scans staged added lines for high-confidence credentials and
private keys, refuses the commit without echoing values, and leaves changes
staged for inspection.
Use `ash plans list`, `ash plans show <sprint-id>`, and
`ash plans update <sprint-id> <item> <status>` to inspect or update persisted
checklists outside the REPL.
`Alt+Enter` or `Ctrl+J` inserts a newline; prompt history is stored under
`~/.ash`. Type `@path` or `@"path with spaces"` to attach bounded workspace
text, a directory listing, or PNG/JPEG/GIF/WebP images to a prompt. Images are
accepted only when the active provider/model advertises vision support, are
bounded to 5 MB each and 10 MB combined, and travel as native Anthropic/OpenAI
image blocks. Session storage retains only path/media/hash descriptors, never
base64 payloads. Secret, unsupported binary, oversized, linked, and
out-of-workspace paths are rejected.

Provider-backed subagents run bounded Ash loops with role-specific tools.
Researchers and reviewers are read-only; coders receive scoped edit tools;
testers receive shell execution only when a full OS sandbox is active. Coder
and tester roles default to locked Git worktrees and retain committed changes
on `ash-agent/*` branches without modifying the lead worktree. The lead must be
clean when a worktree is created or applied. Use `/agents`, `/agents stop ID`,
and `/agents resume ID` for live status and lifecycle control; queued steering
messages are consumed at model-iteration boundaries and marked delivered.
Every provider-backed worker also runs through the durable task contract in
[Agent Tasks v1](docs/reference/AGENT_TASKS_V1.md): cross-process capacity admission,
renewable ownership leases, crash recovery, token/time budgets, dependency
DAGs, cancellation, results, and artifacts are persisted in SQLite. Inspect
them with `ash agents tasks [--state STATE] [--owner ID] [--json]`.
Replay their versioned lifecycle with
`ash agents events [--task ID] [--type TYPE] [--after SEQUENCE] [--json]`.
For multi-stage work, the provider-facing `delegate_agents` tool atomically
submits a dependency graph and automatically runs ready tasks in parallel. The
same workflow is available to embedded callers as
`await client.delegate_agents(goal, tasks, background=False)`.
Dependent workers receive redacted predecessor results as untrusted evidence;
isolated tasks verify and merge retained predecessor branches inside their own
worktrees without changing the lead branch.
Use `ash agents tasks --graph GRAPH_ID` to inspect one graph and
`ash agents cancel GRAPH_ID --yes` to revoke its queued and active work.

Large tool catalogs are deferred automatically above 32 tools. The model uses
`search_tools` to discover and activate exact schemas for plugin, MCP, and
built-in capabilities; set `tool_search_threshold = 0` in config to always send
the full catalog.

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
completion, skills, agents, hooks, executable tools, and MCP servers without
restarting Ash. Executable plugins use the isolated, versioned JSON-RPC stdio
boundary in [Plugin API v1](docs/reference/PLUGIN_API_V1.md); they are lazy-started,
receive no ambient secrets or network access, and cannot bypass ordinary tool
approval, audit, hooks, or dry-run policy.
Project plugins remain disabled until their workspace is trusted.
Command hooks use the versioned, bounded lifecycle contract documented in
[Hook Contract v1](docs/reference/HOOKS_V1.md). Critical `pre_tool` gates fail closed;
session, turn, model, post-tool, and error observers cannot corrupt completed
runtime work when they fail.
Modern `SKILL.md` instruction skills never execute embedded code. The legacy
Python/Markdown executable-skill API is disabled by default; compatibility
callers must opt in explicitly, and even then may use only tools supplied by
the policy-wired runtime registry.

Project-controlled config and extensions are disabled until the workspace is trusted.
API keys are stored in `~/.ash/.env` with restricted permissions. Custom
provider metadata is stored separately in `~/.ash/ash.toml`.
Network fetches and live search require normal tool approval. `web_fetch`
refuses private, loopback, reserved, non-HTTP, oversized, and unsupported
content. `web_search` auto-detects `BRAVE_SEARCH_API_KEY` or `TAVILY_API_KEY`
from `~/.ash/.env`, returns normalized source records with provider provenance,
and falls back between configured providers only in `auto` mode. Set
`web_search_provider` to pin one provider. `allowed_web_domains` filters both
fetch targets and returned search sources. Run `ash setup web` to enter either
search credential through the hidden-input setup flow.

Browser automation is an optional capability pack. Install with
`pipx install --force 'ash-ai[browser] @ git+https://github.com/Suraj-H675/Ash-Harness.git'`,
then run `ash setup browser` once to download
Playwright's pinned Chromium build. Browser pages use an isolated context with
downloads and service workers disabled; navigation, subresources, and
WebSockets share the public-host and `allowed_web_domains` policy. The model
receives bounded ARIA snapshots and stable element references rather than raw
unbounded DOM content. Password fields are never filled by the browser tool.

Remote MCP servers may use OAuth-protected Streamable HTTP (`sse` remains a
configuration alias for the same request path). Ash
discovers protected-resource and authorization-server metadata, requires S256
PKCE, sends the MCP resource indicator, validates callback state, and uses
dynamic client registration when no client ID is configured and the server
supports it:

MCP tool inputs retain their exact draft-aware JSON Schemas and validate
without coercion in a bounded, secret-free subprocess; remote schema references
are never fetched. Rich call results preserve validated content blocks,
structured data, metadata, application errors, and protocol error details;
side-effecting calls are not replayed after an ambiguous failure.
Session-expiry 404 responses are unambiguous protocol rejections: Ash performs
one generation-locked reinitialization, restarts any paginated list, reconciles
the replacement server catalog, and performs one bounded retry only when the
called tool contract is unchanged. Declared MCP tool list changes are validated
and atomically published to the live runtime. Failed refreshes retain the last
working catalog for discovery but quarantine its calls until a later verified
refresh. In-flight turns retain the tool snapshot they were offered and verify
its exact contract again immediately before every HTTP or stdio send.

```bash
ash mcp add docs --transport http --url https://mcp.example/rpc --auth oauth
ash mcp login docs
ash mcp status
ash mcp logout docs
```

Authorization is always an explicit `ash mcp login`; normal agent runs never
open a browser or wait for terminal input. Use `--no-browser` to print the URL
for another browser and paste its final localhost redirect. Access and refresh
tokens are resource-bound and stored under `~/.ash/mcp-oauth` with private
permissions; expired tokens refresh automatically, and a rejected refresh asks
for login again.

If an operation returns OAuth `insufficient_scope`, Ash does not interrupt an
agent run with a browser. It reports the server-required scopes; authorize the
step-up explicitly and retry the operation:

```bash
ash mcp login docs --scope 'files:read files:write'
```

For an authorization server without dynamic client registration, pre-register
the loopback redirect port and keep the client secret in an environment
variable:

```bash
export MCP_DOCS_CLIENT_SECRET='...'
ash mcp add docs --transport http --url https://mcp.example/rpc --auth oauth \
  --oauth-client-id CLIENT_ID \
  --oauth-client-secret-env MCP_DOCS_CLIENT_SECRET \
  --oauth-redirect-port 43123 \
  --oauth-scope 'files:read'
ash mcp login docs
```

The configured port must exactly match the registered
`http://127.0.0.1:PORT/callback` URI. `.mcp.json` stores only the environment
variable reference, never its resolved client-secret value. A user-hosted
OAuth Client ID Metadata Document is also supported by passing its HTTPS URL
as `--oauth-client-id`; its `client_id` and fixed loopback redirect URI must
match the hosted document exactly.

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
command_env_allowlist = ["BUILD_CHANNEL", "NPM_CONFIG_REGISTRY"]
# Emergency compatibility only; never project-controlled.
allow_unsafe_plugin_runtime = false
```

`auto` prefers Bubblewrap on Linux or `sandbox-exec` on macOS, then a locally
available Docker image. Windows uses the Docker fallback. `ash sandbox build`
builds the packaged Python, Node.js, Go, Rust, Java, and native-build baseline;
it is an explicit operation and Ash never downloads or builds it at startup.
Shell commands receive only a small operational environment by default. Add
variable names to `command_env_allowlist` in user configuration when build or
development commands need them; values are read from Ash's process environment
at execution time. Repository configuration cannot alter this allowlist, and
Docker forwarding uses variable names rather than embedding values in process
arguments. The same scrubbed base applies to Git hooks and MCP stdio servers;
MCP variables must be declared in that server's `env` configuration.

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

Turn usage also reports `usage_source` as `provider`, `estimated`, or `mixed`.
When a provider omits usage, Ash uses its configured model token counter for
the actual compacted prompt and streamed response, marks those counts as
estimated, and records estimated prompt, completion, and configured-cost
portions separately in session totals. `/status` labels estimated portions,
the status line prefixes a cost containing estimates with `~`, and structured
surfaces include `has_estimates` and `cost_is_estimated` rather than presenting
fallback counts as exact.

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

See [the production parity checklist](docs/architecture/PRODUCTION_HARNESS_PARITY.md) for
implemented and remaining release requirements.
