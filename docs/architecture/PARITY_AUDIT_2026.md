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

## Audit continuation checkpoint — 2026-09-10

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

### F-03 — subagent batch cancellation

The pre-fix implementation was reproduced with two deterministic in-process
workers blocked on an event. Cancelling `run_batch()` returned
`asyncio.CancelledError` to the caller, but both worker runners remained
active and the lead was persisted as `completed` while the sprint was
`aborted`.

The repair makes cancellation forward-only: `_run_agents()` cancels every
unfinished worker task and awaits the complete task set before propagating
the cancellation. `SubprocessAgent.run_in_process()` persists an actively
cancelled worker as `failed` with bounded cancellation text and re-raises
`CancelledError`; it does not publish a normal report. `run_batch()` skips
consolidation on worker-phase cancellation, persists the sprint as `aborted`,
persists the lead as `failed` with `batch cancelled`, and re-raises. Normal
terminal worker states remain unchanged, and no compensation is attempted for
effects that completed before cancellation.

Deterministic regressions cover active workers, a worker queued behind the
concurrency limit, no remaining orchestrator `_run_one` tasks, preservation of
a completed worker, bounded cancellation text, no cancelled-worker report,
skipped consolidation, and cancellation during a suspended async
consolidation. The affected agent integration and adjacent unit suites pass;
full-suite results are recorded below.

### F-04/F-05 — CLI persistence correctness

The corrupt-session-database CLI defect was reproduced before the fix: a
malformed `sessions.db` made `metrics`, `sessions`, `plans list`, and the
adjacent `audit list` path print a raw traceback, including under `--json`.
The agent persistence defect was also reproduced: `ash --db-directory
<alternate> agents list` read the default `agents.db` because that branch used
a bare `AshConfig.load()`.

The repair routes the affected session CLI construction and operations through
the existing classified CLI error boundary. `SessionStore` initialization,
SQLite failures, and malformed persisted rows/contracts now become Ash's
storage error taxonomy with its existing backup/restore guidance. Human mode
has no traceback; `--json` emits one structured error object with the classified
exit code and no diagnostic text on stderr. Headless startup storage failures
retain the established event envelope, while `ash storage check` remains
unchanged. The session hydration paths now classify invalid stored timestamps,
JSON, enum values, and incomplete SQLite structures as storage errors rather
than leaking raw exceptions.

Every `ash agents` action now uses the selected global CLI configuration. The
same resolved `db_directory` supplies both `<db_directory>/agents.db` and
`<db_directory>/worktrees`, so inspection and mutation cannot silently use
different persistence roots. No schema, dependency, or public configuration
format changed.

Meaningful regressions cover corrupt construction and operation failures,
malformed persisted session/plan data, structurally incomplete SQLite,
malformed schema metadata, headless JSON event output, and distinguishable
default/alternate agent databases. The focused CLI/persistence set passed
**144 tests**. A real subprocess workflow passed all four corrupt-database
human/JSON paths and verified alternate-database agent listing and message
mutation without touching the default database.

### F-09 — plugin lifecycle confinement and immutable validation boundary

F-09 is resolved under the explicit trusted-host and OS-account boundary now
documented in `SECURITY.md`. The authorized implementation work in this batch
added a bounded, disk-backed `PluginSnapshot`: an anchored source tree is
captured once, manifest parsing and semantic validation of skills, commands,
agents, hooks, and MCP configuration consume the snapshot bytes, and
stage/publication writes consume only those same bytes. `InstalledPlugin.root`
remains an ordinary `Path`, and destination identity visibility is checked
before success is returned. The snapshot is the semantic authority; destination
digest checks remain defense in depth and are not treated as the binding
guarantee.

Plugin packages, manifests, Git checkouts, generated content, links, and
filesystem layouts remain untrusted. Ash's application-level filesystem
guarantee assumes that the Ash OS account and Ash-managed host state are not
concurrently controlled by a hostile process with equivalent OS-user write
authority. Ash does not claim to defeat arbitrary same-principal mutation of
the POSIX namespace or post-publication changes to the installed tree. Such
environments require an actual host boundary such as separate OS users,
containers/VMs, or hosts. This is a threat-model boundary, not a claim that
POSIX descriptors solve those races.

The two mechanical Hume repairs also landed: Git now acquires the selected
temporary parent before creating an `ash-plugin-git-*` directory and cleans
the checkout through held descriptors; `_tree_exceeds_bytes_at()` closes
completed child descriptors promptly, so live descriptors scale with traversal
depth rather than directory count. Deterministic regressions cover source
substitution, snapshot-only semantic publication, staged/destination content
replacement, Git temporary-root setup failure without a leaked directory, and
a 1,400-directory descriptor-stress traversal.

The current descriptor-relative POSIX backend explicitly reports
`strict_identity_mutation` unavailable. Strict create and regular-file/tree
cleanup entry points therefore fail closed before mutation, and deterministic
tests verify that replacements remain untouched. The ordinary Linux/macOS
descriptor-relative lifecycle remains available for uncontended behavior, but
the implementation does not claim that repeated `stat`/`fstat`, locking,
quarantine, or rename closes the impossible POSIX same-principal create/delete
namespace races. `strict_identity_mutation` is an internal capability fact for
optional future hardening, not a prerequisite for ordinary plugin lifecycle
support. No Linux/macOS mutation disablement or speculative native Windows
backend was introduced.

The focused lifecycle suite passed **52 tests**; the related plugin,
catalog, extension, manifest, registry, runtime, and agent suites passed
**179 tests**; related skill, hook, and MCP suites passed **338 tests**; the
full repository suite passed **2,221 tests with 4 skips**. Ruff, mypy, the
wheel build, built-wheel installation, and packaging smoke passed. The
deterministic same-principal race evidence is retained as the platform and
threat-model boundary; it is not recorded as a POSIX race fixed by descriptor
rechecks. Native Windows identity semantics remain unverified, and no native
Windows strict backend is claimed. Normal Linux/macOS plugin install,
replacement, and uninstall remain supported under the documented boundary.

### F-06 — JSON-RPC explicit-null request IDs

The JSON-RPC adapter previously used `request.get("id") is None` as the
notification test, so an explicit `"id": null` request was executed without a
response. That conflated the JSON-RPC notification distinction with the
identifier value and also made explicit-null method errors disappear.

The adapter now distinguishes `"id" not in request` from an explicit null
identifier. Valid explicit-null requests return normal JSON-RPC results or
errors containing `"id": null`; omitted-ID notifications still execute
without a response. Explicit-null tasks are intentionally not registered in
the existing string/integer pending cancellation map, because multiple null
requests cannot be uniquely correlated. Other request IDs and invalid-ID
validation remain unchanged.

The focused JSON-RPC and HTTP boundary suite passed **34 tests**, covering
omitted/null/zero/string IDs, method-not-found with null, invalid and
non-finite IDs, cancellation behavior, and HTTP notification/response status
semantics. No public cancellation-map redesign was introduced.

### F-07 — Windows managed process-tree cleanup

The pre-fix fallback was reproduced with deterministic platform-mocked
processes. When `taskkill` was unavailable, the old helper launched the
managed child and root-killed it without reporting that descendant cleanup
was not guaranteed (`missing_backend_root_kills_without_error 1`). It also
ignored a nonzero `taskkill` status (`nonzero_taskkill_returns (1, 1)`) and
treated an already-exited root as successful cleanup without invoking the
tree backend (`exited_root_taskkill_invocations 0`). The stale-PID probe is
classified more narrowly: it confirmed a root-exit check/use timing gap, but
did not model Windows process-object handle pinning and therefore did not
prove PID reuse or unrelated-process termination.

The repair adds a shared immutable `ProcessTreePlan` preflight/controller.
Every active production launcher whose lifecycle contract promises descendant
cleanup now obtains the plan before spawning and passes that same plan to
cleanup. The plan resolves and retains the Windows `taskkill` executable;
missing preflight fails before launch. Async cleanup obtains and strongly
retains the original CPython asyncio transport's underlying `Popen` owner;
synchronous cleanup retains its `Popen` owner directly, across the liveness
inspection, taskkill process creation, bounded status wait, root reap, and
final classification. Thus a root transition to exited while taskkill is
being launched does not release the identity pin. Async and synchronous
cleanup invoke the retained backend with `/PID <pid> /T /F`, enforce bounded
waits, inspect the exit status, reap the root, and report a typed cleanup
failure when the tree cannot be confirmed. Failure paths make bounded
best-effort root cleanup without claiming descendant success. An already-
exited Windows root is reported as unconfirmed and is never targeted merely
because its PID remains pinned. Cancellation remains primary and cleanup
tasks are awaited to terminal state before cancellation propagates; cleanup
failures are retained as diagnostics.

The audited scope includes sandbox execution, command/process/search/git/
patch tools, automation, hooks, worktree Git, Ollama model pulls, LSP,
stdio MCP, executable plugins, plugin Git clone cleanup, and installer
subprocesses. HTTP/SSE MCP, one-shot probes, user-owned subprocesses, and the
unused `SubprocessAgent.spawn_subprocess()` path remain outside F-07 because
Ash does not promise descendant-tree cleanup for them. The standalone
installer retains a dependency-free equivalent so downloaded installation
continues to work without importing the installed package.

Deterministic regressions cover preflight/no-launch behavior, intended
Windows creation flags, taskkill arguments and nonzero/timeout/exec failures,
best-effort root cleanup, root-exit timing with retained process owners,
unsupported async transports, root-reap failures, caller-specific error
contracts, cancellation-safe MCP disconnect and long-lived ownership,
Ollama/MCP/plugin/installer launch gates, HTTP/SSE exemption, the installer
system-directory resolver, and installer standalone execution. One-shot
cleanup uses no orphan registry; long-lived owners retain failed cleanup
state. The focused F-07 suite passes on the development host. Windows
fail-closed/taskkill lifecycle behavior was covered by deterministic
platform-mocked tests; native Windows process-tree behavior has not yet been
verified.

### MCP OAuth private-store race

The MCP OAuth token store had a confirmed filesystem-confinement defect: its
path-based save, load, and remove operations separated path validation from
the security-sensitive filesystem mutation. A deterministic synchronized
directory-substitution harness confirmed that a pathname swap could redirect
the pre-fix path-based operation outside the intended store.

The repair adds the small `ash.safety.private_store.PrivateStore`
abstraction. On supported POSIX systems it opens the trusted anchor and every
store component with descriptor-relative, no-follow operations, retains the
store descriptor for the complete operation, uses private modes (`0700`
directory and `0600` records), bounds reads at 1 MB, and stages/fsyncs/renames
writes through the held directory descriptor. Save, load, and remove
directory-substitution regressions now prove that an outside replacement
directory cannot receive, be read by, or lose a credential record.
Final-record and intermediate-link rejection, ancestor-link rejection,
malformed/duplicate JSON, resource binding, temporary cleanup, refresh
persistence failure, and exact private modes are also covered.

On Windows and other platforms without the required descriptor-relative
primitives, the store fails closed before credential material is touched;
there is no pathname fallback, automatic migration, or unsafe override.
Diagnostics report `credentials=unavailable` rather than `missing`.
Unsupported-platform behavior was forced and tested locally; this CI matrix
has no Windows job, so no native Windows secure-store guarantee is claimed.
The OAuth schema, POSIX location, resource binding, refresh behavior, CLI
login/logout semantics, and dependency range were preserved.

### Verification checkpoint

- `uv run pytest -q --timeout=120 --timeout-method=thread`: **2194 passed,
  3 skipped** after the ACP lifecycle, MCP OAuth private-store, F-03
  cancellation, F-04/F-05 CLI persistence, and F-07 managed-process cleanup
  fixes plus the A2A cancellation lifecycle repair.
- The focused F-07 process/caller regression run passed **525 tests** with
  **1 platform-dependent skip**.
- The focused F-04/F-05 persistence/CLI regression run passed **144 tests**;
  the real subprocess workflow also passed.
- The focused MCP OAuth/CLI regression run reports **49 passed**, including
  synchronized save/load/remove directory-substitution cases and the
  fail-closed unsupported-store path.
- The focused A2A remote/CLI suite passed **31 tests**; the related A2A,
  agents, runtime, and subagent integration suite passed **87 tests**. These
  include deterministic repeated-cancellation, cancellation-request failure,
  client-close cancellation, and pre-task-ID cleanup cases, plus the official
  client/server workflows.
- `uv run ruff check src tests`: passed.
- `uv run mypy src/ash`: passed with no issues in 173 source files.
- `uv build` passed for the current A2A batch; the clean installed-wheel smoke
  (`.smoke-venv/bin/python tests/packaging/smoke_minimal_install.py`) remains
  passing from the preceding verification batch.
- The earlier CLI help/doctor, integration, E2E, and optional browser checks
  remain recorded baseline evidence; the PTY check remains unavailable because
  `tmux` is not installed.

### Browser upload confinement and Playwright compatibility

Browser upload had a confirmed TOCTOU defect: `BrowserSession.upload_file()`
read and approved workspace bytes with `read_scoped_bytes()`, then passed the
pathname to Playwright, which could reopen it after a symlink replacement. A
deterministic synchronized regression reproduced the leak by making the fake
chooser observe `outside-content` after the approved file was swapped.

The repair preserves scoped bounded reads, the size limit, and sensitive-file
policy, then passes Playwright an in-memory `FilePayload` containing the exact
approved bytes, basename-only filename, and filename-derived MIME type. It
does not pass a filesystem pathname to the browser. Synchronized regressions
cover ordinary and sensitive-path swaps, MIME fallback, basename handling,
size limits, and sensitive-name rejection.

The first real Playwright 1.61.0 BrowserSession run then exposed a separate
async API compatibility defect: `expect_file_chooser().value` is awaitable.
The production path now awaits that value before calling `set_files()`. The
real Chromium workflow observes `approved.txt`, `text/plain`, and the exact
`approved-content` bytes. The test is gated by `ASH_RUN_BROWSER_TESTS=1` when
the optional browser dependency or binary is unavailable.

Verification for this batch: focused browser and real Chromium tests passed
(**18 tests**), related browser/scoped-I-O/attachment tests passed (**44**),
the full suite with browser tests enabled passed (**2202 passed, 2 skipped**),
repository Ruff and mypy passed, and `uv build` passed.

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
- Outbound A2A delegation also had a confirmed cancellation/lifecycle
  ownership defect: after a remote task ID was observed, repeated local
  cancellation could leave the shielded `cancel_task()` request running while
  `client.close()` had already started. The fix keeps exactly one explicit
  cancellation task strongly referenced, settles it despite repeated caller
  cancellation, then settles client closure before re-raising
  `CancelledError`. Cancellation-request failure remains secondary, and
  settlement only proves a client-side request result—not that the remote task
  entered a cancelled state. Deterministic regressions cover repeated
  cancellation, cancellation-request failure, repeated cancellation during
  client close, and cancellation before a task ID exists. The focused A2A
  suite passes; no public result schema or cross-subsystem cancellation helper
  was changed.
- A real automation CLI workflow passed trust setup, job creation, manual
  isolated-worker execution, provider-backed streaming, durable success/history
  reporting, and worker cleanup. A real LSP CLI workflow passed trusted project
  configuration loading, status, diagnostics, UTF-8 position negotiation,
  hover, and managed subprocess initialize/shutdown lifecycle. Earlier real
  MCP stdio and executable-plugin Bubblewrap probes also passed environment
  scrubbing, outside-file concealment, provider-backed plugin output, and
  process cleanup.

### F-08/F-10/F-11 — MCP lifecycle and diagnostic integrity

The MCP audit reproduced three independent user-facing defects: targeted
replacement closed the newly published client through a temporary runtime,
quoted/escaped and streamed MCP errors could leak secret values or grow
without bound, and targetless `/mcp refresh` discarded reload failures before
printing a clean-success message. Deterministic catalog, collision, failure,
cancellation, concurrent-cleanup, and streaming-split probes established the
responsible boundaries before the repair.

Replacement is now prepared and published by the live `MCPRuntime`: the old
client/tools remain usable until candidate connection, catalog validation,
collision checks, and tool startup succeed. Candidate cancellation/failure
cleans only candidate resources; publication transfers the complete live
ownership boundary, and retired clients are serialized, retried, retained on
failure, and surfaced as shutdown errors rather than silently orphaned. A
real stdio failure probe confirmed `shutdown_error`, retained runtime/client
state, and a second disconnect attempt when cleanup was forced to fail.

MCP diagnostics now use one bounded redaction boundary for quoted, escaped,
unquoted, nested, malformed, and application-error values. All split points in
the escaped streaming regression passed without marker leakage, while raw
malformed/application diagnostics and emitted events remain bounded. Reload
results retain structured error mappings: interactive targetless refreshes
distinguish clean, partial, and previous-runtime-preserved failure states and
print only redacted bounded server/error text.

The focused MCP/lifecycle/redaction suite passed **218 tests**. At that earlier
MCP checkpoint, the complete repository gate passed **2137 tests with 3
skips**; Ruff and mypy passed, and `uv build` produced the source distribution
and wheel successfully. The later full gate, including the CLI persistence
batch, is recorded in the verification checkpoint above.
