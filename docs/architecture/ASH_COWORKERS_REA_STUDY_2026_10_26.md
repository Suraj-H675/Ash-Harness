# Ash Coworkers: source and shipped-artifact reverse-engineering audit

**Date:** 2026-10-26 (Asia/Kolkata)
**Status:** INVESTIGATION ONLY — not an implementation plan approval, security certification, or runtime benchmark.
**Scope:** Hermes identities, OpenClaw scheduler/proactivity, OpenBot authorization/governance/responsibilities, CopilotKit OpenDots product architecture, NanoClaw isolation, Letta memory ideas, and the current Ash integration seams.
**Ground rules:** No changes to Ash production code; no third-party applications installed or executed; no credential or live-account access; no copying third-party code. All external clone analysis is read-only. Assertions about behavior are sourced from code and documentation, not necessarily observed executions.

## 1. Evidence ledger and reproducibility

### Pinned source revisions

| System | Inspected revision | Evidence type |
| --- | --- | --- |
| Ash-Harness | 32149a73300a53f7ad0f6c04953bedf4155f311b (latest HEAD observed; HEAD advanced concurrently during investigation) | Local source and focused tests |
| Hermes installed checkout | 1bbb6e5bce56e721ab685af4cd87df21bbff4d35 | Local Python source; existing local package-lock.json was modified before/throughout inspection and left untouched |
| Hermes Bot Mode shipped plugin | 80fee22582b871b9765a65e2992b8b5a8211c9f8 | REA 6.8.0 static JavaScript analysis plus source |
| OpenClaw | d777ae1239625a8cadb3da17e7a057d0c4de71fa | Source review |
| OpenClaw npm release | openclaw@2026.10.7; npm tarball SHA256 45dc6dadab3ca4e5286262226d6b61554309435a5e83b5d25c803a3a12ef2f81 | Focused REA static JavaScript analysis, not full release coverage |
| CopilotKit OpenBot | ef13020073e72913d3a216ae2cbbd6c97d33cade | Source review |
| CopilotKit OpenDots | 8091324942b86789ed0da7facbee855145d933b0 | Source and deployment documentation |
| NanoClaw | acc2b1c8f52a954368cfaf04b61c32f9a1efc280 | Source and architectural documentation |
| Letta | Official MemFS/LettaBot documentation only | Design reference, no local source audit |

External repositories were cloned into ~/.cache/ash-coworkers-research/sources, outside Ash. Refer to the pinned commits, not floating main, when continuing the analysis.

### REA setup and actual Evidence

REA upgraded via scoped setup from Codex MCP registration rea-agents@6.4.0 to rea-agents@6.8.0. Bundled reverse-engineer-anything skill updated from version 37 (170 tools) to version 49 (229 tools). Current installed user OS Node 24.21.0 was preserved; Node 24.22.1 was added under mise, and only the REA MCP registration's PATH was configured to put it first. Existing HOPPER_LAUNCHER_PATH was preserved. REA made a Codex config backup; an additional pre-PATH-adjustment backup is under ~/.cache/ash-coworkers-research.

REA evidence records:
- Hermes plugin: ev_6eb8a659e3205f2b190d730639a70221ea4ca85c323e99fc9e9aedcde1bd91ab. File: ~/.cache/ash-coworkers-research/rea-hermes-bot-bundle.json. Analyzed 36 JS files, 559 reported findings, 0 syntax parse failures, 9,293 semantic unknown records. The scanned tree includes the shipped plugin.js and tests; it is not a live desktop trace.
- OpenClaw published npm scheduler subset: ev_54157df8bce8db7ea016cf008aee4c84b66c0d79cb55bb49a59c05f6b3b4fdc7. File: ~/.cache/ash-coworkers-research/rea-openclaw-compiled-focused.json. Analyzed three explicitly selected compiled JS modules, with 46 findings and 613 semantic unknown records. Fourteen other inventoried files are outside this focused analysis. The full package and overall OpenClaw runtime were NOT analyzed.
- A broader OpenClaw packaged-code scan was explicitly terminated after its REA JavaScript worker consumed a sustained CPU core for more than two minutes. It generated no accepted full-release evidence. This is a tool/coverage limit, NOT proof of a defect in OpenClaw.

Both successful REA records contain static syntax/dataflow candidates. They do not establish that a route or RPC is reachable in a running application, that an authorization gate works, or that a job can survive real crash/restart.

Initial REA scan failed due to temporary-storage quota; rerun with TMPDIR/TMP/TEMP under ~/.cache/ash-coworkers-research/tmp succeeded. Nothing in the shared /tmp tree was deleted.

### Ash verification

- Existing .venv Python 3.12 used without syncing/upgrading project dependencies.
- 206 tests PASSED in tests/unit/test_automation.py, tests/unit/test_agent_tasks.py, tests/unit/test_memory_wiring.py.
- 9 tests PASSED in tests/unit/test_automation_sdk.py, tests/unit/test_recovery.py, tests/unit/test_turn_recovery.py.
- This is 215 focused passing tests. It does NOT prove a coworker implementation, live provider quality, daemon durability, secure remote hosting, or cross-platform behavior.
- Repository pins uv==0.12.17; installed uv resolves to 0.13.1. No global downgrade or replacement was performed.
- Ash HEAD changed from c52bb4c to 32149a7 while inspecting. Other process temporarily modified src/ash/repo/repomap.py, src/ash/tools/symbols.py, and tests/unit/test_symbol_tools.py before committing. These were never altered by this audit. Preexisting untracked skills/ was never touched.

## 2. Hermes: identity is a profile, not a new model runtime

Pinned bot plugin entry: [plugin.js](https://github.com/NousResearch/Hermes-Bot-Mode/blob/80fee22582b871b9765a65e2992b8b5a8211c9f8/plugin.js).

Observed in shipped plugin source and REA source inventory:
- Bot creation delegates to the existing profiles.create gateway RPC, e.g. plugin.js near line 498; the roster consumes profiles.list and profile-specific metadata.
- One canonical session titled "Bot Chat" is created for each Bot and is different from ordinary scratch sessions; the desktop plugin manages its routing and visibility.
- Scheduled routines are existing cron jobs decorated with Bot ownership, rather than an entirely separate scheduler. Bots have roles, model configuration, skills, memory and credentials in their own profile.
- Bot-to-Bot communication is a gateway/CLI session handoff into the peer Bot's canonical chat, not a new foundation-model primitive.
- The plugin exposes additional optional features (avatar, groups, notifications) but none is required for the core persistent identity.

Observed in local Hermes runtime:
- cron/jobs.py intentionally uses per-profile HERMES_HOME and a context-local cron-store override. This explicitly prevents one profile from running under another's configuration, tokens or skills.
- Cross-process advisory locking and atomic job replacement protect jobs.json updates. cron/scheduler_provider.py manages profile-aware tickers/health.
- Script-only jobs avoid LLM invocation, and monitored jobs compare deterministic data before deciding whether to wake an agent.
- Hermes uses a long-running gateway; closing a desktop client does not in itself shut down the gateway, but powering off the executing host still stops work.

Ash implication: reuse existing named Ash profiles as one ingredient of a Coworker, but NEVER flip a single global active-profile marker while executing multiple coworker turns concurrently. The worker must receive and enforce explicit profile/coworker ownership; credentials, tool grants, skill lookup, session DB and memory scope must resolve from the same owner. A Bot's canonical chat should not double as every background run session.

Source anchors: Hermes local cron/jobs.py, cron/scheduler.py, cron/scheduler_provider.py; bot-mode plugin.js and its canonical-chat and routine tests. Published docs: https://hermes-agent.nousresearch.com/docs/user-guide/bot-mode and https://github.com/NousResearch/hermes-agent/blob/main/website/docs/developer-guide/cron-internals.md.

## 3. OpenClaw: heartbeat is a scheduled agent turn with durable authority

Source anchors:
- [default-proactive-job.ts](https://github.com/openclaw/openclaw/blob/d777ae1239625a8cadb3da17e7a057d0c4de71fa/src/cron/default-proactive-job.ts)
- [execution-context-admission.ts](https://github.com/openclaw/openclaw/blob/d777ae1239625a8cadb3da17e7a057d0c4de71fa/src/cron/execution-context-admission.ts)
- src/cron/service/*, src/cron/store/*, src/cron/isolated-agent/*, src/cron/session-run.ts.

Concrete observations:
- Default proactive behavior is a persisted, system-created scheduled job. It is bound to an agent ID and an explicit main-session key and uses idleOnly and skipIfScratchEmpty to reduce needless execution. The default source constructs a 30-minute interval; do not generalize that as a universal setting.
- Jobs can target explicitly chosen sessions, including isolated sessions. A main-session proactive check and an isolated scheduled research run have different conversation ownership.
- Cron job definitions and event occurrences feed the same execution/admission machinery; the source explicitly has separate cron/event trigger labels but shared context construction.
- At admission, code resolves the requester identity, owner authority, parent conversation, current lifecycle generation and tool policy. It can re-check authority against current config; it does not merely trust an old string in a saved prompt.
- Delivery code contains pre-attempt gates, ownership assertions and recovery records, including explicit ambiguity/fencing mechanics.
- Scheduler and storage are highly decomposed into queues, store workers, run history, receipts, failed deliveries, and lifecycle handling. Copying this complete architecture would add needless complexity and possibly conflicting semantics to Ash.

Ash implication: add targeted session selection and provenance to Ash's current automation worker; provide optional low-cost heartbeat checks and event wake-ups through the existing durable worker. Capture owner authority and permission revision at admission, revalidate before tool effects, and treat agent reminders as untrusted work hints rather than commands with new privileges. Keep event delivery and LLM execution as distinct concerns.

Source corroboration: https://github.com/openclaw/openclaw/blob/main/docs/gateway/heartbeat.md and https://github.com/openclaw/openclaw/blob/main/docs/cli/cron.md. The focused npm REA Evidence above corroborates compiled entry/authority module presence, NOT full live execution.

## 4. OpenBot: policy gateway, signed origin, per-Bot computers

Source anchors:
- [computer/gateway.ts](https://github.com/CopilotKit/OpenBot/blob/ef13020073e72913d3a216ae2cbbd6c97d33cade/server/src/computer/gateway.ts)
- [computer/policy.ts](https://github.com/CopilotKit/OpenBot/blob/ef13020073e72913d3a216ae2cbbd6c97d33cade/server/src/computer/policy.ts)
- server/src/computer/supervisor.ts, server/src/approvals/policy.ts, server/src/approvals/question-resume.ts.
- server/src/responsibilities/engine.ts, store.ts, triggers.ts and docs/architecture.md.

Source-level flow:
1. Resolve the actual browser element from a server-held snapshot, not an attacker/model-provided label.
2. Evaluate the tool, effect, page, actual element, bot identity, actor identity, file scope, destination, input gesture, and run initiator.
3. Record a policy/audit row before dispatch. Deny supersedes allow; absent policy and broken rules fail closed in enforce mode.
4. Forward only authorized actions to the Bot's supervised computer, then record failed forwarded actions.
5. Preserve distinct "actor whose authority is used" and "initiator that started the work." Unattended routines/handoffs may be more restricted than human-supervised commands.

Notable realistic bypass: blocking "click Submit" is insufficient if typing with submit or pressing Enter can activate it. OpenBot models effect/gesture, but its own source notes that the gateway cannot perfectly infer every form-submit effect from a key press. Treat this as a known limit, not an airtight guarantee.

Responsibilities: server/src/responsibilities/triggers.ts validates inbound trigger types, filters, and vendor-specific signature policies; store.ts commits event, run and work queue entries transactionally; engine.ts claims with leases, renews during long work, checks paused status, and represents a person-question pause as WAITING. question-resume.ts rebinds an answered question to its original responsibility, conversation and waiting run.

Computer isolation: supervisor.ts resolves per-Bot container locations, with persistent browser and workspace data. Fixed shared-computer mode exists as a fallback, but it is not equivalent to per-Bot isolation.

Important caveats from the project's own documentation:
- Example startup policy may use an explicit allow-all policy until deployment operators configure stronger restrictions. Do NOT copy that as Ash's unattended default.
- HTTP proxy environment settings are not an OS-level network containment boundary; a process using raw sockets can bypass that proxy without host/container network policy.
- OpenBot's product stack includes PostgreSQL, an external/optional Intelligence service, AG-UI, and supervisors. Its governance patterns can be adapted without replacing Ash's SQLite or provider runtime.

Ash implication: integrate a native, enforceable authorization and audit boundary for ALL side-effecting adapters (local tools, MCP, browser, plugins, delegated agents). Do not make a second policy engine that conflicts with Ash's established deny/ask/allow semantics. Add durable APPROVAL_WAITING to the coworker orchestration level, not "auto-approve to make cron pass".

## 5. OpenDots, NanoClaw and Letta: components rather than wholesale dependencies

CopilotKit OpenDots README at pinned revision:
https://github.com/CopilotKit/OpenDots/blob/8091324942b86789ed0da7facbee855145d933b0/README.md
- Product UI separates specialists/Dots, document Spaces, conversations, work state and computer controls. Streamed agent/UI events use AG-UI. Page metadata and documents stay in local SQLite, while durable conversations depend on CopilotKit Intelligence.
- Conversation service can be hosted, evaluated locally in Docker, or licensed for self-hosting. The MIT application template is therefore NOT proof of a dependency-free, unlimited self-hosted conversational deployment.
- Per-Dot computer service derives from OpenBot; approvals and real connected accounts introduce more dependencies. Its UI is useful design prior art, but importing that stack would duplicate existing Ash foundations.

NanoClaw README at pinned revision:
https://github.com/qwibitai/nanoclaw/blob/acc2b1c8f52a954368cfaf04b61c32f9a1efc280/README.md
- Host routes messages into per-session inbound SQLite; isolated agent containers poll and write outbound SQLite for host channel delivery. One-writer DBs simplify queue ownership across host/container mounts.
- Each agent group has isolated workspace and explicitly mounted paths; credentials are mediated by a gateway. The core runs Claude Agent SDK, with additional providers exposed through optional skills, including Ollama.
- Lessons for Ash: least-privilege filesystem mounts and minimal components. Do not make Docker or a specific SDK mandatory for the base CLI.

Letta MemFS / LettaBot documentation:
https://github.com/letta-ai/lettabot/blob/main/docs/configuration.md
- Git-backed, editable memory files can support version history, correction and controlled concurrent collaboration.
- Ash should not treat extracted conversational memories as factual authority; proposals to change long-term memory need provenance and correction paths. Keep authoritative task state separate from embeddings/notes.

## 6. What current Ash ALREADY supplies

Current source anchors (inspected locally; paths under src/ash):
- profiles.py: validated named profiles, separate state directories, an active-profile marker and ASH_PROFILE environment override.
- sdk.py and core/loop.py: provider-neutral execution, explicit session selection on prompts, durable Goals.
- core/session.py: session storage, fork/resume, recovery, checkpoints, side-effect intent/audit, bounded Goals.
- agents/tasks.py: task dependency DAGs, leased claims, budgets, persisted events/artifacts.
- automation/{models,store,worker,runner}.py: durable at/every/cron schedules, claims, run history, cancellation, worker health, timeout, per-workspace trust checking and outbound webhook delivery.
- memory/{sqlite_index,pipeline}.py: project-scoped document search with SQLite/FTS/vector option; NOT a finished Coworker personal/semantic memory system.
- safety/, sandbox/, tools/browser.py: permission policy, trust boundaries, filesystem/network protections, sandboxing and optional Playwright with a configurable persistent browser profile.
- server/http.py: authenticated single-operator HTTP/SSE API, session routing, approvals and control interface; NOT a multi-tenant hosted platform.
- MCP/plugins/skills/providers: existing capability routing and provider-neutral model selection.

Important differences from the target:
1. AutomationJob has a prompt, workspace and at/every/cron ScheduleSpec, NOT a first-class coworker or event-subscription foreign key. Its runner currently constructs AshClient and calls prompt without a selected saved coworker session, so run history records a session but recurrence does not automatically continue a canonical Bot conversation.
2. Goals are session-scoped and the store permits only one current Goal per session. A Coworker managing multiple responsibilities needs a separate authority-bearing responsibility object, not a relaxation of this invariant.
3. Agent task owner_agent_id is task-worker ownership, not by itself a complete personal coworker identity, permission principal, and memory namespace.
4. Existing profiles can be selected by environment and persisted global marker. Concurrent workers MUST NOT race on the global active marker or implicitly inherit the wrong profile.
5. Browser profiles exist, but there is no verified persistent isolated computer per coworker. A browser profile alone does not isolate shell processes, secrets, network, or mounts.
6. Unattended ASK currently fails closed rather than suspending for future authenticated approval. This is correct for present Ash; a new durable paused approval/resumption protocol is needed.
7. HTTP server is documented for one trusted operator; remote multi-user deployment needs a separate tenant/identity, network and credential design.
8. No unified responsibility-specific event ingress, event deduplication, cost governor or user-notification inbox was confirmed.

Ash's existing side-effect ambiguity guarantees and trust boundaries are strengths to preserve, not features to bypass in the name of autonomy.

## 7. Integration architecture worth prototyping (no implementation now)

Suggested minimal additions, built as adapters around existing Ash rather than a forked runner:

A. Coworker directory: stable ID, owner and profile, active/paused state, model route, scope, memory namespace, permitted tools, canonical user-visible conversation. Explicit context must be passed to every turn, never inferred from a mutable process-global current profile.

B. Responsibility store: immutable origin + editable description, owner/coworker link, allowed event subscriptions, success condition, per-run and lifetime budgets, state (active/paused/completed), audit references. Existing session Goal remains a separate foreground bounded continuation primitive.

C. Event inbox: signed/authenticated webhook or trusted local event; stable dedupe key, ingress verification, origin, selected responsibility. Commit the event and work dispatch claim transactionally or use a durable outbox so crash between the two cannot lose/duplicate work. Reject unauthorized or stale triggers.

D. Coworker dispatcher: extend the existing AutomationStore/Worker rather than add an entirely separate scheduler. Execution context includes selected profile, responsibility, session target, trigger, auth revision, task graph, budget and event ID. Use isolated worker sessions for background tasks; canonical Bot Chat receives an attributed result event.

E. Tool governance: preserve existing Ash policy as the single point of authorization. Resolve actual browser/file/tool targets server side, apply effect-aware rules, and record dispatch fence before any external action. Explicitly model ambiguous completion; never replay an uncertain send/payment/write merely because a lease expires.

F. Approval suspension: new durable waiting item with exact tool args, user identity, expiry, workflow owner, safe resume token and human decision. A model must not forge approval by writing text into memory or conversation.

G. Memory: profile-scoped curated short memory, editable shared user preferences with provenance, separate responsibility state, retrieved supporting evidence, and optional versioned skill proposal/review. No autonomous silent editing of high-authority policy files.

H. Isolated computer (optional later): separate provider interface for computer lifecycle, persistent workspace/browser profile, per-Bot credentials, container or equivalent OS isolation. Distinguish persistence of a filesystem from persistence of a process, and local hosting from 24/7 uptime.

I. UI: CLI first and eventually a clear roster, activity/provenance timeline, task/responsibility cards, approvals inbox, budget view, and paused/blocked status. UI does not imply a second LLM backend.

## 8. Highest-risk mistakes / roadblocks

| Risk | Why it matters | Mitigation / stop condition |
| --- | --- | --- |
| Profile crossover | Wrong profile might expose credentials/memory to another coworker | Two concurrent identities must prove isolation on every tool and state lookup |
| Replayed side effects | Lease recovery may duplicate irreversible actions | Crash injection before/after dispatch; ambiguous outcome never automatically repeats |
| Fake approval | Model text or tool output might impersonate consent | Durable auth-bound approval record, replay/revocation tests |
| Browser policy bypass | Keyboard activation, direct network, nested tools evade naive tool-name ACL | Effect-aware central policy plus OS/network sandbox, adversarial tests |
| Local-model insufficiency | 6 GiB VRAM cannot guarantee strong deep-agent capability | Benchmark real tasks on local and API models; optional provider fallback, never promise parity |
| Hosting/laptop off | No local scheduler runs while the host is off | State this up front; optional externally hosted worker |
| OAuth/third-party restrictions | Some actions require paid/approved APIs or don't permit automation | Connector capability matrix, no invented service support |
| Cost runaway | Heartbeat loops, repeated summaries, multi-Bot fanout | Deterministic watchers, model budgets, throttle, max-depth/max-fanout |
| Memory contamination | Retrieved untrusted content can steer a privileged coworker | Provenance, role separation, memory corrections, prompt injection tests |
| Stack duplication | Importing full OpenBot/OpenDots/OpenClaw runtimes creates competing state/safety systems | Copy design patterns, not infrastructure wholesale |
| Multi-tenant premature hosting | Current Ash server is single-operator | Local-only in v1; tenant auth/isolation is separate go/no-go |
| Repository concurrency | Ash was being actively edited during this audit | Keep research separate, rebase decisions on the then-current HEAD before implementation |

## 9. Proposed proof gates before ANY broad build

- Gate 0: freeze a baseline and run focused Ash checks (current 215 focused passes observed; re-run after concurrent changes settle).
- Gate 1: create two profile-backed Coworker identities, run concurrently, restart, verify no cross-profile state or credential leakage. No UI required.
- Gate 2: bind a recurring and an authenticated event-triggered task to explicit owned sessions, prove deduplication and stale event refusal.
- Gate 3: kill a worker at before-dispatch, after-dispatch and after-observed-commit points; prove no duplicate external effects, and clear ambiguous state.
- Gate 4: suspend on ASK, restart, approve/deny as the authenticated owner, enforce exact immutable request args and expiration.
- Gate 5: test model independence using the SAME real task corpus on small local and API models. Record task completion, tool accuracy, time, cost and resources.
- Gate 6: test one practical, bounded workflow end-to-end (example: watch test repository CI, diagnose, draft a PR, request approval), with no real third-party state changes during proof setup.
- Gate 7: only after above, decide whether persistent browser/computer workers, inter-Bot collaboration and a nonterminal GUI are worth building.

If any gate requires weakening Ash's existing fail-closed, sandbox, or ambiguous-action invariants, STOP and revisit the architecture. If the product requirement becomes "unlimited top-tier fully autonomous AI for free on a switched-off 6 GiB laptop", that requirement is infeasible without changing infrastructure/quality constraints.

## 10. Not yet established / required follow-up

- No end-to-end competitor deployment, private account integration, production crash replay or formal security audit was performed.
- REA did not inspect all OpenClaw compiled files; full packaged scan was deliberately bounded. Neither REA output proves runtime behavior.
- No independent quality/cost benchmark of actual local or API models was run.
- No persistent unattended Ash coworker exists or was created in this phase.
- No evidence yet establishes exact compatibility of all planned schema extensions with current Ash migration and export/import contracts.
- Latest Ash HEAD changed while collecting evidence; inspect status and rerun relevant tests before editing production code.
- The REA MCP registration is updated on disk; an already-running Codex process may require a restart/reconnect to load the new MCP tools. REA CLI static scans DID execute successfully with Node 24.22.1.
- External service license and Terms-of-Service compatibility for specific connectors has NOT been audited; all prior-art projects retain their original copyright/license requirements.

## 11. Decision

**CONDITIONAL GO for a small proof-of-architecture only; NO-GO on an immediate full Bots/Dots clone.**

The most coherent blueprint is Hermes-style identity using Ash's named profiles + OpenClaw-style deliberate scheduling/owner revalidation + OpenBot-style governed real tool actions and responsibilities + Ash's existing durable execution, task ledger, sessions, and conservative recovery.

The next action should be to agree on acceptance gates and reconcile this audit against Ash HEAD at implementation time. Do not begin implementation merely because competitor source contains analogous classes.
