# Ash coding evaluation portfolio

**Owner decision (2026-11-12):** Shift from repeatedly optimizing a single
failed benchmark to breadth-first, evidence-driven coding improvement with
selective deep dives. This is a maintained **inventory and decision protocol**,
not a claim that all scenarios have been run or passed.

## First portfolio: existing cases, not invented benchmarks

These fourteen fixtures already have runnable code and grading logic. They **do not
constitute fourteen independent real repositories**; synthetic and real-repository
lanes must remain separate. Do not pool their scores into a single win rate.

| ID | Existing task | Lane | What it exercises | Latest evidence / next action |
|---|---|---|---|---|
| R1 | `h11-chunk-footer` | Pinned real repo | One-file parser bug; incremental tests | Historical matched Ash/Codex pair passed; retain as control |
| R2 | `urllib3-header-sanitization` | Pinned real repo | One-file security-oriented normalization | Historical paired pilot passed; retain as control |
| R3 | `urllib3-proxy-redirect` | Pinned real repo | Coordinated two-file redirect/security fix | **Scenario 04: PARKED, unresolved**; prior matched pair: Ash fail / Codex pass (one sample) |
| R4 | `urllib3-url-auth-feature` | Pinned real repo | New public API / URL parsing edge cases | **Initial paired pass for both engines**; one sample, no reliability claim |
| R5 | `click-resource-exception` | Pinned real repo | CLI resource lifecycle, exception propagation and suppression | **Initial pair: Ash fail / Codex pass**; Ash stopped at safe context limit after an incomplete edit |
| R6 | `dateutil-strict-numeric-fields` | Pinned real repo | Two-file ISO date/time and calendar-offset input validation | **Initial pair: Ash timeout / Codex pass**; no Ash source edit or verification |
| R7 | `attrs-generator-setattr-hooks` | Pinned real repo | Two-source-module class-generation feature, generator lifecycle and compatibility | **Initial pair: both fail**; Ash 32K protected-context overflow after one edit, Codex visible pass but hidden fail; Ash 64K ablation reaches 120K total-turn cap. Park fixture-specific remediation. |
| R8 | `packaging-exception-pickle` | Pinned real repo | Two-module exception serialization compatibility | **Ash pass (single trial)** at normal 128K context and uncapped turn total: both source edits, visible and hidden oracle passes, terminal completion, scope and audit valid |
| S1 | `marker` | Synthetic CLI smoke | Exact file creation and protected-file preservation | Exists; record separately as smoke |
| S2 | `build-run` | Synthetic CLI smoke | Create a script, then execute authorized check | Exists; verify tool execution and terminal completion |
| S3 | `repair-test` | Synthetic repair | Single-file bug and independent hidden check | Exists; current strong-model baseline pending |
| S4 | `multi-file` | Synthetic navigation | Inspect multiple modules but edit one | Exists; do not mislabel as coordinated multi-edit |
| S5 | `two-file` | Synthetic repair | Both billing modules must change | Exists; current strong-model baseline pending |
| S6 | `event-log` | Synthetic integration | Three-module parser, aggregation and rendering | Exists; current strong-model baseline pending |

**Current gaps (not runnable tasks):** Further independent source projects
should exercise unfamiliar-repository debugging and test triage, a multi-file
refactor with stable public behavior, dependency/interface compatibility, and
larger-codebase navigation. R7 now contributes a new codebase and a two-module
feature, but does not fill all these gaps. Choose each base/fix pin and sealed
oracle only after provenance and red-original/green-reference verification.
The present **14-case inventory** is not a stable reliability benchmark;
repetitions and broader workflow diversity remain necessary.

**Evidence checkpoint:** Before this workflow change, the locally retained
paired receipts showed **R1 and R2 passing for both engines** in a concurrent
pilot and **R3 failing for Ash but passing for Codex** in one matched pair.
An initial **sequential** R4 pair subsequently passed both independent
oracles and completed CLI for both engines (Ash **77.14s**, plain Codex
**79.15s**). The paired runner verified the comparison receipt. These are
single observations, not a stable portfolio pass rate or a 2-second speed
advantage. Their task difficulty, order and runtime conditions differ.

**R5 provenance:** The Click task uses original `pallets/click` commit
`16fe802a3f96c4c8fa3cd382f1a7577fda0c5321` and the upstream
`Context.__exit__` fix `555fa9bb37770a6845a98be60b0c84876775552e`
([PR #3058](https://github.com/pallets/click/pull/3058)). Independently
authored, stdlib-only visible and hidden checks both failed on the original
and passed on the fixed commit. Only `src/click/core.py` is editable. In one
frozen **sequential** pair at GPT-6 Luna xhigh and 32,768 requested context,
Ash failed both independent oracles and CLI completion after 59.75s; it made
one permitted edit but never executed the test, then hit the safe opaque
context limit. Plain Codex passed both oracles, executed the visible test and
completed in 55.92s. Comparison receipt validated both task inputs and
source pins. This **single observation** is a failure classification, not a
reliability estimate or a timed ranking. The two engines retain different
command/permission enforcement and system-prompt behavior.

**R6 provenance:** The python-dateutil case uses original
`6e1864c7c309f4d905e8b337864ca8b5d3ff9d34` and upstream fix
`6f1362220007b9d868638630043636e0f3285a3f`, which changes
`src/dateutil/parser/isoparser.py` and `src/dateutil/tz/tz.py` (plus upstream
tests/metadata, which the agent cannot modify). Independently authored
public-API checks reject non-digit, partial and signed ISO fields and
invalid `tzical` offsets, while preserving legitimate dates and timezones.
Both visible and hidden tests were **red on the original and green on the
fix**. Both source modules are required to change. A single frozen sequential
pair at GPT-6 Luna xhigh with 32,768 requested context and 170s per engine
yielded **Ash fail** (170.22s, no edits, no required command, cancelled during
a model request) and **plain Codex pass** (151.24s, both modules changed,
required test executed, visible and hidden oracles passed). The paired
comparison receipt validated common pins and task/oracle hashes. Ash used
approximately 151.5s in an unsuccessful final model request, rather than
producing a patch. This is one observation; it does not establish that the
cause was an upstream outage or reliably distinguish model generation latency
from upstream transport latency.

**Emerging patterns, not yet causal conclusions:** R3 exposed difficulty
completing a coordinated two-file repair despite some correct graded code;
R5 ended with a partial repair and a protected opaque-context limit; R6
spent its deadline waiting for a model request before editing. These are
different failure mechanisms. Do not treat them as interchangeable evidence
for a single prompt/tool change, or average them into a stable success rate.

**R7 provenance (2026-11-13):** The attrs case uses original
`python-attrs/attrs` commit `d3a98cf05ab15769003dcca3eae91cdd6504b83b`
and upstream implementation `4b5b295bb815bf845fa3570bf63781a88212db40`
(PR #1592). Two independently authored standard-library visible/hidden oracles
both **fail on the original and pass on the reference fix** using the existing
networkless benchmark sandbox. The only permitted source edits are
`src/attr/_compat.py` and `src/attr/_make.py`; upstream's documentation and
typing edits are not part of the permitted change. This is a separate,
dependency-free codebase and a class-construction/API compatibility task,
not another attempt at the parked R3/R5/R6 repairs. No agent result is implied
by the oracle preflight alone.

**R7 initial paired outcome and bounded context ablation (2026-11-13):**
The first requested GPT-6 Luna xhigh, 32K-context, 170-second paired
evaluation used identical frozen user prompts and graded oracles. Ash took
**70.88s** including preflight/grading (**65.48s** CLI), completed three model
requests, edited only `_compat.py`, never ran the visible test, and stopped
with a protected opaque-reasoning context error after eight tool calls.
Plain Codex completed in **129.15s**, changed both required modules and ran a
passing visible test, but **failed the hidden oracle**. Neither was an overall
coding pass. A single unchanged-task Ash **64K** request-context ablation
also failed (**76.98s**): it avoided the 32K opaque-context overflow, made
six model requests and eleven tool calls, edited one module, ran no test,
and stopped before exceeding the separately configured 120K cumulative
turn-token budget. This provides evidence that two different resource limits
can truncate this constrained task; it does **not** demonstrate inability at
Ash's normal 128K request-context default and uncapped turn-total default.
Both attempts preserved protected files and cleaned isolated scratch/auth
state. The paired receipt checker verified the common prompt, revision pins,
and oracle source hashes, while flagging unmatched tool/sandbox policies,
an asymmetric cumulative-turn cap and unverified equivalence of effective
provider context. No production harness change or repeated retry is warranted
from this one task. Detailed receipts and the ablation decision are in the
private, Git-excluded scenario journal.

**R8 provenance / production-budget question (2026-11-13):** The independent
`pypa/packaging` repository (sixth upstream project) provides a genuine
two-module compatibility fix in commit
`1a9c4cb6b6aac7fb6137fcdc4f440af912d8d87f`, relative to original
`458b0ae53e5a4261df851657d147e590b4546eb2`. Separately authored,
standard-library-only visible/hidden checks test pickling and copying
`InvalidMetadata` and `CyclicDependencyGroup`. Both oracles are **red on the
original and green on the reference** in the existing isolated networkless
harness. Only `src/packaging/metadata.py` and
`src/packaging/dependency_groups.py` are editable. The real-repository runner
now permits up to **131,072** requested context tokens to evaluate Ash at its
normal **128,000** configured request context and **zero cumulative turn cap**
under a separate bounded external time limit. This is evaluation tooling,
not a change to Ash production context policy.

**R8 first production-budget outcome:** A single GPT-6 Luna xhigh Ash trial
used 128,000 requested context tokens, **no cumulative turn-token cap**,
and a separate 170s external deadline. The end-to-end evaluator (including
checkout preflight and hidden grading) took **69.54s**, of which the real
Ash CLI took **64.22s**. Both required source files changed, the exact
`python3 -B qa_visible.py` test command executed successfully, and
independent visible and hidden oracles passed on the repaired code.
The CLI completed (`exit_code=0`), scoped files and audit were intact, and
isolated scratch and access-only credentials were removed. Seven completed
model requests used 39,728 prompt and 2,091 completion tokens. One invalid
`search_text(context_lines=5)` request was rejected by the existing maximum
of three lines, and the model recovered; the invalid operation was not
dispatched. This is a **single Ash-only result**, without a fresh Codex
comparator or a repeatability estimate. It does not show that a larger
context would fix R7. No Ash production code changed for this pass. Private
receipt: `/var/tmp/ash-portfolio-r8-packaging-20261113-r01/ash/result.json`.

## Evaluation and prioritization contract

1. **Freeze the task** before a run: objective, task inputs, source revision,
   authorized tool effects, independent visible/hidden oracles, protected files,
   requested model/effort and context/resource limits. Never rewrite a frozen
   task to rescue a failing agent. Keep private raw receipts outside Git.
2. **Separate grades:** code/oracle correctness; visible test actually run;
   full CLI success; safety/audit/scope; wall time; model requests/tokens;
   repeatability. A correct repair that times out at finalization is not a
   full pass. Exclude provider/routing outages from coding win/loss counts.
3. **Compare fairly:** pair the real-repository lane with plain Codex using
   existing `devtools/repository_benchmark.py`,
   `devtools/codex_repository_benchmark.py`, and
   `devtools/parallel_repository_benchmarks.py`. Match user input, pins,
   requested model/effort, deadline and context; disclose tool, sandbox,
   system-prompt and actual provider-routing differences. Use sequential,
   balanced ordering for latency comparison, not concurrent-load rankings.
   Synthetic fixtures currently have an **Ash-only** runner; do not invent
   matched Codex results or equate local-Qwen runs with GPT-6 Luna trials.
4. **Cluster failure modes:** infrastructure/routing; task understanding;
   discovery and redundant inspection; editing; authorization and side-effect
   boundaries; verification; context/recovery; finalization. Prioritize a
   confirmed cross-task weakness by user impact, frequency, severity,
   safety consequences and fix cost, not recency alone.
5. **Stop and reassess:** after **two unsuccessful fix/retest cycles** on one
   scenario, explicitly decide to change the hypothesis, test held-out cases,
   or park it. Critical security/integrity defects can justify a deeper dive
   with documented evidence. Do not spend another provider-intensive run merely
   to obtain a favorable sample.
6. **Risk-proportional implementation:** focused red/green or invariant tests,
   then related regression checks, then broader gates for meaningful changes.
   Validate consequential harness optimizations on a frozen problem and at
   least one independent held-out task; do not overclaim from synthetic tests.
7. **Close each batch** with one choice: accept, revise, revert, or park. Record
   the result and next portfolio slice. Keep scenario diagnostic transcripts
   in the existing **untracked, locally excluded**
   `docs/benchmarks/ASH_SCENARIO_JOURNAL.local.md`.

## Next portfolio slice

**Now:** no Scenario 04 or parked R5/R6/R7 repeat. R7 adds a fifth upstream
repository and shows a second cross-repository **32K protected-context**
failure, plus a distinct **64K/120K cumulative-budget** stop; plain Codex's
hidden failure prevents any overall correctness-win claim. Before changing
Ash's context architecture, study how its real production-default window and
unbounded total-turn budget behave in independent user journeys, and compare
tool history / early editing decisions across genuinely different tasks.
Prioritize product-correctness, recovery safety, and resource economy over
another case-specific attempt to get a green benchmark. Repetitions require
a predeclared variance or causal question, not a desired score.

R8's distinct production-budget trial has completed successfully. Do not
rerun it merely to improve its score. Prioritize real owner coding work and
new reliability findings from actual user workflows, not more case-specific
benchmark tuning.

**Don't conflate:** passing filesystem/provider regression tests proves those
subsystem contracts; it does not prove that Ash's overall coding success rate
rose. Conversely, failure of one hard coding fixture does not invalidate a
separately verified security or transport fix.
