# Ash coding evaluation portfolio

**Owner decision (2026-11-12):** Shift from repeatedly optimizing a single
failed benchmark to breadth-first, evidence-driven coding improvement with
selective deep dives. This is a maintained **inventory and decision protocol**,
not a claim that all scenarios have been run or passed.

## First portfolio: existing cases, not invented benchmarks

R1–R16 are pinned repository tasks. Some share an upstream project.
S1–S6 are synthetic tasks.
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
| R9 | `tomlkit-comment-control-validation` | Pinned real repo | Three-module TOML comment validation across standalone, item and array APIs; control-character rejection and mutation safety | **Three separate sequential 65K pairs: both engines passed all three**; latest Ash 120.72s / 10 model requests / 26 tools, Codex 83.71s / 3 non-test commands; not a controlled before/after, policy-parity caveat |
| R10 | `pluggy-role-module-split` | Pinned real repo | Seven-module role-based refactor; old import shims and public plugin behavior, wrappers, firstresult, historic replay and subset callers | **Park after two matched pairs**; at 170s both timed out/incomplete; at 300s Ash stopped on protected opaque-context exhaustion after 4/7 files, Codex changed all seven and passed both oracles but timed out without executing the visible test. Neither a complete coding pass |
| R11 | `boltons-bulk-ordered-collections` | Pinned real repo | Two-module ordered-container repair: multi-list index boundaries, insertion and tail handling, and linear-time ordered-set bulk removal | **Controlled Ash-only comparison completed:** no restart 85.33s, 3 requests, 0 edits, context error; one restart 113.86s, 5 requests, actual safe restart, 1/2 edited *before* restart, no edits in child, second context error. Both fail oracles; audits/scopes intact; no verified coding progress after restart |
| R12 | `rich-soft-wrap-background-style` | Pinned real repo | Two-module Rich terminal-rendering bug: prevent background/style bleed across physical newlines under `soft_wrap=True`, while preserving multiline, blank-line and styled text behavior | **First 65K sequential pair: both pass**; Ash 105.80s / 10 model requests / 23 tool calls, Codex 98.29s / 5 non-test commands + visible test; one trial, policy-parity caveat |
| R13 | `moreitertools-peekable-reverse-slice` | Pinned real repo | Prevent `peekable` from exhausting an infinite or large finite iterator when reverse slicing with a known start and omitted stop; preserve caching and genuine right-relative indexing | **First sequential 65K pair: both pass**; Ash 69.16s / 6 model requests / 6 tools / 0 rejects, Codex 53.46s / 3 non-test shell commands; one observation and native policy-parity caveat |
| R14 | `toolz-partition-invalid-length` | Pinned real repo | Detect an iterable's incorrect reported length rather than silently truncate a partially filled partition; preserve correct normal and no-length iteration | **First sequential 65K pair: both pass**; Ash 88.85s / 8 model requests / 8 tool calls (one safe edit rejection), Codex 104.25s / 4 non-test commands; one sample, with policy-parity limits |
| R15 | `click-clirunner-context-settings` | Pinned real repo | Extend Click's `CliRunner.invoke` public API with context-settings overrides that avoid parameter-name collisions while preserving existing invocation behavior | **First sequential 65K pair: both pass**; Ash 113.51s / 12 model requests / 15 tools (one rejected regex), Codex 86.05s / four non-test shell commands; one sample and native-policy caveat |
| R16 | `pyflakes-functional-typing-fields` | Pinned real repo | Python static analysis in Pyflakes. Track name uses in runtime `TypedDict` and `NamedTuple` field expressions while preserving annotation handling | **NOT YET SCORED**. Independent trusted-host preflight passed (red/red base, green/green fix, checker-only repair passed). |
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
Reliable comparisons still require repeated trials and broader workflow coverage.

**R16 provenance.** Pyflakes adds Python static analysis as a distinct source
category. The October 7, 2026 fix addresses false unused-import warnings when
the second argument to functional `TypedDict` or `NamedTuple` uses an imported
name in a runtime expression. The pinned base is
`9f0a7f9c8feb4d086a918a708bb778b38ce7a248` and the pinned fix is
`5f2af952d81a8bead32d3ab91a60f7efbc6d8aea`.
[Pyflakes issue #906](https://github.com/PyCQA/pyflakes/issues/906) and
[fix #907](https://github.com/PyCQA/pyflakes/pull/907) document the change.

Only `pyflakes/checker.py` is editable. The upstream fix also changes
`pyflakes/test/test_type_annotations.py`, which remains protected in the
fixture. Independent trusted-host preflight on both SHA-verified upstream
trees passed: the baseline failed both visible and hidden oracles (1/1), the
upstream fix passed both (0/0), and substituting only the allowlisted
checker module into the base also passed both (0/0). The source-only repair
passed the edit-scope check, while a simulated edit to the protected upstream
test was rejected. The run used exact pinned Git revisions pre-fetched on the
trusted host (rather than nesting Bubblewrap inside Codex's workspace-write
sandbox). No live Ash or plain-Codex model trial has been run for R16.

**R15 provenance and preflight (2026-11-17):**
The independently selected [pallets/click PR #3896](https://github.com/pallets/click/pull/3896),
merged November 6, 2026, adds an optional `context_settings` mapping to
`CliRunner.invoke`. The feature permits root Context settings such as `color`
to be passed separately from parameters controlling the runner itself, while
preserving existing extra keyword arguments and allowing explicit context
settings to override command defaults. Pinned original
`ac0e297ec2d7d78455a7d18ce306996e668c4a85` and reference PR head
`90a2973f85fffc4d8a20effb2bcdff3f394c5e62` differ in five files:
`src/click/testing.py`, upstream tests, changelog, and docs. Only the
production module is editable. Separately authored standard-library-driven
visible and sealed hidden scripts cover context color and terminal width,
declared-default precedence, root-to-subcommand inheritance, default-map
option values, legacy `**extra` forwarding, empty overrides, and preservation
of the caller's mapping. The **official fresh pinned-fetch preflight** confirms
both tests fail on the original, both pass on the reference, no unsafe tree
entries, and only `src/click/testing.py` must change. This is validity
evidence, **not a model coding result**. Private preflight receipts are under
`/var/tmp/ash-r15-click-feature-preflight-20261117/`.

**R15 first frozen Ash/plain-Codex coding pair (2026-11-17):** With Ash
`16ab78a`, ran exactly one sequential pair on the unchanged R15 fixture,
with a 65,536-token request context, 170-second per-engine deadline, and
GPT-6 Luna xhigh requested for both. The independent comparison verified
identical pinned source and reference commits, shared user prompt, visible
and hidden oracle sources, and requested model/effort/context. **Both passed**
the independently graded visible and sealed hidden checks, changed only
authorized `src/click/testing.py`, ran the required visible test, and
terminated normally. Ash **113.51s** (12 model requests, 15 tools, one
invalid-regex search safely rejected and recovered, 109.324s completed model
time, 0.970s completed tool time, valid session audit); plain Codex **86.05s**
(four non-test shell commands). Codex was **27.46s faster in one sample**;
this is neither a stable ranking nor evidence of a causal harness defect.
Native shell authorization, network isolation, system prompts, and skill
stacks remain unverified as equivalent across engines. Private receipts:
`/var/tmp/ash-r15-click-feature-paired-20261117-r01/`.

**R14 provenance and offline preflight (2026-11-17):** A new independent
source project, [pytoolz/toolz PR #603](https://github.com/pytoolz/toolz/pull/603),
merged October 16, 2025, corrects `partition_all` producing the wrong trailing
chunk when an iterable reports a length inconsistent with its contents. The
pinned original `08f2604f599847272d801c7f0467451de59fa33a` and upstream
reference `defe68c33ddaa2c2a9b3d4e53818a1fbe63c3f85` differ in exactly
`toolz/itertoolz.py` and upstream `toolz/tests/test_itertoolz.py`; only the
implementation is editable. Separately authored visible/hidden standard-library
checks cover overstated and understated lengths across different partial
partition sizes, preservation of valid full and partial chunks, empty inputs,
and iterables for which `__len__` raises `TypeError`. Both checks **fail on
the original and pass on the pinned reference** in isolated source trees.
This is benchmark validity evidence, **not** an agent coding pass or an
improvement in Ash reliability. Private source/oracle preflight files are in
`/var/tmp/ash-r14-toolz-preflight-20261117/`.

**R14 first sequential Ash/plain-Codex pair (2026-11-17, one sample):**
At pinned Ash `a2421ec`, the existing evaluator ran the unchanged R14 fixture
sequentially, Ash then *plain, unmodified Codex*, both requesting GPT-6 Luna
xhigh with 65,536 context tokens and 170 seconds per engine. The comparison
receipt independently verifies the same source and reference commits, shared
user prompt, identical visible and hidden oracle sources, requested model and
effort, and matched request-context limits (`comparison_verified=true`).
**Both engines passed both independent oracles**, edited only
`toolz/itertoolz.py`, executed the authorized visible test and completed their
CLI turns. Ash took **88.85s** (eight completed model requests, eight tool
calls, **86.989s** of completed model time, **0.227s** of tool execution) with
one `replace_file_content` rejected for an exact-line mismatch; it reread the
file, recovered, passed the test and verified its session audit. Plain Codex
took **104.25s**, used four non-test shell commands, and terminated normally.
Ash was **15.40s faster in this one pair**, not evidence of a consistent
speed or quality advantage. Equivalent native sandbox enforcement, approval,
network restrictions, system prompts and skill-stack isolation remain
unverified across the engines. The final output and protected-file checks
passed, and both runners cleaned their isolated scratch resources. Private
receipts: `/var/tmp/ash-r14-toolz-paired-20261117-r01/`.

**R13 provenance and reproducible preflight (2026-11-16):** This new task
uses real upstream
[more-itertools PR #1322](https://github.com/more-itertools/more-itertools/pull/1322)
to repair `peekable` reverse slices with a nonnegative start, negative step,
and omitted stop. The pinned original is
`4aeb4338ae1407d8da9723e86da99efeb446f719`; the reference fix is
`69dbc3392928afe1cc892a387bf0e721522f21c2`. Only
`more_itertools/more.py` is editable; upstream tests and documentation are
protected. Independently authored, no-network/no-dependency visible and
hidden tests require a bounded number of reads from a guarded infinite
iterator, correct repeated reverse slicing after a partially cached prefix,
no eager full-source caching on a large finite sequence, preserved normal
iteration, forward slicing, and right-relative negative-index behavior.
Both tests **fail on the original and pass on the reference**, and the
official pinned preflight confirms only the permitted production module
needs modifying, with no unsafe symlinks or special files. This establishes a
valid independent evaluation fixture. Private preflight evidence:
`/var/tmp/ash-r13-moreitertools-official-preflight-20261116/`.

**R13 first sequential Ash/plain-Codex pair (2026-11-16, one observation):**
At pinned Ash `fbf7e92`, the pair ran the unchanged, preflighted R13 task
with requested GPT-6 Luna xhigh, 65,536 request-context tokens, 170-second
per-engine deadline, and normal uncapped Ash cumulative turn budget. The
runner independently verified **all six SHA-256 commitments** (source base,
reference fix, task prompt, user prompt, visible oracle, hidden oracle) in
both Ash and Codex receipts and reported `comparison_verified=true`. Both
engines **passed both independent tests**, edited only
`more_itertools/more.py`, ran `python3 -B qa_visible.py`, preserved protected
files, and terminated normally. Ash passed audit verification in **69.16s**,
with **six model requests, six successful tool calls** (two source reads, one
search, one exact replacement, one Git diff, one visible test), **zero failed
calls**, 53,287 prompt and 2,779 completion tokens. Isolated, unmodified
plain Codex passed in **53.46s**, using three non-test shell commands and
the visible test, with private auth cleanup verified. Codex was faster by
15.70s in this one pair, **not** a stable ranking. Native permission, shell,
skill-stack and network-sandbox parity have not been independently proved,
and the engines' token accounting differs. The clean Ash trace gives no
indication of a harness-specific defect requiring a speculative fix. The
R13 success does **not** establish a coding completion-rate improvement from
the preceding tool-guidance changes. Private paired receipts:
`/var/tmp/ash-r13-moreitertools-paired-20261116-r01/`.

**R12 provenance and independent preflight (2026-11-16):** A new coding task
uses real upstream [Rich issue #3838](https://github.com/Textualize/rich/issues/3838)
and [merged fix PR #3938](https://github.com/Textualize/rich/pull/3938).
Pinned original `Textualize/rich` commit
`05ff970926cbd0b5d0acb6fc18cdbd1fc971e582` and post-fix source
`2c247e6da72dfa69dd6148f1a0657e2440d1ebe9` differ in exactly the
two authorized runtime modules `rich/console.py` and `rich/segment.py`,
plus upstream tests and changelog, which remain protected. Independently
authored, standard-library-driven checks exercise ANSI reset placement before
physical linefeeds, successive separately styled lines, multiline and blank
text, nested `Text` styles, plain output and non-soft-wrapped behavior.
The explicit TERM override is inside each oracle because the isolated runner
uses `TERM=dumb`; `force_terminal=True` alone does not guarantee ANSI output
under that terminal setting. Fresh official pinned-fetch preflight found no
symlinks/special files, **both visible and hidden failed on the original and
both passed on the reference**, and verified both editable modules actually
changed. This is a distinct real-repository terminal coding problem, **not a
benchmark result** until the model execution and independent grading finish.
Private preflight: `/var/tmp/ash-r12-rich-official-preflight-20261116/`.

**R12 first sequential pair (2026-11-16, one sample):** With Ash
`a1d150d`, both Ash and **plain, unmodified Codex** completed the frozen
Rich bug repair, ran the required `python3 -B qa_visible.py`, passed both
independent oracles and changed exactly `rich/console.py` and `rich/segment.py`,
without modifying protected files. The comparison receipt independently
matched task objective, prompt, reference SHA, oracle hashes, requested
`gpt-6-luna` xhigh model/effort, 65,536-token context, 210-second deadlines,
native plain Codex, and temporary authorization isolation and cleanup.
Ash: **105.80s CLI, 10 model requests / 23 tool calls, valid audit**;
Codex: **98.29s CLI, five non-test shell commands and one required test**.
Ash's model requests took 101.345s and its completed tool executions 0.911s.
Two Ash `search_text` calls were rejected (an invalid regex, and a request for
10 context lines against the then-5-line maximum); Ash recovered and completed.
Codex's 7.51s advantage is a single outcome, **not** a stable ranking.
Actual permission, shell-policy and network-sandbox parity is not independently
verified; model token accounting differs. A later, separately tested search
interface change permits context lines 0–10 while retaining existing hard
output budgets. **That later interface change has not been tested with a
model** and is not credited with a success-rate or latency improvement.
Private exact receipts:
`/var/tmp/ash-r12-rich-soft-wrap-paired-20261116-r01/`.

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

**R9 edit-selection guidance trial (2026-11-16, third separate paired
observation):** The previous R9 trace contained **three** rejected
`replace_file_content` calls using stale `expected_sha256` values after prior
successful writes, then additional reads and repairs. At `534ca18`, the
large-*effective*-context coding guidance explicitly recommends discovering
atomic `replace_file_edits` for multiple changes to one file and `apply_patch`
across modules, instead of reusing a pre-edit SHA-256. The default 4K prompt,
tool exposure, permissions, workspace guards and stale-hash enforcement did
not change. The prompt addition was tested red-first and locally passed
**590** loop/context/patch/discovery/real-session and fresh-process CLI
regressions; Ruff and mypy (237 source files) also passed.

One fresh sequential 65K pair of the **unchanged, frozen R9 task** used GPT-6
Luna xhigh, 170s per engine, uncapped Ash cumulative turn tokens and the same
attested task input, pinned upstream commits, visible/hidden oracle SHA-256,
test command and independent strict grading. Both passed, edited exactly
`_utils.py`, `api.py` and `items.py`, executed the visible command, preserved
protected files, and completed with valid Ash audit / isolated Codex events.
Ash: **120.72s CLI, 10 completed model requests, 26 tools, zero failed edits,
187,950 prompt / 3,661 completion tokens**. Plain Codex: **83.71s CLI, three
non-test shell commands and the required test**. Codex was faster by 37.01s
in this single pair. The model **did not select either batch editor**: it
made seven successful individual `replace_file_content` calls. The change
may have helped planning, but the observed zero stale failures cannot be
causally attributed to guidance because the earlier first R9 run also had
zero failures, and neither prompts nor sampled model choices were randomized
across revisions. This outcome **does not demonstrate a higher completion
rate, lower stable latency or strict shell/network/sandbox parity**. Do not
repeat this fixture just to chase a favorable number. Private receipts:
`/var/tmp/ash-r9-atomic-guidance-paired-20261116-r01/`.

**Parity caveat across R9 pairs:** The checker accepted requested model,
effort, context, prompts, pins and independent grading, **not** actual
tool/permission/sandbox equivalence. Codex's receipt does not independently
verify the same network blocking or enforce the same exact shell-command
policy as Ash. Codex input-token counts include cached input and cannot be
directly compared to Ash's prompt-token accounting. These pairs do not prove
a stable throughput gain or causal improvement from an Ash code change.

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

**Now:** R5, R8, R12 and R13 have positive single paired outcomes; R9 has three
sequential paired passes at different Ash revisions. None isolates a source
change's impact or establishes a stable latency distribution. R10 exposed a
genuine complexity and context-pressure limit in a seven-module refactor, but
both the 170s and 300s pairs are complete; **park it instead of chasing a green
result**. A safe
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
