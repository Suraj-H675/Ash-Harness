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
| Remote/distributed agents | `SpawnAgentTool` now supports both the default in-process path and a config-backed provider-running local subprocess mode; the subprocess path serializes durable task/config/policy plus only the selected provider environment into a scrubbed child, rebuilds the provider there, and is exercised by installed-wheel smoke. Ash also has explicit configured A2A remote-agent delegation. | Local provider-backed OS-process isolation is therefore real, but Ash still lacks a general transparent worker-placement layer across remote machines/cloud nodes; configured A2A endpoints are protocol-level delegation, not a distributed worker pool equivalent to the broader remote deployment surfaces in some comparator products. |
| Ecosystem | Local plugins, skills, hooks, MCP, and signed catalog support | OpenClaw and Claude Code expose large plugin/marketplace ecosystems; Gemini documents packaged extensions containing MCP, commands, themes, hooks, sub-agents, and skills ([Gemini extensions](https://geminicli.com/docs/extensions/), [Claude extensions](https://code.claude.com/docs/en/features-overview)). Ash has lifecycle machinery, not comparable ecosystem scale or curation. |
| Client and operations breadth | Linux/macOS CI, a TUI, optional browser automation, and local automation | Codex documents CLI plus IDE, cloud, remote, plugins/marketplaces, review, and automation surfaces; OpenClaw documents gateway operations and mobile/desktop clients ([Codex CLI](https://developers.openai.com/codex/cli/features), [OpenClaw docs](https://docs.openclaw.ai/)). Ash's Windows workflow is intentionally deferred. Browser automation supports loopback-only CDP attachment through an Ash-owned isolated context, while remote CDP and direct takeover of pre-existing tabs remain intentionally unsupported. |
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
- A later read-only status lookup for the same registered scan also failed in
  the external Codex connector with:

  > `Codex Security could not start its Python 3 helper. Reinstall or update Codex to restore its bundled Python runtime, or set the PYTHON environment variable to a working Python 3 executable, then restart Codex.`

  This is a second external tooling limitation; no Ash configuration or global
  Codex permission/orchestration setting was changed, and the scan was not
  restarted.

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
- A later complete gate, `uv run pytest -v --timeout=120
  --timeout-method=thread`, passed **2222 tests with 4 skips** in 64.38s.
- After the automatic-memory runtime-wiring repair, the complete gate
  `uv run pytest -q --timeout=120 --timeout-method=thread` passed **2223 tests
  with 4 skips** in 61.94s.
- After the Phase 1 A–I hardening batch and its independent review, the
  complete gate `uv run pytest -q --timeout=120 --timeout-method=thread`
  passed **2237 tests with 4 skips** in 78.61s. The focused Phase 1 set passed
  **420 tests**; the independent read-only Luna xhigh review found and the
  batch corrected four concrete regressions before this gate.
- An isolated temporary-home CLI smoke matrix also passed the read-only
  `--version`, help, config, provider, sandbox, storage, metrics, sessions,
  plans, cron, permissions, extensions, agents, MCP, LSP, and profile status
  workflows with valid JSON where requested. `doctor --json` correctly
  returned its classified nonzero result for the intentionally credential-free
  environment (missing Anthropic credentials, optional search, and Chromium),
  without a traceback or secret output. A fresh `storage check --json` did not
  create a missing database.
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
- `uv run ruff check src tests`: passed after the Phase 1 changes, and
  `uv run mypy src/ash` passed with no issues in **174 source files**.
- `uv build` passed after the Phase 1 changes. The installed-wheel smoke,
  `.smoke-venv/bin/python tests/packaging/smoke_minimal_install.py`, also
  passed.
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

### Candidate F-12 — WebFetch accepts non-global shared address space

Direct adversarial review found that `ash.tools.web._resolve_public_addresses`
rejects several private/special address categories but does not reject an
address whose standard-library `ipaddress` value is non-global when it does
not fall into one of those categories. RFC 6598's `100.64.0.0/10` Shared
Address Space is one such case: a direct probe accepted both
`100.64.0.1` and `100.127.255.254`, and `_validate_public_url` accepted the
corresponding HTTP URLs. A controlled `WebFetchTool` transport consequently
returned success for `http://100.64.0.1/internal`.

The subsequent Sol decision confirmed the public-fetch contract: every
resolved address must satisfy `address.is_global`, in addition to the existing
explicit special-address checks. The production predicate now rejects both
RFC6598 examples and mixed public/non-global DNS answers while continuing to
accept representative public IPv4 and IPv6 addresses. The pinned exact-address
transport and DNS-rebinding regression remain intact; this finding is fixed in
the Phase 1 batch.

### Candidate F-13 — Browser public-host validation is not connection-pinned

Direct browser adversarial testing found a separate validation/use gap in the
browser network boundary. `BrowserSession` validates each request URL through
`_validate_browser_url`, which resolves and approves the hostname in Ash, then
calls Playwright `route.continue_()`. Chromium performs its own hostname
resolution for the continued request; Ash does not pass the approved address
to Chromium or otherwise pin that connection.

A real Chromium probe used a deterministic host-resolver rule mapping
`rebind.test` to a local HTTP server while Ash's resolver interposition
reported the hostname as the public address `93.184.216.34`. The browser
passed Ash's public-host check and the local server received the request,
showing that URL validation alone does not bind the browser connection to the
address that was approved. This is controlled evidence of the DNS
validation/use boundary, not a claim that a public DNS provider was modified.
No production behavior has been changed for this candidate. A separate Sol
decision is required for the browser's public-network contract and an
appropriate Playwright/Chromium enforcement design.

The subsequent Sol decision authorized the connection-enforcing browser proxy
architecture as a separate Phase 2 batch. `BrowserSession` now owns one
loopback-only `BrowserPolicyProxy` for each browser lifetime and passes its
explicit proxy settings to both persistent and ephemeral Chromium launches.
The proxy applies the allowlist and globally-public-address policy at
connection time, resolves each target once, and connects upstream to the
vetted numeric address. HTTP forwarding, CONNECT tunneling for HTTPS/WSS, and
plain WebSocket forwarding preserve the original host authority without TLS
termination. Existing Playwright route checks remain defense in depth.

Chromium's implicit loopback bypass is explicitly neutralized with the
`<-loopback>` proxy-bypass rule. Real Chromium regressions with the installed
Playwright runtime verify that loopback, link-local, private WebSocket,
redirect, and subresource targets reach the policy proxy and do not reach
controlled local victim servers; a permitted browser response works through
the same proxy. Startup fails closed when the proxy cannot start, and browser
startup/restart/close paths settle proxy ownership without orphaned listeners
or connection tasks. The proxy CONNECT path is covered by deterministic
unit-level tunneling tests; native Windows networking and complete browser
network sandboxing are not claimed.

As a normal-user compatibility check, a real `BrowserSession` navigation to
`https://example.com` completed through the configured proxy with HTTP 200 and
the expected page title. This verifies permitted HTTPS usability in the local
runtime without treating it as a complete network-isolation claim.

F-13 is therefore fixed at Ash's HTTP(S)/WS(S) browser connection policy
boundary. The original resolver-mismatch evidence remains retained above, and
the proxy's connection-time enforcement—not URL validation alone—is the
authoritative repair.

### Phase 1 — cancellation, CLI, logging, and WebFetch hardening (A–I)

The current Phase 1 repair batch addresses the confirmed A–F lifecycle
ownership defects and G–I CLI/output/network-policy defects. MCP request and
task cancellation, failed HTTP-session recovery deletion, A2A server-client
closure, and SpawnAgent worker cleanup now create explicitly owned cleanup
tasks and settle them through repeated caller cancellation before propagating
the primary cancellation. Automation keeps the worker bounded for arbitrary
injected clients: a cancellation-resistant operation transfers ownership to a
strongly retained deferred cleanup that waits for the operation before closing
the client. The default subprocess client remains on its bounded prompt and
process-tree cleanup path; the injected-client finding is not overstated as a
default-subprocess leak.

The CLI paths now classify configuration and session-store failures through
the stable human/JSON error boundary, including malformed user/project and
MCP configuration. No-color logging is reconfigured after effective
configuration is resolved, and the `--ci`, `NO_COLOR`, and truthy
`ASH_NO_COLOR` paths are covered by real log emission. Classified diagnostics
redact secret-like values. WebFetch now requires every resolved address to be
globally reachable while retaining its explicit special-address checks and
pinned-address transport.

Focused Phase 1 regressions currently pass **420 tests**. Full repository and
packaging gates remain the completion criteria for this batch; the exact final
counts are recorded below after they complete.

The external Codex Security Deep Scan and standard scan limitations recorded
above remain tooling limitations and are not interpreted as clean security
results.

### F-14 — session restore left temporary SQLite sidecars

An isolated real `ash storage backup` / `ash storage restore --yes` workflow
found that a successful restore removed the temporary database file but left
hidden `.sessions.db.restore-*.tmp-wal` and `.tmp-shm` artifacts in the
database directory. The residue was reproducible on the local WAL-enabled
SQLite runtime and could accumulate after repeated restores.

The cleanup now removes the temporary main file and its SQLite WAL/SHM
sidecars in the existing `finally` block. The focused regression
`tests/unit/test_storage.py::test_restore_cleans_temporary_sqlite_sidecars`
fails on the old behavior and passes after the repair. A follow-up real CLI
backup/restore workflow completed without leaving any temporary restore
artifacts; the established backup, restore, preservation, and storage error
semantics remain unchanged.

### Candidate F-15 — automatic memory indexing crashed CLI startup

An isolated real CLI run with trusted project memory auto-indexing enabled
reproduced a startup failure before the fix: `build_runtime()` called
`asyncio.create_task()` synchronously, so the CLI raised `RuntimeError: no
running event loop` and emitted an unawaited `index_project_memory` coroutine
warning. This was a confirmed runtime-wiring defect, not a provider failure.

The repair carries the auto-index configuration into `AshLoop` and schedules
the one background indexing task only after `start_session()` is running in an
async event loop. A focused regression verifies runtime construction outside an
event loop, session-start scheduling, indexing, and searchable content. A real
loopback-provider CLI run now exits successfully and leaves the expected FTS5
workspace record. Existing shutdown ownership still cancels the bounded
background task during loop closure; no new persistence or public API semantics
were introduced.

### Audit continuation checkpoint — 2026-09-12

The provider-hardening batch was committed as `e5450f1` and pushed. It fixes
the OpenAI-compatible request-hook await contract and makes the DeepSeek and
Groq adapters tolerate usage-only terminal chunks with `choices=[]`. The
focused provider/registry tests passed **61 tests**. Hosted CI run
`34680050403` completed successfully across Ubuntu/macOS and Python 3.11/3.12,
including Ruff, mypy, the full test suite, wheel build, and installed-wheel
smoke. The unrelated worktree files remain untouched.

The following are new evidence-backed candidates from the continued
adversarial audit. They are deliberately recorded as pending decisions rather
than patched speculatively.

#### Candidate — read-only Git tools execute repository-configured extensions

`GitStatusTool` and `GitDiffTool` call `_git_result()` without the runtime
sandbox manager (`src/ash/tools/git.py:60-81`). `_run_git()` resolves the Git
binary safely, but then launches Git directly (`src/ash/tools/git.py:225-256`).
Git itself remains able to execute repository-configured behavior during these
nominally read-only operations.

A fresh temporary repository configured `core.fsmonitor` to an executable
outside the workspace. The real `GitStatusTool(SafetyGuard(root)).run()`
returned success and the external marker became `fsmonitor`. A configured
`diff.<driver>.textconv` executable likewise ran during the real
`GitDiffTool.run()` despite `--no-ext-diff`; the external marker became
`textconv`. The tools therefore expose an untrusted repository's Git metadata
to host-side executable hooks while the permission policy classifies both
tools as read-only. This is a concrete workspace-metadata trust-boundary
defect, distinct from the excluded same-OS-user namespace races. No fix has
been applied; the safe policy boundary between Git inspection and configured
Git extensions needs a Sol decision.

#### Candidate — macOS sandbox backend can transition to unscoped execution

`SandboxManager._build_backend()` returns `_ScopedBackend` when the selected
`sandbox-exec` backend is no longer available, instead of raising the
`SandboxBackendUnavailable` that `prepare()` is required to propagate when
`allow_scoped_fallback=False` (`src/ash/sandbox/manager.py:470-482`). A
deterministic platform-mocked probe made `has_sandbox_exec()` return true at
selection and false at preparation. The manager reported
`selected=sandbox-exec`, `allow_scoped_fallback=False`, then produced
`prepared_backend=scoped`, `prepared_tier=1`, `fallback_used=False`, and
`argv0=/bin/sh`. Native macOS execution is not available locally; this remains
a platform-mocked lifecycle finding, not a native macOS claim.

#### Candidate — Windows background jobs bypass host PowerShell resolution

`BackgroundProcessTool._start()` selects bare `powershell.exe` on Windows
(`src/ash/tools/process.py:176-183`), while the foreground command path uses
`resolve_host_executable()` and rejects workspace-shadowed PowerShell
(`src/ash/tools/command.py:326-338`). With the process platform mocked to
Windows and a fake `powershell.exe` placed first on `PATH`, the real background
tool started successfully, reported an exited job, and the fake marker became
`shadowed`. Native Windows execution is unverified. This candidate needs a
consistent trusted-executable policy for the background lifecycle.

#### Candidate — stopped background jobs permanently consume capacity

`BackgroundProcessTool.run(action="stop")` returns a successful “Stopped”
result but does not remove the terminal job from `self.jobs`
(`src/ash/tools/process.py:127-158`). A real local run started and stopped 32
short-lived jobs, observed `jobs_retained=32`, and then rejected the next
legitimate start with `Maximum of 32 background jobs reached`. This is a
reproducible user-facing availability/lifecycle defect. The desired retention
semantics for polling stopped jobs versus freeing capacity require a focused
decision before changing behavior.

#### Candidate — redaction corrupts machine-readable usage fields

`save_runtime_events()` applies `redact_value()` to every persisted event
(`src/ash/core/session.py:1782-1809`). `redact_value()` treats any key
containing `token` as secret-like (`src/ash/core/redaction.py:364-379`), so a
real provider-backed turn with numeric usage produced persisted and HTTP/SSE
`turn.usage` fields such as `prompt_tokens` and `completion_tokens` as the
string `"[REDACTED]"` rather than numbers. The same path also affects cache
and estimated token counters. This breaks the usage/event contract even when
no secret is present. The broader redactor gaps for Basic authorization,
Cookie, and credential/proxy-authorization fields remain separately recorded;
no redaction change has been made.

#### Candidate — stream-json emits duplicate completion events

A fresh real one-shot loopback-provider workflow produced the event sequence
`turn.started`, `context.usage`, `assistant.delta`, `turn.usage`,
`turn.completed`, `turn.completed`, with a completion count of 2. The runtime
emits `turn.completed` in `src/ash/core/loop.py:1738-1748`, and the headless
bootstrap then calls `HeadlessUI.emit_result()` (`src/ash/cli.py:4176-4188`),
which emits a second completion event (`src/ash/ui/headless.py:87-90`). This is
a concrete structured-output contract defect, not a test-only duplicate.

#### Candidate — duplicate native tool-call IDs overwrite durable state

`CanonicalToolCall` validates that each ID is a non-empty string but does not
validate uniqueness. `tool_calls.call_id` is the SQLite primary key
(`src/ash/core/session.py:750-763`), and `save_tool_call()` uses
`ON CONFLICT(call_id) DO UPDATE` (`src/ash/core/session.py:2176-2227`). A real
local provider fixture returned two native calls with the same ID. Both tools
executed and two tool messages were sent back to the provider with the same
ID, but the durable table contained one row for the second tool only. This
needs a protocol/persistence contract decision about rejecting malformed
provider output versus namespacing IDs; no schema or runtime change has been
made.

#### Resolved in Batch 5 — REST body buffering preceded bearer authorization

The original `_BoundedRequestBodyMiddleware` read and buffered up to the full
16 MiB REST body before FastAPI reached the route's `authorize` dependency. A
direct ASGI reproduction consumed the unauthenticated streamed body before
returning 422 for malformed JSON, and an unauthenticated request advertising an
oversized `Content-Length` returned 413 before authentication. This was a
concrete ordering and resource-consumption defect, not only a deployment-hardening
preference. Batch 5 moves bearer authentication and per-client rate limiting to
the protected HTTP boundary before any REST body buffering or route parsing.
`/rpc` retains its stricter 1 MiB bounded reader after authentication, while
framework docs/OpenAPI and unrelated routes preserve their previous public/404
behavior. See the Batch 5 evidence below.

#### Candidate — built-in LSP detection can select a workspace-shadowed binary

`load_lsp_server_configs()` resolves built-in server commands with raw
`shutil.which()` (`src/ash/lsp/config.py:115-125`, `:322-333`) rather than the
workspace-excluding helper used for other Ash-owned executables. A direct
probe put an executable named `rust-analyzer` in a temporary workspace and
placed that workspace first on `PATH`; built-in detection selected the exact
workspace path even with `include_project=False`. Runtime LSP is trust-gated,
so the precise product boundary is still to be decided; this is recorded as a
candidate rather than a confirmed untrusted-workspace bypass. A real local
Rust LSP attempt separately found that the installed rustup shim lacked the
`rust-analyzer` toolchain component; that is an environment limitation, not an
Ash failure.

The previously recorded Codex Security Deep Scan and standard-scan errors are
unchanged external tooling limitations. These new candidates have not been
implemented, and no global Codex configuration was changed.

#### Additional candidates from the independent workflow pass

##### Candidate — legacy project config migration can seed user safety policy

`_migrate_old_ash_toml()` reads `./ash.toml` and, after the ordinary interactive
migration confirmation, copies every recognized configuration field into the
user-owned `~/.ash/ash.toml` (`src/ash/commands/setup.py:1204-1256` and
`:1282-1290`). This includes `safety_tier` and `sandbox_backend`, while the
normal `AshConfig.load()` path only consumes project configuration after the
workspace trust gate (`src/ash/config.py:1142-1162`).

In a temporary untrusted workspace containing:

```toml
provider = "ollama"
model_name = "probe"
safety_tier = "auto_approve"
sandbox_backend = "direct"
```

the real migration function, with the confirmation answered yes, created the
user config containing `safety_tier = "auto_approve"` and
`sandbox_backend = "direct"`. A declined confirmation created neither. This is
a concrete trust-boundary issue: project-controlled settings that affect
approval and sandbox policy become persistent user-owned policy after an
apparently routine migration. No fix has been applied; migration semantics
need a Sol decision.

##### Candidate — provider model IDs can inject terminal controls

Provider model-catalog parsing accepts arbitrary non-empty model IDs
(`src/ash/commands/setup.py:985-998`; the shared readiness parser has the same
condition at `src/ash/providers/readiness.py:389-403`). The setup wizard prints
those IDs directly at `src/ash/commands/setup.py:934-936` and `:970-976`, and
the config writer rejects CR/LF/NUL but not other terminal controls
(`src/ash/commands/config.py:270-282`). A local `/models` response containing
`\x1b]52;c;YXR0YWNrZXI=\x07MODEL\u202eTXT` was accepted and printed unchanged in
the model list and saved-model confirmation. This can enable terminal spoofing
and OSC 52 clipboard behavior on supporting terminals. Persisted model output
is also rendered directly in human `doctor` diagnostics
(`src/ash/commands/doctor.py:495-497`). No fix has been applied.

##### Candidate — MCP tool names bypass approval-screen terminal sanitization

MCP tool names are accepted as non-empty strings and are incorporated into
namespaced tool names (`src/ash/mcp/runtime.py:549-553`, `:1381-1385`). The
interactive approval renderer appends `tool_name` directly to a Rich `Text`
object (`src/ash/ui/terminal.py:484-490`) instead of applying
`terminal_safe_text()`. A fake MCP tool named `remote_tool\x1b[2J` retained the
CSI clear-screen bytes in the actual approval line, while the ordinary event
renderer sanitized the same value. A malicious or compromised MCP server can
therefore influence approval-screen rendering when its tool reaches the
approval path. No fix has been applied.

##### Candidate — bidi formatting controls remain in terminal-safe output

`terminal_safe_text()` removes Unicode `Cc` controls but intentionally leaves
formatting controls (`Cf`) such as U+202E/U+202C
(`src/ash/ui/terminal.py:45-57`). A real screen-reader-mode rendering probe
preserved `SAFE \\u202eGNIDOC\\u202c END`; the existing CSI negative control was
escaped. This allows visual-order spoofing of assistant, tool, or repository
text even though arbitrary ANSI control execution was not observed. No fix has
been applied.

##### Candidate — human storage diagnostics emit SQLite metadata controls

`check_database()` interpolates table names returned by
`PRAGMA foreign_key_check` directly into human diagnostic messages
(`src/ash/commands/storage.py:45-50`), which `render_storage_check()` prints in
human mode (`src/ash/commands/storage.py:162-169`). A crafted corrupt database
with an ESC/BEL-containing table name produced raw `\\x1b]52...\\x07` bytes in
human output. The JSON renderer escaped the controls, so this is specifically
a human terminal-output boundary. No fix has been applied.

##### Lower-impact candidate — repository-map DOT output is not escaped

`RepoMap.to_dot_graph()` interpolates repository-relative filenames into quoted
DOT edges without escaping (`src/ash/repo/repomap.py:627-645`). A filename
containing a quote and newline produced an additional forged DOT edge in the
output. There is no in-tree caller that invokes Graphviz and `dot` is not
installed in this environment, so downstream rendering impact was not
executed. This remains a serializer candidate rather than a confirmed command
execution issue.

##### Lower-impact candidate — Ollama child output is not terminal-sanitized

`pull_model()` writes child-process output directly to stdout
(`src/ash/commands/ollama.py:95`). A fake external `ollama` emitted raw OSC/CSI
bytes and the real command path preserved them. The real Ollama executable is
not installed locally, so genuine runtime exploitability remains an environment
limitation. No fix has been applied.

These candidates are pending Sol classification and remediation decisions. The
specialist's focused checks reported **195 tests passed** and the adjacent
config/profile/MCP/Ollama checks reported **248 tests passed**, with no
repository edits. The unrelated worktree entries remain untouched.

### Batch 1 — workspace and host trust-boundary remediation

Sol authorized the four workspace/host boundary candidates as the first
current remediation batch. The changes below are intentionally scoped to
Ash-owned boundaries; the unrelated working-tree entries remain unstaged and
untouched.

#### Legacy project-config migration (#1)

`_migrate_old_ash_toml()` now checks Ash's existing workspace-trust record
before offering migration. Its planner imports the current
`PROJECT_CONFIG_FIELDS` allowlist rather than treating every legacy setting as
portable user configuration. Safety tier, sandbox backend, command-blocking
and host/persistence paths, API keys, and other non-project-owned fields are
not promoted. Safe project-owned settings and the historical model selection
workflow remain available; existing destination values are preserved. A
bounded diagnostic names skipped fields without printing their values, and
the legacy source remains in place with the existing backup and migration
record behavior.

The focused migration regressions cover untrusted workspaces, trusted
workspaces containing sensitive fields, mixed safe/sensitive files, source
backups, destination preservation, duplicate-migration suppression, and
bounded diagnostics. The old real-path probe no longer creates user config or
environment credentials from sensitive legacy fields.

#### Ash-owned read-only Git inspection (#3)

The shared `ash.safety.git` policy now applies `--no-pager`,
`-c core.fsmonitor=false`, `--no-ext-diff`, and `--no-textconv` where
applicable, together with a scrubbed environment containing
`GIT_OPTIONAL_LOCKS=0`, `GIT_PAGER=cat`, and
`GIT_TERMINAL_PROMPT=0`. It is used by the Git status/diff/log tools, staged
secret-scan inspection, review collection, and RepoMap's Git-ignore query.
Ash does not alter repository configuration and explicit user-requested
`run_command("git ...")` behavior is outside this Ash-owned inspection
boundary.

Real temporary-repository probes with marker `core.fsmonitor` and
`diff.<driver>.textconv` executables now return ordinary status/diff/review/
RepoMap results without executing either marker. The focused regression also
checks normal diff content and confirms the repository's local configuration
is unchanged.

#### Windows background PowerShell (#6)

The direct/scoped Windows `BackgroundProcessTool` path now resolves
`powershell.exe` through `resolve_host_executable()` using the effective
scrubbed PATH, excludes workspace candidates, passes the resulting absolute
host path to subprocess creation, and fails before spawn when no trusted host
PowerShell is available. Process-tree, sandbox, environment, and background
I/O behavior are unchanged. The workspace-shadow and no-host-executable
regressions pass with a platform-mocked Windows path. Native Windows
execution remains unavailable locally and is not claimed.

#### Built-in LSP executable resolution (#11)

Built-in and bare configured LSP commands now use the same workspace-excluding
host resolver for discovery, availability checks, and the actual
`LSPClient.start()` launch. Trusted project configuration may still select an
executable from `node_modules/.bin`, and explicit absolute user-configured
paths remain supported. The resolved absolute command is passed to process
creation so the later launch cannot re-resolve a workspace-shadowed binary.
Regressions cover built-in detection with a workspace-first PATH, trusted
project-local support, explicit path behavior, safe host fallback, and runtime
resolution. Native platform-specific executable behavior remains unverified
where the local host cannot exercise it.

At the completed local checkpoint, the Batch 1 focused runs pass **60**
migration/Git/review/RepoMap tests and **45** LSP/background tests. The full
CI-equivalent pytest gate passes **2,260 tests with 7 skips**; repository Ruff
and mypy pass, `uv build` passes, and the installed-wheel packaging smoke
passes. The skipped cases are optional browser/PTY/platform-dependent tests,
not Batch 1 failures. Native Windows execution remains unverified, and no
Codex Security scan is claimed as passed. Hosted-CI results are recorded only
after the pushed batch's run completes. Hosted CI run `34701770836` then
completed successfully across Ubuntu/macOS and Python 3.11/3.12, including
Ruff, mypy, the full test suite, wheel build, and installed-CLI smoke.

### Batch 2 — sandbox backend fail-closed remediation (#5)

The confirmed macOS sandbox transition defect was reproduced with a
deterministic platform-mocked probe: `sandbox-exec` was selected as the
isolation backend, disappeared before `prepare()`, and the old implementation
silently returned a scoped invocation even when scoped fallback was disabled.
That path falsely reported a lower enforcement tier without recording an
explicit fallback.

`SandboxManager._build_backend()` now treats disappearance of the selected
`sandbox-exec` backend as `SandboxBackendUnavailable`. `prepare()` therefore
fails closed unless the caller explicitly enabled scoped fallback. When that
fallback is enabled, the returned invocation truthfully reports
`backend=scoped`, the scoped tier, and `fallback_used=True`. The existing
Docker, Bubblewrap, and read-isolation behavior remains fail-closed.

The focused sandbox regressions cover both disabled and explicitly enabled
fallback after backend disappearance, alongside selected-backend, partial
isolation, and read-isolation behavior. The complete local gate passes
**2,262 tests with 7 skips**; Ruff and mypy pass; `uv build` passes; and the
installed-wheel packaging smoke passes. Native macOS `sandbox-exec` execution
was not available locally, so the new transition coverage is explicitly
platform-mocked rather than native verification. No Codex Security scan is
claimed as passed; its recorded external tooling limitations remain in force.

Hosted CI run `34702347950` completed successfully across Ubuntu/macOS and
Python 3.11/3.12, including Ruff, mypy, the full test suite, wheel build, and
installed-CLI smoke.
### Batch 3 — output, redaction, and serialization integrity

The output-integrity candidates were rechecked as one coherent human-output and
machine-data boundary. `terminal_safe_text()` is now the shared human-terminal
sanitizer: C0/C1-style control characters are rendered visibly, Unicode bidi
formatting controls are escaped, and single-line labels additionally neutralize
newlines and tabs. MCP approval names, provider/model setup output, provider
connectivity output, doctor/storage diagnostics, the main model catalog and
capability renderers, persisted-profile model displays, prompt status-line model
text, and Ollama child output now cross that boundary before human rendering.
Machine-oriented JSON values and stored model identities remain exact.

The provider-model finding was broader than the setup wizard alone. A model ID
accepted from a provider can be persisted and later displayed through `/models`,
live catalog refresh, model-capability output, profile output, the status line,
and model-switch confirmations. Regression coverage now exercises these
alternate sinks so fixing setup does not leave a later terminal-spoofing path.
Ollama output is sanitized before applying the visible-output cap, preventing
control-character expansion from exceeding the documented bound.

Structured redaction now distinguishes numeric usage accounting from secret
fields. Prompt/completion/cache/reasoning and related numeric token counters
remain numbers in persisted/event payloads, while credential-like structured
keys remain fail-closed, including camelCase and compound forms such as
`apiToken`, `accessToken`, `privateKey`, and credential/token-bearing names.
Sensitive Authorization/Proxy-Authorization/Cookie/Set-Cookie text and nested or
escaped secret assignments are covered by the shared redactor without changing
ordinary non-secret strings.

RepoMap DOT serialization now escapes backslashes, quotes, CR/LF, and other
control bytes in repository-relative filenames before inserting them into
quoted DOT strings. This prevents repository filenames from forging additional
DOT statements. Storage diagnostics likewise escape SQLite-controlled metadata
before human terminal rendering.

Reference review found mature harnesses independently treating terminal escape
handling and user-facing secret redaction as explicit boundaries; the Ash fix
keeps Ash's own centralized renderer/redactor contracts rather than copying a
reference implementation.

The focused Batch 3 gate passes **264 tests**. Repository Ruff and mypy pass.
The complete local pytest gate passes **2,294 tests with 7 skips**; `uv build`
passes; and the installed-wheel smoke passes on Python 3.12. The dirty local
build also demonstrates that unrelated untracked Python files can be included
by a source-tree build, so clean hosted CI remains the authoritative packaging
proof for the committed batch. The unrelated untracked development/reference
files remain unstaged and untouched.

Hosted CI run `34872502590` completed successfully for Batch 3 across
Ubuntu/macOS and Python 3.11/3.12, including Ruff, mypy, the full test suite,
clean-tree wheel build, and installed-CLI smoke.
### Batch 3 real-provider workflow evidence

A temporary isolated user HOME/workspace exercised the changed boundaries against
real OpenRouter service using an ephemeral credential that was not written to
repository or Ash configuration. `ash providers test --json` discovered 447
models and verified `openrouter/free` as catalog-available. A real plain turn
completed successfully with provider-reported usage persisted as numeric
`prompt_tokens=3342` and `completion_tokens=34`, with zero reported cost.

A separate real coding journey used the current OpenRouter catalog metadata to
select `cohere/north-mini-code:free`, which advertised tool support. In headless
`auto_edit`, Ash completed repository listing/reads and the requested
`replace_file_content` edit, changing the intentionally broken `add()` function
to addition; the external project test then passed. Four requested
`run_command` calls were correctly denied by the non-interactive permission
boundary, after which the weak free model exhausted the turn iteration limit
without a final prose response. This is recorded as permission/model behavior,
not an Ash defect.

The same real session independently reproduced the already-recorded duplicate
`turn.completed` candidate (two persisted completion events for one turn). A
plain OpenRouter run also surfaced a separate provider/model identity-labeling
oddity for later investigation; neither issue is folded into this output/
redaction batch.

### Batch 4 — runtime lifecycle and durable tool identity

Three previously confirmed runtime defects were treated as one lifecycle and
persistence batch. Background-process capacity now counts only live children;
stopped and naturally exited jobs remain available for recent list/poll use
without permanently consuming one of the 32 running slots. To avoid turning
that availability fix into unbounded memory growth, accepted starts prune the
oldest terminal history beyond a bounded recent window while never pruning a
running job. This follows the same broad mature-harness pattern seen in Hermes,
which separates running processes from bounded finished history.

`stream-json` completion ownership is now singular when HeadlessUI is bound to
the runtime event stream. The core loop remains the authoritative emitter of
`turn.completed`; the one-shot wrapper no longer synthesizes a second terminal
event in that runtime-bound mode. Plain JSON still emits its one final document,
and standalone/unbound HeadlessUI behavior remains supported. Codex's streamed
SDK likewise treats the turn-completed event as the authoritative terminal turn
signal rather than a duplicated wrapper result.

Provider-native tool calls are now rejected before persistence or dispatch when
one completion contains duplicate call IDs. The pre-fix reproduction executed
both same-ID calls and SQLite's primary-key upsert retained only the latter
record. The new boundary raises `ProviderCompletionError` before either tool can
run, preserving provider correlation identity and Ash's durable exactly-once/
ambiguous-outcome model instead of inventing synthetic IDs or migrating storage.
Reference harnesses similarly use provider call IDs as correlation keys for
pending tool state and results.

Regression probes first failed on all three defects, then passed after the
minimal fixes. Focused lifecycle/persistence verification passes **132 tests**.
Repository Ruff and mypy pass. The complete local pytest gate passes **2,300
tests with 7 skips**; `uv build` passes; and the installed-wheel smoke passes on
Python 3.12. Unrelated tracked/untracked development files remain untouched and
will not be included in this batch. Hosted CI run `34882256898` completed
successfully for commit `001cd66` across Ubuntu/macOS and Python 3.11/3.12,
including Ruff, mypy, the full test suite, clean-tree wheel build, and
installed-CLI smoke.

### Batch 5 — HTTP authentication and request-resource ordering

The authenticated HTTP/SSE adapter now enforces its protected boundary in the
order **bearer authentication → per-client rate limit → REST body-size bound →
route parsing/dispatch**. Previously, body-bearing FastAPI routes could consume
and validate request data before the route dependency performed authentication;
this allowed unauthenticated clients to spend the server's body budget and even
observe 413/422 responses before a 401 decision.

Regression tests were written against the pre-fix behavior first. They proved
that an unauthenticated streamed `/v1/turn` body was consumed and returned 422,
and that an unauthenticated oversized `Content-Length` received 413. After the
fix, protected REST and `/rpc` requests reject invalid/missing bearer credentials
without consuming the request body. Authenticated requests that have already
exhausted their rate-limit bucket likewise receive 429 before the body is read.
Duplicate Authorization headers remain rejected and token comparison remains
constant-time.

The middleware is intentionally scoped to Ash's protected namespaces (`/rpc`
and `/v1/...`) rather than every FastAPI route, preserving the prior public
`/health`, `/docs`, `/openapi.json`, and unrelated 404 behavior. `/rpc` continues
to use its separate 1 MiB streaming body cap after the common auth/rate boundary;
other protected REST writes retain the 16 MiB cap. The design follows Ash's
existing A2A edge pattern, which also authenticates before bounded body intake.

A live Uvicorn loopback probe independently verified the boundary outside the
in-process ASGI test transport: an unauthenticated POST with an advertised
99,999,999-byte body and no body bytes returned 401 immediately; a valid
authenticated session-list request returned 200; a subsequent rate-limited POST
with an incomplete advertised body returned 429 immediately; and `/docs`
remained 200. The complete HTTP unit suite passes **28 tests**, and the broader
HTTP/JSON-RPC/serve/SDK/CI-mode set passes **66 tests**. Repository Ruff and
mypy pass. The complete local pytest gate passes **2,305 tests with 7 skips**;
`uv build` passes; and the installed-wheel smoke passes on Python 3.12. Hosted
CI run `34884569384` completed successfully for commit `190b87c` across
Ubuntu/macOS and Python 3.11/3.12, including Ruff, mypy, the full test suite,
clean-tree wheel build, and installed-CLI smoke.

### Batch 6 — provider-route identity and one-shot completion ownership

A real OpenRouter workflow had previously surfaced inconsistent provider/model
labels. Focused reproduction showed that the issue affected every route using
the shared OpenAI-wire adapter: OpenRouter, Mistral, xAI, Together, Fireworks,
Cerebras, LM Studio, vLLM, and custom OpenAI-compatible routes all exposed
`provider_family="openai"` regardless of the route that actually owned the
request. `FailoverProvider` separately retained `provider_family="custom"`
after a backup provider served a turn. This was execution-identity corruption,
not just display text: provider family participates in pricing lookup, circuit
identity, capability lookup, diagnostics, events, and result attribution.

The repair assigns shared OpenAI-wire adapters their real route family while
freezing the adapter's previously resolved OpenAI-wire capability object, so
this identity fix does not silently alter tool/vision behavior. Failover keeps a
writable family identity synchronized with the active provider. `AshLoop` now
exposes one canonical active `provider/model` identity, uses it for new session
metadata and active pricing lookup, and adds `model_id` to `turn.completed` as
an additive event-schema-v1 field while preserving the existing model-only
`model` field. SDK and one-shot JSON results report the provider/model that
actually served the turn, including after failover.

The same end-to-end JSON CLI probe exposed a related completion-ownership gap:
one runtime turn produced two durable `turn.completed` rows because JSON-mode
`HeadlessUI.emit_result()` invoked the runtime event enricher a second time.
The earlier Batch 4 fix covered `stream-json` only. JSON mode now reuses the
already authoritative runtime completion envelope for its final document,
merging result-only fields without re-enriching or re-persisting it. This keeps
the runtime event ID and turn ID identical between the one-shot JSON document
and durable replay.

Pre-fix regressions failed across all probed shared routes, failover family, SDK
failover result identity, and additive event identity. A deterministic real CLI
journey then exercised the OpenRouter route through a loopback OpenAI-compatible
HTTP endpoint. It completed successfully with `result.model` and session metadata
set to `openrouter/identity-model`, exactly one durable completion, legacy
`event.model=identity-model`, additive
`event.model_id=openrouter/identity-model`, identical JSON/durable event IDs and
turn IDs, and integer provider usage counters. A fresh live OpenRouter rerun was
not attempted after the remote-command platform rejected transmitting the
user-supplied credential; earlier Batch 3 live OpenRouter evidence remains the
external-provider proof.

The broader provider/loop/session/SDK/headless/API verification set passes
**246 tests**. Repository Ruff and mypy pass. The complete local pytest gate
passes **2,307 tests with 7 skips**; `uv build` passes; and the installed-wheel
smoke passes on Python 3.12. The dirty local source-tree build still includes
intentional unrelated untracked Python files, so clean hosted CI remains the
authoritative packaging proof.

The first hosted run for Batch 6 (`34888336631`) exposed one test-portability
failure on macOS 26 arm64 with Python 3.12 while Ubuntu 3.11/3.12 and macOS
3.11 passed. `test_browser_proxy_close_settles_accepted_connections` asserted
that the client-side `StreamWriter.is_closing()` becomes true when Ash closes
the accepted server-side stream. That is not a portable peer-close contract;
the portable observation is EOF (or a connection reset) on the client reader.
Production `BrowserPolicyProxy.close()` already closes/waits all accepted
server writers and settles its connection tasks, so the correction changes only
the regression assertion. The corrected test passed 100 consecutive local runs,
the complete browser-proxy file passed, and the full repository gate remained
**2,307 passed with 7 skips**, with Ruff, mypy, build, and installed-wheel smoke
all green. The replacement hosted-CI result is recorded at the next checkpoint.

#### Batch 6 CI portability correction

Hosted CI run `34888336631` for commit `3f6b072` passed both Ubuntu jobs and
macOS/Python 3.11, but macOS/Python 3.12 failed the existing browser-policy
proxy shutdown regression. The proxy itself had settled all Ash-owned
connection tasks and writers; the failing assertion inspected the peer
client's `StreamWriter.is_closing()` immediately after the server closed its
accepted stream. That flag describes the local writer transport and is not a
portable signal that the remote peer has closed. The regression now verifies
the externally observable contract instead: after `BrowserPolicyProxy.close()`,
the connected client receives EOF (`b""`) or a connection reset within the
bound. This preserves the stronger requirement that accepted connections are
actually terminated rather than weakening the shutdown check.

The corrected shutdown regression passes **50/50 repeated runs on Python
3.12.13** locally. The browser-proxy/browser-tool/Playwright-adjacent set passes
**27 tests with 4 environment-dependent skips** on Python 3.12. The complete
Python 3.12 repository gate passes **2,307 tests with 7 skips**; Ruff and mypy
pass; `uv build` passes; and the installed-wheel smoke passes under Python
3.12. Clean replacement hosted CI remains the final portability proof.

### Re-audit continuation — profile/config, supply chain, and workspace identity

The next uncommitted re-audit pass re-opened earlier assumptions rather than
treating the Batch 1–6 implementation or this audit as authoritative. It found
and repaired several concrete integrity gaps.

Profile setup no longer publishes persisted `.env` settings directly into the
process environment. `save_env_values()` now only persists the selected
profile, while `AshConfig.load()` owns precedence and publication of runtime
credentials. This prevents setup-written `ASH_*` values from masquerading as
operator environment overrides and prevents one profile's settings from
leaking into another. Cross-profile regressions cover model, temperature,
database-directory, and provider-credential state.

Legacy config migration now validates the configuration it will actually
persist before any durable migration write; reloads the migrated config before
making follow-up setup decisions; binds approval, backup, and migration records
to the exact reviewed source snapshot; and tolerates a later source rewrite
without leaving a half-applied destination migration. An independent GPT-6
review found two additional defects which were reproduced before repair: a
valid legacy `model_name` could mask an invalid higher-precedence TOML `model`
during preflight, and migration record creation/lookup used inconsistent source
path normalization. Both now have regressions.

Search no longer depends on host ripgrep, so the stale ripgrep tool description
and `ash doctor` warning were removed. The old search boundary races remain
covered by descriptor-scoped regressions. Separate real Git probes placed
workspace-controlled `git-upload-pack` and `git-remote-https` executables first
on `PATH`; neither was executed by plugin Git installs, so that clone-time PATH
concern was classified as not a finding rather than patched speculatively.

Release/update auditing confirmed that GitHub immutable releases lock the
associated tag/assets as assumed by Ash's update flow. A separate reproducible
transport inconsistency showed the wheel installer would accept an HTTP
downgrade to an otherwise allowlisted GitHub asset hostname. The installer now
requires the final download URL to remain credential-free HTTPS before any
package-manager mutation, matching the standalone bootstrap's existing policy.

The memory re-audit exposed a broader workspace-boundary defect. Replacing the
entire workspace directory with another ordinary directory at the same pathname
after Ash startup allowed the replacement tree to be indexed into the original
workspace's durable memory. The root cause was shared: `SafetyGuard` scoped by
canonical pathname but did not pin the selected root directory's identity.
`SafetyGuard` now captures the root identity for the runtime lifetime, rejects
steady-state replacement, and descriptor-scoped filesystem operations verify
the root descriptor they actually opened. A swap-after-validation regression
proves the descriptor check closes the remaining TOCTOU window. Project-memory
search also fails closed while the selected root is displaced so original
memory cannot be injected into a replacement workspace. The broader affected
safety/search/command/memory/repo-map set passes **246 tests**.

Memory documentation now states the privacy consequence of the explicit OpenAI
embedding option: indexed chunk text and memory-search queries are sent to
OpenAI for embedding; the default lexical mode and ONNX embedding mode remain
local.

Current focused verification after these repairs: the
profile/config/setup/install/update slice passes **355 tests**; the shared
safety/search/command/memory/repo-map slice passes **246 tests**; the dedicated
install/update/installer slice passes **88 tests**; and the search/doctor slice
passes **52 tests**. Ruff, repository mypy, and `git diff --check` pass. These
changes remain uncommitted at this checkpoint; a new complete repository gate,
artifact build, installed-wheel smoke, and hosted CI have not yet been claimed
for this continuation.

### Re-audit continuation — control-plane root races and secret-output coverage

The next security pass focused on the still-Partial path-bypass and secret-leak
row rather than re-running already-proven filesystem cases. It confirmed two
additional workspace-root substitution paths and one broader redaction gap.

First, REPL workspace-aware commands were not gated by the runtime's pinned
workspace identity. After replacing the selected workspace directory with a
different ordinary directory at the same pathname, `/reload-plugins` could
rebuild project skill/MCP/plugin state from the replacement tree even though a
normal model turn or scoped file operation would already refuse the displaced
runtime. The REPL now refuses workspace-aware work after displacement while
keeping `/help`, `/status`, and `/exit` available. The core MCP reload/reconnect
and executable-plugin reload methods also verify the runtime root immediately
before reconfiguration, so a swap between the REPL preflight and publication
cannot start replacement components; rejected unpublished plugin tools are
closed before the failure is returned.

Second, a cached project instruction skill could be redirected after turn
entry. A reproduced attack discovered the original skill, replaced the entire
workspace root, and then made `read_skill_resource` return a marker from the
replacement workspace. An even narrower regression swapped the workspace
immediately after the tool's root check and reproduced the same leak, proving
that a pathname preflight alone was insufficient. Live skill discovery now
captures the exact package-directory identity used to read `SKILL.md`.
Resource listing and reads reopen that package only when the same inode remains
visible and traverse descendants through held directory descriptors. Both the
steady replacement and post-check TOCTOU regressions now fail closed.

An independent GPT-6 adversarial review also reported hard links as a possible
workspace read bypass. That candidate was deliberately **not** patched: Ash's
documented local security boundary already defines a pre-existing hardlink
reachable under the selected workspace root as a workspace file, because link
count cannot establish whether another alias is outside the root. Changing that
policy here would silently redefine the threat model rather than repair an
implementation violation.

The review's second finding was confirmed and generalized. The secret-candidate
scanner recognized GitHub tokens, while `redact_text()` allowed them through to
tool/event output. The same mismatch existed for Slack tokens and Stripe live
keys. Those bearer-secret formats now share the output-redaction path, and the
durable runtime-event regression proves a synthetic GitHub token cannot survive
event persistence/replay. The same audit exposed a multiline class: PEM private
keys were detected by the scanner but were not redacted. `redact_text()` now
removes complete or unterminated private-key blocks, while `StreamingRedactor`
enters a bounded withholding state from `BEGIN ... PRIVATE KEY` through the
end marker. Split-at-every-position tests cover the streaming parser, and a real
background-process regression emits a synthetic private key across timed child
process writes and proves polling exposes only the redaction marker plus safe
trailing output.

Focused verification for this continuation is green: the REPL/instruction-skill/
secret-redaction/background-process files pass **116 tests**; plugin-runtime and
MCP-runtime files pass **118 tests**; the instruction-skill file passes **20
tests**; the secret-redaction file passes **66 tests**; and the durable event
redaction regression passes. Ruff passes on all touched files, repository mypy
passes across **195 source files**, and `git diff --check` passes. These are
focused re-audit results, not a claim of a new complete repository/build/hosted
CI gate.

Docker executable-plugin provenance was also re-evaluated rather than preserving
the existing portability fallback. The prior runtime used immutable
descriptor-anchored plugin capture only when `supports_anchored_mutation()` was
true; on a Docker-capable supported-host build lacking those primitives it fell
back to the ordinary live host bind. That fallback silently weakened executable
plugin code identity. The contract is now fail-closed: Docker executable plugins
require anchored immutable capture and daemon-volume staging, and no live-bind
fallback is attempted when the capability is unavailable. The degraded-build
regression was inverted first and failed on the old behavior; Docker-focused
plugin-runtime tests now pass **5 tests**.

The same check-versus-use question was then applied to live project instruction
refresh. Turn entry already verified the selected workspace identity, but
`_build_messages()` refreshed `ASH.md`/`AGENTS.md`/`CLAUDE.md` through a loader
that re-anchored itself to the current workspace pathname. A regression swapped
the whole workspace immediately after `run_turn()` passed its entry identity
check and reproduced provider prompt contamination from the replacement
`AGENTS.md`. Live runtime instruction discovery now receives the runtime's
pinned `SafetyGuard`; every project instruction/import read uses descriptor-
scoped snapshots bound to that guard. A post-entry root replacement therefore
raises before provider dispatch instead of preserving or ingesting replacement
instructions. User-global `~/.ash/ASH.md` remains a separate user-owned source
and retains its existing bounded reader. The complete affected runtime plus
instruction-discovery files pass **53 tests**, with Ruff, targeted mypy, and
`git diff --check` green.

### Re-audit continuation — mixed-generation extension provenance

The next path-boundary pass tested a subtler whole-root race than persistent
replacement: workspace A is selected and pinned, A is temporarily replaced by
B only while an extension/config loader runs, and A is restored before the next
ordinary root check. This A→B→A pattern can leave a runtime looking healthy
while cached metadata still belongs to B.

The first confirmed case was project plugin discovery. A replacement manifest
could declare a hook/component that the restored original plugin did not
declare, allowing Ash to combine B's validated manifest with A's live plugin
tree. `DiscoveredPlugin` now records the plugin-root directory identity captured
after validation; every skill/command/agent/hook/MCP component-path access and
executable-plugin construction requires that same directory generation.

The same attack was reproduced against trusted project MCP and LSP config.
Project `.mcp.json` could survive root restoration as an in-memory stdio or
network server definition; project `.ash/lsp.json` could survive as a command,
override, or disable directive. MCP server configs now retain their trusted
source-root identity and revalidate it at runtime publication plus client/server
connection. Project LSP reads instead use descriptor-scoped snapshots through
the runtime's already-pinned `SafetyGuard`, which also protects transient
disable/override directives that would leave no surviving config object to
revalidate later.

Project instruction-skill metadata had a related lazy-discovery gap. Resource
reads were already inode-bound, but `list_skills` and prompt skill rendering
could return a replacement skill's cached name/description after A returned.
`SkillCatalog.list()` and `get()` now validate every cached package generation,
so metadata, activation, and resources share the same invariant.

Custom subagent definitions were also confirmed vulnerable: a replacement
Markdown definition was cached during B and its instructions reached a real
worker after A was restored. Discovered agent definitions now retain their
source-file identity, and `SpawnAgentTool` refuses a custom role when that file
generation changed. Programmatically supplied definitions retain their prior
behavior because only filesystem-discovered definitions carry provenance.

Trusted project A2A config was then reproduced with the same pattern. A
replacement `.ash/a2a.json` endpoint could remain cached and become an outbound
delegation target after A returned. Project A2A reads now use the runtime's
pinned guard; user-global A2A configuration remains an independent user-owned
source.

Finally, project/custom Markdown command templates could be cached from B and
expanded after A returned. Live-discovered commands now retain their source-file
identity, and both catalog parse and command expansion reject a different file
generation. Immutable byte-parsed command objects used by validation paths keep
no live filesystem provenance and remain compatible.

Focused verification after this batch is green: instruction-skill, custom-agent,
custom-command, plugin-registry, and plugin-runtime files pass **96 tests**;
the larger runtime/MCP/LSP group completed with **219 immediately passing plus
one stale LSP error-wording assertion**, and that strengthened root-identity
assertion was corrected; LSP/A2A follow-up coverage passes **87 tests**. Ruff
passes on all touched files, repository mypy passes across **195 source files**,
and `git diff --check` passes. This remains focused re-audit evidence, not a new
complete repository/build/hosted-CI gate.

### Re-audit continuation — hook provenance, sandbox policy, Git inspection, and LSP executable trust

The next pass finished the mixed-generation check for command hooks. Existing
`cwd_identity` protection already prevented a hook command loaded from transient
workspace B from executing after workspace A returned, but B's matcher still
remained registered and could change A's later pre-tool behavior. Trusted hook
sources now retain the runtime/plugin root identity, verify that source before
and after config loading, and preserve caller-supplied cwd identity rather than
re-snapshotting whichever pathname generation is visible. The A→B→A project
hook regression therefore fails during discovery instead of leaving a dormant
replacement hook in the registry. The complete hook-config file passes **14
tests**.

Linux sandbox assumptions were then re-checked against current upstream
Bubblewrap security information. The August 2026 advisory
[GHSA-pxhw-h44j-8pfx](https://github.com/containers/bubblewrap/security/advisories/GHSA-pxhw-h44j-8pfx)
(CVE-2026-87766) affects versions below 0.12.0 and is fixed in 0.12.0. Ash now
requires parseable Bubblewrap **0.12.0+** before considering the backend
available; the audited host is on 0.13.0. The capability probe and real launch
also require a user namespace rather than `--unshare-user-try`, disable nested
user namespaces, and assert that disablement. This fails closed to another
backend/direct approval policy if the host cannot enforce the stronger
boundary.

Bubblewrap previously mounted the host's entire `/etc` read-only. A real
regression proved sandboxed code could read the host `/etc/machine-id`. The
wrapper now creates an isolated `/etc` and mounts only bounded compatibility
inputs needed for loaders, NSS, resolver/network metadata, certificate stores,
timezone, and alternatives. Real-host probes confirmed Python, SSL, passwd/NSS,
and Git still run while machine identity is hidden. The full affected sandbox,
Git/review, hook, and LSP batch passes **184 tests with one platform-dependent
skip**.

The old "read-only Git can execute repository configuration" finding was also
re-opened rather than assuming earlier fsmonitor/textconv fixes were complete.
A real repository with `.gitattributes` plus `filter.leak.clean` caused both
`git status` and `git diff` to execute a workspace-controlled helper three
times, despite `--no-ext-diff`, `--no-textconv`, and fsmonitor disablement. The
central read-only Git path now first enumerates only the names of effective
`filter.<driver>.{clean,smudge,process,required}` keys (including included Git
config), bounds that set, and shadows every discovered driver's executable
filter entries to no-op command-scope values. Discovery and the actual Git
operation use the same sandbox/cwd/process-tree boundary and original cwd inode;
a deterministic swap between those two stages is rejected. The Git/review
files pass **46 tests** after the change.

Several older audit candidates were re-verified and found already resolved in
the current tree rather than patched redundantly: duplicate native tool-call IDs
are rejected within one completion and on reuse across a session before side
effects; stream-JSON emits one authoritative terminal completion; legacy config
migration refuses untrusted workspaces and excludes sensitive safety/sandbox
fields even when trusted; and structured redaction preserves numeric usage
token counters while redacting credential-shaped token fields.

Finally, managed LSP had one remaining implicit executable-trust mismatch.
Built-in server discovery used `workspace/node_modules/.bin/...` whenever the
workspace was trusted, so ordinary trusted checkout bytes could become a
language-server subprocess without an explicit declaration. Built-in detection
now resolves host-installed servers only. A trusted `.ash/lsp.json` may still
explicitly select a workspace-local executable, preserving intentional project
toolchains while removing implicit execution. The full LSP file passes **51
tests**.

Repository-wide static verification for this continuation is green: Ruff
passes on every touched file, mypy reports no issues across **195 source
files**, and `git diff --check` passes. Security remains **Partial**. In
particular, consistent CPU/memory containment is not yet enforced across all
isolation backends, and repository Git configuration can still reference
external attribute/ignore/include paths; those path-read semantics require a
separate least-privilege design rather than being silently disabled here.

### Re-audit continuation — Git host-path provenance and managed-worktree extension execution

The next pass closed the Git host-path design gap rather than globally disabling
user Git policy. Git's system/global scopes are treated as user/administrator-
owned, while repository `local`/`worktree` scopes remain untrusted. A real
repository-local `core.excludesFile` pointing outside the workspace made
`git status` hide an untracked file based on host data, and a local
`core.attributesFile` changed an ordinary diff into a binary diff. A local
`core.worktree` redirected status to a different directory. A synthetic
signed-looking commit plus local `log.showSignature=true` and `gpg.program`
also proved that an observational `git log` could start an external verifier.

Ash-owned read-only Git now pins `--work-tree=.` and forces
`log.showSignature=false`. Before the real inspection it performs a
`--no-includes --show-scope` provenance query through the same descriptor-bound
cwd/sandbox/process-tree boundary. Relevant repository-local/worktree
`core.attributesFile`, `core.excludesFile`, `diff.orderFile`, `mailmap.file`,
`include.path`, and `includeIf.*.path` settings are refused, while equivalent
system/global settings remain available. Effective filter-driver names may
still come from trusted global config; their executable clean/smudge/process
entries are shadowed to no-op values for Ash's read-only commands. Diff-like
plumbing commands (`diff-tree`, `diff-index`, and `diff-files`) now receive the
same external-diff/textconv suppression as `diff` and `show`.

A fresh scan then found that repository-map Git-ignore evaluation bypassed that
new provenance boundary. A real `RepoMap` reproduction configured local
`core.excludesFile` to an outside file containing `keep.py`; the map omitted
`keep.py` solely because of that host file. The config-scope policy was moved
into `ash.safety.git`, and repo-map now runs the same descriptor-bound,
scrubbed no-include provenance check before `git check-ignore`. Repository-
controlled external ignore/include policy therefore fails closed instead of
selectively shaping model context, while a user-global `core.excludesFile`
continues to work. Focused repo-map coverage passes **6 tests**; the complete
Git/patch/review group passes **56 tests**.

The fresh subprocess audit then found a separate executable Git-extension
boundary in isolated-agent worktrees. A real repository `post-checkout` hook
ran on the host during `WorktreeManager.create()` because `git worktree add`
executes checkout hooks. This was not limited to one hook: `git add` can invoke
clean/process filters, `commit --no-verify` does not suppress `post-commit`, and
merge operations can execute configured merge drivers. Since coder/tester
subagents automatically use managed worktrees, repository Git metadata could
therefore execute host-side programs before or during ostensibly isolated
worker lifecycle operations.

Managed worktree Git now enforces one central host-side policy across create,
commit, artifact merge, apply, reset, cleanup, and branch management. Every
operation uses a scrubbed child environment, a validated Ash-owned empty hooks
directory, a pinned worktree, disabled fsmonitor/signature/external-diff/
textconv behavior, and the same original cwd identity across provenance
preflight plus the actual mutation. Repository-local/worktree config defining
clean/smudge/process filters, merge drivers, external diff/textconv helpers,
local include chains, or external attributes/excludes/order/mailmap/worktree
paths is rejected before mutation. User/system Git configuration remains
available by design. Regressions prove `worktree add` does not run
`post-checkout`, Ash's commit does not run `post-commit`, and repository-local
executable filter/merge-driver configuration is refused without launching its
helper. The complete worktree file passes **27 tests**, and both live
agent-tool worktree integration tests pass.

Aggregate resource containment was researched separately. Docker exposes
cgroup-backed memory/CPU/PID controls, while Bubblewrap itself does not provide
an equivalent aggregate resource-budget interface. Per-process POSIX rlimits
would not provide equivalent tree-wide memory/CPU semantics and can introduce
user-wide behavior for process-count limits. No Docker-only or superficial
rlimit patch was made; consistent cross-backend aggregate resource containment
therefore remains an explicit **Partial** design gap.

### Current-tree resolution ledger for historical audit candidates

The earlier candidate sections above are retained as the evidence trail from
the state in which each defect was originally reproduced. They must not be read
as current unresolved findings. Re-verification against the present tree shows:

- macOS `sandbox-exec` disappearance now fails closed unless scoped fallback was
  explicitly enabled, and the backend is correctly classified as partial
  isolation;
- stopped/exited background jobs no longer consume live-job capacity, and
  terminal history is independently bounded;
- numeric token-usage fields retain numeric types through structured
  redaction while credential-shaped token fields remain redacted;
- stream-JSON emits one authoritative terminal completion;
- duplicate native tool-call IDs are rejected before side effects both within
  one completion and on reuse in the session;
- legacy project-config migration refuses untrusted workspaces and excludes
  user-owned safety/sandbox/storage fields even after trust;
- provider/model IDs, approval tool names/arguments, persisted storage
  diagnostics, session metadata, and free-form terminal output pass through the
  shared terminal/bidi-control renderer in human surfaces;
- Ollama child output is incrementally terminal-sanitized and bounded, with
  OSC/CSI/OSC-52/BEL/bidi regressions;
- repository-map DOT output uses a dedicated string-literal quoter with an
  adversarial filename regression; and
- built-in LSP autodetection resolves host-installed servers only; workspace
  executables require explicit trusted project LSP configuration.

This ledger supersedes the old `No fix has been applied` wording for current
prioritization without deleting the historical reproduction evidence.

### Re-audit continuation — MCP no-cwd executable provenance

The fresh subprocess inventory found a second current launcher bug in MCP
stdio startup. `MCPServerConfig.resolved_command` only expands environment
variables; it did not resolve a bare executable. Both `MCPServerManager` and
`MCPClient` therefore handed a no-cwd command such as `server` directly to the
OS with the scrubbed-but-still-user-derived PATH. A real regression prepended
the active workspace to PATH, placed an executable `mcp-shadow-server` there,
and showed the manager launched it. This bypassed the workspace-shadow
protection already used when an MCP cwd was configured.

Manager and client now share one stdio launch resolver. Bare commands are
resolved to an absolute host executable through the configured child PATH while
excluding the relevant trusted cwd/source/current workspace root. Explicit
paths remain an intentional opt-in but are canonicalized and checked for an
executable regular file. For trusted project MCP configuration with no explicit
cwd, the pinned `source_root` is now both the deterministic base for relative
commands and the launch cwd, using the already-recorded source-root inode as
the expected cwd identity. User-global configs without cwd/source-root retain
their current-directory semantics but still receive an absolute host-resolved
bare executable before spawn.

Regressions prove both manager and async client reject a workspace-shadowed
bare command before launch while an explicit trusted-project `./server`
continues to work from its source root. The complete MCP config/server file
passes **59 tests**, reconnect coverage passes **22 tests**, and the full MCP
tools/runtime file passes **83 tests**. Ruff, targeted mypy, and
`git diff --check` are green for the touched MCP files.

### Re-audit continuation — Playwright setup/doctor child-process containment

The launcher inventory then found that browser setup/diagnostics were weaker
than the browser runtime itself. `setup_browser()` and doctor Playwright probes
used one-off `subprocess.run(..., timeout=...)` calls with the full parent
environment. That unnecessarily exposed unrelated provider/API credentials to
the Playwright/Node child process, and a timeout only guaranteed termination of
the direct child rather than any downloader/browser descendants it had spawned.

Browser setup and diagnostics now share a managed synchronous child-process
boundary. It keeps only operational Playwright, proxy, certificate, and normal
safe environment variables; unrelated provider credentials are omitted. The
child is launched in a managed process group using the same process-tree
preflight/cleanup primitives as other Ash-owned subprocesses. Timeout or other
exception paths terminate the whole descendant tree before returning.

A real POSIX regression starts a timed-out parent that forks a delayed child;
the child never reaches its marker write after timeout, proving tree-wide
cleanup rather than parent-only termination. Environment coverage proves a
synthetic `OPENAI_API_KEY` is absent while explicit HTTPS proxy and Playwright
browser-path settings remain available. The complete setup wizard passes **88
tests**, doctor passes **31 tests**, the new browser-process file passes **2
tests**, and targeted Ruff/mypy are green.

### Re-audit continuation — inherited Git environment and restore identity contract

The next direct-launch pass found two small Ash-owned Git probes that still
trusted the parent shell's Git environment: the interactive status-line branch
probe and storage debug-bundle revision probe. A real reproduction set
`GIT_DIR`/`GIT_WORK_TREE` to an outside repository while selecting a different
workspace; `git_branch()` displayed the outside branch and the debug bundle
recorded the outside commit. Descriptor-pinned cwd was insufficient because Git
was explicitly redirected by inherited environment variables.

Both probes now use the centralized scrubbed read-only Git environment and
read-only argv policy, so parent `GIT_DIR`, `GIT_WORK_TREE`, `GIT_CONFIG_*`,
pager/fsmonitor/signature settings, and related Git-specific variables cannot
redirect Ash's own metadata lookup. Regressions create two real repositories,
set hostile Git redirection to the outside one, and prove status/debug metadata
still comes from the selected workspace. The full status-line file passes **7
tests** and the storage file passes **37 tests**.

That full storage run also exposed two stale concurrency expectations around
restore. `SessionStore` deliberately pins the database inode, while
`restore_database()` atomically replaces that inode under exclusive
coordination. The old tests expected writers that had already pinned the old
database to continue automatically after restore. Silently repinning on any
inode change would weaken the path-swap defense, and the CLI already tells the
user to **stop other Ash processes** before restoring. The safer contract is
therefore explicit fail-closed behavior: waiting writers remain quiesced while
publication occurs, then pre-existing stores/processes reject the new inode and
must be restarted/reopened. The restore CLI now prints that restart reminder
after success. Both same-process and cross-process concurrency regressions prove
the writer cannot publish into restored state, while a newly opened
`SessionStore` sees exactly the restored backup. Ruff, targeted mypy, and
`git diff --check` are green for this slice.

### Re-audit continuation — release metadata redirect pinning

The outbound-fetch review found one remaining trust inconsistency in Ash's own
release flow. The first-install bootstrap already constrained redirect hosts for
installer assets, and the installed release-wheel downloader validated its
final asset host plus SHA-256/declared size, but release **metadata** itself was
still fetched through default `urllib` redirect behavior. `ash update --check`
had the same issue for both latest-release metadata and the tagged
`pyproject.toml` verification request. No authorization header was exposed, but
silently changing the endpoint that selects the release/digest is an avoidable
trust-boundary expansion.

All release-metadata fetches now require the final response to remain on HTTPS
`api.github.com` at the exact requested path/query. This applies to the
generated first-install bootstrap, the package installer used for exact release
refs, and `ash update --check` (including package-version verification).
Artifact delivery remains intentionally different: GitHub release assets may
use the expected `github.com` / `release-assets.githubusercontent.com` delivery
flow and remain bounded by declared size plus SHA-256 verification.

Redirect regressions prove release/package metadata from an unexpected host is
rejected before parsing or package-manager mutation. The update file passes
**17 tests**, the generated bootstrap passes **14 tests**, and the complete
installer file passes **61 tests**. Ruff, targeted mypy, and
`git diff --check` are green for this release slice.

### Re-audit continuation — browser download identity and debug-bundle privacy

The next file-use pass found a small check/use window in browser downloads.
Playwright returns a temporary host pathname; Ash previously called `stat()` on
that name and then reopened it separately to copy bytes into the workspace. A
replacement or symlink inserted between those operations could redirect the
copy to a different host file. Browser download ingestion now uses the shared
bounded no-follow reader, which opens once, verifies the opened descriptor is a
regular file, and enforces the byte limit on that descriptor. A regression
replaces the Playwright temp path with a symlink to an outside synthetic secret
and proves the read is refused. The full browser-tool file passes **50 tests**.

The same review challenged the documented “redacted debug bundle” claim. The
bundle was secret-light and atomically written, but it serialized absolute
workspace and session-database paths, which can disclose usernames/home layout
when users attach the artifact to bug reports. Debug bundles now replace the
workspace root with `<workspace>`, render database locations as
`<workspace>/…`, `<home>/…`, or `<external>/<filename>`, and apply secret
redaction to the model/diagnostic text before JSON serialization. Ordinary
`storage check --json` remains unchanged because it is a local machine-readable
diagnostic, not a shareable redacted artifact. Focused debug-bundle tests pass
**5 cases**, the full storage file passes **37 tests**, and Ruff/targeted mypy/
`git diff --check` are green.

### Re-audit continuation — audit persistence redaction and human rendering

The shareable-artifact review then found that audit-log redaction depended on
callers rather than the persistence boundary. Most runtime callsites already
passed sanitized fields, but `SessionStore.append_audit_log()` itself hashed and
stored `target_resource` plus `details` exactly as supplied. One missed caller
could therefore durably persist a provider key or other credential and carry it
into later audit export.

Audit persistence now applies the canonical text/structured redactors before
computing the hash and inserting the record. The tamper-evident chain therefore
covers exactly the redacted durable representation; reloading and verification
need no special cases. A regression writes a synthetic provider key in both the
target and nested details, proves it is absent from the returned/stored record,
and verifies the hash chain remains valid.

Human audit rendering was tightened independently for historical/external data:
session identifiers, targets, and verification-error strings pass through
secret redaction plus terminal/bidi escaping before display. JSON and audit
export continue to represent the durable record rather than a display-mutated
copy. The audit CLI file passes **10 tests**, the complete session file passes
**44 tests**, and Ruff/targeted mypy/`git diff --check` are green.

### Re-audit continuation — structured logging claim made real

The observability pass found a concrete parity-claim mismatch. Ash imported a
module named `ash.logging` and used Loguru, but the implementation only emitted
formatted stderr text. It had no structured file sink, no rotation, no central
redaction/terminal escaping, and no bound correlation context, while the
production parity table claimed all of those properties. The existing
`temporary_level()` helper was also non-functional: it called Loguru's
`configure()` with unsupported `partial`/`level` keyword arguments and raised a
`TypeError` when exercised.

The logging boundary now retains the normal human stderr surface but centrally
redacts secrets, bounds messages, and renders control/bidi characters visibly
before emission. The already-documented `ASH_DEBUG=1` mode additionally enables
a private `~/.ash/logs/ash.jsonl` sink rather than creating persistent logs for
ordinary invocations. That sink is rooted through descriptor-anchored directory
I/O, refuses symlinked/non-regular log paths, writes mode-0600 files under a
mode-0700 log directory, caps each JSON record, rotates by size with bounded
retention, and fails closed locally without making logging an Ash availability
dependency. Structured extras and exception messages are redacted; raw
tracebacks are intentionally not persisted.

Correlation uses `contextvars` rather than synthetic IDs. Normal turns bind the
actual durable session ID and generated turn ID, and the actual persisted tool
call ID is temporarily bound as `operation_id` around tool execution. The outer
turn `finally` restores prior context on normal completion, errors, and
cancellation, while async child tasks inherit the active context naturally.
One stale `%s` Loguru call was corrected to `{}` formatting, and
`temporary_level()` now rebuilds/restores the active sinks correctly.

The dedicated logging file passes **7 tests**, including redaction/control
escaping, rotation, symlink refusal, disabled-mode behavior, temporary-level
restoration, a real turn correlation regression, and a real tool-operation
correlation regression. Existing CI/color logging checks pass **2 tests**, and
native-tool plus cancellation compatibility checks pass **2 tests**. Ruff,
targeted mypy, and `git diff --check` are green for this slice.

### Re-audit continuation — interactive prompt-history privacy and retention

The local-persistence review found that interactive prompt history was already
well protected against pathname attacks: the file is opened through anchored
no-follow I/O, existing permissions are repaired to `0600`, and redirected
parents/files fail closed. Its **contents**, however, were persisted exactly as
typed. That created a privacy inconsistency: session messages and audit/log
surfaces could redact a pasted provider key while `~/.ash/history` retained the
raw secret for future history completion.

`PrivateFileHistory` now applies Ash's canonical structured/string redaction
before writing the persisted copy, including signed-URL credentials, while the
in-memory prompt delivered to the active turn remains unchanged. Prompt history
also has explicit storage bounds: each persisted entry is capped at **512 KiB**
and the history file at **2 MiB**. When the cap is reached, Ash reads only a
bounded recent tail, aligns it to complete prompt-toolkit entries, retains as
much recent history as fits, and appends the new redacted entry under an
exclusive file lock. Legacy oversized history is therefore reduced without an
unbounded read. Oversized persisted entries receive a visible truncation marker
without changing the submitted prompt itself.

Focused prompt-history coverage passes **9 tests**, and the complete
prompt-input file passes **15 tests**. The regressions cover secret/signed-URL
redaction, bounded recent-tail retention, oversized single-entry handling,
private permissions, symlink refusal, and parent-swap behavior. Ruff, targeted
mypy, and `git diff --check` are green.

### Re-audit continuation — OAuth private-store concurrent mutation safety

The credential-persistence review confirmed that MCP OAuth records already use
a strong filesystem boundary: descriptor-relative traversal from a trusted
anchor, `O_NOFOLLOW`, private `0700` directories, `0600` atomic records,
bounded reads, and fail-closed behavior when those primitives are unavailable.
One concurrency gap remained in the reusable `PrivateStore.update()` primitive.
The OAuth record can contain multiple issuer bundles, but read/merge/replace was
not serialized across processes. Two Ash processes refreshing or authorizing
different issuers for the same MCP server could both read the same old record
and let the later rename silently erase the other issuer's fresh bundle.

Private-store mutations now take a persistent per-record descriptor-relative
`flock` before write/update/remove. The lock entry is itself no-follow, regular,
and mode `0600`; after acquiring the OS lock Ash verifies that the visible lock
entry still names the opened inode. Reads remain lock-free because atomic record
replacement already guarantees a complete old-or-new view. Lock files remain in
the private store intentionally: deleting/recreating a lock path between
operations can split waiters across different inodes and defeat serialization.

A deterministic two-thread regression holds the first update inside its merge
callback, starts a second update, and proves the second callback cannot enter
until the first publishes. The second then observes the first update and the
final value contains both changes. Focused lifecycle/path-swap coverage passes
**5 tests**, the complete OAuth file passes **58 tests**, and Ruff/targeted mypy
are green.

### Re-audit continuation — MCP project config literal-secret prevention

The MCP persistence review then separated filesystem safety from content
safety. `save_mcp_servers()` already writes `.mcp.json` through anchored,
atomic mode-`0600` replacement, and OAuth client secrets created through the CLI
are stored as `${ENV_VAR}` references. However, `ash mcp add --env/--header`
accepted arbitrary literal values. Because the CLI deliberately writes the
current project's `.mcp.json`, commands such as `--header
Authorization=Bearer <secret>`, `--header X-Api-Key=<secret>`, or `--env
API_KEY=<secret>` could put a real credential directly into a repository file
that users may later commit or share. Credential-like command arguments such as
`--api-key=<secret>` had the same issue.

CLI-created MCP configs now require environment indirection for fields whose
**names** are credential-bearing: secret/token/password/key/credential-style
environment variables must be exactly `$VAR` or `${VAR}`; Authorization,
Proxy-Authorization, and X-API-Key headers must be an environment reference or
a standard auth scheme followed by one (for example `Bearer ${MCP_TOKEN}`);
and long-form credential command options such as `--token`, `--api-key`, or
`--password` must use environment indirection as well. Independent
high-confidence provider/private-key detection also rejects obvious credential
literals in otherwise opaque persisted values. Diagnostics mention only the
field/option, never the rejected value. Ordinary literals such as `PORT=4312`,
`X-Tenant=acme`, and `--mode=fast` remain valid.

Existing live MCP probe regressions were migrated to set real process secrets
and persist `${VAR}` references, proving runtime expansion still delivers the
credential while the repo config remains secret-free. The focused persistence/
probe slice passes **9 tests**, the complete MCP CLI file passes **27 tests**,
and Ruff/targeted mypy are green.

### Re-audit continuation — strict structured-log JSON

The structured debug sink was already redacted and bounded, but Python's
default JSON encoder permits `NaN`, `Infinity`, and `-Infinity`. Those tokens
are not valid RFC JSON and can break downstream tooling that uses a strict
parser. Structured log extras now normalize non-finite floats to the strings
`"nan"`, `"inf"`, and `"-inf"`, and both the normal and oversized-record JSON
encoders use `allow_nan=False`. A regression parses the emitted JSONL with a
decoder that explicitly rejects non-standard constants. The complete logging
file now passes **8 tests**, with Ruff/targeted mypy and `git diff --check`
green.

### Re-audit continuation — strict JSON at bounded IPC/config boundaries

The parser inventory found three bounded trust boundaries still using ordinary
`json.loads`: automation parent/child IPC, the isolated MCP schema-validator
response, and the extension-inventory preflight of hook JSON. Ordinary
`json.loads` silently accepts duplicate object keys and non-finite constants,
so two components could disagree about the same nominal payload depending on
which parser consumed it.

Those boundaries now use Ash's shared `strict_json_loads`, which rejects
duplicate object keys plus `NaN`/`Infinity` constants. Wire formats are
unchanged; ambiguous input simply fails closed as malformed protocol/config
data. Focused regressions cover duplicate automation request fields, duplicate
automation result fields, duplicate isolated schema-worker result fields, and
duplicate hook-config fields. The broader automation subprocess slice passes
**18 tests**, the MCP schema slice passes **18 tests**, the hook-inventory
regression passes, and Ruff/targeted mypy/`git diff --check` are green. During
that compatibility pass one automation test was updated to assert the current
stronger `SafetyGuard` diagnostic (`project root identity changed`) rather than
the lower-level subprocess wording; the fail-closed behavior itself was
unchanged.

### Re-audit continuation — instruction-skill YAML structural budgets

The structured-config pass re-opened Agent Skills YAML frontmatter rather than
assuming `yaml.SafeLoader` solved every untrusted-input risk. SafeLoader blocks
arbitrary Python object construction, and Ash already rejected duplicate
mapping keys plus bounded the whole `SKILL.md` to 512 KiB. However, upstream
PyYAML still does not impose a general nesting-depth bound; the open PyYAML
issue [#895](https://github.com/yaml/pyyaml/issues/895) demonstrates deeply
nested input reaching `RecursionError`. YAML aliases/recursive anchors also add
structural complexity that Agent Skills frontmatter does not need.

Instruction-skill frontmatter now has independent parser budgets: at most **64
KiB** of YAML frontmatter, **32** nested composition levels, and **4096** YAML
nodes. YAML aliases are rejected entirely before construction. The existing
duplicate-key rejection remains in place. These limits apply only to metadata;
the skill instruction body keeps the existing 512 KiB whole-file boundary.

Focused adversarial coverage passes **5 tests** for aliases, excessive nesting,
node count, frontmatter size, and duplicate mapping keys. The complete
instruction-skill file passes **25 tests**, with Ruff/targeted mypy and
`git diff --check` green.

### Re-audit continuation — notification previews and canonical URL redaction

The terminal/desktop surface review found that OSC-9 notification messages were
control-safe and bounded to 200 characters, but preview text was not secret-
redacted. With `notification_include_preview=true`, the assistant response was
embedded directly into the notification payload. That option is intentionally
available in trusted project configuration, so notification rendering itself
must enforce the same privacy policy as sessions/logs rather than relying on
callers.

The first focused regression exposed a broader inconsistency: `redact_text()`
removed provider keys, assignments, sensitive headers, and private-key material
but did **not** redact signed-URL query credentials such as
`X-Amz-Signature`; `redact_value()` did, because it routed strings through the
URL-aware redactor. Instead of patching notifications alone, Ash's canonical
human-text redaction API was refactored so public `redact_text()` now includes
URL credential scanning. URL internals use a private non-URL pattern primitive
to avoid recursive re-entry while preserving the existing nested/encoded URL
handling.

Notifications now therefore redact credentials before removing terminal/bidi
controls and bounding the OSC payload. The canonical secret-middleware plus
notification suite passes **72 tests**. A broader logging/history/audit/session-
export consumer slice passes **48 tests**, with Ruff/targeted mypy and
`git diff --check` green.

### Re-audit continuation — structured ToolResult secret side channels

The model-visible tool boundary had one remaining structured leak. Normal
runtime installs `SecretRedactionMiddleware`, but its `after_tool()` hook only
redacted `ToolResult.output` and `ToolResult.error`. `ToolResult` also carries
`diagnostics`, `citations`, `images`, and `image_blocks`. For example,
`web_search` stores provider-returned result URLs in `citations`; a signed URL
could therefore be removed from the main JSON output yet remain raw in the
structured result that is serialized/persisted later.

The middleware now recursively redacts diagnostics, citations, and image
metadata using the canonical structured redactor. Image-block metadata is
redacted too, but opaque `data` payloads are deliberately left untouched so
base64/image content is never corrupted by text regexes. Numeric/boolean
structured values retain their types through the existing structured-redaction
rules.

A regression injects a provider key into diagnostic/citation/image metadata and
a signed URL into a citation, then proves every structured text side channel is
clean while the opaque image payload remains exact. The complete secret-
middleware file passes **67 tests**, with Ruff/targeted mypy and
`git diff --check` green.

### Re-audit continuation — bounded DNS preflight for public web fetches

The network-boundary review found that `web_fetch` already had strong SSRF
properties—public-address validation, redirect revalidation, a pinned transport,
decompressed-byte limits, content-type checks, signed-URL redaction, and a
five-redirect cap—but its initial URL validation still called
`socket.getaddrinfo()` synchronously on the async turn. A stalled libc resolver
could therefore block the event loop before the pinned transport ran. The same
module already contained a daemon-thread DNS helper with an explicit timeout,
but `web_fetch` itself was not using it.

Public fetches now separate non-network URL syntax/domain validation from DNS
validation. Each request/redirect hostname is resolved through the bounded
daemon-thread helper, and the resulting vetted public-address tuple is passed
directly into `_PinnedPublicTransport`. The actual connection therefore reuses
the exact preflight result instead of resolving the hostname a second time.
Redirects repeat the same bounded resolve-and-pin process for their new host.
This closes both event-loop DNS stalls and validate/connect DNS rebinding.

Regressions prove a DNS timeout fails before transport dispatch, the connection
receives the exact preflight address tuple, and a synthetic rebinding resolver
is invoked only once rather than getting a second private-address opportunity.
The complete web-fetch file passes **20 tests**, with Ruff/targeted mypy and
`git diff --check` green.

### Re-audit continuation — web-search result privacy and per-hit bounds

The search-provider review found that Brave/Tavily response transport was
already bounded and strict—2 MiB decompressed-byte limits, JSON content checks,
duplicate-key/non-finite rejection, explicit timeouts, and provider-specific
credential handling—but normalized **result fields** were passed through more
loosely. Provider-returned URLs were only scheme/host filtered, then emitted raw
into tool output and citations; titles/snippets/date strings were also emitted
raw with no per-hit cap. A provider result could therefore preserve signed or
OAuth-style query credentials, embed URL userinfo, or consume most of the
model-visible result budget with one oversized snippet.

`WebSearchTool` now normalizes every provider hit at the common tool boundary.
Credential-bearing or malformed URLs (embedded user/password, control
characters, unsupported scheme, missing host, or >4096 characters) are
dropped. Accepted URLs pass through Ash's URL redactor before output, so signed
query parameters remain structurally useful while their values become
`[REDACTED]`. Titles, snippets, and publication strings use canonical secret
redaction and are capped at **500**, **4000**, and **128** characters
respectively before JSON/citation emission. Ordinary results retain their
existing shape.

Regressions cover signed-result URL redaction, secret-shaped title/snippet/date
text, embedded-credential URL rejection, oversized URL rejection, and per-hit
field caps. The complete web-search file passes **11 tests**, with Ruff,
targeted mypy, and `git diff --check` green.

### Re-audit continuation — external plan-editor executable and environment trust

The residual child-process review found that interactive sprint-plan editing
launched `$VISUAL`/`$EDITOR` with two weaker assumptions than the rest of Ash's
owned subprocess surface. A bare editor name was handed directly to the OS, so
a workspace directory present on `PATH` could shadow the user's intended host
editor. The editor also inherited the entire Ash process environment, including
provider/API credentials unrelated to plan editing.

External plan editing now resolves bare editor executables with Ash's
host-executable resolver while excluding the active `TerminalUI.workspace_root`
from PATH shadowing (falling back to the process cwd only for a standalone UI
without a configured workspace). An explicitly configured path such as
`./local-editor` remains an intentional user opt-in, but is canonicalized and
must name an executable regular file. The editor child receives the normal
scrubbed environment plus only desktop/session variables needed by graphical or
terminal editors; provider credentials are not forwarded.

Regressions prove a workspace-shadowed bare editor resolves to the host copy,
ambient OpenAI/Anthropic keys are absent while display/session state remains
available, explicit workspace editor paths still work, and `_edit_plan()`
actually passes the scrubbed environment into the child. The complete terminal
UI file passes **34 tests**, with Ruff/targeted mypy and `git diff --check`
green.

### Re-audit continuation — `git apply` ambient-environment removal

The final Git-adjacent subprocess review found that `apply_patch` already
resolved the host Git executable outside the workspace, pinned cwd identity,
bounded input/output, revalidated mutation paths, and killed the process tree
on timeout/cancellation, but its `git apply` child still inherited the full
parent environment. A disposable reproduction with hostile `GIT_DIR` and
`GIT_WORK_TREE` did **not** redirect plain `git apply` writes outside the
selected cwd, so this was not recorded as a demonstrated workspace escape.
Nevertheless, provider credentials and unrelated `GIT_*` overrides had no
reason to cross into this Ash-owned mutation helper.

`git apply` now receives a scrubbed non-interactive child environment and an
explicit `--work-tree=.` argument. Ambient `GIT_DIR`, `GIT_WORK_TREE`,
`GIT_CONFIG_GLOBAL`, provider keys, and other unrelated process state are not
forwarded; pager/prompts/askpass are disabled. Existing descriptor-cwd and
post-check path revalidation remain unchanged. A spawn-level regression asserts
both the argv pin and environment exclusions, while the existing workspace-swap
and stable-cwd fail-closed regressions remain green. The complete Git/patch file
passes **50 tests**, with Ruff/targeted mypy and `git diff --check` green.

### Re-audit continuation — sandbox control-plane environment minimization

The remaining subprocess inventory showed that Ash-owned sandbox **control**
helpers still inherited the full parent environment even though sandboxed user
commands themselves already used their explicit policy environment. Bubblewrap
version/capability probes therefore received unrelated provider credentials,
and Docker daemon/image probes, Docker volume staging/cleanup, and the explicit
`ash sandbox build` command received every Ash credential in addition to the
Docker context state they legitimately need.

Bubblewrap probes now use the ordinary scrubbed environment. Docker control
commands use one shared `docker_cli_environment()` policy that preserves the
normal safe OS variables plus Docker context/host/TLS/config/platform options,
SSH agent state, and proxy settings used by real local/remote Docker setups,
while excluding provider/model credentials and unrelated application secrets.
The same policy is used by Docker readiness probes, manager volume/staging
control, and sandbox-image builds. This does not change the environment of the
actual command running **inside** a sandbox; it only minimizes Ash-owned host
control processes.

Focused regressions prove Docker context/host/SSH/proxy state survives while a
synthetic provider key does not, the async Docker manager control path receives
the same policy, sandbox-image build uses it, and Bubblewrap probes do not see
the provider key. The complete sandbox plus sandbox-CLI files pass **79 tests
with one platform-dependent skip**, and Ruff/targeted mypy/`git diff --check`
are green.

### Re-audit continuation — complete direct-subprocess environment inventory

After the sandbox control-plane cleanup, an AST-based inventory of every direct
`subprocess.run/Popen/check_*` and `asyncio.create_subprocess_exec` call under
`src/ash` reduced the remaining ambient-environment launches to three
process-tree helpers: async Windows `taskkill`, sync Windows `taskkill`, and the
POSIX fallback `ps` descendant scan. None needs provider/application secrets.

Those helpers now use Ash's normal scrubbed environment as well. Windows task
termination still receives the basic OS/path variables required to run
`taskkill`, and the POSIX `ps` fallback keeps normal locale/path state while
dropping unrelated credentials. The full process-utils file passes **32 tests
with three platform-dependent skips**; Ruff/targeted mypy/`git diff --check`
are green. Re-running the AST inventory now returns **NONE**: every direct
subprocess launch in `src/ash` either supplies an explicit `env=` or passes a
previously audited kwargs bundle that contains one. This closes the ambient
child-environment audit as a code-wide invariant rather than a list of known
call sites.

### Re-audit continuation — installer/bootstrap external JSON strictness

The external-process/download parser pass found that the public standalone
installer still used ordinary `json.loads` for `pipx list --json` and
`pipx_metadata.json`. Both inputs are bounded, but permissive JSON would accept
duplicate fields and `NaN`/`Infinity`, creating ambiguity in ownership/repair
decisions. Because `installer.py` must remain dependency-free when downloaded
as a standalone script, it now carries a tiny local strict JSON helper instead
of importing Ash internals. The helper rejects duplicate object fields and
non-finite constants and is used consistently for pipx state, pipx metadata,
and GitHub release metadata.

The tiny `ash.install` bootstrap already rejected duplicate release-metadata
keys; it now also rejects non-finite JSON constants before any installer asset
is selected or executed. Focused installer regressions cover duplicate pipx
state, `NaN`, and duplicate metadata; the complete standalone installer file
passes **64 tests**. Bootstrap coverage passes **11 tests** including non-finite
metadata rejection. Ruff/targeted mypy/`git diff --check` are green.

### Re-audit continuation — automation maintenance and project LSP JSON

Two additional JSON boundaries were made consistent with the shared strict
parser. The isolated automation-maintenance subprocess now rejects duplicate
request fields/non-finite constants just like the main automation child
protocol. Trusted project `.ash/lsp.json` configuration now uses
`strict_json_loads` as well; it already rejected duplicate keys, but could
previously accept `NaN` inside server `settings` or `initialization_options`
and later serialize non-standard JSON toward the language server.

Focused automation protocol tests pass **3 tests**. The LSP config regression
passes, the complete LSP unit file passes **51 tests**, and Ruff/targeted
mypy/`git diff --check` are green.

### Re-audit continuation — schema-worker bidirectional strict JSON

The isolated MCP schema validator originally had asymmetric parsing: the parent
now rejected ambiguous child responses, but the child still used permissive
`json.loads` on the parent's bounded request. The worker now uses
`strict_json_loads` as well, so duplicate fields and non-finite constants fail
closed in both directions of the isolated IPC protocol. Focused request and
response regressions pass **2 tests**; the complete MCP schema slice passes
**19 tests**, with Ruff/targeted mypy/`git diff --check` green.

### Re-audit continuation — trust and permission policy JSON fails closed

Workspace trust and persisted permission-grant state already rejected duplicate
JSON keys, but their local parsers still accepted Python's non-standard
`NaN`/`Infinity` constants. That mattered for policy semantics: a trust file
could contain a valid trusted workspace plus an ignored `NaN` field and still
grant trust, and a permission file could likewise apply otherwise-valid rules
despite malformed non-standard JSON.

Both stores now use the shared strict parser. Any duplicate key or non-finite
constant makes trust fail closed or raises a permission-state error before
rules are applied. The four exact regressions pass, and the complete trust plus
permission-grant files pass **50 tests**. Ruff/targeted mypy and
`git diff --check` are green.

### Re-audit continuation — provider SDK redirect credential binding

The outbound-network review checked redirect behavior rather than assuming SDK
defaults. The installed OpenAI SDK (`2.54.0`) and Anthropic SDK (`1.8.0`) both
construct default HTTP clients with `follow_redirects=True`. The underlying
`httpx`/`httpx2` redirect logic strips `Authorization` when leaving an origin,
but it does **not** generically strip credential headers such as Anthropic's
`x-api-key` or custom OpenAI-compatible `X-Api-Key` headers. A credentialed
provider endpoint could therefore redirect an Ash-owned SDK request and carry
those headers to the new destination.

Every Ash-owned inference transport now disables automatic redirects at client
construction. `OpenAIProvider` centralizes a no-redirect
`DefaultAsyncHttpxClient`; DeepSeek and Groq reuse that helper, so custom
OpenAI-wire providers inherit the same policy. Anthropic supplies its own
`DefaultAsyncHttpxClient(follow_redirects=False)`. Injected clients remain
entirely caller-owned and are not wrapped or replaced. Existing HTTPS
requirements and zero nested SDK retries remain unchanged.

Focused tests assert `follow_redirects=False` for all four Ash-owned clients and
that injected clients allocate no HTTP transport. The complete provider file
passes **41 tests**, provider-registry coverage passes **42 tests**, and
Ruff/targeted mypy/`git diff --check` are green. One unrelated stale provider
test fixture was corrected to use the same workspace for `SafetyGuard` and
`AshLoop`, matching the runtime's existing workspace-identity invariant.

### Re-audit continuation — exact provider-credential error redaction

The provider exception pass found a second credential boundary that generic
pattern redaction could not guarantee. OpenAI, DeepSeek, and Groq adapters
wrapped raw SDK exception text directly, and Anthropic SDK failures propagated
without adapter-level sanitization. A custom or buggy upstream can echo the
credential it received in its error response; short/arbitrary configured keys
do not necessarily match Ash's provider-token regexes, so they could survive
the later generic `redact_text(str(exc))` boundary.

`providers.readiness` now exposes one exact provider-error sanitizer: canonical
secret-pattern redaction runs first, then every known configured credential is
replaced exactly, longest value first. OpenAI applies it to the primary API key
and all configured default-header values; DeepSeek and Groq apply it to their
API keys; Anthropic applies it to the explicit key and the ambient
`ANTHROPIC_API_KEY` fallback. Sanitized adapter errors preserve provider names
and useful non-secret detail while never emitting the known credential value.

Focused regressions deliberately use short keys such as `tiny-k` and custom
header values that generic pattern matching would miss, then make fake upstream
SDKs echo them in exceptions. Explicit, custom-header, and ambient Anthropic
credentials are all removed. The focused slice passes **2 tests**, the complete
provider plus readiness files pass **81 tests**, and Ruff/targeted mypy plus
`git diff --check` are green.

The failover aggregator now also applies canonical redaction before retaining
or joining child-provider failures. This is defense-in-depth for injected or
custom providers that do not use Ash's built-in adapter sanitizer; it does not
change failover/retry selection. The complete failover file passes **15 tests**
with Ruff/targeted mypy/`git diff --check` green.

### Re-audit continuation — subagent report secret egress

The next secret-output pass found a boundary outside ordinary tool middleware.
`SubprocessAgent` converted runner failures directly into `AgentReport.summary`
and `artifacts`, then published those values to agent status and the SQLite IPC
channel. A real in-process reproduction made a runner raise an exception
containing a provider-shaped credential and confirmed the secret survived in
the returned report, `agent_status.current_task`, and the lead `agent_report`
message. Tool-result redaction could only sanitize a later outer tool response;
it could not repair already-persisted cross-agent state.

`AgentReport` now treats summary/artifact redaction as an invariant at report
construction, and report payload serialization re-applies the same policy in
case the mutable artifact mapping is changed after construction. This keeps
ordinary task text intact for authorized delegation while preventing
model/provider/tool output from becoming a secret side channel through status,
IPC, durable task result/error consumers, or orchestrator fallback reports.

The regression exercises the original exception path and proves the credential
is absent from the returned report, status row, and IPC payload. A second
regression mutates an artifact after report construction and proves payload
serialization still fails safe. The complete subprocess-agent plus subagent
integration slice passes **40 tests**, with Ruff, targeted mypy, and
`git diff --check` green.

### Re-audit continuation — auxiliary embedding credential errors

The same pass found that the optional OpenAI embedding adapter did not inherit
the chat-provider exception sanitizer. `OpenAIEmbedding` owned the configured
API key but wrapped raw SDK exception text in `EmbeddingBackendUnavailable`.
A fake injected client that echoed a deliberately short key (`tiny-k`) proved
the exact configured credential survived because it does not match the generic
provider-token patterns.

Exact-known-secret redaction is now a core redaction primitive rather than a
provider-readiness-only implementation detail. The existing provider sanitizer
delegates to that primitive, and the embedding adapter applies it with its own
configured API key before exposing backend failures. This preserves the prior
provider behavior while giving non-chat credential owners one shared safe
boundary instead of duplicating replacement logic.

The embedding regression proves the short configured key is removed from the
raised error. Embedding, canonical secret-redaction, and exact provider-error
coverage pass **77 tests** together; Ruff, targeted mypy, and
`git diff --check` are green for the affected source files.

### Re-audit continuation — outbound A2A credential echo containment

The adjacent protocol pass found that outbound A2A tools knew the configured
bearer-token environment variable but only applied generic text/URL redaction
to remote failures. A real tool-level reproduction configured the deliberately
short token `tiny-k`, made the remote task-status path echo that value in an
exception, and confirmed `RemoteAgentTaskStatusTool.error` returned it
verbatim. A second reproduction made a successful delegated task echo the same
token in normal response text; that value also survived into tool output.

All four network-facing A2A task tools now use one credential-aware error
sanitizer, combining canonical redaction with exact replacement of the bearer
token actually configured for that remote agent. Successful delegated/status
response text passes through the same exact-token sanitizer before JSON output.
Durable task IDs/context IDs remain protocol handles and are not rewritten;
the change is limited to human/model-facing remote text and diagnostics.

Regressions cover both a protocol error and a successful remote response using
the short token that generic token patterns intentionally cannot recognize.
The complete A2A unit file passes **39 tests**, with Ruff, targeted mypy, and
`git diff --check` green.

### Re-audit continuation — MCP exact transport-credential echo containment

MCP had the same class of issue across a deeper boundary. Generic bounded MCP
diagnostics correctly redact structured secret fields and recognizable token
patterns, but the HTTP client did not bind output redaction to credentials it
actually sent. A failing real-client regression connected with OAuth, sent
`Bearer tiny-k`, then had the server echo `tiny-k` in a JSON-RPC tool error;
both the error message and nested `data` retained the credential. A separate
tool-level regression returned the same token in a successful MCP text result
and confirmed that it survived even after ordinary `SecretRedactionMiddleware`.

`MCPClient` now records the exact credential values carried in configured
`Authorization`, `Proxy-Authorization`, and `X-Api-Key` headers, plus a bounded
set of the most recently generated OAuth authorization values. OAuth values are
captured when the header is actually generated, so refreshed/rejected tokens
are covered without re-reading private credential storage. Remote JSON-RPC
error message/data are sanitized before `MCPProtocolError` escapes the client.
For successful tool results, `MCPTool` invokes the client-owned sanitizer only
after MCP protocol/task handling, preserving internal session/task IDs and
validation semantics while removing exact sent credentials from surfaced
content. Runtime exception rendering applies the same defense in depth.

The OAuth regressions prove exact-token removal from nested protocol errors and
normal tool output. A non-OAuth regression proves an exact configured
`X-Api-Key` is also removed from successful remote content. The broader OAuth,
transport, modern-protocol, and MCP tool-runtime slices pass **263 tests**;
Ruff, targeted mypy, and `git diff --check` are green for the affected files.

### Re-audit continuation — web-search provider credential echoes

The next external-output pass found the same exact-credential issue in live web
search. Brave sends `BRAVE_SEARCH_API_KEY` as `X-Subscription-Token`; Tavily
sends `TAVILY_API_KEY` as a bearer token. Search result title, URL, snippet, and
published-date fields are provider-controlled, but normalization previously ran
only canonical pattern/URL redaction. A failing Brave regression used the short
configured key `tiny-k`, asserted that exact value was sent on the request, then
returned it in normal result fields. The key survived into both tool output and
citations because its shape intentionally does not match generic provider-token
patterns.

Each built-in search adapter now redacts the exact credential value it used for
that request before returning provider-controlled fields. URL handling keeps the
stronger pre-existing invariant that results containing embedded URL userinfo
are rejected: exact-key sanitization does not first transform an unsafe
`user:password@host` URL into an apparently safe one. Safe URLs can still have
an echoed provider key removed from path/query text before later normalization.

The full web-search unit file passes **12 tests**, including exact short-key
echoes, signed URL redaction, bounded fields, provider fallback, and embedded
credential URL rejection. Ruff, targeted mypy, and `git diff --check` are
green.

### Re-audit continuation — complete MCP surfaced-output boundary

PR-style review of the first MCP fix found two important follow-ups. First,
sanitizing the raw `tools/call` result before output-schema validation could
change protocol semantics. Raw MCP values are now validated first; only the
diagnostic/rendered representation is exact-credential-redacted. A regression
uses an output schema whose `const` is the echoed credential: validation passes
against the original structured result while model-facing output contains only
`[REDACTED]`.

Second, authenticated MCP has several surfaced-data paths beyond `tools/call`.
`resources/read`, `prompts/get`, resource/prompt/template listings, task
list/cancel results, runtime error state, refreshed tool catalogs, subscription
failure events, and modern durable task-state callbacks could otherwise carry
the same exact short OAuth/header credential. These runtime boundaries now use
the client-owned sanitizer before data is exposed or persisted, while the MCP
client continues to perform protocol/session/task validation on the original
wire values. Tool definitions are sanitized before becoming model-facing Ash
tools on initial discovery, replacement, and refresh.

A real HTTP/OAuth resource regression proves `resources/read` cannot echo the
short bearer token into `mcp_read_resource` output. Combined OAuth, transport,
modern-protocol, MCP tool-runtime, and web-search validation now passes **277
tests**; Ruff, targeted mypy, and `git diff --check` are green for all affected
source files.

### Re-audit continuation — sensitive tool arguments in durable state

The persistence/telemetry pass found a different class of secret leak in the
core tool lifecycle. Ash deliberately persists approved tool intent before
execution so crash recovery can distinguish an unstarted call from a dispatched
one. `ToolCallRecord.arguments`, audit/event payloads, and persisted assistant
`metadata.tool_calls` previously used only generic value redaction. Arbitrary
sensitive form values under neutral field names therefore survived unchanged.

`browser_type` is the concrete reproduction. The browser correctly refuses
`input[type=password]`, but that refusal happens after the approved call has
already crossed Ash's durable-intent boundary. A short synthetic password
(`tiny-k`) supplied as `browser_type.text` was therefore eligible to be written
to session history/audit state even though the browser never typed it.

Tools can now declare top-level `sensitive_argument_fields` as class metadata.
The core persistence serializer combines that declarative policy with canonical
secret redaction before building tool-call records, audit/event argument
payloads, budget-rejected calls, and persisted assistant tool-call metadata.
The live approval/execution path still receives the original arguments; this is
intentional so users can make an informed one-time approval and the tool can
perform the requested operation. Sensitive-field discovery reads validated
class metadata only, so preparing a pre-approval persistence record does not
invoke extension/plugin instance code. `browser_type` marks only `text` as
sensitive.

Two end-to-end regressions cover direct mediated execution and a native model
tool call. They prove the original value is absent from the loaded session,
`ToolCallRecord`, tamper-evident audit details, persisted assistant tool-call
metadata, and emitted typed events while the live call still reaches the real
tool boundary. The browser refusal itself remains unchanged.

The same review found that project/session exact-scope permission rules could
persist the sensitive value independently as an `ArgumentMatcher`. Automatic
exact scoped allow/deny now fails closed when a current tool call contains any
declared-sensitive field. Ash does not silently omit that field because doing
so would broaden an "exact" grant. The explicit scope editor may proceed only
with non-sensitive fields and tells the user which fields were excluded; one-
time and explicit whole-tool/session approvals remain available.

The complete affected loop, browser, interactive-approval, and permission-rule
suites pass **192 tests**. Ruff, targeted mypy, and `git diff --check` are green
for the affected source/test files.

### Re-audit continuation — standalone MCP probe credential echo

The remaining direct `MCPClient` command path was `ash mcp probe`. Unlike the
runtime, probe rendering consumed `client.server_info` directly and then used
only generic diagnostic redaction. A configured MCP server therefore had one
remaining opportunity to echo an exact short OAuth/header credential through
`serverInfo.name` or `serverInfo.version`. A failing probe regression modeled an
authenticated client with `X-Api-Key: tiny-k` and server identity fields that
echoed the same value; the probe payload exposed it unchanged.

`probe_mcp_server()` now passes server identity metadata through the
client-owned exact-credential sanitizer before rendering. Probe disconnect
cleanup diagnostics use the same boundary so a cleanup exception cannot undo
the protection. The other non-runtime `server_info` consumer only derives an
internal fingerprint and does not render the raw value.

The full MCP CLI unit file passes **28 tests**, including the exact-credential
probe regression plus existing cancellation and cleanup-failure coverage.
Ruff, targeted mypy, and `git diff --check` are green.

### Re-audit continuation — rewind and fork no longer preload complete source history

Explicit rewind/fork operations still loaded the full source `Session` into
Python even though both ultimately operated by message boundary. Rewind then
loaded the complete `(message_id, turn_id)` list again and materialized every
removed message ID just to delete those rows one-by-one. Fork similarly loaded
the complete transcript, then queried the full turn-ID list a second time before
performing its already set-based `INSERT ... SELECT` copy.

Both operations now establish their boundary inside the owning SQLite
transaction using one message count plus at most the adjacent retained/removed
rows. Fork preserves the assistant/tool-call-pair and Ash-turn split checks by
validating only those two rows, then copies with the existing set-based SQL.
Because `BEGIN IMMEDIATE` owns the source snapshot, the old preload-vs-current
message-count race check is no longer needed.

Rewind derives distinct removed turn IDs directly in SQL ordered by their first
message, uses the retained boundary row for the tool-call timestamp cutoff, and
deletes the transcript tail with one set-based `DELETE ... message_id >= ?`.
Usage rollback, tool-call cleanup, restored checkpoints, turn-journal deletion,
and compaction-summary reset retain their existing semantics. Unknown sessions
still fail before mutation.

Regressions instrument `load_session()` and prove fork/rewind call it only once
for the final returned session, never to preload source history. Existing
assistant/tool and turn-boundary rejection remains intact. The complete session
+ turn-recovery surface passes **58 tests**; Ruff, targeted mypy, and
`git diff --check` are green.

### Re-audit continuation — permission-mode audit transition accuracy

The audit-persistence review found a factual-integrity defect in the live REPL
permission-mode transition. `/permissions <mode>` replaced the runtime policy
and assigned `config.safety_tier = mode.value` before constructing its
tamper-evident audit record. `details.previous_mode` then read that already
mutated config value, so a real `interactive -> plan` transition was recorded
as `{"previous_mode":"plan","mode":"plan"}`. The audit hash chain remained
valid, but the event it authenticated was wrong.

The REPL now captures `loop.permission_policy.mode.value` immediately before
replacing the policy and uses that snapshot for the audit event. Runtime
behavior, approval semantics, and the resulting mode are otherwise unchanged.
A production-path regression drives `/permissions plan` through the real REPL
parser and verifies the stored audit details are exactly
`interactive -> plan`, while both runtime policy and config end in `plan`.

The combined permission CLI and REPL diagnostic files pass **30 tests**. Ruff,
targeted mypy, and `git diff --check` are green.

### Re-audit continuation — temporary subprocess owners clean up after unexpected I/O failure

Two command/helper subprocess paths already handled timeout and caller
cancellation correctly, but not arbitrary I/O failure after spawn. `ollama
pull` manually drains child stdout; if that reader raised unexpectedly, the
error escaped while the child process tree could remain alive. Hook execution
used the shared bounded `communicate_process()` helper, but a non-output-limit
I/O failure there likewise escaped without caller-owned process-tree teardown.

Both callers now use the existing shielded
`settle_process_tree_after_cancellation()` cleanup path for unexpected
non-cancellation failures. The original I/O error remains primary; process-tree
cleanup failure is attached as diagnostic context. If caller cancellation
arrives while cleanup is settling, teardown finishes first and cancellation is
propagated afterward. Hook `ProcessOutputLimitExceeded` remains excluded from
this path because `communicate_process()` already owns termination for that
specific bounded-output failure, avoiding double-cleanup.

Regressions inject an Ollama stdout-reader failure and a hook communication
failure after process creation and prove each process tree is cleaned before the
original error escapes. The complete Ollama-command + hook validation slice
passes **22 tests**; Ruff, targeted mypy, and `git diff --check` are green.

### Re-audit continuation — transactional provider/model switching

The runtime-lifecycle pass found that provider/model switching was not
transactional. `switch_model()` and `switch_provider()` assigned the newly
built provider and replacement config before generated tool-protocol
synchronization completed. `switch_provider()` then performed an additional
skills-runtime reconfiguration. If either step raised, the public switch call
failed while `AshLoop.provider`/`_config` already pointed at the replacement;
the previous provider remained live and the rejected replacement was not
retired. This left runtime state inconsistent with the reported failed switch.

Provider transitions now share one commit helper. It snapshots the active
provider, config, provider-close state, circuit key, and generated/base/current
system-prompt state; installs the candidate; performs protocol synchronization
and the provider-only skills-runtime reconfiguration; and commits only after
those steps succeed. On failure every captured runtime field is restored and
the rejected replacement provider is retired asynchronously. The previous
provider is retired only after a successful commit, and `config_changed` is
emitted only for the committed transition.

Regressions cover both failure points: a replacement whose capability access
fails during generated tool-protocol synchronization, and a provider switch
whose skills-runtime reconfiguration is injected to fail. Both prove the old
provider/config remain authoritative and the rejected replacement is closed.
The complete loop unit file passes **105 tests**; Ruff, targeted mypy, and
`git diff --check` are green.

### Re-audit continuation — MCP reload commit/cleanup consistency

The next lifecycle pass found two state-consistency defects in full MCP runtime
reload. First, active-session publication updated `_mcp_configs` by merging the
new entries into the previous map. Removing a server therefore removed its live
tools/runtime client but left the deleted server in Ash's control-plane config;
a later targeted reconnect could treat that removed server as configured. A
real reload-to-empty regression now verifies that the live config snapshot
loses the removed server while an in-flight old tool remains usable only through
its deliberately retained runtime snapshot. Successful publication now replaces
`_mcp_configs` with the complete desired config map; the existing failed-
candidate branch still returns before commit and preserves the previous map.

Second, after publishing a replacement runtime Ash synchronously closed the old
runtime. If that cleanup raised, the public reload call raised too even though
the new runtime/tools/config were already committed. The no-active-session
reload branch had the same post-commit ambiguity. Both paths now transfer the
failed old runtime into the existing retired-runtime ownership set. Ordinary
cleanup failure is logged and retried later; cancellation remains cancellation
and also retains cleanup ownership. A committed reload therefore does not
masquerade as an uncommitted failure merely because retirement cleanup needs a
retry.

The same review aligned turn-end cleanup with the earlier MCP lifecycle
contract. Retired-runtime/client cleanup after a turn is opportunistic: failed
cleanup remains owned and is retried, but it no longer overrides a turn whose
response and `turn.completed` state were already committed. Shutdown remains
strict and surfaces a persistent cleanup failure. Cancellation during cleanup
still propagates, while turn finalization now restores the caller's prior log
correlation context unconditionally so a cancelled finalizer cannot leak stale
session/turn IDs into later work.

Two pre-existing MCP warning sites also used stdlib `%s` placeholders with
Ash's Loguru-backed logger, producing literal `%s` rather than useful runtime
diagnostics. They now use the logger's brace formatting.

Regressions cover active-session and no-session committed cleanup failure,
retry ownership, strict shutdown surfacing, successful-turn preservation,
cleanup cancellation/context restoration, stale config removal, failed reload
preservation, and in-flight snapshot lifetime. The combined MCP runtime,
reconnect, diagnostics, and core-loop files pass **227 tests**; Ruff, targeted
mypy, and `git diff --check` are green.

### Re-audit continuation — background memory-index failure observability

Automatic project-memory indexing is intentionally non-blocking: the earlier
F-15 repair moved it out of synchronous runtime construction and schedules one
bounded task only after an async session is live. That product decision remains
correct. The follow-up review found an observability gap, however: the task had
no completion observer, so an indexing/provider/database failure could remain
silent until shutdown, where `gather(..., return_exceptions=True)` consumed it.

The auto-index task now has a stable task name and one completion callback. A
normal cancellation during shutdown remains silent; a real background failure
is rendered through canonical text redaction and logged once without affecting
the active session. The task remains owned by `AshLoop`, and the one-task-per-
runtime scheduling semantics are unchanged. A regression injects an indexing
failure after `start_session()` succeeds and proves that the session stays live,
the failure is diagnosed, and normal shutdown still consumes the task.

### Re-audit continuation — shutdown cleanup survives persistence/MCP failures

The shutdown retry audit found two early-abort boundaries that could strand
independent resources. First, `_flush_runtime_events()` ran before any external
cleanup; a SQLite persistence failure therefore returned from `aclose()` before
tools or the provider were closed. Ash now retains that flush error and the
pending events, continues deterministic cleanup, and surfaces the persistence
failure afterward when no higher-priority cleanup error occurred. A later
`aclose()` retries the pending event flush without re-closing tools/providers
that already shut down successfully.

Second, active and retired MCP runtime cleanup previously raised before the
tool/plugin/provider cleanup phases. Shutdown now attempts those MCP closes,
keeps failed runtime objects owned for retry, records successful MCP closure so
it is not repeated, then continues closing independent tools and providers.
The active MCP failure remains the primary surfaced error when present; retired
MCP cancellation remains cancellation rather than being converted to a generic
runtime failure. Retrying shutdown closes only the resources that remain.

Regressions inject fail-once runtime-event persistence, active MCP cleanup, and
retired MCP cleanup while counting independent tool/provider closes. They prove
the first shutdown attempt releases every resource it can, preserves failed
state for retry, and a second attempt completes without double-closing successful
resources. Combined core-loop, runtime, project-memory, MCP runtime/reconnect,
and diagnostics validation passes **278 tests**; Ruff, targeted mypy, and
`git diff --check` are green.

### Re-audit continuation — shutdown cancellation settles owned cleanup

The cancellation pass found that `aclose()` still differed from Ash's other
owned-cleanup boundaries. Cancelling the outer shutdown task while the tool
cleanup `gather()` was blocked cancelled that gather and its tool coroutine
immediately; later provider cleanup never ran. A regression with a blocking
tool and counting provider reproduced `close_finished=False` after cancellation.

Shutdown is now one explicitly owned cleanup operation. Public `aclose()`
starts the private cleanup task, shields it using the same settle-before-
propagating-cancellation pattern already used by MCP/plugin/process cleanup,
and temporarily clears the caller's cancellation while cleanup reaches a
terminal state. Caller cancellation is then re-raised; if owned cleanup also
failed, the cancellation carries a redacted cleanup note rather than silently
discarding that evidence. Internal cleanup cancellation/failure still follows
the existing strict shutdown error semantics.

The regression now proves that cancelling shutdown while a tool close is
blocked does not cancel that close: after release the tool finishes, the
provider is closed, `_closed` is true, and only then does the caller receive
`CancelledError`. The complete affected lifecycle gate passes **279 tests**;
Ruff, targeted mypy, and `git diff --check` are green.

### Re-audit continuation — retired provider cleanup ownership

Provider/model switching already retired replaced or rejected providers on a
background task so a synchronous switch does not block on transport cleanup.
The shutdown review found that a failed background `provider.aclose()` retained
only its exception, however, not the provider object that owned the resource.
Shutdown could therefore report the same stale failure on every retry but had
no way to attempt the actual cleanup again.

Retirement now tracks the provider owner alongside each in-flight close task.
A failed or self-cancelled background close moves that provider into an owned
retry set. Shutdown first settles any still-running retirement tasks, then
retries the actual failed provider objects; successful retries are removed and
persistent failures remain owned for later shutdown attempts. Current-provider
cleanup remains independently idempotent, so retrying retired cleanup does not
re-close resources that already shut down successfully.

A fail-once regression proves that the first asynchronous retirement attempt
can fail and the same old provider is closed successfully during shutdown
instead of surfacing a stale exception. Always-failing retired-provider and
multi-provider shutdown behavior remain strict. The complete core-loop unit
file passes **109 tests**; Ruff, targeted mypy, and `git diff --check` are
green.

### Re-audit continuation — retryable session runtime-tool startup

Session startup persisted and published a new `Session` before invoking
runtime-tool `start()` hooks. That ordering is necessary because runtime tools
are intentionally initialized only once a session exists, but a startup
failure had no retry identity. Calling `start_session()` again after a tool
failed therefore created a second empty durable session instead of completing
the first startup attempt.

Ash now records only the session ID whose runtime-tool startup remains pending.
A retry with no explicit ID (or that same ID) finishes tool startup against the
same durable/current session; `_started_tool_ids` continues to prevent tools
that already started successfully before the failure from being started twice.
The marker is cleared only after runtime tools and configured background memory
index scheduling have completed. An explicit switch to another session remains
a normal session transition rather than being captured by the retry path.

`run_turn()` also resolves pending runtime-tool startup before session recovery
or model work. A persistent tool-start failure therefore aborts before any turn
journal/provider request is created instead of allowing a partially initialized
tool set to serve a turn. Regressions prove a fail-once startup reuses one
durable session and a persistent failure never reaches a provider. The complete
core-loop unit file passes **111 tests**; Ruff, targeted mypy, and
`git diff --check` are green.

### Re-audit continuation — runtime-event persistence is retryable and bounded

Runtime-event persistence had a second committed-operation ambiguity. The
event queue flushed automatically at 64 entries and again for terminal turn
events. A SQLite failure from either path escaped `_emit_event()` even after
the assistant response and turn journal had already been committed, so callers
could observe a failed turn despite Ash having durably completed the model
work. The same strict flush in `run_turn()` finalization could mask an existing
turn result or primary exception.

Ordinary runtime-event flushing is now explicitly non-fatal observability work.
Failed batches remain queued with their event IDs intact, one redacted warning
is emitted, and terminal turns retry the batch. Turn finalization performs only
the non-fatal flush; shutdown continues to use the strict flush introduced in
the shutdown-cleanup batch, so an unresolved durability problem is still
surfaced before the process is considered cleanly closed. A regression keeps
event persistence unavailable for one complete successful turn, proves the
turn still returns its committed response, then restores storage and proves a
later turn drains the retained event batch.

Making persistence retryable exposed a memory-pressure edge: 64 was only a
flush trigger, not a backlog bound. Persistent storage failure could therefore
grow the queued events indefinitely. Ash now attempts a flush at 64 events or
approximately 1 MiB, retains at most 256 events / approximately 4 MiB after
failed persistence, drops the oldest queued observability entries first so the
newest terminal/current-state events survive, and reports that degradation
once until persistence recovers. Successful persistence resets both warning
states and the tracked byte budget. Separate regressions prove the count and
serialized-byte bounds under a permanently failing event store while retaining
the newest event. Because public event envelopes intentionally preserve
arbitrary payload values, backlog size accounting also treats serialization
failure as a conservative large-event estimate rather than raising; a circular
payload regression proves malformed observability data cannot make the
accounting path fail runtime work.

The combined core-loop, runtime, project-memory, MCP runtime/reconnect, and
diagnostics validation passes **285 tests**; Ruff, targeted mypy, and
`git diff --check` are green.

### Re-audit continuation — MCP notification activation is pre-commit

The MCP reload commit-boundary review found one remaining post-publication
exception point. A candidate runtime and its tools/config were published before
`MCPRuntime.activate_notifications()` ran. If notification activation raised,
the public reload call failed even though `_mcp_runtime`, the live tool map, and
the desired MCP config already pointed at the replacement.

Notification activation now runs during candidate preparation, after candidate
runtime/tool startup and collision validation but before any live MCP state is
replaced. Activation failure therefore uses the same cancellation-safe
candidate-runtime cleanup path as startup failure, leaving the old runtime,
tools, and config authoritative. Any startup refresh tasks scheduled by
activation cannot execute before publication because activation and the commit
tail contain no intervening `await`.

The same failure-path review found that candidate MCP tools were inserted into
`_started_tool_ids` as they started. Rejecting the candidate cleaned its runtime
but left those object identities behind; a future Python object-ID reuse could
make an unrelated runtime tool appear already started. Candidate-preparation
failure now rolls back every candidate tool identity before runtime cleanup.

A regression injects notification-activation failure after a candidate tool has
started and proves the old runtime/config remain active, the candidate runtime
is closed once, and the rejected candidate tool identity is absent from the
live started-tool registry. The complete MCP runtime/reconnect/diagnostics slice
passes **123 tests**; Ruff, targeted mypy, and `git diff --check` are green.

### Re-audit continuation — session mutation and shutdown serialization

The core lifecycle concurrency pass found that `AshLoop` had no internal
session-mutation serialization. Two overlapping `start_session()` calls could
run provider capability negotiation concurrently and both create/publish
different durable sessions. A plain queueing lock would be misleading because
two sequential `start_session()` calls intentionally mean two new sessions;
the safe contract is to reject overlap rather than silently coalesce or
supersede calls.

Session lifecycle mutation now has one internal lock. Public `start_session()`
fails fast when another session mutation is already in progress and also
refuses to change sessions while a turn is active. `run_turn()` performs its
implicit first-session creation before setting `_turn_running`, then enters the
turn only after session mutation is quiescent. Sequential CLI/SDK `/new`,
resume, and fork semantics remain unchanged; the SDK's existing turn lock still
provides its higher-level serialization.

Shutdown now participates in the same boundary. Public `aclose()` marks the
loop closing before its first await, and the owned cleanup task acquires the
session-lifecycle lock before touching provider/tool/MCP resources. An already-
started session mutation is therefore allowed to finish, no new mutation can
enter, and teardown starts only after the active session state is stable.
Conversely, direct core shutdown during an active turn now fails closed before
setting `_closing` or closing resources; callers can cancel/wait the turn and
retry. This matches the SDK's existing behavior instead of relying on callers
to know that the raw loop was unsafe.

Regressions reproduce and cover overlapping session start, session switching
during a blocked turn, shutdown racing a blocked session capability probe, and
shutdown attempted during a blocked provider turn. The complete core-loop unit
file passes **119 tests**; Ruff, targeted mypy, and `git diff --check` are
green.

### Hosted CI evidence check — expanded matrix still awaits proof

The cross-platform parity row remains intentionally `Partial`. GitHub Actions
run **36118934236** is an exact successful hosted CI run for committed main SHA
`46a44f26434bc8c0eb87cb858f0614527e3f7e11`, but its job list contains only
Ubuntu/macOS on Python **3.11 and 3.12**. The current working-tree
`.github/workflows/ci.yml` is modified and expands that matrix to Ubuntu/macOS
on Python **3.12, 3.13, and 3.14**, plus separate quality,
minimum-dependencies, browser-E2E, and viewport-PTY jobs. That expanded workflow
has not yet produced hosted-run evidence at the current committed SHA.

No status promotion or workflow mutation was made from configuration alone.
The architecture matrix's existing wording — configured expanded coverage,
hosted-run proof still required — remains accurate.

### Re-audit continuation — bounded SDK live-event streaming

The SDK streaming review found that `AshClient.stream_prompt()` subscribed to
every live runtime event through an unbounded `asyncio.Queue`. The UI listener
is synchronous, so a slow async consumer cannot exert backpressure on the
provider/tool turn; token, reasoning, tool-output, and lifecycle events could
therefore accumulate without bound while the turn continued producing them.

The SDK now keeps the existing exact-event contract for consumers that keep up,
but makes lag explicit instead of silently dropping or coalescing runtime event
identities. The live stream queue is capped at **4096 events and 8 MiB**. Each
queued event carries an estimated serialized size that is released as the
consumer drains it; non-serializable events conservatively exceed the byte
budget rather than making accounting fail. On the first count/byte overflow,
Ash cancels the owned prompt task, ignores subsequent live events, drains any
already-queued events, and terminates the stream with one synthetic
`turn.error` categorized as `backpressure`. If overflow occurs before any
ordinary event is queued, the synthetic terminal event is inserted immediately
so a consumer already blocked in `queue.get()` cannot hang.

Regressions use a two-event burst budget and a deliberately oversized tool
event to prove bounded fail-closed behavior, while the existing real-turn and
cancellation streaming tests prove normal consumers still receive the complete
assistant delta sequence and ordinary terminal semantics. The full SDK unit
file passes **27 tests**; HTTP server, A2A, and ACP adapter suites pass **99
tests**; Ruff, targeted mypy, and `git diff --check` are green.

### Re-audit continuation — SDK creation preserves the primary startup failure

`AshClient.create()` correctly attempted to close the allocated runtime when
initial session startup failed or was cancelled, but the cleanup call lived
inside the same exception path without preserving the primary failure. If
`runtime.loop.aclose()` also failed, callers observed the cleanup exception
instead of the actual startup/cancellation reason.

Client creation now keeps the original `BaseException` authoritative. Runtime
cleanup is still attempted immediately; any cleanup failure is attached to the
original exception as a redacted diagnostic note, after which the original
startup error or cancellation is re-raised unchanged. This composes with
`AshLoop.aclose()`'s existing owned-cleanup/cancellation guarantees rather than
adding a second SDK-specific teardown model.

A regression injects a startup failure followed by a cleanup failure and proves
the startup error remains primary while cleanup evidence is retained. The
existing create-cancellation regression continues to prove an allocated runtime
is closed before cancellation escapes. The combined SDK, HTTP, A2A, and ACP
validation slice passes **134 tests**; Ruff, targeted mypy, and
`git diff --check` are green.

### Re-audit continuation — browser teardown retains failed resource ownership

`BrowserSession._close_unlocked()` previously treated browser/context,
Playwright, and policy-proxy teardown as best effort: every close/stop exception
was swallowed and all references were cleared unconditionally. A failed proxy
close could therefore leave its listener/process resources alive while the
session reported clean shutdown and permanently lost the handle needed to retry
cleanup. The same helper is used during partial browser startup recovery, so a
failed cleanup could also be ignored and then overwritten by a new resource on
restart.

Browser teardown now clears each resource reference only after that resource's
own async close/stop succeeds. Failed context, browser, Playwright, or proxy
handles remain attached and are summarized through a redacted
`BrowserUnavailableError`; additional cleanup failures are retained as notes.
Public `close()` remains a terminal fence for normal browser use, but records
failed cleanup and allows a later `close()` to retry the retained handles. When
caller cancellation arrives during teardown, the owned cleanup task settles
first and cancellation is re-raised afterward with cleanup diagnostics if
needed.

Browser restart now treats cleanup of any previous partial resources as a hard
precondition. Cleanup failure stops before a new proxy/Playwright/browser is
created, so failed ownership cannot be overwritten. Startup failures and
startup cancellation still remain primary; cleanup failure is attached as
diagnostic context and the failed handles remain available for explicit close
retry.

Regressions prove fail-once policy-proxy close is retained and succeeds on a
second session close, failed partial-resource cleanup prevents restart from
replacing the old proxy, and startup failure preserves both its primary error
and cleanup evidence. Existing repeated-cancellation shutdown and successful
startup cleanup tests remain green. The complete browser tool + policy-proxy
slice passes **61 tests**; Ruff, targeted mypy, and `git diff --check` are
green.

### Re-audit continuation — failover provider cleanup is attempt-all and retryable

`FailoverProvider.aclose()` previously closed child providers sequentially and
returned immediately on the first exception. A persistently failing primary
provider could therefore prevent every later backup provider from ever being
closed, even across repeated AshLoop shutdown attempts.

Failover cleanup now tracks successful child closures by provider identity.
Each `aclose()` attempts every child that is still owned, records successful
closures so they are not repeated, and retains only failed children for the
next retry. Ordinary failures are surfaced afterward as a bounded aggregate
error. If a child close produces `CancelledError`, cancellation remains the
primary result but only after the other independent children have still been
attempted; successfully closed children stay recorded while the cancelled child
remains retryable.

Regressions prove a persistently failing primary no longer blocks backup
cleanup, successful backups are not double-closed on retry, and child
cancellation propagates only after the other provider closes. The complete
failover-provider file passes **17 tests**, and AshLoop's provider-shutdown
retry plus a real failover turn regression remain green. Ruff, targeted mypy,
and `git diff --check` are green.

### Re-audit continuation — subagent subprocess monitor cleanup survives stop-signal failure

Background subprocess subagents are tracked by monitor tasks. Shutdown tries to
send a durable `stop` message before waiting briefly and then force-cancelling
the monitor. That graceful message write previously happened outside the
cleanup guard: if shared-state persistence raised, `_stop_subprocess_monitor()`
returned by exception before cancelling the monitor. `SpawnAgentTool.aclose()`
gathered those failures with `return_exceptions=True` and then cleared both
subprocess tracking maps, permanently losing ownership of the still-running
monitor.

Graceful stop-message persistence is now advisory rather than the authoritative
cleanup step. Failure to write the message falls through to forced monitor
cancellation. The monitor task is cancelled and settled with Ash's repeated-
cancellation cleanup helper before ownership can be released. Caller
cancellation during either graceful signaling or the grace wait is temporarily
cleared while the monitor settles, then propagated afterward; an unexpected
monitor cleanup failure is attached as redacted cancellation context.

A regression injects a shared-state stop-message failure while a subprocess
monitor is blocked and proves `SpawnAgentTool.aclose()` still cancels and joins
the monitor before clearing `_subprocess_tasks` and `_subprocess_task_ids`. The
complete subagent tool unit file passes **40 tests**; Ruff, targeted mypy, and
`git diff --check` are green.

### Re-audit continuation — foreground approval listener cleanup is retryable

`ForegroundApprovalServer.aclose()` previously cleared its `_server` reference
before the listener's `wait_closed()` completed. Caller cancellation during
that wait cancelled teardown immediately, leaving the listener cleanup
unfinished while also removing the handle needed to retry it. A listener-close
failure had the same lost-owner shape.

Approval-server shutdown now owns one named cleanup task. Public `aclose()`
shields that task through repeated caller cancellation, temporarily clears the
caller's cancellation while cleanup settles, then propagates `CancelledError`
afterward. The listener reference is cleared only after `wait_closed()`
succeeds; a failed close remains owned and causes a later `aclose()` to create a
fresh cleanup task and retry. The caller task is explicitly excluded from the
handler-cancellation set so calling close from an approval handler cannot make
the cleanup task cancel its own waiter.

Regressions prove cancellation while listener shutdown is blocked does not let
`aclose()` return early, and a fail-once listener close retains the listener and
succeeds on retry. Existing active-handler cancellation and protocol tests
remain green. The complete approval-channel unit file passes **9 tests**; Ruff,
targeted mypy, and `git diff --check` are green.

### Re-audit continuation — automation client cleanup is retained and admission-bounded

Automation execution already handled slow/unsettled cancellation by retaining a
deferred cleanup task, but a *completed* `AutomationClient.close()` that raised
followed a different path: the error was logged and `client_close_started` was
left true, so the client was never retried. A successful run could therefore
remain durably `succeeded` while its runtime cleanup was silently abandoned.

The worker now owns failed closes in a private retired-client set. Client-close
failure remains post-commit cleanup and does not rewrite the durable automation
run outcome. Before another automation runtime is admitted, the worker retries
all retained clients; if any still cannot close, the claimed run is failed
before its client factory is invoked, preventing cleanup debt from accumulating
additional runtimes. Successful retries remove only the clients that actually
closed.

`AutomationWorkerService.aclose()` performs the same retired-client retry under
the worker's settle-before-cancellation contract. The shared settle helper was
also corrected so an owned cleanup task's own exception is returned instead of
escaping early from `asyncio.shield()`. CLI `run_worker()` and `run_manual()`
now drain the service lifecycle before returning; if primary worker execution
also failed, any shutdown failure is attached as a redacted note so the primary
error remains authoritative.

Regressions prove a fail-once close remains owned after a durably successful
manual run and succeeds during explicit worker shutdown, and prove persistent
cleanup failure blocks a second client factory call until cleanup succeeds. The
existing repeated-cancellation/deferred-cleanup regression remains green. The
full automation worker plus automation CLI suite passes **130 tests**; Ruff,
targeted mypy, and `git diff --check` are green.

### Re-audit continuation — ACP session cleanup retains failed runtime ownership

ACP session shutdown had the same false-clean ownership pattern previously
found in other runtime boundaries. `close_session()` removed a session from the
callable session map before closing its `AshClient`; if that close failed, the
error escaped but the runtime was no longer reachable for retry. Agent-wide
`aclose()` was worse: it cleared every published session, closed all clients
through `gather(..., return_exceptions=True)`, and discarded every cleanup
failure, so shutdown could report success after abandoning a live runtime.

ACP now separates *publication* from *cleanup ownership*. Closing a session
still removes it from `_sessions` immediately, preserving the existing prompt-
versus-close fence, but moves its state into a private `_retired_sessions` map.
Each retired session owns a close lock so concurrent retries cannot close the
same client simultaneously. Prompt cancellation is settled first, client close
is attempted next, and the retired owner is removed only after that close
succeeds. `close_session()` can therefore retry a previously failed close by
session ID without republishing the session.

Agent-wide `aclose()` retires all still-published sessions, closes every retired
runtime, removes successful owners, retains failures, and raises a bounded
aggregate error instead of swallowing cleanup failures. Retired sessions also
continue to count against `max_sessions` and block reuse of the same session ID
until cleanup succeeds, preventing failed teardown from freeing capacity or
allowing duplicate runtime ownership prematurely.

The same ownership rule now covers failed `load_session()` replay. Replay is
performed only after the new client has been published so prior transcript/tool
state can be sent to the ACP peer. If replay fails, Ash moves that exact state
into `_retired_sessions` before attempting client cleanup. A cleanup failure is
attached to the original replay exception instead of replacing it, the failed
runtime remains unpublished and retryable, and it continues to consume ACP
capacity until cleanup succeeds.

`resume_session()` and `fork_session()` now create their private `_ACPSession`
owner immediately after the client factory returns, before validating that the
client attached the requested durable session. If attachment validation fails,
that state is retired under the requested/reserved session ID before cleanup.
Fail-once cleanup therefore remains retryable even when the client reported the
wrong session identity; the original resume/fork error remains primary, and an
unchanged fork is discarded only when runtime cleanup was actually confirmed.

`new_session()` has one additional case because a rejected client may not have
a trustworthy public session ID at all. Those clients are wrapped in a private
unpublished-session owner keyed only by object identity. Failed cleanup stays
in that pool for agent-wide shutdown retry, contributes to the ACP session
capacity bound, and never becomes addressable through normal session lookup.
The setup error remains primary while cleanup failure is attached as a note.

ACP shutdown is now serialized against session lifecycle construction as well.
`aclose()` has one shutdown owner, marks the agent closing before waiting, and
waits for outstanding new/load/resume/fork reservations to drain before taking
its final runtime-owner snapshot. New reservations fail once closing begins,
and already-reserved operations re-check the closing state before publication;
they abort through their normal retained-cleanup paths instead of publishing a
runtime after shutdown. Per-session and unpublished close helpers also verify
that their owner is still registered after acquiring the state close lock, so
concurrent `close_session()`/`aclose()` paths cannot close the same client twice
after one path has already completed ownership release.

Regressions prove a fail-once per-session client remains unpublished but owned,
continues to consume the configured one-session capacity, succeeds on explicit
retry, and frees capacity afterward. A second regression proves agent-wide
close removes a successful runtime while retaining only the failed one for a
later retry. A replay-failure regression proves the replay error remains primary
while fail-once client cleanup is retained for retry and capacity accounting.
The existing resume/fork primary-error regressions now additionally prove that
failed cleanup is retained and succeeds through an explicit `close_session()`
retry.
An unpublished-client regression proves an invalid new-session client whose
first close fails remains capacity-accounted and is successfully retried by
agent-wide `aclose()`. A shutdown-race regression blocks a session factory,
starts `aclose()`, proves shutdown waits, then proves the in-flight creation is
rejected as closing and its client is cleaned without late publication. The
existing concurrent prompt/close regression remains green. The complete ACP
unit file passes **36 tests**; Ruff, targeted mypy, and `git diff --check` are
green.

### Re-audit continuation — JSON-RPC request admission is shutdown-atomic

`JSONRPCServer.close()` previously snapshotted `_pending` requests and
notification tasks, cancelled that snapshot, then optionally closed the SDK
client. Request admission was not serialized against the snapshot, so a request
arriving after it could start work and continue after server shutdown returned.
There was a second ownership gap: explicit JSON-RPC `id: null` is a real request
in Ash, but is intentionally absent from `_pending` because multiple null IDs
cannot be used as a unique cancellation key. Such requests were therefore not
owned by shutdown at all.

Request ownership and cancellation lookup are now separate. Every request task
is retained in `_request_tasks`; only non-null IDs are additionally exposed
through `_pending` for `$/cancelRequest`. A short admission lock covers the
closing check and task registration, while `close()` takes the same lock to mark
the server closing and snapshot all owned request/notification tasks. No task
can therefore be admitted between the shutdown fence and snapshot. Later valid
requests receive JSON-RPC server error **-32000** (`Server is closing`), while
notifications remain response-free as required.

Shutdown itself has one close owner. It cancels and joins every owned request
and notification task, and SDK-client closure is recorded only after the close
actually succeeds so a failed client close remains retryable. A deterministic
race regression pauses client shutdown after the task snapshot and proves a
late `turn/run` cannot start provider work. A second regression proves an
in-flight explicit-null request is cancelled and joined even though `_pending`
is empty. A fail-once client-close regression proves request admission remains
closed after cleanup failure, a later close retries the client, and subsequent
closes do not double-close it. The complete JSON-RPC plus HTTP-adapter slice
now also covers shutdown cancellation: public `close()` owns one shielded cleanup
task, temporarily clears caller cancellation while request/notification/client
teardown reaches a terminal state, then re-raises `CancelledError`. Cleanup
failure remains attached as a redacted cancellation note/cause instead of being
lost. The complete JSON-RPC plus HTTP-adapter slice passes **44 tests**; Ruff,
targeted mypy, and `git diff --check` are green.

### Re-audit continuation — A2A terminal state survives deferred client cleanup

The A2A executor had a committed-operation ambiguity after terminal task
publication. A successful Ash turn could publish `TASK_STATE_COMPLETED` through
the official `TaskUpdater`, then fail while closing the per-task `AshClient` in
`finally`. The close failure escaped from `execute()`, so the A2A framework
could observe an executor exception after terminal success had already been
published. Direct reproduction captured the completed status event before the
cleanup exception escaped.

`AshA2AExecutor` now owns failed per-task client cleanup. The existing
cancellation-safe settle helper was corrected so an owned cleanup task's own
exception is returned through the helper instead of escaping early from
`asyncio.shield()`. When client close fails after a terminal A2A status has been
published, the client is retained in the executor's private retired set and the
terminal task result remains authoritative. Caller cancellation still remains
primary and receives cleanup notes as before.

Before starting a later task, the executor retries all retained clients. If any
cleanup remains unresolved, the new A2A task is failed before a new
`AshClient` is created, preventing sequential cleanup debt from accumulating.
Executor `aclose()` performs the same retry under the existing repeated-
cancellation settle contract and reports unresolved retired clients. The A2A
application lifespan now drains active handler tasks first, then executor-owned
client cleanup, then the optional SQLAlchemy engine; cleanup failures are
collected so one teardown failure does not skip later owned resources.

Regressions prove fail-once cleanup after `TASK_STATE_COMPLETED` no longer
changes the committed outcome and is successfully retried by executor shutdown,
and prove persistent cleanup failure blocks a second client from being created
until the retired owner can close. The existing repeated-cancellation executor
cleanup regression remains green. The complete A2A unit file passes **41
tests**; Ruff, targeted mypy, and `git diff --check` are green.

### Re-audit continuation — runtime tool assembly and agent SQLite construction roll back partial ownership

`build_tools()` eagerly creates closeable local-state owners for agent tools
before plugin runtime tools are assembled. A later construction failure could
therefore escape after two `SharedState` connections had been opened, leaving
neither state attached to a returned tool set and with no destructor fallback
to close them. Direct reproduction forces plugin-tool construction to fail and
proved both partial agent states remained open.

Tool assembly now tracks every eager synchronous owner as soon as construction
succeeds: the shared outbound A2A `RemoteTaskStore` and each agent
`SharedState`. Any later assembly exception closes those owners in reverse
construction order before the original error is re-raised. Cleanup failures are
retained as redacted exception notes, so teardown trouble does not replace the
actual configuration/plugin construction failure. Successful assembly transfers
ownership to the returned tools exactly as before.

The lower-level constructors had the same lost-owner shape internally.
`SharedState` and `AgentTaskStore` each opened a pinned SQLite connection and
then initialized schema without rollback if schema setup raised. Both
constructors now mark the partial owner closed, close the connection, preserve
the initialization exception as primary, and attach any secondary connection-
close failure as context. `RemoteTaskStore` already had constructor-local
rollback for schema/permission failures and required no change.

Regressions prove a forced plugin-tool build failure closes both partially
constructed agent `SharedState` owners, and prove injected schema failures close
the already-open connection in both `SharedState` and `AgentTaskStore`. The
combined shared-state, durable-agent-task, runtime, plugin-runtime, and plugin-
agent validation slice passes **135 tests**; Ruff, targeted mypy, and
`git diff --check` are green.

The wider runtime assembly order is now transactional as well. Instruction,
hook, MCP-source, and MCP interaction validation complete before stateful tools
are allocated, so a declarative startup error cannot strand agent/A2A state
owners. `build_tools()` can also hand `build_runtime()` a private startup-
rollback list for its eager synchronous owners. That rollback remains active
through `AshLoop` construction and final permission/hook/middleware wiring; it
is discarded only when a complete `RuntimeComponents` object is ready to
return. A loop-construction failure or post-loop setup failure therefore closes
the unpublished tool-owned state before the original error escapes.

Additional regressions prove MCP validation allocates no `SharedState` at all,
forced `AshLoop` construction failure closes both already-built agent states,
and forced post-loop permission-policy setup failure does the same. The final
runtime + plugin-runtime + plugin-agent publication gate passes **84 tests**;
Ruff, targeted mypy, and `git diff --check` are green.

### Re-audit continuation — Ash-owned provider transports are allocated lazily

`build_runtime()` is intentionally synchronous because it serves both the CLI
and the SDK, including SDK creation from inside an already-running event loop.
Several provider constructors nevertheless created async HTTP/SDK clients
eagerly. If later synchronous runtime assembly failed, there was no safe
portable way to await provider cleanup before re-raising the construction
error. Direct runtime reproduction forces plugin discovery to fail immediately
after an Ash-owned OpenAI provider object is built and verifies the transport
allocation boundary.

OpenAI, Ollama, DeepSeek, and Groq now defer creation of their Ash-owned async
client until the first operation that actually needs it. Anthropic already used
this pattern and required no change. OpenAI-compatible subclasses—including
OpenRouter, Google, NVIDIA, LM Studio, vLLM, and custom compatible providers—
inherit the same lazy transport boundary. Injected clients remain immediate and
caller-owned exactly as before.

Provider `aclose()` methods now no-op safely before first use. Once an owned
client exists, cleanup clears the stored handle only after close succeeds, so a
failed close remains retryable. Existing retry policy is unchanged: SDK clients
are still created with nested retries disabled and owned HTTP transports still
refuse redirects where previously configured.

Tests explicitly prove Ash-owned SDK/HTTP constructors are not called during
provider construction, Ollama allocates its HTTP client once on first resolve,
Google/NVIDIA headers are still applied at actual client creation, and a
runtime-construction failure after provider object creation allocates neither
the OpenAI SDK client nor its HTTP transport. The complete provider,
provider-registry, readiness, retry, and runtime slice passes **180 tests**;
Ruff, targeted mypy, and `git diff --check` are green.

### Re-audit continuation — MCP sampling retains one-off provider cleanup ownership

`MCPInteractionController.handle_sampling()` creates a fresh provider for each
approved sampling request. Cleanup previously used a bare
`await provider.aclose()` in `finally`. If sampling had already failed and
provider close also failed, the cleanup exception replaced the actual MCP
protocol/provider error. If the caller cancelled while provider close was
blocked, cancellation interrupted the only cleanup owner and the local provider
could be dropped before teardown completed.

Sampling now gives provider cleanup one named owned task and settles it through
`asyncio.shield()` despite repeated caller cancellation. A sampling/protocol
failure remains primary; cleanup failure is attached as a redacted exception
note rather than replacing it. If a new caller cancellation arrives during
cleanup after a non-cancellation sampling failure, cleanup still settles first
and cancellation propagates afterward with the prior failure retained as its
cause/context. Successful sampling cancelled during provider close follows the
same settle-before-cancellation rule.

Regressions prove unterminated sampling remains the surfaced
`MCPProtocolError` when provider close also fails, and prove cancellation during
a blocked provider close does not complete the sampling task until cleanup has
finished. The existing generation-cancellation regression still proves provider
cleanup runs and the sampling lock is released. The complete MCP interaction
unit file passes **24 tests**; Ruff, targeted mypy, and `git diff --check` are
green.

### Re-audit continuation — direct subagent providers allocate only after worker preflight

`SpawnAgentTool._run_worker_loop()` previously called its provider factory at
the very start of worker setup, before role-specific tool validation, worker
policy/config preparation, and `AshLoop` construction. A custom agent that was
rejected because it requested unavailable tools therefore still allocated a
provider that never acquired a runtime owner. This matters for opaque/custom
factories even though Ash's built-in provider transports are now lazy.

Provider construction now occurs only after worker guard/sandbox/tool
selection, custom-tool validation, worker policy/config, instructions, and UI
setup have succeeded. A strengthened custom-agent elevation regression proves
the rejected worker makes **zero** provider-factory calls.

Once the provider is created, `AshLoop` construction is the ownership-transfer
boundary. If loop construction fails, the provider is closed through the
existing repeated-cancellation cleanup helper before the setup error escapes.
Provider cleanup failure is attached as redacted diagnostic context rather than
replacing the worker setup failure; cancellation arriving during cleanup is
propagated only after cleanup reaches a terminal state. A regression forces
worker-loop construction to fail and proves the provider is closed exactly
once before the subagent reports failure.

The complete direct subagent + plugin-agent validation slice passes **52
tests**; Ruff, targeted mypy, and `git diff --check` are green.

### Re-audit continuation — provider completion probes settle temporary-provider cleanup

The explicit provider connectivity command creates one temporary provider to
verify a real completion. Its `finally` previously bare-awaited
`provider.aclose()`. A completion failure followed by cleanup failure silently
dropped the cleanup evidence, while caller cancellation during a blocked close
cancelled the only cleanup owner immediately.

Provider-probe cleanup now runs in one named, shielded task and is settled
despite repeated caller cancellation. An existing completion failure remains
the primary probe result and cleanup failure is appended as secret-redacted
diagnostic evidence. A successful probe whose cleanup fails is still marked
unverified, preserving the prior readiness behavior. Cancellation arriving
during cleanup is propagated only after provider close reaches a terminal
state; cancellation that already caused the probe to stop remains primary and
receives cleanup notes rather than being replaced.

Regressions prove a rejected completion plus failing close reports both the
completion failure and cleanup failure, and prove cancellation during a blocked
provider close cannot finish the probe before cleanup completes. Existing
timeout cleanup remains green. The complete provider-command unit file passes
**25 tests**; Ruff, targeted mypy, and `git diff --check` are green.

### Re-audit continuation — MCP OAuth owned cleanup is attempt-all and cancellation-safe

Two MCP OAuth paths owned temporary async resources without a retained cleanup
owner. Token refresh created an internal `httpx.AsyncClient` and bare-awaited
its close in `finally`; a refresh/auth failure followed by client-close failure
therefore surfaced the cleanup exception instead of the intended
`MCPAuthorizationRequired`, and caller cancellation during close could abandon
the client. Interactive authorization had a second ordering problem: callback-
listener shutdown was awaited before the owned HTTP client was closed, so a
listener cleanup failure could both replace the OAuth failure and skip HTTP-
client cleanup entirely.

OAuth cleanup now uses one shared settle helper that runs each async teardown
operation in a named, shielded task and temporarily clears repeated caller
cancellation until cleanup reaches a terminal state. Refresh keeps the original
authorization error primary and attaches a bounded cleanup note if owned client
close also fails. Interactive login attempts callback-listener cleanup and
owned HTTP-client cleanup independently, accumulating failures so one cannot
skip the other. Cancellation is propagated only after all owned cleanup that
can still run has settled; diagnostic notes deliberately avoid copying OAuth
exception text or credentials.

Regressions prove refresh preserves `MCPAuthorizationRequired` when internal
HTTP-client close fails, prove cancellation during a blocked refresh-client
close cannot finish early, and prove interactive-login discovery failure stays
primary even when callback-listener cleanup fails while owned HTTP-client close
is still attempted. The complete MCP OAuth unit file passes **64 tests**; Ruff,
targeted mypy, and `git diff --check` are green.

### Re-audit continuation — command and hook subprocesses clean up after unexpected I/O failure

Two temporary subprocess owners handled timeout/cancellation correctly but had
an uncovered non-cancellation failure path. `ash ollama pull` manually drains
child stdout; if that reader raised after spawn, the exception escaped without
terminating the managed process tree. Trusted command hooks delegate pipe I/O
to `communicate_process()` and likewise only handled caller cancellation, so an
unexpected stream/I/O failure could return while the hook child remained live.

Both callers now use Ash's existing shielded process-tree settle helper for
unexpected non-cancellation failures before re-raising the original error.
Cleanup failure is attached as diagnostic context; if cancellation arrives
during teardown, cleanup still settles first and cancellation propagates
afterward. Hook `ProcessOutputLimitExceeded` remains excluded from this generic
path because `communicate_process()` already owns and attempts process-tree
termination for output-limit enforcement, avoiding a second cleanup pass over
that committed path.

Regressions inject an Ollama stdout-read failure and a hook communication
failure after spawn and prove managed process-tree cleanup is attempted before
the original error escapes. The complete Ollama-command + hook unit slice
passes **22 tests**; Ruff, targeted mypy, and `git diff --check` are green.

### Re-audit continuation — core command and Git runners clean up after unexpected communication failure

Four additional post-spawn subprocess owners had the same uncovered branch as
the Ollama/hook runners: agent worktree Git, the generic command tool, the Git
helper, and `git apply`. Each already handled timeout, caller cancellation, and
bounded-output termination, but an unexpected `communicate_process()` failure
could escape while the managed child remained live.

Each runner now settles its managed process tree before re-raising an unexpected
non-cancellation communication error. Cleanup failure is attached to the
original exception; if caller cancellation arrives while teardown is running,
the shared shielded settle helper finishes cleanup before cancellation is
propagated. Existing timeout, cancellation, and `ProcessOutputLimitExceeded`
branches are unchanged, so their established return/error contracts and
single-owner cleanup semantics remain intact.

Focused regressions inject communication failures after spawn in all four
runners and prove process-tree cleanup occurs before the original error
continues. The broader agent-worktree + Git/patch + tool validation slice
passes **142 tests**; Ruff, targeted mypy, and `git diff --check` are green.

### Re-audit continuation — automation active-run shutdown survives repeated cancellation

`AutomationWorkerService._stop_active_runs()` cancelled active run tasks and
then bare-awaited `asyncio.wait(..., timeout=4)`. If the worker itself was
cancelled while that bounded wait was in progress, shutdown returned before
`_collect_finished()` and the claim/task retirement loop ran. Active task and
claim ownership could therefore remain half-transitioned even though
command-level service cleanup only owns retired clients, not those run maps.

The bounded active-run wait is now itself an owned named task settled through
the worker's existing repeated-cancellation helper. Caller cancellation is
temporarily cleared until the existing four-second wait completes; Ash then
runs the normal collect/finalize/detach ownership transition and only afterward
propagates `CancelledError`. The prior timeout/detach policy remains unchanged,
so cancellation-resistant run coroutines are still bounded rather than waited
forever.

A regression uses a cancellation-resistant synthetic active run and proves a
second cancellation cannot finish `_stop_active_runs()` before the child
settles and `_tasks` ownership is cleared. The targeted automation lifecycle
slice covering timeout, repeated client-close cancellation, once-mode stop,
pre-start cancellation, heartbeat failure, cross-process cancellation, and the
new active-run race passes **7 tests**; Ruff, targeted mypy, and
`git diff --check` are green.

The worker's lower-level `_cancel_task()` helper had the same repeated-
cancellation gap in its own bounded `asyncio.wait()`. It is used by operation,
heartbeat-monitor, and once-batch stop cleanup. That wait is now also owned by
a named task and settled before result consumption or timeout detach. Normal
timeout behavior still returns `False` and retains the prior detach policy;
caller cancellation is propagated only after the owned task has either settled
or been detached according to that existing bound.

A second cancellation-resistant regression proves `_cancel_task()` itself
cannot be interrupted before child settlement. With that coverage added, the
bounded automation lifecycle slice passes **8 tests**; Ruff, targeted mypy, and
`git diff --check` are green.

### Re-audit continuation — sandbox runners clean up after unexpected communication failure

Three sandbox subprocess owners still had a post-spawn branch not covered by
their existing timeout, cancellation, and output-limit cleanup: the direct
scoped runner, wrapped backend runner, and Docker control helper all allowed an
unexpected `communicate_process()` exception to escape without terminating the
managed process tree. That could leave an isolated or control subprocess alive
after its caller had already received an error.

The sandbox manager now centralizes this branch in one small cleanup helper.
Each runner settles its managed process tree before re-raising the original
communication failure. Process-tree cleanup failure is attached to the primary
exception, while caller cancellation arriving during cleanup is propagated only
after teardown settles. Existing `ProcessOutputLimitExceeded` branches remain
unchanged because `communicate_process()` already owns termination for that
case, avoiding duplicate cleanup.

A parametrized regression injects the same post-spawn stream failure into all
three runners and proves the shared settle helper receives the live process
before the original exception continues. The complete sandbox unit file passes
**77 tests with 1 platform-dependent skip**; Ruff, targeted mypy, and
`git diff --check` are green.

### Re-audit continuation — spawned subagents retain child ownership through communication and cancellation failure

The subprocess-backed subagent path had two remaining ownership gaps after a
child was launched. An unexpected `communicate_process()` failure escaped
without terminating the managed process tree or retiring the durable agent
task. Its explicit cancellation branch did terminate the tree, but did so with
a bare await; a second caller cancellation during teardown could interrupt the
cleanup and return while the child was still live. Cancellation after a
cancelled launch produced a child had the same repeated-cancellation race.

Loss of the subprocess communication channel is now fail-closed. If the durable
task is still active, Ash cancels it with a fixed diagnostic reason, then
settles the managed child tree through the shared cancellation-safe process
helper before re-raising the original communication error. Cleanup failures are
attached as context, and cancellation arriving during teardown propagates only
after the tree cleanup reaches a terminal state.

Both explicit cancellation branches now use that same settle helper too:
cancellation while communicating with the child and cancellation while the
launch task is completing cannot be interrupted by a second cancellation once
a real child needs cleanup. The durable task is retired before cancellation is
returned to the caller.

Regressions prove unexpected communication failure cleans the child tree and
marks the durable task cancelled, and prove repeated cancellation cannot finish
either communication-phase or launch-phase shutdown before blocked process-tree
cleanup completes. The complete subagent tool unit file passes **45 tests**;
Ruff, targeted mypy, and `git diff --check` are green.

### Re-audit continuation — outbound A2A task helpers clean all transport owners without masking primary errors

The manually opened outbound A2A task helpers (`recover`, `get`, and `cancel`)
owned both an A2A SDK client and an `httpx.AsyncClient`. Their `finally` blocks
closed those owners sequentially with bare awaits. If the remote operation
failed and A2A client close also failed, the cleanup exception replaced the
remote/protocol error and HTTP cleanup was skipped entirely. Cancellation
during either close had the same early-return risk.

Those helpers now share one attempt-all cleanup path. A2A client and HTTP
client teardown each run in their own shielded task and settle despite repeated
caller cancellation. Both owners are attempted independently; cleanup failure
is attached as bounded generic context when an operation error is already
primary. A new cancellation arriving during cleanup is propagated only after
both owned resources have reached terminal cleanup states.

Regressions prove remote task lookup failure remains primary when A2A client
close also fails while HTTP cleanup still executes, and prove cancellation
during a blocked A2A client close cannot finish the helper before both client
and HTTP cleanup complete. The complete A2A unit file passes **43 tests**;
Ruff, targeted mypy, and `git diff --check` are green.

### Re-audit continuation — A2A CLI owns SDK and HTTP cleanup without masking operation failures

The standalone `ash a2a send` command still had a lifecycle variant not covered
by the remote-agent helpers above. Its SDK client was closed with a bare await
inside `finally`, while the HTTP client relied on an async context-manager exit.
A directly reproduced send failure followed by SDK-client close failure surfaced
the cleanup exception and hid the actual remote operation error. Repeated caller
cancellation could also interrupt the close and return while client cleanup was
still unfinished.

The CLI now uses the same A2A cleanup ownership contract as configured remote
agents. The shared cleanup helper accepts whichever A2A/HTTP owners were
successfully constructed, attempts each independently, preserves an existing
operation error as primary, and settles cleanup through shielded named tasks
before propagating repeated cancellation. `inspect` uses the same HTTP cleanup
path, so card-resolution failures cannot be replaced by HTTP-client teardown
failure. Remote task cancellation during `send` is also retained as an owned
task before client teardown begins.

Regressions prove a send failure remains primary when A2A client close also
fails while HTTP cleanup still runs, and prove repeated cancellation during a
blocked successful-send client close cannot finish the command before client
and HTTP cleanup complete. The complete A2A CLI + core A2A slice passes **60
tests**; Ruff, targeted mypy, and `git diff --check` are green.

### Re-audit continuation — top-level AshClient owners preserve operation failures during cleanup

Two short-lived top-level `AshClient` owners still used bare `finally` closes:
the HTTP `ash serve` command and the isolated automation child runner. Both were
reproduced with an operation failure followed by `client.close()` failure. In
each case the cleanup exception replaced the server/prompt failure that actually
caused the operation to fail.

Both owners now keep the operation `BaseException` authoritative. Client close
is still attempted unconditionally; if it fails after an existing operation
error, Ash attaches only secret-redacted cleanup context and re-raises the
original failure. A close failure after an otherwise successful operation still
surfaces normally, so cleanup is not converted into false success. The
underlying `AshLoop.aclose()` remains the owner of its established bounded,
cancellation-safe runtime teardown rather than adding another teardown model at
these command boundaries.

Regressions prove HTTP server failure survives a simultaneous client-close
failure and automation prompt failure survives the same condition. The complete
automation + HTTP serve unit slice passes **132 tests**; Ruff, targeted mypy,
and `git diff --check` are green.

### Re-audit continuation — subprocess agent driver closes pre-publication state and preserves task failures

The durable subprocess-agent driver opened `SharedState` before validating the
optional live-approval endpoint and before constructing `SpawnAgentTool`. The
existing stale-approval regression exposed the interrupted state directly: a
mismatched approval attempt raised before ownership transferred to the tool and
left the opened shared-state SQLite owner live. The same driver also bare-awaited
`SpawnAgentTool.aclose()` in `finally`, so a task-execution failure could be
replaced by a later tool-cleanup failure.

The driver now treats tool construction as the explicit ownership-transfer
boundary. Approval validation and tool construction run while the driver still
owns `SharedState`; any failure before successful tool construction closes that
state and retains the original failure, attaching only redacted cleanup context
if state cleanup also fails. After construction, the tool remains the owner of
shared state. Task execution likewise remains primary if `tool.aclose()` fails;
a close failure after otherwise successful execution still surfaces normally.

The previously failing stale-approval owner regression now passes, and an
additional regression proves task execution failure remains primary when tool
cleanup also fails. The complete approval-channel/driver unit file passes **10
tests**; Ruff, targeted mypy, and `git diff --check` are green.

### Re-audit continuation — managed LSP query cleanup preserves primary failures

`inspect_lsp()` owned a `LanguageServerManager` around one query but closed it
with a bare await in `finally`. A deterministic reproduction forced the query to
fail and manager cleanup to fail afterward; callers received only the cleanup
exception, losing the actual LSP operation failure.

The command now records an in-flight operation failure before teardown. Manager
cleanup still runs unconditionally; a cleanup failure after a query failure is
attached as redacted diagnostic context and the query failure remains
canonical. Cleanup failure after an otherwise successful query still surfaces,
so teardown cannot become false success.

The new regression passes, and the complete managed-LSP unit file passes **52
tests**; Ruff, targeted mypy, and `git diff --check` are green.

### Re-audit continuation — configured A2A send shares attempt-all transport cleanup

The configured remote-agent `send_remote_agent()` path still differed from the
already-hardened recover/get/cancel helpers. It used an HTTP async context
manager plus a bare SDK-client close for non-cancellation exits. A reproduced
`send_message` failure followed by client-close failure surfaced the cleanup
exception instead of the remote send failure, and the two transport owners did
not share the attempt-all cleanup policy used elsewhere.

`send_remote_agent()` now owns the HTTP and optional SDK client explicitly and
settles both through the shared A2A cleanup helper. Setup, observer, streaming,
and cancellation failures therefore retain their primary identity while client
and HTTP teardown are each attempted independently. Remote task cancellation
still runs before transport teardown when a task ID is known, and repeated
caller cancellation cannot abandon transport cleanup.

A regression proves remote send failure remains primary when SDK-client close
also fails while HTTP cleanup still executes. Together with the CLI A2A work,
the complete A2A core + CLI slice passes **61 tests**; Ruff, targeted mypy, and
`git diff --check` are green.

### Re-audit continuation — ACP stdio runner preserves transport failures during shutdown

The top-level ACP stdio wrapper was outside the previously hardened ACP session
ownership model. `run_acp_agent()` bare-awaited `agent.aclose()` in `finally`,
so an ACP transport/runner failure followed by agent-shutdown failure surfaced
only the cleanup exception. A direct reproduction confirmed that ordering.

The wrapper now keeps the ACP transport failure authoritative, still attempts
agent shutdown, and attaches secret-redacted cleanup context if shutdown also
fails. A shutdown failure after an otherwise successful protocol run continues
to surface normally. The underlying `AshACPAgent.aclose()` remains responsible
for its existing retained-session and cancellation-safe ownership semantics.

The focused runner regression and the complete ACP unit file pass **37 tests**;
Ruff, targeted mypy, and `git diff --check` are green.

### Re-audit continuation — CLI bootstrap shutdown preserves command failures

Both interactive and one-shot CLI bootstrap wrappers handled operation failures
inside their own error boundary and then unconditionally awaited
`AshLoop.aclose()` in `finally`. A deterministic reproduction forced session or
provider failure followed by runtime-close failure. Ash first emitted the
correct classified error, but the cleanup exception then escaped and replaced
the command's intended return contract; headless structured output therefore
contained the right error event while the coroutine still failed with the
unrelated shutdown exception.

The bootstrap wrappers now retain the in-flight operation or cancellation as
the primary outcome while runtime shutdown still runs. A secondary shutdown
failure is logged through Ash's redacting logger instead of emitting another
terminal event or replacing the classified error. A cleanup failure after an
otherwise successful command is still surfaced normally. Cancellation is also
kept authoritative rather than being replaced by teardown failure.

Regressions cover both structured headless and interactive bootstrap failure
followed by runtime-close failure. The broader error-taxonomy, headless UI,
HTTP, and JSON-RPC validation slice passes **73 tests**; Ruff, targeted mypy,
and `git diff --check` are green.

### Re-audit continuation — HTTP lifespan cleanup preserves application failures

The FastAPI lifespan wrapper around `JSONRPCServer` had the same outer-boundary
masking shape even though `JSONRPCServer.close()` itself was already retryable
and cancellation-safe. If the lifespan body failed and JSON-RPC/client shutdown
also failed, the context-manager finalizer surfaced only the shutdown exception.
A direct reproduction with `close_client_on_shutdown=True` confirmed the
application/lifespan failure was lost.

The lifespan now records any body failure before JSON-RPC shutdown. Cleanup is
still attempted unconditionally; shutdown failure after an existing body error
is attached as redacted diagnostic context and the body failure remains
canonical. A shutdown failure after an otherwise successful lifespan remains a
real failure. No JSON-RPC ownership or close/retry semantics were changed.

A focused lifespan regression passes, and the surrounding HTTP/JSON-RPC tests
remain green in the **73-test** validation slice above; Ruff, targeted mypy, and
`git diff --check` are green.

### Re-audit continuation — live subagent approval cleanup retains failed listener ownership

The final live-approval subprocess wrapper exposed two related defects. First,
`_settle_spawn_cleanup_task()` differed from Ash's other settle helpers: if the
owned cleanup task itself failed while shielded, that exception escaped directly
from `asyncio.shield()` instead of being returned to the caller for attribution.
This made cleanup failures bypass the caller's primary-error logic. Second, the
post-launch approval-listener finalizer held its `ForegroundApprovalServer` only
in a local variable. A failed listener close therefore both replaced a prior
subprocess communication failure and discarded the retryable listener owner.

The shared spawn cleanup helper now observes terminal cleanup-task failures and
returns them to its caller, preserving the repeated-cancellation contract.
`SpawnAgentTool` also retains approval servers whose close fails. All existing
pre-launch/launch cleanup branches now use the same retained close helper, and
the post-run finalizer preserves an existing subprocess/cancellation failure
while attaching bounded redacted approval-cleanup evidence. Failed approval
servers remain owned until `SpawnAgentTool.aclose()` retries them; shared state
is not closed before that retry succeeds.

A regression forces subprocess communication failure followed by a fail-once
approval-listener close. The communication failure remains primary, the listener
is retained after the first close attempt, and tool shutdown retries and clears
it successfully. The complete subagent-tool unit file passes **46 tests**;
Ruff, targeted mypy, and `git diff --check` are green.

### Re-audit continuation — agent SQLite shutdown retains retryable ownership

The agent coordination stores still had a lower-level lifecycle flaw beneath the
short-lived SDK/CLI wrappers. `SharedState.close()` marked itself closed before
closing either owned SQLite connection, then closed `AgentTaskStore` followed by
the shared-state connection sequentially. If the task-store close failed, the
second connection was skipped and the already-set closed flag prevented any
later retry. `AgentTaskStore.close()` had the same premature closed-flag update
for its own connection.

Both stores now retain ownership until cleanup actually succeeds.
`SharedState.close()` attempts both owned connections even when the first close
fails, preserves the first cleanup failure while attaching additional cleanup
context, and leaves itself retryable until all owners close successfully.
`AgentTaskStore.close()` likewise marks itself closed only after the connection
close succeeds. Idempotent successful close behavior is unchanged.

Regressions inject fail-once connection cleanup and prove the stores remain
retryable, including proof that `SharedState` still attempts its second owner
after the first owner fails. The complete shared-state + durable-task unit slice
passes **57 tests**; Ruff, targeted mypy, and `git diff --check` are green.

### Re-audit continuation — short-lived SharedState wrappers preserve operation failures

Even with retryable store teardown, short-lived agent SDK/CLI wrappers could
still replace the actual query or mutation failure with a later cleanup failure.
A deterministic reproduction forced an agent-status query to fail and then
forced `SharedState.close()` to fail; the cleanup exception became the public
error and the original operation failure survived only as exception context.

`SharedState` now provides a non-suppressing context-manager boundary. Successful
operations still surface cleanup failure, while an operation already failing
remains authoritative and receives cleanup failure as exception-note evidence.
The straightforward short-lived owners in the public SDK, agent operator
commands, and the simple subprocess-agent driver now use this ownership
boundary. Long-lived runtime and durable-driver owners remain explicit because
their ownership transfers are materially different.

A context-manager regression proves a primary query failure survives failed
state cleanup. The migrated agent CLI + SDK validation slice passes **53 tests**
(alongside the focused SharedState regression); Ruff, targeted mypy, and
`git diff --check` are green.

### Re-audit continuation — OAuth response and callback cleanup preserve protocol failures

Two OAuth transport finalizers were bypassing the module's existing
cancellation-safe cleanup contract. `_bounded_json_response()` bare-awaited
`response.aclose()` in `finally`; a reproduced HTTP 500 followed by response
close failure surfaced only the cleanup error. The localhost callback handler
had the analogous `drain()` followed by bare `writer.wait_closed()` teardown,
so a response-write failure could likewise be replaced by writer-close failure.

Both paths now use the established `_settle_oauth_cleanup()` /
`_finish_oauth_cleanup()` policy already used by the top-level login flow.
Protocol or write failures remain primary, cleanup is settled despite repeated
cancellation, and cleanup failures after otherwise successful work still
surface normally. Callback writer `close()` itself is also collected as an
owned cleanup failure instead of skipping the rest of teardown.

Regressions cover HTTP-error-plus-response-close failure and callback-drain-plus-
writer-close failure. The complete OAuth unit file passes **66 tests**; Ruff,
targeted mypy, and `git diff --check` are green.

### Re-audit continuation — A2A lifespan preserves application failure identity during shutdown

The A2A application lifespan had already been hardened to attempt handler,
executor, and optional SQLAlchemy-engine cleanup independently, but its outer
failure identity was still wrong. If the Starlette lifespan body failed and any
shutdown owner also failed, the finalizer raised the first cleanup error and
replaced the actual application failure.

The lifespan now records an in-flight body failure before teardown. All owned
shutdown steps are still attempted. When cleanup also fails, the application
failure remains authoritative and receives bounded shutdown-failure notes;
when no body failure exists, shutdown failure continues to surface exactly as a
real failure. The existing executor-retained-client and attempt-all teardown
semantics are unchanged.

A regression forces simultaneous lifespan-body and handler-shutdown failures and
proves the body failure remains primary. The complete A2A unit file passes **46
tests**; Ruff, targeted mypy, and `git diff --check` are green.

### Re-audit continuation — modern MCP subscription teardown survives repeated cancellation

Modern MCP task and resource subscriptions kept watcher tasks and protocol
correlation state in the client until `_stop_modern_*_subscription()` completed.
Those stop methods sent a best-effort stdio `notifications/cancelled` message
before cancelling the watcher and clearing its maps. Caller cancellation while
that notification write was blocked escaped immediately, leaving the watcher
and subscription state owned but still live; a later disconnect therefore had
to discover cleanup debt that the original stop operation had abandoned.

Task- and resource-subscription shutdown now each create one owned cleanup task
and settle it through the client's existing repeated-cancellation helper.
Cancellation is propagated only after the notification attempt, watcher
cancellation/join, and state clearing reach a terminal outcome. Cleanup failure
still surfaces when no cancellation is pending, while cancellation remains the
public outcome when cleanup itself also fails.

A parametrized regression blocks the cancellation notification for each
subscription kind, repeatedly cancels the stop caller, and proves the stop call
cannot finish until its watcher is joined and every correlation owner is
cleared. The complete ACP + modern-MCP validation slice passes **85 tests**;
Ruff, targeted mypy, and `git diff --check` are green.

### Re-audit continuation — ACP lifecycle reservations cannot be stranded by cancellation

ACP shutdown deliberately waits for `_pending_sessions` to drain before taking
its final runtime-owner snapshot. Reservation release previously acquired the
shared lifecycle lock directly in method `finally` blocks. A deterministic
reproduction held that lock, started `_release_reservation()`, cancelled the
caller, and observed `_pending_sessions == 1` afterward. That stranded fence
could consume session capacity indefinitely and make `aclose()` wait forever.
The fork path had a broader variant because child-ID, fork-source, and session
reservations were released sequentially; cancellation during the first release
could skip the remaining owners.

ACP now uses one cancellation-safe cleanup-task settlement primitive for
lifecycle reservation release. Session, pending-child-ID, and fork-source
release each settle before propagating cancellation. Fork teardown groups all
three ownership releases into one shielded cleanup operation, so cancellation
cannot expose a partially released fork lifecycle. Cleanup errors remain
visible, while caller cancellation is restored only after the reservation state
is coherent.

Regressions block the lifecycle lock, cancel both a normal session release and
a complete fork-reservation release, and prove cancellation does not finish
until `_pending_sessions`, `_pending_session_ids`, and `_forking_sessions` are
cleared as applicable. The complete ACP + modern-MCP validation slice passes
**85 tests**; Ruff, targeted mypy, and `git diff --check` are green.

### Re-audit continuation — subprocess pipe cleanup preserves ownership and primary failures

`communicate_process()` owned stdout/stderr readers, stdin writing, and the
return-code waiter, but its exception cleanup cancelled those tasks and then
awaited an unshielded `gather()`. A second caller cancellation could interrupt
that cleanup while a pipe task was still cancellation-resistant, allowing the
communication coroutine to lose its cleanup fence before all owned pipe tasks
settled. The stdin writer also bare-awaited `wait_closed()` in `finally`; a
reproduced `drain()` failure followed by close failure surfaced only the cleanup
failure.

Communication failure/cancellation now creates one owned pipe-cleanup task and
settles it despite repeated caller cancellation. A non-cancellation operation
failure remains the cause if cancellation arrives while cleanup is running;
initial cancellation remains authoritative once every pipe task is terminal.
The stdin writer separately records its write/drain failure before teardown, so
later close failure is attached as diagnostic context rather than replacing the
actual I/O failure. Successful stdin operation still surfaces an unexpected
cleanup failure normally.

Regressions use a cancellation-resistant stream reader to prove two caller
cancellations cannot make `communicate_process()` return with a live owned pipe
task, and force simultaneous stdin drain/close failures to prove primary-error
identity. The complete process-utils file passes **34 tests** with **3 skipped**
platform-specific tests; Ruff, targeted mypy, and `git diff --check` are green.

### Re-audit continuation — foreground approval client cleanup preserves protocol failures

The loopback approval server itself already had retryable, repeated-cancellation
shutdown, but the requesting client side still closed its `StreamWriter` with a
bare `wait_closed()` in `finally`. A deterministic reproduction returned a
malformed/correlation-invalid approval response and then failed writer cleanup;
the writer-close exception replaced the protocol failure, obscuring why the
approval exchange was rejected.

`request_foreground_approval()` now records its in-flight protocol or transport
failure before client teardown and settles writer cleanup through the module's
existing approval cleanup helper. Protocol failure remains primary when cleanup
also fails, caller cancellation is not returned until client cleanup settles,
and a cleanup failure after an otherwise successful request still surfaces
normally. Cleanup diagnostics are intentionally generic so writer errors cannot
introduce unreviewed response or argument data into exception notes.

A regression proves response-correlation failure remains authoritative when
`wait_closed()` also fails. The complete approval-channel unit file passes **11
tests**; Ruff, targeted mypy, and `git diff --check` are green.

### Re-audit continuation — worktree-creation cancellation cannot strand durable agent tasks

The remaining isolated-agent setup path had one real cancellation hole before
provider execution began. If worktree creation succeeded and artifact handoff
was still in progress, an initial caller cancellation entered the setup cleanup
branch and started `WorktreeManager.remove()`. A second cancellation while that
remove awaited I/O escaped immediately, skipping durable task terminalization
and heartbeat shutdown. A deterministic reproduction left the claimed task in
state **running** after the outer coroutine had already returned
`CancelledError`.

Worktree removal is now an explicitly owned cleanup task settled through the
existing repeated-cancellation helper. The creation-time cancellation path does
not return cancellation until worktree removal reaches a terminal outcome, the
durable lease is cancelled (when still owned), and the lease heartbeat is
closed. Cleanup errors are attached as bounded/redacted cancellation evidence
rather than replacing cancellation. The same worktree-settlement helper now
covers execution-finalizer cleanup so repeated cancellation cannot skip a
remaining isolated-worktree removal before heartbeat shutdown.

A regression cancels worktree setup, blocks the first cleanup removal, cancels
the caller a second time, and proves the outer task stays pending while the
durable task remains running. Once cleanup is released, the durable task is
persisted as cancelled and only then does caller cancellation escape. The
complete subagent-tool unit file passes **47 tests**; Ruff, targeted mypy, and
`git diff --check` are green.

### Re-audit continuation — managed worktree storage creation is descriptor-anchored

The broader path-bypass review found one remaining check/use race in the host-side
managed-worktree storage setup. `WorktreeManager._prepare_storage_root()` first
validated the configured storage path, then called pathname-based
`mkdir(parents=True)`, then validated again. A deterministic parent-swap
reproduction replaced the already-validated state directory with a symlink just
before `mkdir`; Ash created `worktrees/<repo-id>` under the replacement target
before the post-check could reject the new path. The later rejection therefore
did not undo the escaped host-side mutation.

Managed worktree storage and its Ash-owned empty Git-hooks directory are now
created/opened through `AnchoredDirectory.open(..., create=True)`. Every path
component is traversed through held directory descriptors, newly created
components are opened and identity-checked before traversal continues, and the
final visible pathname must still identify the held directory. The existing
unlinked-path preflight remains so previously supported symlink/junction errors
keep their stable diagnostic wording; it is advisory only, while descriptor
creation is the mutation authority.

The race regression swaps the visible state parent immediately before creation
of the `worktrees` component. Ash may create the component only under the held
original directory, then fails closed when the visible path no longer matches;
the replacement target remains untouched. The complete managed-worktree unit
file passes **29 tests**; Ruff, targeted mypy, and `git diff --check` are green.

### Re-audit continuation — JSON-RPC admission is bounded and request IDs stay cancellable

The remote-admission review found that JSON-RPC notification work was already
bounded, but ordinary requests were not. Every accepted request is retained in
`_request_tasks` until completion and every non-null ID is additionally kept in
`_pending` for `$/cancelRequest`; a slow authenticated stdio or HTTP peer could
therefore accumulate unbounded in-flight request state even though HTTP has a
separate per-address rate limiter. Stdio has no such network-rate boundary.

JSON-RPC now admits at most **64** ordinary in-flight requests globally. Once
that ownership budget is full, later requests receive server error **-32001**
(`Server is busy`) without starting handler work. `$/cancelRequest` remains
admissible while at capacity so a client can release existing work instead of
being fenced out by the limit. Notification ownership keeps its existing,
separate 64-task budget.

The same review found an identity bug in the cancellation map. Two concurrent
non-null requests could reuse one JSON-RPC ID; the second replaced
`_pending[id]`, and completion of either request could remove the remaining
request's cancellation handle. Ash now rejects a duplicate non-null in-flight
request ID as an invalid request before handler dispatch. Explicit-null request
IDs retain their existing semantics: they are owned by `_request_tasks` but are
not inserted into the cancellation map because null cannot uniquely identify
multiple concurrent requests.

Regressions prove the request budget refuses additional handler execution while
still accepting cancellation, and prove a duplicate ID cannot replace the
original request's cancellation identity. The complete JSON-RPC + HTTP adapter
slice passes **47 tests**; the combined subagent/worktree/JSON-RPC/HTTP security
gate passes **123 tests**. Ruff, targeted mypy, and `git diff --check` are green.

### Re-audit continuation — A2A execution has bounded runtime admission

The remote-admission review found that every inbound A2A task constructed its
own `AshClient`, but the executor had no in-flight task budget. The authenticated
per-address requests-per-minute limiter bounded arrival rate, not concurrency:
slow tasks could remain active while later requests created additional complete
Ash runtimes. A deterministic executor reproduction submitted **20** blocking
tasks and observed **20 Ash clients and 20 live streams** concurrently.

`AshA2AExecutor` now owns a default **16-task** in-flight budget. Admission is
checked after request validation and failed-client cleanup but before allocating
a new `AshClient`; excess tasks receive a terminal failed status instructing the
caller to retry rather than being queued invisibly or starting more runtime
state. The slot is released in an outer `finally`, so client-close failures,
ordinary exceptions, and cancellation cannot leak executor capacity. The limit
is an executor constructor contract rather than an HTTP-only throttle, so it
also applies to official A2A transports independently of client address.

A focused regression holds two admitted clients open under a test limit of two,
proves a third task starts no client and receives a failed status, then proves
capacity returns to zero after the admitted tasks finish. The complete A2A unit
file initially passed **47 tests** after the admission fix.

The adjacent failed-client cleanup path had one more ownership hole. A client
whose `close()` failed was retained by awaiting the same lock used by retired-
client retry. If another retry held that lock and the task was cancelled while
waiting, `execute()` could return `CancelledError` without recording the failed
client anywhere, permanently losing the only handle available for later
cleanup. A deterministic contention reproduction confirmed exactly that state.
Retired-client retention is now a synchronous ownership handoff: cleanup retries
operate from a snapshot, so a newly retained client can be added without
waiting and remains for the next pass. The contention regression proves caller
cancellation cannot erase the failed-close owner. The complete A2A unit file now
passes **48 tests**; Ruff, targeted mypy, and `git diff --check` are green.

### Re-audit continuation — managed worktree leases bind storage and worker identity

The broader managed-worktree path review found a narrower external-process race
after descriptor-anchored storage creation. `git worktree add` accepts a
pathname destination. A deterministic real-Git reproduction swapped the
Ash-owned state parent **after** the final hooks/storage validation but
immediately before `_run_git` spawned `git worktree add`; Git followed the new
pathname and created the checkout under the replacement directory. Before this
pass Ash then returned a lease for that redirected worktree.

Git's current `worktree add` interface documents the destination as a `<path>`;
Ash therefore does not pretend that the external Git process has the same
descriptor-bound destination contract as Ash's own filesystem mutations.
Instead, the enforceable application boundary is now explicit and fail-closed:

- `WorktreeManager` pins the Ash-owned storage-root device/inode for its
  lifetime and rejects a later regular-directory replacement as well as links;
- after `git worktree add` returns, Ash reopens and validates the storage root
  and created worktree before publishing a lease;
- each `WorktreeLease` records the created worktree device/inode, and every
  later managed Git operation refuses a replaced worktree directory;
- the isolated worker passes that lease identity into both `SafetyGuard` and
  `SandboxManager`, whose constructors now optionally require an expected root
  identity before model/tool execution begins.

Consequently, a concurrently hostile same-account process can still cause Git
to perform a host-side write through a replaced destination pathname during the
external command, but Ash will not turn that write into an executable agent
workspace or later accept a replaced lease. This matches Ash's broader local
application threat model instead of making a false strict same-principal
namespace claim.

Regressions cover initial expected-identity rejection in `SafetyGuard` and
`SandboxManager`, storage-root replacement around worktree creation, and
worktree-directory replacement after a valid lease is issued. The complete
managed-worktree suite passes **30 tests**, the safety+sandbox suites pass
**158 tests with 1 platform skip**, and the combined subagent/worktree/A2A
slice passes **125 tests**. Ruff and targeted mypy are green.

### Re-audit continuation — sandbox workspace identity survives nested-cwd launch paths

The broader sandbox path-bypass review found one remaining whole-workspace
replacement hole in `SandboxManager.run()`. The manager pinned its workspace
root inode at construction, but direct/scoped launch helpers created a fresh
`SafetyGuard` from the visible workspace pathname. When the requested `cwd`
was a nested directory rather than the workspace root itself, the manager also
snapshotted that nested cwd after the replacement. A deterministic reproduction
replaced the entire workspace tree after manager creation and then ran a direct
command in `workspace/nested`; Ash executed the command in the replacement tree
and created the marker there.

The manager now carries its original workspace identity through both direct and
wrapped launch helpers. Their `SafetyGuard` construction requires that identity
before subprocess creation, so a nested cwd cannot silently re-anchor to a new
workspace tree. Docker and macOS pathname-based wrapped launches receive the
same pre-spawn root identity check. Descriptor-backed Bubblewrap is deliberately
exempt from the later pathname check: its launch context has already opened and
verified the original workspace descriptor, and existing live regressions prove
that replacing the visible path after wrapping still mounts the held original
workspace and extra read-only directory.

Regressions prove both root-cwd and nested-cwd direct execution fail before any
side effect after whole-workspace replacement, and the wrapped-backend helper
refuses a replaced pinned workspace before `create_subprocess_exec` is called.
The complete sandbox file passes **80 tests with 1 platform skip**; Ruff,
targeted mypy, and `git diff --check` are green.

### Re-audit continuation — direct HTTP turns have bounded admission

After bounding JSON-RPC and A2A ownership, the direct authenticated HTTP turn
routes still had an admission asymmetry. `/v1/turn` and `/v1/turn/stream` both
share one `AshClient`; slow requests therefore wait behind its turn lock, but
the HTTP adapter itself had no in-flight turn budget. The per-address sliding
window limits arrival rate over time rather than concurrent retained request
state, so it was not an ownership bound.

The direct turn routes now share a default **16-turn** admission budget. A full
budget returns HTTP **503** with a short `Retry-After` hint before invoking the
client. Synchronous turns release their slot in `finally`; SSE turns retain the
slot for the lifetime of the streaming generator and release it in generator
cleanup. Steering remains outside this budget so an operator can redirect an
active turn even while direct execution capacity is full. JSON-RPC keeps its
separate 64-request ownership budget and cancellation semantics.

A concurrency regression holds two admitted turns open under a test budget of
two, proves a third request returns 503 without another client call, proves
steering remains accepted at capacity, and then proves capacity is reusable
after both turns finish.

The first implementation attached slot release to the SSE async generator's
`finally`, which left one lifecycle hole: constructing a `StreamingResponse`
acquired capacity before ASGI began consuming the body. Closing a never-started
async generator does not enter its body/finally, so an abandoned response could
permanently strand a slot. A deterministic one-slot reproduction created the
response, closed its never-started iterator, and proved the next ordinary turn
received 503. Admission ownership now lives in a response subclass instead:
the slot is acquired immediately before ASGI response execution and released in
the response `__call__` outer `finally`. A response object that is never started
therefore owns no capacity, while mid-stream disconnect/cancellation still
returns the slot. Regressions cover both cases. The combined HTTP + JSON-RPC +
A2A adapter slice now passes **98 tests**; Ruff, targeted mypy, and
`git diff --check` are green.

### Re-audit continuation — Docker gets hard resource limits without fake native parity

The resource-containment gap was revisited against current platform and
competitor behavior rather than treating the previous audit conclusion as
immutable. Docker's current resource contract exposes cgroup-backed hard memory,
CPU, and PID controls; `--memory` has a documented 6 MiB minimum, `--cpus`
constrains aggregate container CPU quota, and the existing Ash Docker backend
already emitted `--pids-limit=256`. Current OpenClaw and Hermes container
sandboxes likewise expose Docker/container CPU and memory controls instead of
claiming equivalent native-backend aggregate containment.

The earlier conclusion remains correct for cross-backend parity: Bubblewrap is
an isolation/mount namespace tool and does not itself expose a corresponding
aggregate CPU/RAM budget, while POSIX/macOS `setrlimit` semantics are
per-process/inherited rather than an equivalent tree-wide cgroup budget. Ash
therefore still does **not** add a superficial per-process rlimit layer or call
resource containment solved across backends.

A fresh Linux prototype also verified that `systemd-run --user --scope` can
create a real user cgroup with `MemoryMax`/`CPUQuota` on the current supported
host and preserves an explicitly inherited directory descriptor into the
scoped command. That makes a Bubblewrap+cgroup enhancement technically viable,
but it was deliberately not merged here: user-manager/cgroup delegation is not
uniform across supported Linux environments, macOS still has no equivalent
aggregate boundary, and silently reusing the newly Docker-specific settings
would create a misleading configuration contract. This remains design evidence
for a later explicit native-resource policy, not an implicit fallback.

What changed is the Docker-specific decision. Ash now applies finite defaults
where the backend can enforce them honestly: **4096 MiB RAM, 2 CPU, and 256
PIDs**. `sandbox_docker_memory_mb` and `sandbox_docker_cpus` are user-owned
config fields and remain unavailable to project TOML; `0` explicitly disables
the corresponding Docker limit. The memory value must be `0` or at least 6 MiB
to match Docker's runtime minimum. The manager mirrors the config bounds so
internal callers cannot bypass the same contract. Docker's RAM limit is not
misstated as a no-swap guarantee: when Ash does not set `--memory-swap`, Docker
and host swap policy still determines additional swap availability.

The policy is propagated through the main runtime sandbox, isolated subagent
workers, executable-plugin runtime and immutable Docker staging, live plugin
reload, `ash sandbox status`, and doctor. Docker status diagnostics report the
effective RAM/CPU/PID settings. The low-level argv regression proves the
configured values become `--memory 2048m`, `--cpus 1.5`, and the existing
`--pids-limit=256`; zero-valued RAM/CPU controls omit those Docker flags.
Trusted project-config regression proves a repository cannot raise, disable, or
otherwise replace the user's Docker resource policy.

Validation after the change: the complete config+sandbox slice passes **162
tests with 1 platform skip**; runtime/plugin/doctor/sandbox-status passes **109
tests**; the complete subagent-tool file passes **47 tests**. Ruff and targeted
mypy are green. The parity matrix intentionally remains **Partial** because
Bubblewrap/macOS aggregate CPU/memory containment is still not equivalent to the
Docker cgroup boundary.

### Re-audit continuation — subagent startup failures release durable claims immediately

The durable subagent admission review found a post-claim ownership gap in
`SpawnAgentTool`. `AgentTaskStore.claim_task()` atomically counted and leased a
slot before the task transitioned to `running` and before the lease heartbeat
was fully established. Those setup steps are synchronous, so ordinary asyncio
cancellation cannot interrupt between them, but an exception from the running
transition, lifecycle sink, initial lease renewal, or heartbeat setup could
escape with the durable row still `leased`. A deterministic reproduction made
`start_task()` fail after a one-agent claim and observed the row remain leased,
causing later work to see false concurrency exhaustion until lease expiry.

Post-claim setup is now treated as one ownership handoff. Any setup failure
preserves the original exception while immediately releasing durable capacity:
a task created by the current tool call fails terminally, while pre-existing
dispatcher work uses the store's retryable failure transition and returns to
`queued` when attempts remain. Once a heartbeat object exists, rollback is
serialized through its operation lock before heartbeat cleanup so lease renewal
cannot race the terminal/requeue write. Rollback or cleanup failures are added
as redacted exception notes rather than masking the original setup failure.

Regressions prove a failed newly created start leaves no leased capacity and a
replacement worker can run immediately; a pre-existing two-attempt queued task
returns to `queued` after the first startup failure and succeeds on attempt two.
The complete subagent-tool file passes **49 tests**. Ruff, targeted mypy, and
`git diff --check` are green.

### Re-audit continuation — browser policy proxy bounds hostile connection fan-out

The browser-network review revalidated the proxy's DNS-rebinding boundary: the
requested hostname is resolved once, every answer must be globally reachable,
and the upstream connector receives an accepted **numeric IP** with
`AI_NUMERICHOST`, so connection establishment does not perform a second
hostname resolution. Relay I/O is also chunked and awaits `drain()` in both
directions.

The remaining resource gap was accepted-connection ownership. Every local
Chromium connection created one retained handler task and writer, but there was
no ceiling on `_connection_tasks`. A hostile webpage can create many long-lived
HTTP/WebSocket connections through the loopback proxy without additional Ash
tool calls, so this was an attacker-influenced unbounded server-side resource
set even though individual relay buffers applied backpressure.

`BrowserPolicyProxy` now defaults to at most **256** simultaneously accepted
connections (constructor-validatable from 1 through 4096). Once full, a newly
accepted socket is closed synchronously before it is added to `_writers` or a
handler task is created. The focused regression holds one permitted connection
open under a one-connection test budget, proves the second connection is closed
without increasing task/writer ownership, then proves the first task retires
cleanly. The complete browser-proxy unit file passes **9 tests**; Ruff,
targeted mypy, and `git diff --check` are green.

### Re-audit continuation — doctor storage probe uses the production SQLite identity boundary

The remaining raw-host-mutation sweep found that `_check_storage()` did not use
the same path contract as production SQLite. It validated
`db_directory/probe.sqlite3`, called pathname-based `mkdir(parents=True)`, then
validated again before creating a temporary SQLite database. A deterministic
parent-swap reproduction replaced the already-validated state parent with a
symlink immediately before `mkdir`; doctor created the database directory under
the replacement target and still reported **storage: pass**.

The storage diagnostic now uses `PinnedSQLiteDatabase.prepare()` with a unique
`.doctor-<uuid>.sqlite3` filename. That is the same descriptor-anchored primitive
used by Ash-owned production databases: parent creation/traversal is anchored,
the database file and parent inode are pinned, SQLite is opened in `mode=rw`
only after pinning, and identity is verified again after the round trip. Cleanup
reopens the pinned parent and removes the exact probe inode plus present SQLite
sidecars through anchored directory operations; a changed probe identity fails
cleanup closed rather than deleting a replacement.

The parent-swap regression now proves the diagnostic fails and the replacement
tree receives no `db` directory. Existing regressions continue to prove
concurrent probes do not collide, a pre-existing `.doctor.sqlite3` sentinel is
untouched, SQLite failure artifacts are removed, malformed directories fail,
and symlinked database directories are rejected. The complete doctor unit file
passes **32 tests**; Ruff, targeted mypy, and `git diff --check` are green.

### Re-audit continuation — session backups stay bound to the store's pinned database

The pinned-owner audit found that `SessionStore` established a stable
`PinnedSQLiteDatabase` at construction, but `backup()` later bypassed that owner
for the source copy. It checked `Path(self.db_path).is_file()`, reacquired a
pathname-based coordination lock, and opened the visible source path with a
fresh descriptor. Those checks proved only that the newly opened file remained
stable during the copy; they did **not** prove it was the database inode the
store had originally pinned.

A deterministic reproduction created a `SessionStore`, renamed its pinned
`sessions.db` away, installed a different regular SQLite database at the same
pathname, then called `backup()`. Before this fix the backup succeeded and the
result contained the replacement database's table/data rather than the store's
original session database.

Backup now holds `self._database.parent_directory()` for the source-copy window,
verifies the visible source entry matches the store's pinned device/inode, binds
exclusive Ash database coordination to that held parent descriptor, and opens
the source file through the anchored directory with the expected metadata. The
opened descriptor is checked against the store's pinned database identity before
copying. WAL/SHM/journal inspection is also performed through the same held
parent descriptor instead of pathname `lstat` calls, and the parent path is
revalidated before the source ownership window closes. When a coordination
parent descriptor is supplied, `exclusive_database_access()` no longer performs
a redundant pathname `mkdir`, avoiding a weaker re-resolution step.

Regressions now cover both a symlink replacement before backup and a different
regular SQLite file installed before backup; both are rejected and no backup is
published. The existing mid-copy regular-file ABA regression remains, as do the
live-WAL and storage restore/export checks. The combined browser-proxy + doctor
+ session/storage/export gate passes **138 tests**; the session/storage/export
slice alone passes **97 tests**. Ruff, targeted mypy, and `git diff --check` are
green.

### Re-audit continuation — database coordination is descriptor-anchored by default

The session-backup work exposed a latent footgun in the generic
`exclusive_database_access()` helper. Its security depended on callers remembering
to supply a pinned parent descriptor; the default branch still performed a raw
pathname `mkdir(parents=True)` and opened the coordination lock by pathname.
Current hardened callers were safe, but a future storage-level caller could
silently downgrade the same identity boundary.

`exclusive_database_access()` now establishes an `AnchoredDirectory` for the
database parent whenever the caller does not already provide a coordination
parent descriptor. The lock file is opened relative to that held directory and
the visible parent identity is validated before and after the exclusive window.
Anchored/OS path failures are normalized to `SessionStorageError`, preserving the
storage API contract. A deterministic parent-swap regression now proves that a
replacement tree receives no database directory or coordination file. Existing
queued-writer/nested-reader behavior remains intact. The complete
session/storage/export slice passes **98 tests**.

### Re-audit continuation — browser popup bursts coalesce admission ownership

The browser already capped live tabs at **32**, but each Playwright `page` event
created a separate asynchronous `_admit_page()` task before that cap was
enforced. A hostile page could therefore emit a large popup burst in one event
loop turn and allocate an unbounded `_page_tasks` set even though overflow tabs
would eventually be closed.

Popup admission is now coalesced. A page event marks admission dirty and ensures
at most one background admission-drain task exists. That task serializes on the
tab lock, enforces the context-wide 32-tab limit, remembers only surviving pages,
and loops again only when more page events arrived while it was running. A
regression injects **256** overflow popup events and proves Ash retains one
admission task, closes all overflow pages, and keeps tab bookkeeping within the
32-page budget. The complete browser-tool suite passes **54 tests**.

### Re-audit continuation — provider tool batches and read-only fan-out are independently bounded

The core loop previously had no canonical bound on tool calls returned by one
provider completion. Native providers could accumulate an arbitrarily large
`native_tool_calls` list, and fallback XML could append arbitrarily many parsed
calls. If every call name appeared in the permission-policy `READ_ONLY_TOOLS`
set, Ash then scheduled the whole batch concurrently with `asyncio.gather()`.
That coupled authorization semantics to concurrency safety and allowed a
malformed/adversarial provider response to create very wide live-operation
fan-out.

Ash now rejects completions containing more than **64 tool calls**, with the
limit enforced while native/XML output is accumulated so the oversized batch is
rejected before assistant/tool persistence or dispatch. Independent parallel
execution is capped at **8** live calls. Concurrency safety is now explicit in
`ToolExecutionContract.parallel_safe` (default `False`) rather than inferred
solely from permission-read-only classification. Only core independent local
inspection tools currently opt in: file read, directory/glob/text search,
symbol/reference lookup, and read-only Git status/diff/log. Interactive or
stateful permission-read-only tools such as `ask_user`, browser/session reads,
remote-task recovery, skill activation, and extensions without an explicit safe
contract remain sequential.

Provider tool-call reuse checking was tightened at the same boundary. The loop
no longer loads every historical tool-call ID for a session into a Python set at
the start of each turn; it performs a bounded database lookup for only the
current completion's at-most-64 candidate IDs while preserving same-turn and
cross-turn reuse rejection. Regressions prove an oversized native batch is
rejected before any durable tool call exists, parallel-safe reads never exceed
8 concurrent executions and preserve ordering/cancellation semantics, and
permission-read-only alone does not authorize parallel execution. The complete
core-loop suite passes **122 tests**.

### Re-audit continuation — workspace-bound subsystems use one pinned root identity

A bounded identity-authority sweep found three constructors that created a
`SafetyGuard` and then separately `stat()`ed the same workspace/root to derive a
second expected inode: LSP, repo-map, and the automation subprocess client. The
runtime paths were already descriptor-safe, but the duplicate stat created an
unnecessary mixed-generation window and two potential authorities for the same
workspace identity.

All three now use `SafetyGuard.project_root_identity` as the single startup pin.
Automation also passes a supplied expected workspace identity directly into
`SafetyGuard` construction, so a mismatch is rejected at object creation rather
than deferred until process launch. Existing pre-launch replacement and
mid-launch cwd-swap regressions remain green. The complete LSP + repo-map +
automation gate passes **211 tests**.

During final validation DevSpace's host `uv` had upgraded to **0.12.20** while
Ash deliberately pins **0.12.17** in `pyproject.toml` and CI. The project pin was
not changed to accommodate host drift; validation continued through the existing
project `.venv` (Python 3.12.13) without mutating the system toolchain. Ruff,
targeted mypy across **11 changed source files**, and `git diff --check` are
green.

### Re-audit continuation — automation cleanup debt remains inside effective run capacity

Automation run admission was bounded, but cancellation-resistant operations could outlive their durable run outcome in `_deferred_cleanup_tasks`/`_detached_tasks`. The normal run slot was freed as soon as the durable failure returned, so another automation could start while the prior provider operation or client cleanup was still alive. Repeating that pattern made `automation_max_concurrent_runs` an incomplete bound on actual Ash-owned work. `AutomationWorkerService.aclose()` could also report success while those owned tasks were still active.

The worker now treats unresolved owned cleanup as capacity debt. Scheduled claiming pauses while that debt exists and direct execution fails durably before allocating another runtime. `aclose()` gives owned cleanup a bounded opportunity to settle without cancelling it, then refuses to report successful closure while any owned cleanup remains; only after settlement does retired-client retry run. Regressions cover cancellation-resistant operations and close paths. The complete automation suite passes **126 tests**.

### Re-audit continuation — provider replacement serializes retirement and bounds provider identity fan-out

Provider replacement previously retired the old provider asynchronously and immediately allowed another switch. A transport whose close stalled could therefore be followed by arbitrarily many additional switches, growing retained provider-close tasks/owners. Provider identity had two adjacent cardinality gaps as well: canonical `provider/model` strings had no size ceiling, and `fallback_models` had no count limit even though the registry eagerly constructs the full fallback chain. The provider circuit breaker also retained one state entry for every distinct one-off failing identity until that exact identity later succeeded.

Ash now serializes provider switches against prior provider cleanup debt: a new switch is refused while the previous transport is closing, and unresolved cleanup failure requires close/restart rather than stacking more transports. Canonical model identifiers are limited to **512 UTF-8 bytes** and runtime switches go through the same parser instead of bypassing validation with `model_copy`. Fallback chains are limited to **16** validated, canonical, distinct model IDs. Provider circuit state retains at most **256** identities, with recency-aware oldest-key eviction. The complete core-loop suite passes **122 tests**; provider registry/retry plus approval-channel validation passes **79 tests**, configuration + SDK passes **107 tests**, and runtime integration passes **38 tests**.

### Re-audit continuation — durable subagent coordination is workspace-isolated and cardinality-bounded

The shared `agents.db` is global by default. Durable task rows already carried workspace scope, but `agent_status`, IPC addresses, approvals and sprint owners were keyed only by logical agent IDs. A deterministic reproduction showed two workspaces registering the same `worker` overwrote the same status row, and one workspace could read the other workspace's `lead` IPC messages. Separately, repeated task creation could grow queued/leased/running work indefinitely even though live execution was capped, and successful agent/IPC history accumulated without retention bounds.

Workspace-bound `SharedState` now namespaces stored agent addresses with a hash of the canonical workspace while preserving logical IDs at the public API. Status, IPC, approval resolution/recovery, broadcasts and sprint ownership all scope to that namespace; approval correlation additionally requires the durable task workspace. Runtime assembly, child drivers, SDK helpers and `ash agents` operator commands bind the same workspace end to end. Legacy unbound access remains available for compatibility but scoped production/CLI access does not fall back to unscoped rows.

Durable nonterminal agent tasks are capped at **1024 per workspace** transactionally, while terminal task history remains durable. IPC payloads are valid-JSON objects capped at **512 KiB**, pending IPC is capped at **1000 messages per recipient**, delivered IPC history retains the newest **10,000 rows per workspace**, and terminal agent-status history retains **2048 completed/failed rows per workspace** without pruning live agents. Regressions prove canonical workspace aliases share quota, terminalization frees task capacity, queue exhaustion happens before provider allocation, cross-workspace status/messages/approvals remain isolated, and pending messages are never silently pruned. Shared-state + agent CLI + task-store validation passes **91 tests**; the complete higher-level SpawnAgent/subprocess/approval suite passes **70 tests**.

### Re-audit continuation — outbound A2A tracking has a fail-closed live-state budget

`RemoteTaskStore` already scoped rows by workspace and bounded every field, but every distinct remote handle and unresolved pre-acceptance intent could be retained forever. Accepted nonterminal handles cannot simply be evicted: they may be the only durable identity available for later status, cancellation or ambiguity recovery.

Ash now budgets **1024 nonterminal remote tracking slots per workspace**, counting unresolved intents and nonterminal handles together under `BEGIN IMMEDIATE`. Converting a matching intent into a handle reuses its slot, terminalizing a handle frees live capacity, and unknown states count conservatively as nonterminal. Terminal remote history is independently retained at the newest **10,000 rows per workspace**. New delegation performs a preflight before remote `send_message` when possible, while the transactional store remains the race-safe authority; persistence failure after remote acceptance still triggers the existing remote cancellation path. Workspace independence, intent-to-handle recovery, terminal capacity release, history pruning and pre-dispatch refusal are covered by regressions. The complete A2A suite passes **50 tests**.

### Re-audit continuation — LSP root ownership and retry history are bounded

LSP configuration limited server definitions to 32 and diagnostic caches to 256 files, but each configured server could own a persistent client for every distinct nested root-marker directory encountered. Repeated queries across a large/adversarial monorepo could therefore accumulate unbounded language-server processes. Failed roots also accumulated independently in `_broken`/`_failure_counts`.

The manager now owns at most **64 `(server, root)` clients/startups** at once, enforced under the existing manager lock so concurrent startup requests cannot race past the ceiling. Failure/retry history retains at most **256 root keys**, evicting the oldest record when a new key arrives. Existing per-file diagnostics limits and restart/backoff behavior remain unchanged. The complete LSP suite passes **54 tests**.

### Re-audit continuation — foreground approval replay protection is bounded without forgetting replays

Foreground live approval replay defense retained every authenticated request ID for the lifetime of one subagent attempt. Evicting old IDs would weaken replay protection, but retaining an unlimited set let a long-lived child grow server memory with unique valid approval requests.

Each foreground attempt now accepts at most **1024 unique approval request IDs**. The server never evicts IDs from the replay set; once the ceiling is reached, further authenticated requests fail closed without invoking the approval handler or extending replay history. The existing client treats that closed request as an approval-channel failure. Focused replay-limit tests and the complete higher-level approval/subagent suite are green.

### Re-audit continuation — asynchronous lifecycle hooks have bounded ownership and shutdown semantics

`config_changed`, `permission_changed`, and `context_compacted` lifecycle notifications were previously launched with anonymous `asyncio.create_task()` calls. A deterministic regression showed five cancellation-resistant observers starting concurrently under a test budget of two: hook timeouts bounded each observer's intended duration, but there was no admission bound on already-scheduled observer tasks and `AshLoop.aclose()` did not own them. Hook configuration also limited file size but not callback cardinality, so many hook sources could multiply one runtime event into an arbitrarily large number of sequential subprocess observers.

Ash now owns scheduled lifecycle observers explicitly. At most **32** asynchronous lifecycle observer tasks may be live; finished tasks are retired with failures retrieved, `context_compacted` uses the same owner, and shutdown cancels/settles the retained tasks before reporting success. A cancellation-resistant observer prevents a false successful close until it actually settles. Hook registration is capped at **32 hooks per event across the merged registry**, with the same per-event precheck at config parse time so oversized files fail clearly before publication. The complete loop + hook gate passes **159 tests**.

### Re-audit continuation — orchestrator fan-out uses a bounded worker pool

`SubagentOrchestrator._run_agents()` used a semaphore to limit active subagents but still created one `asyncio.Task` for every submitted spec immediately. A regression with 20 specs and `max_concurrency=2` reproduced **20** orchestrator-owned pending tasks. Direct construction also accepted concurrency above the canonical configuration ceiling, so internal/SDK callers could bypass the configured maximum of 32.

The orchestrator now enforces **1–32** concurrency consistently with `AshConfig.max_concurrent_agents` and processes arbitrary-size batches with a fixed worker pool of at most `min(max_concurrency, len(specs))` tasks. This preserves batch support and completion-order report collection without pending-task amplification. Cancellation still settles every owned worker before returning. The complete subagent integration suite passes **32 tests**.

### Re-audit continuation — automation durable state has finite workspace and audit-history bounds

Automation execution concurrency and run retention were already bounded, but durable job creation itself had no workspace ceiling. Repeated model/user creation could therefore grow `automation_jobs` indefinitely; the scheduler also fetched every due job before applying its 32-run claim limit, turning lifetime job growth into recurring per-poll query/memory work. Separately, run retention intentionally detached old lifecycle rows from deleted runs and kept those `automation_events` forever, so recurring automations still produced an unbounded durable audit ledger.

Ash now admits at most **1000 non-deleted automation jobs per workspace** under the existing immediate transaction. Deleted jobs free capacity and other workspaces remain independent. Orphaned lifecycle events remain deliberately longer-lived than run payloads, but are now retained for at least **365 days**, or longer when the configured run-retention window exceeds one year; events still linked to retained runs are untouched. This preserves useful audit history without lifetime-proportional database growth. The complete automation suite passes **128 tests**.

### Re-audit continuation — MCP server and tool-catalog fan-out are bounded before publication

MCP configuration files were byte-bounded, but server cardinality was not. A compact config—or multiple namespaced plugin sources—could define hundreds of servers, and programmatic SDK/runtime/reload inputs could bypass the file loader entirely. Runtime startup allocates/configures one client per server and local stdio definitions may each own a subprocess. In addition, MCP pagination bounded aggregate catalog bytes but not tool count, allowing a server to pack a very large number of tiny tool definitions into the byte budget and force Ash to materialize every adapter.

Ash now allows at most **32 MCP servers** in one runtime. The same shared check applies to individual payload parsing, merged MCP sources, runtime assembly with programmatic additions, direct `MCPRuntime` construction, initial `AshLoop` configuration, and hot reload—including reloads before a session exists. Tool discovery is separately capped at **256 tool definitions per MCP server**, enforced during pagination before adapter construction or registry publication. File-splitting, programmatic reload, and 257-tool catalog regressions are covered. Full MCP transport passes **75 tests**, MCP runtime/tool integration passes **92 tests**, and the combined plugin-runtime + MCP-config gate passes **99 tests**.

### Re-audit continuation — executable plugin expansion and lifecycle identity bookkeeping are bounded

Plugin manifests already limited one plugin to 64 runtime tools, but there was no aggregate ceiling across active plugins, so many individually valid plugins could still expand into thousands of executable proxies in one runtime. The adjacent hot-reload audit found a correctness issue in lifecycle bookkeeping as well: plugin replacement removed old tool objects without always removing their numeric `id()` values from `_started_tool_ids`, and partial cleanup recorded closed IDs for tools it immediately unpublished. Because Python may later reuse object IDs, a future unrelated tool could be mistaken for an already-started or already-closed object.

Runtime assembly now caps executable plugin tools at **256 total across all active plugins**, checked before any sandbox manager, host client, or proxy is constructed. Plugin reload also forgets started/closed IDs whenever a tool leaves active ownership and no longer records closed IDs for successfully cleaned tools that are immediately unpublished. Shutdown retry semantics remain intact because IDs retained during a failed shutdown correspond to tool objects still strongly referenced by the active registry. Regressions cover normal replacement and mixed cleanup failure, and the full plugin-runtime suite is green within the **99-test** combined plugin/MCP-config gate.

### Re-audit continuation — remove the unused legacy MCP subprocess manager

The retained-owner sweep found one MCP process lifecycle implementation that no
longer had a production owner. `MCPServerManager` and `MCPServerInstance` could
start and retain stdio server subprocesses, but repository-wide usage showed no
production caller or current exported/documented API; only their dedicated unit
tests and archived design material referenced them. The live MCP path already
uses `MCPClient`, which owns stdio launch, cwd/source identity validation,
process-tree cleanup, transport readers, disconnect, and cancellation.

Keeping the second manager would preserve duplicate child-process lifecycle code
that could drift independently without providing a user-visible capability. The
legacy manager, its lifecycle-only exception/helper, and manager-only tests are
therefore removed. Shared `MCPServerConfig`, `resolve_mcp_stdio_launch()`, config
loading/validation, and the production `MCPClient` path remain intact.

Two focused regressions also pin the existing MCP config resource boundary:
argument cardinality is limited before publication and environment expansion is
revalidated against the configured byte ceiling. The complete MCP
config/runtime/transport slice passes **215 tests** after the removal; the MCP
config file alone passes **50 tests**. Ruff, targeted mypy, and
`git diff --check` are green.

### Re-audit continuation — synchronous Docker control commands own descendant cleanup

The external-process sweep found one remaining split in Docker control-plane
ownership. Async Docker volume/staging commands already use Ash's managed
process-tree lifecycle, but `probe_docker()` and the explicit
`ash sandbox build` command still launched the trusted Docker CLI through raw
`subprocess.run()`. That matters for remote Docker contexts because the CLI may
own SSH or other helper descendants. Python's timeout handling kills the direct
child only. A local reproduction with the same `subprocess.run(..., timeout=)`
shape confirmed the root timed out while its spawned child survived and later
wrote its marker.

Synchronous Docker CLI work now preflights a managed process tree, launches the
CLI in that owned group, preserves the existing scrubbed Docker environment,
and terminates the whole tree on timeout or any caller-side interruption before
the original failure propagates. Readiness probes keep their five-second bound;
the user-invoked image build remains unbounded during normal execution but now
has deterministic descendant cleanup if interrupted. A POSIX regression starts
a parent/child pair through the shared synchronous Docker runner, times out the
root, and proves the descendant cannot survive to publish its marker. The
complete sandbox + sandbox-CLI gate passes **89 tests with 1 platform skip**.

### Re-audit continuation — MCP transport startup and SSE shutdown retain ownership through cancellation

`MCPClient.connect()` previously installed the stdio/HTTP/SSE transport before
entering its disconnect-on-failure fence. Legacy SSE made the gap directly
reproducible: cancelling connect while the initial endpoint discovery future was
pending returned with the SSE reader task still live and the Ash-owned
`httpx.AsyncClient` still open. Disconnect also cancelled `_sse_task` without
joining it, so a cancellation-resistant event reader could outlive a reported
disconnect.

Transport establishment now occurs inside the existing startup rollback fence,
so failures or cancellation during stdio, HTTP, or SSE setup use the same
cancellation-safe disconnect path as protocol initialization failures. HTTP/SSE
event shutdown now cancels and settles the owned reader before teardown
continues, distinguishes the reader's expected cancellation from cancellation
of the caller, and only re-propagates caller cancellation after the reader has
settled. Disconnect also marks its internal discovery exception retrieved while
preserving the exception for any real waiter, avoiding an abandoned-future
warning during cancelled startup.

The regression uses a deliberately cancellation-resistant legacy SSE reader:
the outer connect task remains pending until the reader releases, then returns
`CancelledError` only after the reader is gone and the owned HTTP client is
closed. Existing stdio-connect and disconnect-cancellation regressions remain
green. The combined MCP config/transport/runtime plus sandbox/CLI gate passes
**307 tests with 1 platform skip**. Ruff, targeted mypy, and
`git diff --check` are green.

### Re-audit continuation — background-process close cannot be cancelled before teardown

`BackgroundProcessTool.aclose()` already made process-tree termination and
reader settlement cancellation-safe, but it first gave live jobs a bounded
natural-exit grace with a plain `await asyncio.gather(...)`. Cancellation during
that initial grace returned immediately, before Ash entered the protected tree
cleanup. A real managed-process reproduction cancelled close during the grace
window and observed the child still live after `aclose()` had returned
`CancelledError`.

The natural-exit phase is now itself settled through the same retained cleanup
helper. Cancellation during the grace window is recorded, teardown continues
through process-tree termination and reader settlement, and only then is
`CancelledError` re-propagated. Natural exits still retain their previous grace
period and are not needlessly re-terminated. The regression starts a real
long-running background process, cancels `aclose()` during the grace window,
and proves the process and both reader tasks are terminal before cancellation
returns. The complete background-process unit file passes **22 tests**.

### Re-audit continuation — plugin failure cleanup survives late caller cancellation

Executable-plugin calls already handled direct caller cancellation with a
retained cleanup task, but ordinary protocol/runtime failures still performed
`_discard_process()` through a bare await. A second event could therefore race
the failure path: after a malformed response, timeout, or other
`PluginRuntimeError`, caller cancellation during teardown could cancel the
teardown itself and let the host process or staged Docker volume outlive the
failed call.

Plugin error cleanup now runs as an owned task and is settled with the same
cancellation-safe helper used by the explicit-cancellation path. The original
runtime error remains primary when cleanup completes normally; if caller
cancellation arrives while cleanup is in progress, Ash waits for teardown to
settle and then propagates `CancelledError`. Cleanup failures remain attached as
diagnostics in either case. A regression forces a protocol failure, blocks
cleanup, cancels the outer call, and proves the call cannot finish until cleanup
is released and completed. The full plugin-runtime unit file passes **39
tests**. The combined background-process + plugin-runtime gate passes **61
tests**; Ruff, targeted mypy, and `git diff --check` are green.

### Re-audit continuation — direct background subagents reserve agent identity before launch

Direct background `spawn_agent` admission previously checked duplicate
`agent_id` values only through durable `SharedState` status. The newly created
background task did not register that status until it actually received event-
loop time, leaving a same-turn gap after the first call returned. Two immediate
launches with the same explicit ID could both pass admission; the second then
overwrote `_tasks[agent_id]` while both durable tasks and both provider workers
remained live. A deterministic blocking-provider reproduction observed
**2 live workers, 2 nonterminal durable tasks, but only 1 tracked in-memory
task owner**.

`SpawnAgentTool` now reserves an agent ID synchronously before entering the
awaitable execution body. The reservation is shared across direct in-process
and subprocess launches, transfers to the existing background task/monitor
owner on successful background publication, and is released by the matching
terminal callback. Foreground/error paths release it in the admission wrapper,
and tool shutdown clears the set after settling owned tasks. The existing
durable status check remains as the cross-process/persisted fence; the new set
closes only the local scheduling gap.

A regression performs two same-tick background launches with one explicit ID
before the first worker can register itself. The first succeeds, the second is
rejected as already running, exactly one durable task is created, and the
reservation disappears when that worker reaches a terminal state. The focused
background/subprocess slice passes **29 tests**, the complete SpawnAgent tool
file passes **51 tests**, and targeted mypy plus `git diff --check` are green.

### Re-audit continuation — LSP incremental-diagnostic IDs obey the file-cache bound

Managed LSP diagnostics already retained at most **256 files** of diagnostic
payloads. The adjacent incremental-state map was not independently bounded,
however. `diagnostics_for()` stored every returned `resultId` before deciding
whether a report was `full` or `unchanged`; only `_store_diagnostics()` applied
the 256-file eviction. A server returning `unchanged` reports for fresh URIs
could therefore grow `_diagnostic_result_ids` without ever inserting a
diagnostic payload. With the configured ceiling reduced to three, a direct
reproduction sent five such replies and observed **0 diagnostic payloads but 5
retained result IDs**.

Result IDs now have their own 256-file retention boundary. Re-storing a key
refreshes its insertion position and a new key at capacity evicts the oldest
incremental token, so payload and incremental caches are each finite even when
a nonstandard client bypasses normal protocol semantics. `LSPClient` also
rejects an `unchanged` document-diagnostic report when the request did not send
a `previousResultId`; Ash no longer treats an incremental no-op as a valid
first diagnostic result.

Regressions prove a stream of `unchanged` reports cannot exceed the result-ID
cache ceiling and prove the protocol client rejects `unchanged` without prior
incremental state. Existing incremental and push-only diagnostic behavior
remains green. The complete managed-LSP unit file passes **56 tests**; Ruff,
targeted mypy, and `git diff --check` are green.

### Re-audit continuation — deferred MCP client cleanup debt blocks same-server fan-out

Targeted MCP replacement keeps the old client alive while a turn may still own
the pre-reload tool snapshot. That deferred retirement was intentional, but the
runtime did not charge it against later replacement admission. Reconnecting the
same server repeatedly before turn-end cleanup therefore appended another old
client each time. A direct runtime reproduction performed five deferred
replacements of one server and observed **5 retired clients for 1 active
server**.

Replacement now treats an unresolved retired client for the same server as
cleanup debt. While cleanup is intentionally deferred, another replacement of
that server fails before allocating a candidate. Outside a deferred window,
`replace_server()` first retries existing retired-client cleanup; persistent
cleanup failure therefore blocks new candidate fan-out instead of accumulating
more owners. With the existing maximum of 32 configured MCP servers, deferred
retirement is consequently bounded to at most one unresolved old client per
server.

The adjacent candidate-cleanup review also reconciled runtime rollback with the
client's newer self-rollback behavior. `MCPClient` now exposes whether any
owned connection state still requires disconnect; a failed connect that already
closed its process/HTTP/task state is not disconnected redundantly by runtime
candidate cleanup, while partially cleaned candidates are still retried.

A regression proves the first deferred replacement succeeds, the second
same-server replacement is rejected before constructing another client, and the
active/retired owners remain unchanged. Existing targeted-refresh overlap and
failed-reconnect cleanup behavior remain intact. The complete MCP reconnect +
transport + runtime/tool gate passes **191 tests**; Ruff, targeted mypy, and
`git diff --check` are green.

### Re-audit continuation — compacted sessions shed durable prefixes from live memory

Context compaction previously reduced only the provider request. The active
`Session.messages` list still retained every user, assistant, tool, steering,
and ephemeral context message for the lifetime of the runtime, so a long-lived
session could keep growing resident memory even though SQLite already held the
full durable transcript and the model saw only compacted history.

Live sessions now track a private, non-persisted resident-history offset. After
`HistoryCompactor` removes an oldest protocol-safe prefix, Ash first redacts and
durably saves the replacement summary; only after that write succeeds does the
live session discard exactly the summarized prefix. Durable messages are never
deleted. If summary persistence fails, both the prior in-memory summary and the
complete resident message list remain intact.

Windowed history also makes the persisted summary part of the active provider
context on subsequent turns. `HistoryCompactor` therefore has an explicit
`include_previous_summary` mode used only when the live session is known to be
windowed. Freshly loaded sessions still contain complete durable history and do
not duplicate the previous summary. This avoids the subtle failure mode where
the now-small resident window would fall below the compaction threshold and the
model would otherwise lose all summarized older context.

A repeated-compaction reproduction persisted **40 messages** while the live
session retained **4**, with a resident offset of **36** and exactly one prior
summary still provider-visible. Regressions also prove durable history remains
complete, redacted summary persistence precedes shedding, and a failed summary
write leaves live history untouched. The complete history + session + core-loop
gate passes **193 tests**; Ruff, targeted mypy, and `git diff --check` are green.

The adjacent attempt to omit historical `Session.tool_calls` from runtime resume
was deliberately reverted after the full core gate showed that the resumed
`Session` contract exposes recovered tool-call records. Removing that memory
cost safely requires a true lazy/session-view design rather than returning an
observably incomplete `Session`.

### Re-audit continuation — session permission rules have finite fail-closed admission

Persistent and managed permission sources are bounded by their on-disk policy
limits, but interactive session rules had no cardinality ceiling. Each scoped
"allow for session" decision can produce a distinct rule ID, so a sufficiently
long interactive session could grow permission memory without bound and make
every later authorization decision scan an ever-larger rule list.

`PermissionPolicy` now accepts at most **1024 distinct session rules**. Exact
duplicates remain idempotent and do not consume additional capacity. Oversized
preconstructed session policy state is rejected at initialization, and adding a
new distinct rule at capacity raises an explicit overflow instead of evicting an
older allow/ask/deny decision.

The interactive approval controller handles that overflow fail-closed. A user
request for tool/session or scope/session approval is denied when the requested
rule cannot be recorded; Ash does not silently downgrade it to approve-once,
does not mark the terminal UI's tool/session cache, and does not discard an
older authorization rule. The user can still choose a one-time approval on a
subsequent prompt.

Focused policy + interactive-controller tests pass **37 tests**. The broader
permission, CLI, approval-channel, and foreground-subagent slice passes **63
tests**; targeted mypy and `git diff --check` are green.

### Re-audit continuation — provider and RPC correlation fields have bounded wire identities

Native provider tool calls previously required only non-empty call IDs and tool
names, while their JSON argument objects had no serialized-size ceiling. A
misbehaving provider could therefore return a multi-megabyte correlation ID,
name, or argument object that Ash would then carry into assistant metadata,
SQLite tool-call state, runtime/audit events, checkpoint correlation, and tool
dispatch. The existing per-completion tool-count bound limited cardinality but
not per-call amplification.

Canonical provider tool calls now enforce three provider-neutral resource
boundaries before a completion can be finalized: tool-call IDs are at most
**512 UTF-8 bytes**, tool names must match the shared provider-portable
`[A-Za-z0-9_-]{1,64}` envelope, and serialized tool-call arguments are at most
**2 MiB**. Raw JSON argument strings are checked before parsing as well as after
decoded-object normalization, so an oversized wire argument cannot first become
a large durable/dispatch object. Tool-result `tool_call_id` values use the same
512-byte correlation-ID bound. Native tool schemas validate the same name
contract before provider network I/O.

Turn-level regressions prove oversized native call IDs and arguments fail before
assistant persistence and before tool dispatch; only the already-durable user
message remains. The canonical message suite passes **39 tests** and the full
provider-message + core-loop gate passes **166 tests**.

The same correlation review found that MCP server-initiated requests already
had a 64-request ownership budget, but their JSON-RPC IDs could still be almost
as large as the 8 MiB transport frame. MCP now rejects string message IDs above
**512 UTF-8 bytes** and integer IDs outside the interoperable JSON safe-integer
range before `_incoming_requests` ownership or response echoing. The sibling
Ash JSON-RPC server now applies the same 512-byte string and JSON-safe numeric
envelope to request and cancellation IDs while retaining its existing finite
float compatibility.

Production MCP server-request payloads were reviewed separately rather than
given another arbitrary generic cap: sampling already rejects requests above
**512 KiB**, elicitation independently bounds its message/schema/response
surfaces, roots/ping are small, and Ash runtime does not install the generic
custom `server_request_handler` hook used by tests/embedders. The existing 8 MiB
transport frame therefore remains the outer protocol ceiling.

The adjacent MCP naming gap is now closed without changing the remote protocol
identity. Current OpenAI and Anthropic tool contracts share a 1-64 character
letters/digits/underscore/dash envelope (DeepSeek currently permits 128, so 64
is the conservative common denominator). Simple MCP identities therefore keep
their existing `mcp__<server>__<remote>` local name when it already satisfies
that envelope. Non-portable or longer identities receive a deterministic
readable-prefix + SHA-256-derived alias capped at 64 characters. `MCPTool` keeps
the exact `remote_name` for `tools/call`, durable server-task metadata, and
contract fingerprints, and runtime collision checks still fail closed.

Provider-history replay rewrites an old raw MCP name only when it maps uniquely
to one current MCP tool, without mutating the stored transcript. Durable MCP
task recovery likewise accepts either the current alias or the exact legacy raw
name, so an upgrade does not turn an already-dispatched remote task into an
unrecoverable local-name mismatch. The comprehensive no-replay task-recovery
regression now exercises a dotted server plus slash-containing remote name and
successfully resumes through `tasks/get` using the new alias while the persisted
tool-call row retains its legacy raw identity.

The initial combined provider/core/MCP-transport/JSON-RPC correlation gate
passes **265 tests**. After portable MCP naming, the broader provider adapter +
core loop + MCP config/runtime/reconnect/transport + permission gate passes
**477 tests**; Ruff, targeted mypy, and `git diff --check` are green.

### Re-audit continuation — deferred tool schemas stay bounded and one model iteration owns one schema snapshot

Deferred tool discovery had a long-session retention gap. `search_tools`
activated every match for the remainder of the session, so repeated searches
could eventually make the provider-visible set approach the complete runtime
catalog again. That defeated the feature's reason for existing and allowed
provider schema memory/context to grow with every distinct search even though
the underlying runtime catalog was already stable.

Deferred activation now uses a **64-schema LRU window**. Search results still
return every requested match (up to the existing per-search limit), and
re-searching a tool refreshes its recency. When the window is full, only the
oldest provider-visible activation falls out; the runtime tool remains present
and can be found/activated again through `search_tools`. Session changes still
clear all activations. The explicit `tool_search_threshold = 0` opt-out keeps
its documented full-catalog behavior.

The same review found a provider-input integrity bug. One model iteration
selected a tool snapshot, but then regenerated `json_schema()` independently
for token budgeting/provenance and again for native provider dispatch. Dynamic
extension schemas could therefore make the reported "exact" tool context differ
from what was actually sent, and arbitrary extension code was invoked multiple
times for one request. Each iteration now snapshots the visible tools and their
deep-copied schemas once. That exact immutable payload is reused for context
accounting and native provider dispatch; execution still uses the separately
frozen tool-object map from the same iteration.

For non-native/XML fallback providers, the audit found an even more direct
gap: Ash charged tool-schema tokens but never made the catalog provider-visible.
The generic XML syntax explained how to call a tool without telling the model
which tools or arguments existed. Fallback requests now embed the exact visible
name/description/JSON-schema snapshot in the system message under an explicit
**untrusted metadata** boundary. The catalog is counted once as provider-visible
input rather than double-counted as both external native schema and message
text. Native providers continue to receive the same snapshot through their
structured tool API instead of prompt injection.

Regressions prove LRU activation/refresh/eviction, prove a real XML fallback
turn receives the exact visible tool catalog while `tools=None` stays on the
provider API call, and prove a dynamic tool's `json_schema()` is evaluated
exactly once in a model iteration while that same version reaches the native
provider. The existing oversized-tool-schema regression still fails before
provider dispatch. The complete core-loop + tool-search + provider-message +
provider-adapter gate passes **220 tests**; Ruff, targeted mypy, and
`git diff --check` are green.

### Re-audit continuation — provider streams have finite retained output and request lifetime

The provider loop trusted `max_completion_tokens` as a request hint but did not
own a hard retained-output boundary. A buggy/custom endpoint could ignore that
hint and make Ash accumulate unbounded text fragments, XML deltas, reasoning
blocks, or native tool-call payloads before a terminal chunk. Per-call tool
limits did not solve the aggregate case: up to 64 individually valid calls
could still carry a very large combined argument payload.

The core loop now enforces a provider-neutral **16 MiB retained completion
budget** across streamed text, XML tool-call deltas, native tool calls, and
reasoning data before appending them to long-lived completion buffers. The
value matches the existing decoded-stream ceiling already used by Ash's
OpenAI-compatible and Ollama adapter families rather than introducing a second
smaller transport policy. Reasoning cardinality is independently capped at
**4096 blocks**, and one completion may emit at most **100000 chunks**, so many
tiny objects or empty chunks cannot bypass a byte-only resource bound.

The same review found that custom/local providers could wait forever before a
first chunk or stall indefinitely mid-stream. Current comparable harnesses
expose finite provider request deadlines; Ash now owns the same invariant
directly rather than depending on each SDK's defaults. New
`provider_request_timeout_seconds` is project-configurable from **1 second to
24 hours** and defaults conservatively to **1800 seconds**. Each attempt gets a
monotonic total deadline; every next stream item receives only the remaining
budget. Timeout cancellation unwinds a stalled async generator. A timeout is
retryable through the existing provider retry/circuit path only before any
output was emitted; after partial output it fails without replay.

Regressions prove oversized retained payloads leave only the already-persisted
user message, reasoning floods and empty-chunk floods fail deterministically,
a never-yielding provider is cancelled and runs its generator cleanup, a
pre-output timeout can retry successfully, and a partial-output timeout is not
retried. The full config + core-loop + provider suites passed **299 tests**
before the final partial-output regression; that regression adds a focused
**3/3** timeout slice. Ruff, targeted mypy, and `git diff --check` are green.

### Re-audit continuation — deferred tool search avoids full-schema amplification

The deferred provider catalog was bounded, but `search_tools` still performed
unbounded work over the hidden catalog itself. Every search called
`json_schema()` for every runtime tool and serialized the entire result to text
for ranking. For MCP tools that meant deep-copying a validated input schema on
every query. With the existing per-server/catalog limits, thousands of hidden
tools and 256 KiB-valid MCP schemas could turn one small search query into very
large transient copies and JSON serialization work. The final search result
also embedded each matched schema in full, so up to 20 large matches could be
written into one tool result even though only activation, not schema replay in
the result, is needed for the next provider iteration.

Tool discovery now has a dedicated read-only search-schema path. MCP discovery
walks its already validated internal schema without deep-copying it; executable
plugin discovery likewise reuses its validated manifest schema; ordinary tools
retain the default exact-schema behavior. Ranking extracts at most **8192
characters across 512 schema nodes per tool** instead of serializing an entire
schema tree. Tool descriptions are bounded to **2048 characters** for search
and output.

Matched schemas remain exact in the `search_tools` result when their compact
JSON representation is at most **8 KiB**. Larger matches still activate their
exact provider schema for the next iteration but return only a bounded summary
of top-level type plus at most **32** property/required names, each clipped to
128 characters. This keeps discovery useful without duplicating a large remote
schema into the conversation transcript. Provider dispatch remains unchanged:
the exact `json_schema()` path still owns the provider-facing contract.

An adversarial regression installs a hidden tool with a >20 KiB schema whose
provider-facing `json_schema()` deliberately raises if search calls it. The
tool remains discoverable through bounded schema metadata, activates normally,
and returns a summarized search result below 16 KiB. The complete
`search_tools` file passes **7 tests**. The broader tool-search + MCP runtime +
plugin runtime + core-loop gate passes **277 tests**; Ruff, targeted mypy, and
`git diff --check` are green.

### Re-audit continuation — provider tool-schema snapshots have a hard byte ceiling

Context budgeting already rejected tool catalogs that consumed the model's
usable token window, but that check happened only after Ash had deep-copied the
visible schemas, encoded the complete catalog to JSON, and passed the aggregate
text through the provider token counter. A pathological custom catalog—or an
explicit `tool_search_threshold = 0` configuration exposing many otherwise
valid remote schemas—could therefore force very large transient allocations and
tokenization work before the eventual context-budget failure.

One provider iteration now admits at most **16 MiB** of serialized tool-schema
catalog data. The ceiling is enforced incrementally while the immutable
iteration snapshot is built, before aggregate catalog encoding/tokenization and
before provider dispatch. It applies to both native structured tools and the
XML/fallback catalog representation. The value matches Ash's existing
provider-stream and HTTP-scale hard resource envelopes; ordinary model-context
budgeting remains the tighter semantic limit for normal requests.

The guard preserves the existing exact-snapshot invariant: each dynamic tool's
`json_schema()` is still evaluated exactly once for an iteration, and the same
captured schema is what later budgeting/provider dispatch would have used. A
regression lowers the ceiling to 512 bytes, supplies a dynamic >1 KiB schema,
and proves both native and fallback paths reject after one schema evaluation but
before `count_tokens()` or `stream_chat()` run. Only the already-durable user
message remains. The focused schema-snapshot/oversize slice passes **4 tests**;
targeted mypy and `git diff --check` are green.

### Re-audit continuation — direct turns, steering, and provider content have shared byte bounds

The public transports already rejected very large prompts (JSON-RPC/A2A/ACP
and piped CLI input use approximately 1 MiB boundaries), but direct
`AshLoop.run_turn()`/SDK calls had no equivalent core guard. A caller could hand
the runtime an arbitrarily large string; Ash could create/recover a session and
eventually persist that input before context budgeting rejected it. Steering
had a count limit of 20 messages but no text-size budget, so direct SDK steering
could retain and later persist arbitrarily large queued guidance as well.

Core turn input is now limited to **1,000,000 UTF-8 bytes** and validated before
session creation, recovery, provider negotiation, or turn mutation. Pending
steering has the same per-message ceiling plus a **1,000,000-byte aggregate
queue budget**, so the existing 20-message cardinality limit cannot multiply
unbounded text retention. Invalid UTF-8 text fails locally rather than reaching
persistence/provider encoding.

Direct SDK metadata was hardened at the same boundary. Non-attachment metadata
must be JSON-serializable and its compact representation is limited to
**1,000,000 UTF-8 bytes** before a session is created. Reserved `content_blocks`
and `image_blocks` are canonical-validated before turn start instead of being
trusted until provider dispatch. Canonical message content now permits at most
**64 blocks** and **16 MiB aggregate UTF-8 content** per message; the existing
single-image base64/media-type validation still applies inside that aggregate
budget. These canonical bounds protect every provider path, not only SDK
metadata.

Regressions prove oversized direct turns create no session, steering overflow
does not mutate the pending queue, oversized persisted metadata creates no
session, malformed attachment base64 fails before session creation, and valid
image/ordered-content flows remain intact. The focused boundary slice passes
**6 tests** and the complete provider-message + core-loop + SDK gate passes
**211 tests**; Ruff, targeted mypy, and `git diff --check` are green.

### Re-audit continuation — one provider chunk cannot bypass aggregate completion bounds

The core loop already bounded one completion to 64 native tool calls, 4096
reasoning blocks, 100000 chunks, and 16 MiB of retained output. Those checks ran
after a provider had yielded a fully constructed `StreamChunk`, however. A
custom or faulty adapter could therefore materialize one enormous
`native_tool_calls`/reasoning list or one oversized text/tool-delta string
before the aggregate loop guard had a chance to reject it.

`StreamChunk` now enforces the same structural ceiling at the provider model
boundary: at most **64 native tool calls per chunk**, at most **4096 reasoning
blocks per chunk**, and at most **16 MiB UTF-8** in either `content` or
`tool_call_delta`. Native-call and reasoning lists also carry a **16 MiB
serialized structured-payload ceiling before nested model parsing**, so 64
individually valid near-2-MiB native calls cannot make Pydantic construct a
>100-MiB chunk before the loop rejects it. `CompletionOutcome.reasoning_blocks`
carries the same 4096 cardinality ceiling. The loop's existing aggregate limits
remain authoritative across multiple valid chunks.

Chunk validation errors are normalized without stringifying rejected payloads.
Only a root `native_tool_calls` list overflow becomes the existing “more than
64 tool calls” error; nested invalid call IDs/arguments preserve their precise
bounded-field diagnostic as `ProviderCompletionError`, and oversized chunk text
uses a stable Ash-level text-size error. This avoids leaking enormous Pydantic
input representations while preserving caller-facing provider failure
semantics.

Focused chunk/cardinality tests pass **5 tests**. The complete provider +
provider-message + core-loop gate passes **231 tests**; Ruff, targeted mypy,
and `git diff --check` are green.

### Re-audit continuation — structured ToolResult data survives execution with bounded post-processing

The main tool-execution path had regressed against Ash's structured-result
contract. First-party tools already return meaningful `ToolResult` metadata:
command tools emit diagnostics and summaries, web tools emit durable citations,
and browser screenshots emit image metadata plus an opaque image block. But
`_execute_tool_once()` reduced every result to only success/output/error,
truncation/token count, and outcome; `_execute_tool_calls()` then reconstructed a
new `ToolResult` from that reduced dictionary. Diagnostics, summaries,
citations, images, and image blocks were therefore silently lost before
post-hooks, secret-redaction middleware, persistence, and mediated SDK results.

`_execute_tool_once()` now preserves the complete typed result and the main loop
reconstructs every structured field before post-processing. Diagnostics,
diagnostic summaries, citations, and image metadata once again survive in the
runtime result; diagnostics/citations remain part of the persisted/model-visible
tool response. Opaque `image_blocks` are preserved for the in-memory mediated
tool result and middleware path but deliberately remain excluded from durable
tool-response XML, so base64 screenshot payloads are not copied into session
history without an explicit multimodal tool-result protocol.

Restoring those fields also exposed the need for a shared extension boundary.
`ToolResult` now permits at most **256** diagnostics/citations/image metadata
items, **64** diagnostic-summary fields, **2 MiB** of serialized structured
metadata, **16** image blocks with at most **16 MiB aggregate opaque image data**,
and **16 MiB aggregate output+error text**. These limits are above current
first-party workloads (web citations max at 20, command diagnostics at 50, and
browser screenshots cap raw PNGs at 5 MiB) while bounding custom/extension
results.

Post-tool hooks/middleware can mutate results, so Ash now retains a deep copy of
the known-valid tool result, runs post-processing, then revalidates the complete
`ToolResult`. If middleware produces invalid/oversized state, Ash discards that
mutation, keeps the original known-safe tool result, and marks only
post-processing as failed with a redacted/bounded diagnostic. This prevents a
middleware from bypassing the base result limits after validation.

Regressions prove structured fields survive real execution and secret
redaction, image data remains exact in memory but absent from durable XML, base
structured/text/image limits reject overflow, and a middleware injecting 257
citations cannot persist that mutation. The first-party producer slice across
command/web/browser/secret-middleware/plugin paths passes **255 tests**; the
complete core-loop file passes **149 tests**; Ruff, targeted mypy, and
`git diff --check` are green.

### Re-audit continuation — tool-result projections cannot exceed provider/event envelopes

After restoring structured `ToolResult` data, one projection mismatch remained.
A base result could satisfy its 16 MiB text + 2 MiB structured limits yet expand
past the **16 MiB canonical provider-message ceiling** once JSON escaping,
untrusted-content metadata, and `<tool_response>` framing were added. Ash would
then durably persist that tool message and only fail on the next iteration when
canonical provider history rejected it.

`_render_tool_response()` now measures the exact encoded response before
persistence. Normal responses remain byte-for-byte on the existing rich path.
If the full rendered response would exceed the canonical-message limit, Ash
produces a bounded provider view: structured diagnostics/citations are omitted,
`truncated` and explicit tool-response/structured-metadata truncation markers are
set, output receives a bounded preview (up to 2 MiB and scaled down for smaller
configured limits), and error text receives a bounded preview (up to 256 KiB).
The authoritative `ToolResult` / tool-call record is not replaced by that
provider projection. A regression with heavily JSON-escaped control characters
proves the rendered XML stays within the configured canonical limit while
ordinary small responses remain unchanged.

The lifecycle event path had the same mismatch at a smaller envelope. Runtime
event backlog is bounded to 4 MiB, but `tool.completed`, `turn.completed`,
assistant/reasoning deltas, and error events could each carry up to the full
tool/provider output before queue accounting. Event text fields (`text`,
`response`, `output`, `error`, `reason`, `delta`) are now capped at **1 MiB**
before both UI delivery and durable event buffering, with
`event_text_truncated=true` when any field is clipped. Full function return
values and tool records remain unchanged; ACP/SDK/UI receive the bounded event
projection.

Runtime-event backlog/accounting regressions pass **4 tests**, terminal UI + ACP
+ SDK event consumers pass **101 tests**, and the complete core-loop file now
passes **152 tests**. Ruff, targeted mypy, and `git diff --check` are green.

### Re-audit continuation — direct tool calls share the provider persistence envelope

Provider-native tool calls already had bounded identifiers and a 2 MiB JSON
argument envelope, but direct SDK/mediated calls entered `_execute_tool_calls()`
after provider normalization and could bypass those checks. A caller could
therefore supply an arbitrarily large call ID/name/argument object, causing Ash
to persist the intent and approval/audit state before any shared resource bound
ran. Callback-provided denial feedback and arbitrary exceptions could likewise
write unbounded text into durable tool-call errors.

Every new direct tool call is now validated before the first record/event/audit
write: call IDs must be non-empty strings of at most **512 UTF-8 bytes**, tool
names must satisfy the shared provider-portable `[A-Za-z0-9_-]{1,64}` contract,
and arguments must be finite/JSON-serializable objects whose compact encoding is
at most **2 MiB**. This is the same argument envelope used for native provider
calls, so SDK and model-originated execution no longer have different durable
resource contracts.

Non-`ToolResult` durable error text—approval feedback, unexpected extension/tool
exceptions, and the conservative post-dispatch ambiguity wrapper—is redacted and
bounded to **1 MiB** before it reaches the tool record/audit/event path. The
`UNKNOWN` tool-outcome transformation was also switched from unvalidated
`model_copy(update=...)` to a byte-fitted, revalidated `ToolResult`, closing the
last way Ash itself could prefix a valid near-limit result into an invalid one.

Parameterized regressions prove oversized direct call IDs, non-portable names,
and oversized arguments fail before any tool record or execution. A tool that
throws a 10,000-character exception persists only the configured bounded error.
The complete core-loop file passes **156 tests**; Ruff, targeted mypy, and
`git diff --check` are green.

### Re-audit continuation — tool-response framing and structured metadata are syntax-safe

The model-visible `<tool_response>` wrapper previously interpolated `tool_name`
and `call_id` directly into XML attributes. New tool names are provider-portable,
but provider-generated call IDs are opaque strings whose contract permits XML
metacharacters, and durable legacy MCP names can contain non-portable characters.
A malicious/malformed ID containing quotes, `<`, `&`, or XML-forbidden controls
could therefore break the response framing even though its size was valid.

Rendered XML attributes are now normalized to XML 1.0-safe characters and
quoted with XML attribute escaping. This affects only the model/human-facing
wrapper: the exact call ID and MCP identity remain unchanged in storage and
native provider correlation metadata. A regression injects quotes, markup,
ampersands, and a forbidden control character, parses the resulting block as
XML, and proves no attacker-controlled child tag is created.

The JSON payload inside that wrapper is XML-escaped as text as well. Previously,
an untrusted tool output such as `</tool_response><call_tool ...>` remained
literal markup even though it appeared inside a JSON string—XML framing does
not understand JSON quoting, so the closing tag could terminate the wrapper.
Ash now escapes the serialized JSON body before insertion. XML parsing yields
one `tool_response` element with no injected children, and decoding its text
recovers the exact original JSON/tool output. The response-size guard measures
the fully escaped representation, so entity expansion cannot bypass the
canonical-message ceiling.

The restored `ToolResult` structured-data path was tightened at the same
boundary. Diagnostics/citations/image metadata must now be genuinely strict
JSON-serializable (`NaN`/Infinity and arbitrary Python objects are rejected)
rather than being accepted for sizing via `default=str` and failing only later
after a tool had already executed. The renderer also uses strict JSON. Provider
canonical validation now converts malformed UTF-8 raw tool-argument strings to
a normal validation error; unpaired-surrogate string fields are already rejected
by Pydantic's Unicode layer before Ash validators run.

The combined core-loop + base-tool + canonical-provider-message gate passes
**261 tests** before the final body-framing regression; the complete core-loop
file now passes **159 tests**. Ruff, targeted mypy, and `git diff --check` are
green.

### Re-audit continuation — oversized tool audits project evidence instead of duplicating payloads

Tool-call audit rows previously copied the complete redacted tool arguments and,
on terminal execution, the complete redacted tool output into the tamper-evident
audit chain. Those values are already retained authoritatively in the durable
tool-call record, so a valid 2 MiB argument object plus a 16 MiB tool result
could be duplicated again in `audit_logs`, increasing SQLite growth and hash/
load costs without adding independent evidence.

Small tool-audit entries remain unchanged. Once the redacted canonical details
exceed **1 MiB**, Ash now stores a bounded evidence projection instead: overall
canonical byte count + SHA-256, exact small scalar status fields, and at most
**64 KiB** previews plus independent byte counts/SHA-256 digests for large
`arguments`, `output`, and `error` fields. The authoritative tool-call record is
not truncated or replaced, so recovery/debugging still has the full bounded
execution data while the append-only audit chain remains compact.

A regression lowers the audit ceiling to 256 bytes and proves the durable tool
record still retains a 500-character argument/result exactly, while the audit
row contains only bounded previews and 64-character hashes. Existing small audit
and redaction behavior remains unchanged. The focused slice passes **2 tests**;
Ruff, targeted mypy, and `git diff --check` are green.

### Re-audit continuation — runtime events have a total structured-payload envelope

The earlier event hardening capped known text fields, but extension tools can
emit arbitrary additional keys through `BaseTool.emit_event()`. A custom event
could therefore carry a giant nested list/object, a non-JSON Python value, or a
circular structure under another key and bypass the text-specific caps before
UI/SDK delivery and runtime-event persistence.

Runtime events now have a **3 MiB total strict-JSON envelope** in addition to the
existing 1 MiB per-text-field cap and 4 MiB persistence backlog. Size is measured
incrementally with `JSONEncoder.iterencode()` and stops once the ceiling is
crossed, avoiding construction of a second full serialized copy merely to
measure an already-large event. NaN/Infinity, circular references, invalid
Unicode, and non-JSON objects are treated as invalid event bodies.

Oversized or invalid events are replaced before envelope/persistence/UI delivery
with a bounded lifecycle preview that preserves the event type, call/tool/model
identity where safely representable, basic status/counters, and at most **64 KiB
per useful text preview field**. The preview is marked
`event_payload_truncated=true`; invalid/non-JSON bodies additionally carry
`event_payload_invalid=true`. The unsafe original nested body is not retained.

Regressions prove oversized structured events collapse to a useful bounded
preview, circular events remain trackable without retaining the cycle, text
projection/backlog limits still hold, and UI + ACP + SDK consumers remain
compatible. The focused event/backlog slice passes **5 tests**, the event
consumer slice passes **101 tests**, and the complete core-loop file passes
**158 tests**; `git diff --check` is green.

### Re-audit continuation — permission rules bound matcher fan-out and aggregate scope state

Session permission-rule cardinality was already capped at 1024, and individual
exact matcher values were capped at 8 KiB, but one rule could still contain an
unbounded number of otherwise-valid matchers. `build_exact_scope_matchers()`
creates one matcher per non-bulk argument, so a large direct/SDK argument object
with many small keys could collapse into one enormous session rule. Every later
policy evaluation would then scan that matcher fan-out despite the outer rule
count remaining finite.

`PermissionRule.create()` now admits at most **64 matchers** and at most **64
KiB** for the complete canonical serialized rule. Existing operator-specific
limits remain intact: exact values are still at most 8 KiB, set/prefix/command/
path/domain operators keep their own narrower shapes, and persistent rule files
retain their existing 1 MiB boundary. Exact matcher JSON and complete rule JSON
also reject NaN/Infinity and malformed Unicode rather than leaking raw encoding
errors during rule-ID hashing.

The rule model is the authoritative constructor for persistent, managed,
interactive-session, and subagent-propagated rules, so the bound applies before
those surfaces can retain or serialize an oversized scope. Scoped approval
therefore fails closed when the requested rule cannot be represented; Ash does
not evict an older authorization decision to make room.

Regressions prove 65 tiny matchers are rejected, a smaller set whose aggregate
payload exceeds 64 KiB is rejected, NaN exact values fail strict JSON, and
malformed Unicode in a text matcher becomes `PermissionGrantError`. The complete
permission-grants file passes **24 tests** and the broader policy/interactive/
CLI/approval-channel/subagent slice passes **65 tests**; Ruff, targeted mypy,
and `git diff --check` are green.

### Re-audit continuation — session metadata and JSONL import have finite envelopes

Human session titles were whitespace-normalized but previously had no maximum
length, and imported session model metadata bypassed the canonical runtime
model-ID byte envelope. Import/fork suffixes (`" (imported)"`, `" (fork)"`) were
also appended after title validation, so a valid source title could still create
an oversized stored derived title.

Session titles now have a shared **256-character** write boundary. Explicit
rename rejects larger titles atomically; JSONL import applies the same source
title bound; derived import/fork titles fit their base text before appending the
suffix, keeping the stored title inside the same envelope without making legacy
sessions unforkable. Session creation now also enforces the existing provider
model identity ceiling of **512 UTF-8 bytes** while preserving the exact stored
model string rather than re-canonicalizing imported metadata.

The JSONL importer itself previously materialized `content.splitlines()` and all
decoded records before validation, with no total byte or message-count limit.
Imports are now measured incrementally and rejected above **64 MiB UTF-8**, then
parsed line-by-line through `StringIO`. At most **10,000 messages** are admitted,
matching Ash's canonical provider-message cardinality ceiling. Validation remains
atomic: oversized title/model, total bytes, or message count create no session.

Focused rename/fork/model/import tests pass **6 tests**; byte/message-count import
plus existing atomic/round-trip coverage passes **4 tests**. Ruff, targeted
mypy, and `git diff --check` are green.

### Re-audit continuation — audit verification and export stream long hash chains

`verify_audit_log()` and `export_audit_log()` previously materialized the entire
append-only audit chain, and export then built a second monolithic JSON payload
before writing it. Long-running sessions could therefore turn an explicit audit
verification/export into memory proportional to the complete historical chain.

`SessionStore.iter_audit_logs()` now yields records in bounded batches (default
**256**, accepted range 1–4096) while preserving append order and stored-data
validation. Hash-chain verification consumes that iterator directly. Audit
export verifies first, then writes its anchored temporary file incrementally:
header, one record at a time, and closing JSON framing. The public
`list_audit_logs()` contract remains unchanged for callers that explicitly want
a materialized list.

The focused audit/session gate passes **11 tests**. An export regression
deliberately makes `list_audit_logs()` raise and still produces a valid verified
bundle, proving the user-facing export path is genuinely streaming. Ruff,
targeted mypy, and `git diff --check` are green.

### Re-audit continuation — startup recovery projects large terminal results safely

Startup recovery reconstructs missing terminal tool-result messages from durable
`tool_calls` rows. That path previously copied the full recovered `result` into
a new provider-visible JSON message and audit row without passing through the
new `ToolResult`, tool-response renderer, or tool-audit projection boundaries.
Even a currently valid ~16 MiB tool result could therefore become larger than
Ash's 16 MiB canonical message ceiling once recovery framing was added; legacy
databases could be worse.

The authoritative recovered call and durable `tool_calls.result` remain exact.
Only the reconstructed provider/audit projections are bounded. Recovery first
renders the complete JSON result and keeps it unchanged when it fits. If it
would exceed the canonical message ceiling, output/error are replaced with
bounded previews (normally up to **2 MiB** output and **256 KiB** error, scaled
down to the active message ceiling), plus the original output byte count and
SHA-256. Large recovery audit output similarly becomes a **64 KiB** redacted
preview with byte count/SHA-256 rather than duplicating the complete result in
the audit chain.

An end-to-end regression lowers the canonical message ceiling, recovers a 5 KiB
terminal result, and proves `RecoverySummary` plus the durable tool-call record
retain the full output while the reconstructed tool message/audit carry bounded
previews and hashes. The existing small-result recovery remains byte-for-byte
unchanged. The complete checkpoint/recovery file passes **28 tests**; Ruff,
targeted mypy, and `git diff --check` are green.

### Re-audit continuation — legacy recovery repairs oversized assistant tool arguments atomically

Interrupted legacy sessions can contain an assistant tool-call intent without a
matching durable `tool_calls` row. Recovery already refuses to replay such an
intent, but the assistant message itself could still carry arguments larger than
Ash's current **2 MiB** tool-call envelope. Synthesizing only a bounded durable
row was insufficient because provider-history replay would continue reading the
original oversized assistant metadata.

Missing-result discovery now preserves the originating assistant `message_id`
and a separately bounded assistant-argument projection through
`RecoveredToolCall`. During the same recovery transaction, if those arguments
required recovery compaction, Ash rewrites only the matching assistant call's
`arguments` field; call ID/name and every unrelated call/message remain
unchanged. The synthesized durable row uses that exact already-bounded projection
instead of compacting it a second time.

The recovery evidence object is itself guaranteed to fit the active
`MAX_TOOL_CALL_ARGUMENT_BYTES` envelope. It starts with a truncation marker,
adds original byte count/SHA-256 only when they fit, then binary-searches the
largest UTF-8 preview that keeps the complete compact JSON object inside the
limit. Extremely small configured envelopes degrade to the smallest safe marker
rather than creating an invalid projection.

An end-to-end regression lowers the argument ceiling to 512 bytes, persists a
2 KiB legacy assistant argument with no durable tool row, interrupts the turn,
and proves recovery creates matching bounded durable/assistant evidence,
preserves call ID/name, removes the original oversized argument from replay
history, and marks the reconstructed result as not dispatched/not replayed.
Together with neighboring terminal-result recovery cases, the focused slice
passes **3 tests**; Ruff, targeted mypy, and `git diff --check` are green.

### Re-audit continuation — resumed sessions reconstruct a bounded live working set

Live compaction already discarded summarized messages from the in-memory
`Session`, but only for the current process. The durable database stored the
summary text without recording how many leading messages it replaced, so
`load_session()` had to materialize every message and every historical tool-call
row on resume before the live loop could compact again. Long-lived sessions
therefore regained an unbounded restart-time memory spike despite bounded live
history during the original process.

Schema v15 adds `sessions.context_summary_message_count`, the absolute durable
message count covered by the persisted summary. Compaction writes summary +
boundary atomically before dropping the live prefix; repeated compaction advances
the boundary even when summary text is unchanged. Rewind clears both fields.
Full forks copy both only when they copy the complete transcript; partial forks
start without inherited compaction state. Legacy v14 summaries migrate with a
boundary of zero, deliberately preserving old full-load behavior instead of
guessing coverage.

`load_session()` keeps its existing full-history contract for fork/rewind/export
and explicit callers. Runtime resume uses `runtime_window=True`: messages are
loaded only after the persisted summary boundary, `_resident_message_offset` is
restored to that boundary, and only the newest **256 tool-call records** are
materialized. Historical tool calls are not needed by loop execution/recovery,
which already uses durable queries, but a bounded recent tail preserves the
observable `start_session()` contract for recovery/status callers.

Compaction of purely in-memory, not-yet-durable messages remains supported: if
the newly summarized boundary exceeds SQLite's durable message count, Ash keeps
that summary/window only in memory and does not weaken the store's fail-closed
boundary validation. Once compaction covers durable history, summary + count are
persisted normally.

The v14→v15 migration, runtime-window store load, bounded recent tool-call tail,
persisted compaction boundary, persistence-failure behavior, repeated
compaction, and live resume are covered directly. Storage/rewind/export passes
**73 tests**, the complete core-loop file passes **161 tests**, and focused
runtime/migration coverage passes **3 tests**; Ruff, targeted mypy, and
`git diff --check` are green.

### Re-audit continuation — durable MCP task state has a finite recovery envelope

Durable MCP task handles were lifecycle-managed but the store accepted arbitrary
task snapshot size, answered-input history, and row count. A remote task can
remain `working`/`input_required` across recovery, so repeated distinct task IDs
or large state snapshots could otherwise turn resume into an unbounded SQLite
and memory surface even though individual MCP transport frames are capped.

Persistence now matches Ash's protocol/runtime envelopes. One session may retain
at most **64 durable MCP tasks**. Each serialized task snapshot is strict JSON
and at most **8 MiB**, matching the MCP frame ceiling. Answered-input history is
strict JSON, capped at **256 entries** and **8 MiB** aggregate. Updating an
existing `(server, taskId)` row does not consume another capacity slot. NaN,
Infinity, malformed Unicode, and other non-strict JSON fail before SQLite.

`list_mcp_tasks()` reads at most capacity+1 rows and fails closed when a
legacy/corrupt session exceeds the supported count, so resume never materializes
an arbitrarily large durable task table. Normal task terminalization still
deletes the row through the existing lifecycle path.

A focused store regression covers task bytes, answered-input bytes/count,
update-vs-new capacity, strict JSON, and legacy read overflow; **3 tests** pass.
Modern MCP task polling/input-required/persistence/recovery coverage passes
**46 tests** under the new envelope; Ruff, targeted mypy, and `git diff --check`
are green.

### Re-audit continuation — hot history lookups no longer scan complete sprint/transcript history

Two hot-path lookups still scaled with all durable history even after live
session windowing. With sprint planning enabled, every model iteration listed
every sprint ID for the session and hydrated each sprint until it found a
non-terminal one. New-session memory recall likewise used SQL `GROUP_CONCAT` to
assemble every message from each recent session, only for the loop to slice each
string to 2,000 characters afterwards.

Sprint state is already durable, so v15 now creates
`idx_sprints_session_state_created(session_id, state, created_at DESC)` and
`load_latest_active_sprint()` performs one indexed `planning|active` query with
`LIMIT 1`. The model loop uses that result directly; historical complete/aborted
sprints are never hydrated just to discover they are terminal.

Recent-session continuity retrieval now applies its existing 2,000-character
contract at the storage boundary. At most **20 sessions** may be requested; for
each session Ash reads at most the **32 newest message snippets**, each via SQL
`substr`, and builds at most **2,000 characters** of recent chronological
context. This removes full-transcript `GROUP_CONCAT` allocation while preserving
the downstream prompt shape and making “recent context” genuinely recent-tail
oriented.

The complete plans surface passes **7 tests**, session + plans persistence passes
**61 tests**, and the complete core-loop file passes **161 tests**. Focused
regressions cover newest-active sprint selection, all-terminal behavior, v15
index creation/migration, and bounded recent transcript tails. Ruff, targeted
mypy, and `git diff --check` are green.

### Re-audit continuation — rewind and fork operate on SQL boundaries, not full transcripts

Explicit rewind/fork operations still materialized the complete source session
even though both ultimately act on one message boundary. Rewind then loaded the
full `(message_id, turn_id)` sequence again and materialized every removed
message ID just to delete the tail row-by-row. Fork similarly loaded every
message/tool record, then separately queried all message turn IDs before its
already set-based `INSERT ... SELECT` copy.

Both operations now establish their boundary inside the owning SQLite
transaction using one message count plus at most the adjacent retained/removed
rows. Fork preserves assistant/tool-call-pair and Ash-turn split checks from
those two rows, then copies the requested prefix directly in SQL. Rewind derives
distinct removed turn IDs in SQL ordered by first message, uses the retained
boundary timestamp for tool-call cleanup, and deletes the transcript tail with
one set-based `DELETE ... message_id >= ?`. Usage rollback, checkpoint restore
markers, turn-journal cleanup, summary reset, and unknown-session behavior remain
unchanged.

Because `BEGIN IMMEDIATE` owns the source snapshot, the old preload-vs-current
message-count race check is no longer required. Regressions instrument
`load_session()` and prove fork/rewind invoke it only once for the final returned
session, never to preload the source history. The complete session +
turn-recovery surface passes **58 tests**; Ruff, targeted mypy, and
`git diff --check` are green.

### Re-audit continuation — session export streams from SQLite to the atomic file writer

`export_session()` previously called full-history `load_session()`, JSONL built a
second list of every record, and then joined the whole export into a third large
string. Markdown likewise built all sections before joining. The real CLI
`/export` path then passed that monolithic string to the scoped atomic writer, so
explicit export could still require memory proportional to several copies of a
long transcript.

`SessionStore.iter_session_export()` now reads one session header and iterates
message rows directly from SQLite, yielding the existing redacted JSONL or
Markdown representation incrementally. `export_session() -> str` remains as the
compatibility API and simply joins that iterator for callers that explicitly
want an in-memory string; it no longer materializes a `Session` or intermediate
record list.

The scoped I/O layer now supports `atomic_write_scoped_text_chunks()` /
`atomic_write_scoped_chunks()`. Anchored POSIX and fallback paths keep the same
validated destination, temporary-file, fsync, overwrite/no-overwrite, expected
digest, and cleanup semantics while writing each yielded chunk directly. The
CLI `/export` command uses this streaming writer, so normal file export never
assembles the full transcript in memory.

Regressions make `load_session()` raise and prove both compatibility export and
streamed-to-disk export still succeed with identical output. Chunked atomic
writes are tested on anchored and fallback paths to preserve the original target
and remove temporary files when the iterator fails mid-export. The complete
scoped-I/O + session-export surface passes **38 tests**; Ruff, targeted mypy,
and `git diff --check` are green.

### Re-audit continuation — session discovery has explicit bounded materialization

Session discovery still had three avoidable unbounded reads. `list_sessions()`
accepted any positive caller-provided limit; `resolve_session()` materialized
every duplicate title even though it only needs to distinguish zero, one, and
ambiguous matches; and `session_tree()` plus `get_session_lineage()` could load
an arbitrarily large branch graph. Tree construction also queried every child
edge separately even though each session row already carries `parent_session_id`.

Session listing now accepts at most **1,000 rows** per request. Exact ID
resolution is attempted first; title resolution then uses **`LIMIT 2`**, which
is sufficient to prove ambiguity without loading every duplicate. Full tree and
direct-child materialization use a generous **4,096-node** ceiling and fail
explicitly when exceeded rather than silently truncating a supposedly complete
tree.

`session_tree()` now builds parent/child relationships from the single bounded
tree query, removing the second full edge query. Normal ordering and lineage
semantics remain unchanged. Focused coverage verifies three-way title ambiguity,
oversized list rejection, and fail-closed direct/tree node limits. Session CLI,
SDK, HTTP, and JSON-RPC tree surfaces remain compatible: **4 storage tests + 3
session-CLI tests + 4 SDK/server tests** pass; Ruff, targeted mypy, and
`git diff --check` are green.

### Re-audit continuation — audit inspection streams complete history with bounded diagnostics

Audit export and hash verification already streamed the durable chain, but the
CLI `audit list` path still called full-history `load_session()` merely to check
existence, then materialized every audit record and a second rendered string
before printing. Verification itself streamed rows but accumulated an unbounded
Python error list if a heavily corrupted long chain produced mismatches on many
records.

Audit commands now use a lightweight `session_exists()` probe rather than loading
the transcript. `iter_render_audit_records()` renders text or JSON incrementally
from `iter_audit_logs()`; the CLI writes those chunks directly to stdout, so
complete audit-list semantics are preserved without whole-chain materialization.
The existing `render_audit_records()` API remains as a compatibility wrapper that
joins the iterator for callers explicitly requesting a string.

Verification continues scanning the entire hash chain, but retains at most
**1,000 concrete integrity diagnostics** in memory. Additional mismatches are
counted and represented by one explicit `N additional ... omitted` marker, so
verification work is complete while diagnostic memory remains finite.

Regressions prove the renderer consumes only its first record before producing
its first chunk, capped verification reports omitted-error counts correctly, and
the CLI audit-list path still works when `load_session()` is forced to raise.
The complete audit CLI surface passes **13 tests**; Ruff, targeted mypy, and
`git diff --check` are green.

### Re-audit continuation — plans and shared IPC reads enforce caller limits

Two CLI-facing list surfaces still accepted arbitrary positive limits. Plan
listing passed the value directly to SQLite, and subagent report/message/approval
helpers ultimately delegated to `SharedState.fetch_messages()`, which likewise
had no upper bound despite the durable IPC layer already enforcing finite
pending/delivered history.

Plan listing now accepts **1–1,000** rows. Shared IPC fetches accept **1–10,000**
messages, aligned with the existing delivered-history ceiling while remaining
well above normal report/message defaults; pending queues remain independently
capped at 1,000 per recipient. The limits are enforced in the backing layers, so
all CLI/helper callers inherit them rather than relying on duplicated checks.

Regressions cover direct store rejection plus CLI oversized plan/report/message
requests. The complete shared-state + plans CLI surface passes **26 tests** and
focused agent CLI limit coverage passes **2 tests**; Ruff, targeted mypy, and
`git diff --check` are green.

### Re-audit continuation — low-level memory search shares the pipeline candidate ceiling

The normal memory retrieval pipeline already capped lexical/vector candidate
fan-out at 100, but the underlying SQLite index accepted any positive `limit`.
Custom/internal callers could therefore request arbitrarily large FTS result
sets or grow the vector top-K heap toward the full 100,000-record software scan
ceiling, bypassing the intended retrieval budget.

`MAX_MEMORY_SEARCH_RESULTS = 100` now lives in the index layer and is imported by
the pipeline as the single shared contract. Both lexical and vector search reject
limits outside **1–100** before querying/scanning. The higher-level pipeline keeps
its existing `top_k < 1 -> []` behavior and still derives at most 100 retrieval
candidates, so normal product semantics are unchanged.

Direct regressions cover both lexical and vector search at zero and 101. The
complete SQLite memory-index + pipeline surface passes **23 tests**; Ruff,
targeted mypy, and `git diff --check` are green.

### Re-audit continuation — agent task fan-in and artifact history have finite envelopes

Durable agent tasks still had two multiplicative growth paths. Task creation
accepted arbitrary dependency fan-in, while each task could accumulate an
unbounded number of artifacts. `list_artifacts()` returned the complete set and
`agents tasks` nested every artifact into every listed task, so even a bounded
task-count query could expand into an unbounded response. Dependency handoff
also read every dependency's complete artifact set before applying its existing
16 KiB prompt truncation.

Task contracts now allow at most **32 dependencies** after identifier
normalization/deduplication. Each task may persist at most **32 artifacts**;
admission fails before insert at capacity, and `list_artifacts()` reads at most
capacity+1 and fails closed when legacy/corrupt state exceeds the supported
complete-set contract. Artifact fields retain their existing URI/digest/128 KiB
metadata bounds, so callers never receive silently truncated artifact sets.

The user-facing `agents tasks` helper additionally enforces an **8 MiB aggregate
rendered payload ceiling** while constructing its nested task/artifact payload.
This preserves complete task/artifact semantics for accepted responses while
preventing a bounded task count from multiplying into an arbitrarily large CLI
or JSON payload.

Focused regressions cover dependency fan-in, artifact admission, defensive
legacy overflow, and aggregate task-list payload rejection. The complete
agent-task store + agent CLI surface passes **77 tests**; Ruff, targeted mypy,
and `git diff --check` are green.
