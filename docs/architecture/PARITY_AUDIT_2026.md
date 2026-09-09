# Ash parity audit — 2026-09-01

## Blunt answer

No. Ash is not currently on the same overall level as Hermes Agent, OpenClaw,
OpenCode, Claude Code, Codex CLI, Gemini CLI, or Aider.

Ash is a substantial local-first terminal coding harness. Its coding core has
strong coverage: durable sessions, provider-neutral streaming, guarded file and
command tools, approval/policy layers, sandbox integration, MCP, local
provider-backed delegation, automation, an SDK, JSON-RPC, HTTP, ACP, and A2A.
That is meaningful parity in one product slice. It is not product-wide parity.

## What the evidence says

This audit used first-party comparator documentation/source and direct Ash
repository inspection. Ash has 58,000+ source/test lines, 121 unit-test files,
four integration-test files, and four E2E files. The full local suite has
passed, but much of the suite uses deterministic providers and mocks; optional
browser and PTY tests are gated. The repository's “Verified locally” label is
therefore evidence of an Ash-local contract, not evidence of cross-vendor
interoperability or feature equivalence.

The latest bounded command smoke audit exercised the installed CLI's status,
profile, provider catalog, doctor, sandbox, config, storage, metrics, audit,
sessions, plans, cron, permissions, extensions, and agent surfaces. The
browser setup timeout path was also fixed and tested. These checks support the
local reliability claims; they do not change the product-scope verdict.

## Material gaps versus the public baselines

| Area | Ash today | Why this matters |
|---|---|---|
| Product surface | Terminal/TUI, SDK, HTTP/JSON-RPC, ACP, and A2A | OpenClaw is a gateway spanning chat channels, WebChat/Control UI, mobile nodes, media, voice, and routing ([official feature list](https://docs.openclaw.ai/concepts/features)). Ash has no equivalent channel/mobile/media product surface. |
| Provider/auth breadth | Thirteen built-in descriptors, OpenAI-compatible custom routes, local servers, and API-key configuration | OpenClaw documents 35+ providers and subscription OAuth; Hermes documents many hosted/local routes and OAuth options ([OpenClaw](https://docs.openclaw.ai/concepts/features), [Hermes](https://github.com/NousResearch/hermes-agent)). Ash's custom endpoint support is useful but is not the same as native provider integration breadth. |
| Remote/distributed agents | A real provider-backed worker path exists in `SpawnAgentTool`, but it runs in-process; `SubprocessAgent.spawn_subprocess()` launches a simple driver that does not serialize the supplied Python runner | Ash's parity matrix previously called this cross-process capacity. That wording was corrected to Partial. The low-level subprocess API is not proof of provider-backed OS-process isolation. |
| Ecosystem | Local plugins, skills, hooks, MCP, and signed catalog support | OpenClaw and Claude Code expose large plugin/marketplace ecosystems; Gemini documents packaged extensions containing MCP, commands, themes, hooks, sub-agents, and skills ([Gemini extensions](https://geminicli.com/docs/extensions/), [Claude extensions](https://code.claude.com/docs/en/features-overview)). Ash has lifecycle machinery, not comparable ecosystem scale or curation. |
| Client and operations breadth | Linux/macOS CI, a TUI, optional browser automation, and local automation | Codex documents CLI plus IDE, cloud, remote, plugins/marketplaces, review, and automation surfaces; OpenClaw documents gateway operations and mobile/desktop clients ([Codex CLI](https://developers.openai.com/codex/cli/features), [OpenClaw docs](https://docs.openclaw.ai/)). Ash's Windows workflow is intentionally deferred and its browser cannot attach to an existing browser through CDP. |
| Interaction modalities | Text, structured output, images as model attachments, and browser screenshots | The compared products document voice/audio/media, richer computer-use or web surfaces, and in some cases video/image generation. Ash does not provide those as a complete user-facing product. |
| Pair-programming maturity | Strong guarded coding primitives and repository map | Aider documents 100+ language support, automatic Git commits, IDE use, images/web pages, voice-to-code, and automatic lint/test loops ([Aider features](https://aider.chat/)). Ash has pieces of this workflow but not the same mature, user-proven ecosystem. |

## Positioning that is safe to claim

The accurate claim is:

> Ash is a strong local-first coding harness with broad safety, persistence,
> extensibility, and integration primitives, but it remains a WIP and is not a
> feature-equivalent replacement for the full Hermes/OpenClaw/OpenCode/Claude
> Code/Codex/Gemini/Aider product families.

The next major parity work should be deliberate architecture work around a
gateway/channel model, provider/auth adapters, remote worker execution, and
client/media surfaces. Those are not safe to claim complete from unit tests or
to implement piecemeal without a product decision.

## Audit continuation checkpoint — 2026-09-09

This section records the current continuation evidence without reopening the
completed audit work above.

### Security tooling limitations and evidence

- The requested Codex Security Deep Scan was attempted once. It did not start
  because of an external Codex/runtime capability limitation. The exact error
  was:

  > `Codex Security Deep Scan discovery did not start or rejoin.`
  >
  > `Deep Scan cannot safely start a read-only worker: the parent must provide a managed filesystem permission profile.`

  No global Codex permission or orchestration configuration was changed to
  force it to run. This is tooling evidence, not an Ash failure.
- The standard Codex Security scan was available and launched against the
  repository root with scan ID
  `32b22661-fbc5-4059-84db-960d6e36d6c7`, target revision
  `69bd88010e42f98683d7b3a42c0c1affc47a3db5`, and a 349-file snapshot. Its
  native continuation remains `running` in `preflight`, with no report or
  findings artifact available yet. Progress updates returned
  `Scan updates are owned by another continuation.` The Codex Security access
  connector also reported `connector_openai_codex_security_access is not
  connected`. The scan is therefore retained as pending external tooling
  evidence; its empty interim finding count is not a clean security result.
- Direct source review, the bounded security specialist pass, deterministic
  adversarial tests, and the behavior gates below remain the authoritative
  available evidence while that scan is pending.

### F-01 — Bubblewrap host-read probe

F-01 is closed as **NOT REPRODUCED / ORIGINAL PROBE FALSE POSITIVE**. The
original probe only observed that `/home/suraj` existed inside the namespace.
The controlled test instead created a unique file outside the workspace and
all explicitly exposed paths, ran the actual `/usr/bin/bwrap` Tier-2
invocation, and attempted to read the sentinel contents. The result was
`backend=bubblewrap`, `tier=2`, `isolated=True`, and `cat` failed with
`ENOENT`; the sentinel cleanup succeeded. Bubblewrap may create empty parent
directories needed as absolute mount destinations, so parent-directory
existence is not a host-subtree visibility assertion. No Bubblewrap behavior,
capability classification, backend selection, auto-approval, or plugin policy
was changed for F-01. The regression is retained in
`tests/unit/test_sandbox.py::test_run_with_real_bwrap_hides_outside_file_contents`.

The separately noted upstream Bubblewrap `<0.12.0` setup-symlink advisory was
also checked against Ash's actual argument topology. Ash emits fixed system
mounts, `/proc`, `/dev`, one canonical workspace bind, and optional
network/chdir/environment flags; it does not emit `--dir`, `--file`,
`--symlink`, overlay, or a later setup destination below untrusted workspace
content. A locally built Bubblewrap `0.11.2` executed those exact Ash-shaped
arguments with a hostile symlink present in the workspace: it exited cleanly,
and an outside sentinel was unchanged. This is bounded evidence, not a claim
that every concurrent setup race on an affected dependency is safe. The
tested host uses Bubblewrap `0.12.0`, and no version-gating production change
was made because the advisory's vulnerable setup topology was not established
for Ash. Revisit the candidate if Ash adds attacker-influenced setup
destinations or if a concurrent setup reproduction becomes available.

### F-02 — checkpoint recovery parent-swap race

The source hypothesis was confirmed with a deterministic, no-sleep harness.
After recovery validated and hashed `workspace/victim/target.txt`, the harness
renamed `victim` and replaced it with a symlink to an outside directory. On
the pre-fix implementation, recovery's raw `Path`/`os.replace` path wrote the
checkpoint payload to the outside target while the displaced workspace file
remained unchanged.

The fix keeps checkpoint snapshots, restore, deletion, mode restoration, and
rollback on the existing scoped-I/O abstraction. POSIX operations open the
project root and every parent component with no-follow directory descriptors;
temporary files are created and renamed through the anchored parent, expected
current hashes are checked inside the mutation, and modes are applied to the
staged file. The non-anchored platform path follows the existing conservative
fallback policy; no Windows-strength claim is made from Linux evidence.

The final adversarial regression
`tests/unit/test_checkpoints.py::test_startup_recovery_parent_swap_refuses_restore_without_escape`
now leaves the outside sentinel unchanged and reports unresolved recovery
instead of guessing. Targeted checkpoint/scoped-I/O tests pass, including
created-file deletion, mode restoration, stale-state refusal, rollback, and
fallback coverage.

### Verification checkpoint

- `uv run pytest -q --timeout=120 --timeout-method=thread`: **2095 passed,
  3 skipped** before the ACP continuation fix; after that fix and the focused
  ACP lifecycle regression the same full suite reports **2097 passed, 3
  skipped**.
- `uv run ruff check src tests`: passed.
- `uv run mypy src/ash`: passed with no issues in 170 source files.
- `uv build` and the clean installed-wheel smoke
  (`.smoke-venv/bin/python tests/packaging/smoke_minimal_install.py`) passed
  after the ACP fix.
- The earlier CLI help/doctor, integration, E2E, and optional browser checks
  remain recorded baseline evidence; the PTY check remains unavailable because
  `tmux` is not installed.

### Protocol and real-workflow continuation

- ACP had one confirmed integration defect. The real official-client wire probe
  reached the production `ash acp` subprocess, initialized, created a session,
  streamed a real loopback-provider prompt, and then received `Method not found`
  for `session/close`. The installed ACP SDK marks that route unstable, while
  Ash already advertised and implemented session close. The minimal fix enables
  `use_unstable_protocol=True` only at Ash's production `run_agent()` entry
  point. The regression now verifies the advertised capability, real prompt,
  actual response text, successful official-client close, post-close and
  repeated-close resource-not-found behavior, active-prompt cancellation and
  client release, and method-not-found for unadvertised fork/resume. No
  dependency range or broader ACP capability was changed.
- A real A2A TCP server probe passed public health and agent-card access,
  bearer-auth rejection, official JSON-RPC task execution, official REST task
  execution, and two provider-backed streaming turns. Both tasks reached the
  completed state and used the expected model route.
- A real automation CLI workflow passed trust setup, job creation, manual
  isolated-worker execution, provider-backed streaming, durable success/history
  reporting, and worker cleanup. A real LSP CLI workflow passed trusted project
  configuration loading, status, diagnostics, UTF-8 position negotiation,
  hover, and managed subprocess initialize/shutdown lifecycle. Earlier real
  MCP stdio and executable-plugin Bubblewrap probes also passed environment
  scrubbing, outside-file concealment, provider-backed plugin output, and
  process cleanup.
