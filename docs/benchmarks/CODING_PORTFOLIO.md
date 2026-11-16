# Ash coding evaluation portfolio

**Owner decision (2026-11-12):** Shift from repeatedly optimizing a single
failed benchmark to breadth-first, evidence-driven coding improvement with
selective deep dives. This is a maintained **inventory and decision protocol**,
not a claim that all scenarios have been run or passed.

## First portfolio: existing cases, not invented benchmarks

These seventeen fixtures already have runnable code and grading logic. They do
not represent seventeen independent real repositories: R1–R11 are pinned
repository tasks (some share an upstream project), while S1–S6 are synthetic.
Keep the lanes separate and do not pool their scores into a single win rate.

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
| R9 | `tomlkit-comment-control-validation` | Pinned real repo | Three-module TOML comment validation across standalone, item and array APIs; control-character rejection and mutation safety | **Two separate sequential 65K pairs: both pass in both trials**; latest Ash 168.05s / 14 model requests / 33 tools, Codex 87.27s / 6 shell commands; one outcome per revision, policy-parity caveat |
| R10 | `pluggy-role-module-split` | Pinned real repo | Seven-module role-based refactor; old import shims and public plugin behavior, wrappers, firstresult, historic replay and subset callers | **Park after two matched pairs**; at 170s both timed out/incomplete; at 300s Ash stopped on protected opaque-context exhaustion after 4/7 files, Codex changed all seven and passed both oracles but timed out without executing the visible test. Neither a complete coding pass |
| R11 | `boltons-bulk-ordered-collections` | Pinned real repo | Two-module ordered-container repair: multi-list index boundaries, insertion and tail handling, and linear-time ordered-set bulk removal | **Controlled Ash-only comparison completed:** no restart 85.33s, 3 requests, 0 edits, context error; one restart 113.86s, 5 requests, actual safe restart, 1/2 edited *before* restart, no edits in child, second context error. Both fail oracles; audits/scopes intact; no verified coding progress after restart |
| S1 | `marker` | Synthetic CLI smoke | Exact file creation and protected-file preservation | Exists; record separately as smoke |
| S2 | `build-run` | Synthetic CLI smoke | Create a script, then execute authorized check | Exists; verify tool execution and terminal completion |
| S3 | `repair-test` | Synthetic repair | Single-file bug and independent hidden check | Exists; current strong-model baseline pending |
| S4 | `multi-file` | Synthetic navigation | Inspect multiple modules but edit one | Exists; do not mislabel as coordinated multi-edit |
| S5 | `two-file` | Synthetic repair | Both billing modules must change | **Ash passed** visible + hidden oracles in one pre-change and two post-change GPT-6 Luna trials; post-policy review tool succeeded without Git. No Codex control |
| S6 | `event-log` | Synthetic integration | Three-module parser, aggregation and rendering | **Ash passed** visible + hidden oracles in one baseline GPT-6 Luna trial; post-change trial had provider timeouts before any model response, so no post-change semantic grade. No Codex control |

**Current gaps (not runnable tasks):** Further independent source projects
should exercise unfamiliar-repository debugging and test triage, dependency/
interface compatibility, and larger-codebase navigation. R7, R9 and R10 add
distinct codebases and multi-module workflows, with R10 covering a seven-module
refactor behind compatibility shims; they do not fill all these gaps. Every
additional task still needs pinned base/fix provenance and independently
authored sealed oracles with red-original/green-reference preflight.
The present **16-case inventory** is not a stable reliability benchmark;
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

**R9 provenance and oracle preflight (2026-11-13):** The new
`python-poetry/tomlkit` case uses original commit
`55d07680cabf023885d511d2bf2000963e4e3dc7` and the upstream fix commit
`7e0016217ea2dbc369fc721acfd8ace52a899a99` (“Reject forbidden control
characters in generated comments”, [PR #705](https://github.com/python-poetry/tomlkit/pull/705)).
Three source modules are editable:
`tomlkit/_utils.py`, `tomlkit/api.py`, and `tomlkit/items.py`. Independently
authored, standard-library-only visible and hidden checks cover all forbidden
ASCII controls, standalone multiline LF/CRLF versus bare CR, single-line item
and array comments, valid tabs/Unicode, TOML round trips, and unchanged document
state after rejected mutations. Both visible and hidden oracles are **red on
the original and green on the reference fix** in the existing isolated,
networkless preflight.

**R9 first sequential paired outcome (2026-11-13, one sample):** At matched
GPT-6 Luna xhigh, 65,536 requested context, 170-second per-engine deadline,
and uncapped Ash turn-total budget, **both Ash and plain Codex passed** the
same pinned task, visible command and independent hidden oracle. Both changed
exactly the three required modules, preserved protected files and completed
with exit 0. Ash took **106.68s CLI**, completed **14 model requests / 22 tool
calls**, had **zero failed tool calls**, and passed audit verification. Plain
Codex took **78.74s**, used **five shell commands** (four non-test commands),
and its event log, isolated private HOME/XDG, temporary auth cleanup and
scratch cleanup verified. The comparison receipt verified common task
objective, user prompt, source pins, oracle hashes, requested model/effort/
context, direct native Codex binary, and ephemeral auth cleanup. It did **not**
verify actual sandbox/policy equivalence: Codex's network blocking was not
independently verified and its exact shell-command policy was not enforced by
the comparator. Codex was 27.94s faster in this single observation; this is
not a stable latency ranking or throughput claim. Ash's prompt-token and
Codex's cached-input accounting are not directly comparable. No production
Ash change is justified by this pass; R9 establishes a positive result on a
new codebase, not a repeatability estimate.

**R9 current-revision pair (2026-11-16, one additional trial):** At Ash
`82f33a9`, the existing frozen R9 task was compared sequentially against
unmodified plain Codex with the same requested GPT-6 Luna xhigh, 65,536
context tokens, 170-second per-engine CLI deadline, task prompt, repository
pins and independent visible/hidden oracles. The pair receipt validated
shared inputs and recorded **both engines passing**, performing the required
visible test, preserving source scope and completing normally. Ash: **168.05s
CLI, 14 completed model requests / 33 tool calls**, 162.734s in model requests
and 1.464s executing tools, valid audit. Codex: **87.27s CLI, six shell
commands**, valid event record and isolated profile. This is not a matched
code-level before/after experiment: model output and infrastructure conditions
vary across dates, so the old 106.68s Ash result versus 168.05s here does
not establish a regression from any specific source change. In the new pair,
Codex was faster by 80.78s, but one observation at this revision is not a
stable timing distribution. The comparator still does not independently prove
Codex sandbox/network or exact shell-command-policy parity, and the engines'
token usage accounting is not directly comparable. This pair motivated a
separate coding-tool usability investigation, **not** a claim that later
patch-format support has improved real-model success or efficiency. Private
receipt: `/var/tmp/ash-r9-current-parity-20261116-r01/`.

**Parity caveat for both new pairs:** The checker accepted requested model,
effort, context, prompts, pins and independent grading, **not** actual
tool/permission/sandbox equivalence. Codex's receipt does not independently
verify the same network blocking or enforce the same exact shell-command
policy as Ash. Codex input-token counts include cached input and cannot be
directly compared to Ash's prompt-token accounting. Neither new pair proves
a throughput gain or motivates a production-code adjustment.

**R10 provenance and preflight (2026-11-13):** The new
`pytest-dev/pluggy` case uses original commit
`d70fe1840d01c78b7d85f693cfef01ccdf2f80a6` and the exact parent commit from
upstream PR #703, `17d4ee957ffe86e5e65f892fef8898bb2a386d6e`
([role-specific hook module split](https://github.com/pytest-dev/pluggy/pull/703)).
The seven editable modules are `_caller.py`, `_callers.py`, `_config.py`,
`_decorators.py`, `_execution.py`, `_hooks.py`, and `_impl.py`. The upstream
change creates five role-specific modules and retains `_hooks.py` and
`_callers.py` as compatibility re-export shims. Independent visible and hidden
oracles verify legacy import names and `__all__`, object identity across old
and new module paths, top-level exports, actual PluginManager calls, new- and
old-style wrappers, firstresult, historic replay and subset callers. The
existing networkless preflight independently confirms visible and hidden
**failure on the original and pass on the reference**, with exactly five
reference-added modules and two modified shims. This is a structural refactor
and compatibility task, not a new behavior feature. At the time of preflight,
the paired Ash/Codex model evaluation had **not yet run**.

**R10 first sequential paired outcome (2026-11-13, one sample):** At matched
GPT-6 Luna xhigh, 65,536 requested context and 170-second per-engine CLI
deadlines, **both engines timed out** before completing the seven-module
refactor. Ash used 170.47s, seven completed model requests and 11 successful
tool calls (zero failed); it changed `_config.py`, `_decorators.py` and
`_impl.py`, but did not finish `_caller.py`, `_execution.py` or either
compatibility shim. Plain Codex used 170.06s and six shell commands; it changed
only `_config.py` and `_decorators.py`. Neither executed the visible test;
both visible and hidden oracles failed because the required module split was
incomplete. Both preserved protected files. The comparison receipt verified
the common task, pins, prompt, oracle hashes and requested settings, but not
actual Codex/Ash sandbox or command-policy equivalence. This is an incomplete
task result, not a pass or a speed ranking. One controlled resource ablation
with a 300-second deadline is planned to test whether the 170-second cap was
the binding constraint; all other task and model settings remain frozen.

**R10 300s deadline-only ablation (2026-11-13, second and final pair):** Task,
source pins, shared prompt, oracles, requested model GPT-6 Luna xhigh and 65,536
context remained frozen. Only the separate CLI deadline changed to 300s. Ash
stopped after **188.00s without timing out** because the context allocator
correctly refused to rewrite the opaque provider reasoning prefix when its
estimated 59,840 input tokens exceeded the 59,024 limit. It completed eight
model requests and 14 successful tool calls, changed four of seven required
files (`_config.py`, `_decorators.py`, `_execution.py`, `_impl.py`), and emitted
a structured `context` failure. The 65K requested context is a **deliberate
paired-test constraint**, not Ash's normal 128K setting. Ash's audit, protected
file scope and recovery-safe failure behavior were intact; both oracles still
failed on the incomplete work and no visible command ran. Plain Codex used
**300.03s and timed out**, but changed all seven modules; **both independent
oracles passed when the evaluator inspected its resulting files**, while its
authorized visible command never ran and the turn never completed. This
counts as code correctness **without** full workflow completion, not a Codex
pass. The comparison receipt verified the common objective, inputs and
requested limits, **not** identical actual tool/sandbox policies or provider
context behavior. Under the stated two-cycle limit, **park R10**. The two
attempts do not establish that simply extending the deadline makes Ash pass.
Ash already has a separately tested, explicit `--context-continuations` safe
reasoning-window restart path, which was not enabled for these frozen pairs;
it must not be assumed effective without a separate causal test. No production
context safety rule was weakened to score this scenario.

**Diagnostic repair:** The repository evaluator now extracts only a stable,
allowlisted failure *category* from bounded headless JSON output into the
private machine-readable receipt (`terminal_error_category`). It never copies
error messages, workspace paths or raw model output into this field. The
focused regression suites verify malformed, oversized and sensitive output
handling and now pass **80 tests**. This improves distinction between a
context-safety stop and a provider outage without reclassifying failures as
successes. The combined benchmark, context-continuation, history and error
classification suites additionally pass **161 focused tests**. No provider
trial was rerun to validate this diagnostic extractor: its actual-category
smoke check used the existing R10 stdout receipts.

**R10 evaluator support:** The repository and plain-Codex graders previously
required every editable path to exist in the original checkout and rejected
all added/deleted files. They now accept additions/removals only for explicitly
editable paths proven by the pinned reference diff, while still rejecting
unapproved path additions/deletions, any symlinks, modified protected files,
or incomplete required-module changes. This is necessary for genuine module
splits, not a general relaxation of file-scope protection. The focused
repository/Codex/parallel benchmark unit suites pass **73 tests** after this
change; Ruff and `git diff --check` pass. This harness repair is committed
separately before any R10 model trial.

**R11 independent repository and restart experiment (2026-11-14):** The
`mahmoud/boltons` source is pinned to original
`e66cade323f5c11cebb4bd0f099e634b245adccd` and upstream PR #440's
pre-merge fix `a82b55af744fec5ac38b9f6b8ae24bb937b22666`
([upstream PR](https://github.com/mahmoud/boltons/pull/440)). The exact fix
changes two source modules: `boltons/listutils.py` and `boltons/setutils.py`.
Independently authored standard-library-only visible and hidden checks
exercise a multi-sublist `BarrelList`'s index bounds, out-of-range inserts,
drained-tail pop, and an `IndexedSet`'s order, index and compaction behavior
after large and small removal batches. Both sealed checks are **red on the
original and green on the upstream fix** in the existing networkless,
read-only evaluator. Neither pinned tree contains symlinks. The authorized
visible command is `python3 -B qa_visible.py`.

**Controlled R11 outcome (2026-11-14, one sample per condition):** Frozen task
at commit `a080db9`, GPT-6 Luna xhigh, 24,576 requested context, zero
cumulative turn cap, identical tools, user objective, visible/hidden oracles
and 240-second external deadline; only `--context-continuations 0` vs `1`
changed. **Ordinary Ash:** context error after 85.33s CLI / 3 model requests
and six successful read/search tools, no edit and no authorized test; both
oracles fail, source protected and audit intact. **Opt-in Ash:** real safe
context restart (`session_count=2`, `context_continuations=1`), but second
window also ended on a context error after 113.86s CLI / 5 requests and
15 recorded tool calls; one of two source modules changed **before the
context restart** (at ~100.8s), two stale-hash edit attempts rejected before
dispatch, and one over-limit `search_text`
`context_lines` call rejected before dispatch. Neither required visible test
nor independent oracle passed. Protected files and both session audits
remained intact. The actual restart followed the first turn error at ~100.94s;
the child started at ~101.02s and made read-only inspection calls before its
second context failure. The opt-in trial produced a partial edit relative to
the baseline, but **the child made no edits and did not complete the task**.
This does
not show a stable improvement in reliability or compare with plain Codex;
the 24K context is deliberately smaller than default 128K. Both trials are
closed; no additional runs are planned merely to improve the score.
Private receipts: `/var/tmp/ash-r11-boltons-control-20261114-r01/result.json`
and `/var/tmp/ash-r11-boltons-continuation-20261114-r01/result.json`.

**Cross-task tool-interface follow-up:** R8 independently recorded a rejected
`search_text(context_lines=5)` invocation; R11's continued turn also tried
an over-limit `context_lines` invocation. Both were safely refused, but the
tool already caps contextual search results separately (24,576 bytes hard,
16,384-byte default) with 512-character context previews. A deliberately
narrow change now permits `context_lines=0..5` instead of `0..3`, while
preserving the same capture bounds, zero-context default, path/permission
safety and match counting. The separately maintained Explorer offered/schema
validator must update its **exact** recognized maximum at the same time and
still reject forged broader schemas. This addresses invalid but useful tool
requests; it is **not a fix for R11's opaque reasoning-context exhaustion**
and must not be credited as a measured coding pass-rate gain.

**Excluded candidate:** A preliminary `python-dotenv` task for escaping
backslashes passed its own red-original/green-reference oracles, but the
upstream pinned checkout contained symlinks. The repository evaluator
correctly rejected it before any model request; no checkout safety rule was
relaxed and that non-runnable task was not added to the tracked portfolio.

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

**Now:** R5, R8 and R9 have positive paired outcomes, each a single observation;
none isolates a source change's impact. R10 exposed a genuine complexity and
context-pressure limit in a seven-module refactor, but both the 170s and 300s
pairs are complete; **park it instead of chasing a green result**. A safe
opaque-state stop is expected behavior, not permission to discard provider
reasoning or replay tool actions. Future evaluation may deliberately test the
existing opt-in safe context-restart path on a separate frozen scenario with
its own time and cost budget. R3 remains parked, R6's prior provider stall is
unresolved as a causal coding result, and R7's resource-limited runs remain
distinct findings. Next select a genuinely new owner-facing coding workflow
or an independently demonstrated production defect, with attention to
larger-codebase navigation, dependency compatibility, safe recovery and
verification. **Ash Crew v1 is complete and merged into `main`; there are no
remaining Crew acceptance gates in the active portfolio.**

**Don't conflate:** passing filesystem/provider regression tests proves those
subsystem contracts; it does not prove that Ash's overall coding success rate
rose. Conversely, failure of one hard coding fixture does not invalidate a
separately verified security or transport fix.
