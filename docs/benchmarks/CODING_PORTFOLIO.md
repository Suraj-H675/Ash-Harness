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
| R5 | `click-resource-exception` | Pinned real repo | CLI resource lifecycle, exception propagation and suppression | **Latest sequential 65K pair: both pass**; Ash also passed hidden oracle and verified test after its earlier 32K protected-context failure. One paired trial, not a controlled change attribution |
| R6 | `dateutil-strict-numeric-fields` | Pinned real repo | Two-file ISO date/time and calendar-offset input validation | **Initial pair: Ash timeout / Codex pass**; no Ash source edit or verification |
| R7 | `attrs-generator-setattr-hooks` | Pinned real repo | Two-source-module class-generation feature, generator lifecycle and compatibility | **Initial pair: both fail**; Ash 32K protected-context overflow after one edit, Codex visible pass but hidden fail; Ash 64K ablation reaches 120K total-turn cap. Park fixture-specific remediation. |
| R8 | `packaging-exception-pickle` | Pinned real repo | Two-module exception serialization compatibility | **Latest sequential 65K pair: both pass**; earlier Ash-only normal-128K trial also passed. Exact independent oracle and scope checks verified; no speed/reliability ranking |
| S1 | `marker` | Synthetic CLI smoke | Exact file creation and protected-file preservation | Exists; record separately as smoke |
| S2 | `build-run` | Synthetic CLI smoke | Create a script, then execute authorized check | Exists; verify tool execution and terminal completion |
| S3 | `repair-test` | Synthetic repair | Single-file bug and independent hidden check | Exists; current strong-model baseline pending |
| S4 | `multi-file` | Synthetic navigation | Inspect multiple modules but edit one | Exists; do not mislabel as coordinated multi-edit |
| S5 | `two-file` | Synthetic repair | Both billing modules must change | **Ash passed** visible + hidden oracles in one pre-change and two post-change GPT-6 Luna trials; post-policy review tool succeeded without Git. No Codex control |
| S6 | `event-log` | Synthetic integration | Three-module parser, aggregation and rendering | **Ash passed** visible + hidden oracles in one baseline GPT-6 Luna trial; post-change trial had provider timeouts before any model response, so no post-change semantic grade. No Codex control |

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

**Synthetic S5/S6 checkpoint (2026-11-13, not a real-repository comparison):**
Using `gpt-6-luna`, 65,536 request-context tokens, 240,000 cumulative turn
tokens, 4,096 completion tokens, 205s deadline, and the exact permitted test
command, current Ash initially passed both S5 (two edited modules; 29.926s
CLI, six model requests) and S6 (three edited modules; 39.266s CLI, six model
requests). Visible/hidden independent oracles, protected-file scope and audit
checks passed. S5 then attempted `git_diff` inside a disposable non-Git
workspace and received a failed Git usage message despite successful code.

To expose Ash's existing checkpoint diff as a read-only model tool,
`workspace_diff` was added to the full runtime with bounded, per-turn, no-Git
source review. The first post-change S5 run kept code correct but was denied
the tool under `auto_edit`; this revealed missing read-only permission policy
classification. After correcting that policy (verified red-first), a second
post-change S5 completed both oracles with **zero failed tools** and one
successful `workspace_diff` call: 32.465s CLI and seven model requests, versus
29.926s and six requests pre-change. This is **tool workflow correctness, not a
speed win**. The sole attempted post-change S6 trial produced no model output,
no edits and no tool calls; upstream provider requests timed out repeatedly,
so no inference about semantic task correctness after the change is warranted.
Evidence: `/var/tmp/ash-semantic-s5-current-20261113-v1`,
`/var/tmp/ash-semantic-s6-current-20261113-v1`,
`/var/tmp/ash-semantic-workspace-diff-after-20261113-v1`, and
`/var/tmp/ash-semantic-workspace-diff-policy-green-20261113-v1`.
These are single trials, with no paired plain-Codex control, under synthetic
rather than pinned real-repository tasks; they must not be pooled with R1–R8.

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

**R5 causal context follow-up (2026-11-13, one new pair):** After the
red-first diagnostic and edit-recovery changes in `main` at `c13be3d`, the
frozen Click task was run **once** under an explicit 65,536-token request
context and **uncapped** Ash cumulative turn budget, both engines using
GPT-6 Luna xhigh, the same requested context and 170s deadlines. The
sequential, verified comparison found **Ash pass** (83.89s CLI, eight completed
model requests, nine tool calls) and **plain Codex pass** (78.77s CLI,
seven shell commands). Both completed a successful authorized visible check,
passed sealed hidden and visible oracles and preserved protected files. Ash
completed with exit 0, a valid session audit and cleaned scratch/access-only
credential state. Codex's event receipt and private-home/auth cleanup also
verified. The prior Ash 32K outcome was **fail after partial edit and protected
context exhaustion**; the new result establishes that Ash can complete the
same task at 65K/uncapped in this run. It does **not** isolate larger context
from other changed source/model/transport conditions or demonstrate a
reproducible reliability gain. No further R5 attempt is warranted without a
new independent causal question. Private receipts:
`/var/tmp/ash-r5-64k-paired-20261113-r01`.

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

**R8 independent Codex comparator (2026-11-13, one pair):** On untouched
`packaging-exception-pickle` pins, with GPT-6 Luna xhigh and matching 65,536
request-context tokens, 170-second separate CLI deadlines, and no Ash
cumulative-turn cap, the sequential receipt checker verified the shared
task objective and user prompt, source pins, visible/hidden oracle hashes,
requested settings, temporary isolation and credential cleanup. **Ash passed**
(71.53s CLI, nine completed model requests, 14 tool calls) and **plain Codex
passed** (71.27s CLI, three shell commands). Both required source modules
were changed, the visible command ran, and visible and hidden oracles passed;
Ash audit and scoped-file checks also passed. The 0.26s timing difference is
noise at single-sample scale, not a speed advantage. The earlier Ash-only
128K trial also passed, so R8 has two positive Ash observations at distinct
requested contexts, but no repeatability distribution. Private receipts:
`/var/tmp/ash-r8-paired-post-repair-20261113-r01`.

**Parity caveat for both new pairs:** The checker accepted requested model,
effort, context, prompts, pins and independent grading, **not** actual
tool/permission/sandbox equivalence. Codex's receipt does not independently
verify the same network blocking or enforce the same exact shell-command
policy as Ash. Codex input-token counts include cached input and cannot be
directly compared to Ash's prompt-token accounting. Neither new pair proves
a throughput gain or motivates a production-code adjustment.

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

**Now:** The paired R5 and R8 follow-ups both pass at matched 65K and uncapped
Ash turn-total settings, but neither isolates a source change's impact. Avoid
more reruns of R5/R8 merely to improve the score; R3 remains parked, R6's
prior provider stall is unresolved as a causal coding result, and R7's hidden
Codex failure and Ash's budget-limited runs remain distinct findings. New
real-repository coding tests should be chosen for a previously unmeasured
user workflow or explicitly causal hypothesis, not score accumulation. Focus
on real owner coding usage, Crew's remaining accepted production v1 gates
when authorized, tool-level correctness, and recovery safety; commit
implementation only for independently demonstrated gaps.

**Don't conflate:** passing filesystem/provider regression tests proves those
subsystem contracts; it does not prove that Ash's overall coding success rate
rose. Conversely, failure of one hard coding fixture does not invalidate a
separately verified security or transport fix.
