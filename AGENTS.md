# AGENTS.md — Ash-Harness Repository Instructions

## 1. Scope

These instructions govern work in the Ash-Harness repository.

They supplement applicable higher-level and user instructions. They define
Ash-specific engineering, safety, testing, repository, and delivery practices.

Do not reinterpret this file as permission to violate higher-priority
instructions.

Nested `AGENTS.md` files may define additional rules for their own subtrees.

The `ref/` directory contains external/reference codebases used for study,
comparison, or provenance. Do not modify reference trees as part of ordinary
Ash work. Their nested `AGENTS.md` files apply only when a task explicitly
requires working inside that reference subtree.

---

## 2. Project intent

Ash is a terminal-native, local-first, provider-neutral coding-agent harness
for serious and long-running engineering work.

Its important properties include:

- durable sessions and recovery;
- real coding and process tools;
- provider-neutral model execution;
- plugins, skills, MCP, LSP, ACP, A2A, HTTP, JSON-RPC, and SDK integrations;
- subagents and automation;
- explicit trust and approval boundaries;
- safe filesystem, process, network, and sandbox behavior;
- deterministic and inspectable state;
- conservative handling of external side effects;
- useful terminal, structured-output, and accessibility surfaces.

Changes must preserve those properties unless an explicitly accepted
requirement requires otherwise.

---

## 3. Core invariants

Treat the following as high-value project contracts.

Do not weaken or change them casually.

### 3.1 Tool and side-effect integrity

Ash records approved intent before dispatch where required by the runtime.

Local, plugin, and MCP side-effecting dispatch must preserve the project's
exactly-once and ambiguous-outcome guarantees.

A dispatched external or non-file side effect must not be silently replayed
when its outcome is ambiguous.

Do not introduce retries that can duplicate externally visible side effects
without an explicitly established safe semantic basis.

### 3.2 Conservative recovery

Recovery must prefer preserving known completed work over guessing.

Direct file compensation or restoration must rely on sufficiently strong state
evidence such as expected hashes or equivalent validated state.

Do not guess that a non-file side effect did or did not happen.

Do not silently replay ambiguous work during recovery.

Crash recovery must remain inspectable and conservative.

### 3.3 Trust boundaries

Workspace files, tool output, memory, citations, external content, and
repository-derived instructions may contain untrusted content.

Untrusted content must not gain authority over user intent or runtime policy.

Keep user-owned policy distinct from repository-controlled configuration.

Project-controlled configuration must not silently gain privileges reserved
for trusted or user-owned configuration.

Deny and fail-closed behavior must remain fail-closed where the project
contract requires it.

### 3.4 Approval and permission integrity

Do not weaken approval, authorization, trust, path, domain, command, or
capability boundaries merely to make a workflow succeed.

Do not turn denied or ambiguous operations into implicit approval.

Unattended execution must not become less restrictive by accident.

### 3.5 Sandbox integrity

Project configuration must not silently weaken user-owned sandbox policy.

Preserve expected filesystem, process, environment, network, and cleanup
boundaries.

Do not replace explicit isolation or failure behavior with a silent unsafe
fallback.

### 3.6 Workspace and file integrity

Preserve protections around:

- workspace boundaries;
- path traversal;
- symlinks and junctions;
- stale reads;
- concurrent no-overwrite behavior;
- encoding preservation;
- binary/text classification;
- atomic or conflict-aware writes where required.

Do not weaken these protections to simplify an implementation.

### 3.7 Secret and audit integrity

Do not expose credentials, tokens, private keys, sensitive environment values,
or other secrets in:

- logs;
- tests;
- fixtures;
- commits;
- generated artifacts;
- debug output;
- reports.

Preserve redaction and audit guarantees.

### 3.8 Compatibility

Preserve intended public interfaces and observable behavior unless a confirmed
bug or accepted requirement requires changing them.

Avoid gratuitous breaking changes.

---

## 4. Working-tree discipline

Before meaningful work, inspect the current repository state.

At minimum understand:

- repository root;
- current branch;
- current HEAD;
- tracked modifications;
- staged changes;
- untracked files relevant to the working area.

Existing user or development work is not disposable scratch space.

Never:

- delete unrelated changes;
- overwrite unrelated changes;
- revert unrelated changes;
- clean unrelated untracked files;
- reset the working tree to make testing easier;
- use destructive Git commands to obtain a clean state;
- assume an unfamiliar modification is safe to discard.

Preserve unrelated and concurrent work.

Do not modify existing untracked development files unless the task explicitly
requires those exact files.

If another change overlaps the required area, understand and preserve it rather
than blindly replacing it.

---

## 5. Git staging discipline

Never use broad staging commands such as:

- `git add -A`
- `git add .`
- `git add --all`

when creating a task-specific commit.

Stage explicit intended paths only.

Before committing, inspect:

- `git status`;
- the staged file list;
- the staged diff.

Ensure the commit contains only the intended coherent change.

Never force-push or rewrite shared history unless the user explicitly requires
it and the action is otherwise permitted.

---

## 6. Evidence before changes

For bug-fix or adversarial-audit work:

1. establish the claimed behavior;
2. reproduce it when reasonably possible;
3. collect concrete evidence;
4. identify the responsible boundary or root cause;
5. only then modify production code.

Do not make speculative production changes merely because they might fix a
suspected problem.

If exact reproduction is impossible, gather the strongest available evidence
and distinguish clearly between:

- confirmed behavior;
- probable explanation;
- hypothesis;
- unknown.

Do not present hypotheses as confirmed bugs.

---

## 7. Root-cause-first fixes

For confirmed defects, fix the responsible cause rather than merely hiding the
symptom.

Prefer the smallest correct change that restores the intended invariant.

Avoid:

- unrelated refactoring;
- opportunistic cleanup;
- broad rewrites;
- unnecessary dependency additions;
- unrelated formatting churn;
- changing public behavior without necessity;
- weakening tests or guards to obtain green results.

If a larger redesign appears necessary, establish why before proceeding.

---

## 8. Regression tests

A confirmed bug fix should normally include a meaningful regression test when
the behavior is testable.

A good regression test should:

- fail for the demonstrated defect or meaningfully encode the missing invariant;
- exercise the responsible boundary;
- pass because of the actual fix rather than an unrelated side effect;
- avoid overfitting to implementation details when behavior-level coverage is
  practical.

Do not add tests whose only purpose is increasing test count.

Do not weaken an existing correct test simply because new code fails it.

---

## 9. Verification strategy

Use risk-proportional verification.

Prefer this progression:

1. focused reproduction;
2. targeted test or tests for the affected behavior;
3. closely related module or subsystem tests;
4. static, type, or lint checks relevant to touched code;
5. broader repository gates when warranted by blast radius;
6. packaging, runtime, or integration smoke checks when the affected surface
   requires them.

Do not run an enormous suite repeatedly when a focused test can answer the
current question.

Do run broader relevant gates before declaring a substantial or cross-cutting
change complete.

---

## 10. Canonical CI-parity gates

The repository CI currently validates Python 3.12, 3.13, and 3.14 on Ubuntu
and macOS.

The principal CI commands are:

```bash
uv sync --locked --group dev --extra server --extra browser --extra acp --extra a2a --extra local-embeddings

uv run --locked ruff check src tests

uv run --locked mypy src/ash

uv run --locked pytest -v --timeout=120 --timeout-method=thread

uv build --sdist --out-dir dist --clear
sdist="$(printf '%s\n' dist/*.tar.gz)"
uv build --wheel "$sdist" --out-dir dist
```

CI also verifies the built wheel in a clean smoke environment using:

```bash
uv venv .smoke-venv --python <python-version>
uv pip install --python .smoke-venv dist/*.whl
.smoke-venv/bin/python tests/packaging/smoke_minimal_install.py
```

Do not blindly run every CI command after every small edit.

Select verification according to the affected surface, then run the broader
relevant gates before completion of substantial work.

---

## 11. Risk-specific verification

### 11.1 Provider and model changes

Check as relevant:

- provider-neutral behavior;
- request and response translation;
- streaming;
- error classification;
- retry boundaries;
- failover;
- capability metadata;
- reasoning and tool behavior;
- usage and cost normalization;
- cancellation;
- first-output semantics.

Never introduce retry behavior that violates side-effect safety.

### 11.2 Tool and runtime changes

Check as relevant:

- schema and argument validation;
- approval intent;
- dispatch uniqueness;
- cancellation;
- result ordering;
- timeout handling;
- ambiguous outcomes;
- process cleanup;
- persisted recovery state.

### 11.3 MCP, plugin, and extension changes

Check as relevant:

- trust ownership;
- manifest and schema validation;
- capability refresh;
- contract snapshots;
- OAuth and authentication boundaries;
- token handling;
- sandbox and environment behavior;
- ambiguous dispatch;
- secret exposure;
- reload and uninstall failure handling.

### 11.4 Filesystem and sandbox changes

Check as relevant:

- traversal;
- symlinks and junctions;
- workspace escape;
- race and no-overwrite behavior;
- environment scrubbing;
- network restrictions;
- child-process cleanup;
- platform differences;
- failure and fallback behavior.

### 11.5 Session, state, and recovery changes

Check as relevant:

- transaction boundaries;
- persistence across restart;
- interrupted turns;
- checkpoints and hashes;
- ambiguous work;
- rewind and fork behavior;
- schema migrations;
- import and export validation;
- backups and restores;
- corrupted or partial state.

### 11.6 CLI and TUI changes

Check as relevant:

- interactive behavior;
- non-interactive and redirected behavior;
- structured output;
- terminal sizing and fallbacks;
- cancellation;
- screen-reader, reduced-motion, and no-color modes;
- command parsing and completion;
- error messages.

### 11.7 Packaging and installation changes

Check as relevant:

- clean installation;
- built wheel contents;
- entry-point behavior;
- optional dependency boundaries;
- supported Python versions;
- absence of undeclared runtime dependencies.

---

## 12. Cross-platform awareness

Ash supports Linux, macOS, and Windows-facing behavior even when development is
performed on one platform.

Do not introduce platform assumptions without checking the affected path.

Pay particular attention to:

- path semantics;
- signals and process trees;
- executable discovery;
- shell behavior;
- sandbox backends;
- terminal behavior;
- filesystem locking;
- symlink and junction differences.

If a platform-specific path cannot be exercised locally, keep that limitation
explicit and use available CI or focused tests as evidence.

---

## 13. Reference trees

`ref/` contains reference and upstream projects.

Use them to:

- understand prior art;
- compare behavior;
- trace provenance;
- validate compatibility assumptions.

Do not casually copy behavior from a reference project into Ash.

Ash has its own safety, trust, recovery, and compatibility contracts.

Do not modify files under `ref/` during ordinary Ash implementation or audit
work.

If a task explicitly requires modifying a reference tree, obey its applicable
nested instructions in addition to higher-level instructions.

---

## 14. Dependencies, tools, skills, plugins, and MCPs

Use existing project capabilities when they are sufficient.

Additional development, research, testing, debugging, analysis, or inspection
tools may be used when they materially improve evidence or correctness and are
permitted by the active environment.

This includes legitimate:

- packages;
- CLIs;
- debuggers;
- profilers;
- static analyzers;
- browser tools;
- MCP servers;
- plugins;
- skills;
- code intelligence;
- documentation systems;
- instrumentation utilities.

Do not artificially avoid a useful development tool merely because it is not
already installed.

It is acceptable to install or configure local development tooling when that
materially improves correctness or efficiency and the environment permits it.

Tool freedom does not mean production-dependency freedom.

Do not add a new production dependency merely for convenience.

A production dependency addition should have a concrete project-level reason
and should account for:

- maintenance;
- security;
- compatibility;
- supported platforms;
- packaging;
- startup and runtime cost;
- failure behavior.

---

## 15. Documentation

Update documentation when a change alters documented user-visible behavior,
configuration, interfaces, installation, compatibility, or operational
expectations.

Do not document behavior that has not actually been implemented and verified.

Keep examples consistent with real commands and public APIs.

---

## 16. Commits and pushes

Do not commit or push merely because files were modified.

Commit and push only when the active task or user instructions call for it.

When commits are required:

- accumulate related verified work into coherent, meaningful batches;
- strongly prefer fewer substantial commits over commit/push spam;
- do not create a new commit for every tiny fix when related work can be
  verified and grouped safely;
- stage explicit intended files only;
- inspect the staged file list;
- inspect the staged diff;
- use a clear commit message describing the coherent change.

Do not commit partially verified work merely to create a checkpoint.

Do not push a batch until its relevant local verification has passed.

When pushing and hosted CI monitoring are part of the task:

1. finish a coherent verified batch;
2. stage only the intended files;
3. inspect the final staged diff;
4. commit the batch;
5. push it to the current intended branch;
6. monitor hosted CI to completion;
7. investigate any CI regression;
8. fix and re-verify regressions before continuing to the next grouped batch.

Do not begin another commit/push cycle merely because one small additional
finding appears while a coherent batch is still being developed.

Do not force-push or rewrite history.

---

## 17. Adversarial-audit discipline

For broad audit work, prioritize real user-facing and trust-boundary failures
over theoretical issue volume.

Probe as relevant:

- malformed input;
- malicious or untrusted content;
- path boundaries;
- permissions and approvals;
- provider failures;
- retries and ambiguity;
- sandbox escape and fallback behavior;
- installation, setup, and runtime paths;
- MCP, plugin, and extension boundaries;
- session and recovery behavior;
- cancellation and concurrency;
- UI and CLI failure paths;
- packaging and clean-install behavior.

Report and fix only confirmed, reproducible breakages or issues supported by
strong concrete evidence.

Do not manufacture findings to appear productive.

Do not change production behavior merely because a hypothetical edge case might
exist.

When uncertainty remains, gather more evidence or surface the uncertainty
rather than converting it into a speculative fix.

For long-running audits, preserve continuity:

- do not restart already-completed investigation without a concrete reason;
- keep track of confirmed findings, fixed findings, pending evidence, and
  remaining surfaces;
- avoid repeating completed work unless verification or changed evidence
  warrants it;
- continue from the latest verified checkpoint.

---

## 18. User-facing workflow verification

When a change affects a user-facing workflow, verify the workflow itself when
practical rather than relying only on isolated unit tests.

Depending on the surface, this may include:

- actual CLI invocation;
- interactive terminal behavior;
- structured-output mode;
- clean installation;
- provider setup/onboarding;
- MCP connection/setup;
- plugin or skill lifecycle;
- browser automation;
- session resume/recovery;
- approval flows;
- failure and recovery paths.

Do not equate internal test coverage with end-to-end product correctness when
the affected behavior crosses subsystem boundaries.

---

## 19. Security-sensitive decision boundaries

Be especially conservative when a proposed change affects:

- authentication;
- authorization;
- trust;
- approvals;
- sandboxing;
- secrets;
- provider credentials;
- network boundaries;
- command execution;
- side-effect replay;
- recovery;
- persistence;
- concurrency;
- external integrations.

Do not weaken a security or trust guarantee because the stricter behavior is
inconvenient.

Do not convert a fail-closed behavior into fail-open without an explicit,
well-supported requirement.

Do not bypass validation, verification, or authorization to make a test pass.

---

## 20. Scope discipline

Stay within the active task.

Do not use a focused bug fix as an excuse to redesign unrelated subsystems.

Do not change files unrelated to the confirmed root cause unless the broader
change is clearly necessary.

If the investigation reveals a distinct additional issue, record it separately
rather than mixing an unrelated fix into the current change without reason.

When multiple confirmed issues are closely related and share a coherent root
cause or subsystem, they may be fixed and verified as one meaningful batch.

---

## 21. Change review

Before declaring substantial work complete:

- inspect the final diff;
- check that every changed file belongs to the task;
- check for accidental formatting churn;
- check for debug code or temporary instrumentation;
- check for secret exposure;
- check for weakened validation or tests;
- check for unintended public-interface changes;
- check that new tests actually exercise the intended behavior.

For high-risk work, use an independent review pass when useful.

---

## 22. Hosted CI behavior

When the active task requires pushing and monitoring CI:

- treat hosted CI as a verification gate, not a formality;
- monitor all relevant jobs to completion;
- do not assume a partially green matrix means the batch passed;
- investigate platform-specific failures rather than dismissing them;
- distinguish infrastructure/transient failures from product regressions using
  evidence;
- re-run or fix only when justified by the actual failure mode.

Do not continue accumulating unrelated commits while a required grouped batch
still has unresolved hosted CI failures.

---

## 23. Completion criteria

A significant fix is complete only when the relevant items below are true:

- the original problem is reproduced or otherwise concretely established;
- the responsible root cause is understood;
- the chosen change is minimal and scoped;
- intended interfaces and invariants remain intact;
- meaningful regression coverage exists where practical;
- targeted verification passes;
- broader relevant gates pass;
- important diffs have been reviewed;
- unrelated working-tree changes remain untouched;
- no secrets were exposed;
- remaining limitations or unverified platform behavior are stated clearly.

If commit, push, and hosted CI are part of the active task, completion also
requires:

- a coherent grouped commit;
- explicit-path staging;
- successful push to the intended branch;
- hosted CI monitored to completion;
- regressions resolved before continuing.

Correctness and evidence matter more than producing many changes.

---

## 24. Final repository principles

For Ash work:

- reproduce before fixing;
- prefer evidence over assumption;
- understand root cause before changing production behavior;
- make the smallest correct change;
- preserve trust, approval, sandbox, recovery, and side-effect guarantees;
- protect unrelated and untracked user work;
- test the behavior that actually matters;
- use broader gates when the blast radius warrants them;
- avoid speculative fixes;
- avoid unrelated refactors;
- avoid commit/push spam;
- stage only intended files;
- never force-push or rewrite history;
- monitor required hosted CI to completion;
- preserve public interfaces unless a confirmed issue requires change;
- state uncertainty honestly;
- do not claim success without verification.
