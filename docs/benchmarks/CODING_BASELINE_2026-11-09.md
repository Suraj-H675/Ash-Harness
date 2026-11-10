# Coding-agent baseline — 2026-11-09

Historical, **small-sample** evaluation on Arch Linux, before a production
coding-harness change. Recorded Ash HEAD: `8622816` (`main`). Task fixtures,
bounded execution, and independent scoring are maintained in
[`devtools/coding_benchmark.py`](../../devtools/coding_benchmark.py); usage and
limitations are in [`CODING_EVALUATION.md`](../guides/CODING_EVALUATION.md).

## Environment and scoring

- Model: Qwen3.5-4B Q4_K_M, local GGUF SHA-256
  `00fe7986ff5f6b463e62455821146049db6f9313603938a70800d1fb69ef11a4`.
- Runtime: llama.cpp b11552, CPU-only, 8 threads, 16,384-token context,
  loopback native-tool provider. This is not a strong-model comparison.
- Ash: one-shot CLI in `auto_edit`, isolated per-task home/database/workspace;
  only exact test commands explicitly granted. Bubblewrap sandbox verified.
- Grading: workspace artifact, external behavioral oracle, tool record,
  protected files, audit integrity, and **CLI completion within the configured
  200- or 285-second task deadline**.
  Model claims of success do not count as proof. Failures of a visible test and
  hidden checks are scored separately.

## Observed baseline

| Task and run | Code correct | Entire trial passed | Time | Evidence |
|---|---|---|---|---|
| Exact file | Yes | Yes | 63.55s | `write_file` succeeded |
| Write + execute Python | Yes | Yes | 73.15s | `write_file`, `run_command` succeeded |
| Repair test A | Yes (visible test) | Yes (original rubric) | 196.78s | Two denied out-of-scope reads; correct patch and test |
| Repair test B | Yes (visible + hidden) | Yes | 180.72s | Same two denied reads; correct patch and test |
| Multi-file percentage fix | Yes (visible + hidden) | **No** | 286.79s | Correct edit and command, but CLI timed out before completing |

The initial repair-A rubric did not run the later-added hidden oracle, though
its recorded edit was `return a + b`. The repair-B and multi-file trials used
hidden oracles. The multi-file timeout was **not** counted as a pass despite
its correct final workspace and verified audit.

The two repair baselines both tried two invented `/workspace/...` paths before
recovering. Ash denied those reads; the file boundary was not bypassed.

## Controlled probes and decision

Two low-cost interventions were tested independently and then **reverted**:

- A default-system-prompt hint to use actual project-relative paths: repair
  still had two denied reads, 198.8s, and one extra denied command attempt.
- A `read_file.file_path` schema description giving the same guidance: repair
  still had two denied reads and then stopped without editing; its hidden and
  visible tests failed (83.82s).

These do not establish a useful improvement; changing the production prompt
or tool schema on this evidence would be speculative. The original code was
restored. Future experiments should preserve the same model and task contract,
use more independent trials and stronger models where authorized, and inspect
actual request/tool/event trajectories before modifying the harness.

The durable outcome of this batch is the **evaluation harness and honest
scoring**, not a claimed boost in coding performance. The sample is too small
to estimate general reliability or infer an Ash defect from local-model
behavior. Private raw run receipts were kept outside the repository under
`/var/tmp/ash-coding-eval-20261109/` on the test host.

## Follow-up: GPU runtime and parallel-read recovery (2026-11-09)

The same GGUF was also exercised through llama.cpp **b12223**, with Vulkan
offload to the NVIDIA RTX 3050 6GB Laptop GPU, 4 CPU threads, and the same
16,384-token context and tool protocol. This is a **different runtime and
hardware path** from the CPU baseline above; the following comparisons must
not be attributed solely to Ash code changes.

| Trial | Ash revision | Result | Wall time | Observed |
|---|---|---|---:|---|
| GPU marker smoke | `9805dfc` | Pass | 30.54s | Actual write tool succeeded; two model requests |
| GPU multi-file 1 | `dbd0ed7` | Pass | 24.76s | Five model requests; no rejected edits |
| GPU multi-file 2 | `9805dfc` | Pass | 29.08s | Seven requests; one rejected edit and recovery |
| GPU multi-file 3 | `9805dfc` | Pass | 28.95s | Seven requests; one rejected edit and recovery |
| GPU multi-file 4 | `dbd0ed7` | Pass | 26.21s | Seven requests; one rejected edit and recovery |

The edit-feedback change did **not** demonstrate a measured improvement: both
revisions passed 2/2, and the small sample favored the older revision on
latency. The guard/error behavior remains independently valuable. These runs
share a serving process/cache and are not statistically independent.

The newly added `two-file` fixture is a genuinely coordinated repair requiring
both `billing/tax.py` and `billing/receipt.py` to change, with a protected
visible test and independent hidden oracle. Its first GPU trial on `9805dfc`
failed in 9.07s after **one model request**: four invalid out-of-scope reads
in one parallel batch hit the three-failure circuit breaker before the model
could receive all results.

With the breaker corrected to score a parallel-safe, read-only batch as one
failed attempt (working tree based on `9805dfc`), the corresponding GPU trial
ran **seven model requests** over 20.92s. All invalid paths remained denied,
and no source files were changed. It ultimately stopped on the 40,000-token
turn budget after repeatedly proposing forbidden paths, so **the full two-file
task still failed**. This demonstrates narrower recovery progress, not task
success or a generalized coding-performance improvement. Sequential operations
retain immediate breaker behavior, and the circuit trips after consecutive
failed parallel batches. Raw receipts remain private under the same external
evidence directory; the model server was explicitly shut down after trials.

## Follow-up: model-visible path refusal (2026-11-09)

An additional local trial used the same Qwen GGUF and Vulkan serving binary,
with a working-tree change to return **known, actionable scope refusals** from
read-only discovery tools rather than treating thrown validation errors as
ambiguous external effects. `read_file` also advised using project-relative
paths instead of guessing `/workspace`. The guard still rejected every
out-of-scope request without normalizing it into an authorized path.

The `two-file` replay **failed** after 31.50s and six model requests: the
40,000-token turn budget stopped it before it could repair both files. Unlike
the preceding run, the model eventually issued a successful workspace listing
and **four successful file reads**, then attempted an invalid edit and reread
the source. Both sources remained unchanged. This demonstrates a successful
path-recovery sequence **within one failed run**, not a controlled success-rate
improvement. The serving process and all benchmark child processes were shut
down after the trial. Evidence is retained privately at
`/var/tmp/ash-coding-eval-20261109/gpu-two-file-scope-feedback/`.

## Follow-up: separate turn-budget and context-window limits (2026-11-09)

The isolated evaluation runner imposed **two limits not representative of Ash's
normal turn policy**: 40,000 cumulative input/output tokens across one turn,
and 8,192 tokens of per-request context, despite the tested llama.cpp server
having a 16,384-token context window. Normal Ash defaults to **no turn-wide
token cap**. Its history compactor preserves the active turn and tool-call
relationships, rather than silently discarding them to fit a small window.

The runner now records and accepts distinct `--turn-token-budget` and
`--context-tokens` settings. The old 40k/8k defaults remain available for
historical comparisons. The new probes kept the same Qwen3.5-4B Q4_K_M model,
llama.cpp b12223 Vulkan runtime, task, permission grants, independent oracle,
and Ash runtime code from `2dc2ba4`; only evaluation limits changed between
these two new runs:

| Turn total / request context | Result | Requests | Time | Evidence |
|---|---|---:|---:|---|
| 80k / 8k | **Fail** | 7 | 40.96s | Both sources fixed; visible and hidden oracles pass, but a protected-current-turn context overflow occurs before the required test command |
| 80k / 16k | **Pass** | 8 | 43.53s | Both sources fixed, allowed `python3 -B test_billing.py` executed successfully, visible/hidden oracles pass, unrelated files unchanged, audit intact |

The 80k/16k trial required **49,639 estimated prompt tokens** and 1,524
completion tokens across eight requests, not a 49k-token single model input.
It still tried four forbidden paths; all were rejected, and the model corrected
its path use. It did not need an exception to the permission policy. The
successful trial's evidence is under the private
`/var/tmp/ash-coding-eval-20261109/gpu-two-file-budget80k-context16k/` path.
The 80k/8k evidence is in the adjacent `gpu-two-file-budget80k/` folder.

These **single-trial, non-randomized, sequential** observations demonstrate
that the restrictive benchmark budgets were real blockers in those runs. They
do **not** establish a statistically reliable success rate, causal latency
improvement, or justify increasing the token/context limits silently for real
users. No production context-compaction code or runtime defaults changed.
Both model servers were stopped after the tests.

## Original three-module event-log challenge (2026-11-09)

The benchmark now also includes an **original synthetic** `event-log` task,
designed to probe slightly broader repository editing without copying a public
benchmark's memorized issue/patch. The agent must repair a JSON-lines parser,
an aggregation function, and a deterministic report renderer. The task requires
all three modules to change and the exact permitted test command to execute.
Visible tests, independent hidden behavior checks, protected fixture bytes,
and session audit integrity are all required. The oracle was verified to fail
on the initial fixture and partial repairs, pass a complete three-module fix,
and reject a fix hard-coded for the visible example.

One real Ash CLI trial used Qwen3.5-4B Q4_K_M, llama.cpp b12223 with Vulkan,
a **16,384-token context window**, **80,000 cumulative turn-token budget**,
and the unchanged production loop at `0795e89`:

| Metric | Observed result |
|---|---|
| Overall pass | **No** |
| Wall time / model requests | 59.69s / 10 |
| Estimated prompt / completion tokens | 75,883 / 1,768 |
| Out-of-scope path calls | 7, all refused |
| File modifications | All three required source files changed |
| Visible / hidden tests | Both failed; the parser still dereferenced `None` |
| Required test command | Not executed; the model added an unauthorized `cd` prefix, and permission policy denied it |
| Protected fixture files / session audit | Intact / valid |
| Stop condition | Reached 10 model-request iterations without successfully finishing |

The result is a genuine **failed coding trial**, not a regression proved in
Ash's runtime or a reason to relax path/command permissions. This single run
demonstrates that the new fixture distinguishes a harder workflow than the
previous passing `two-file` smoke; it does not estimate general success rates.
The evidence stays local under
`/var/tmp/ash-coding-eval-20261109/gpu-event-log-v1/`, and the benchmark's GPU
model server was stopped after the trial.

## Follow-up: command working-directory guidance (2026-11-10)

The original event-log trial added a `cd` prefix to its otherwise authorized
test command, which made the call fail exact-command permission matching. A
small model-facing change now states that `run_command` already starts in the
project root, and explicit command strings must be passed verbatim. It also
repeats the preference for project-relative file paths. Permission matching,
working-directory selection, and side-effect handling remain unchanged. A
deterministic runtime regression verifies that a prefixed command is refused
without execution while the exact command runs from the project root.

One exploratory rerun with the same Qwen3.5-4B GGUF, llama.cpp Vulkan runtime,
80k-turn/16k-request limits, and event-log task **did not complete**. It
executed four model requests in 29.07s, refused six out-of-scope reads, then
successfully listed the workspace and read six files. The next provider stream
failed validation (`provider emitted an invalid stream chunk`) before any edit
or test command. The visible and hidden oracles therefore still failed; file
protections and session audit checks passed. This early provider failure makes
the single replay **inconclusive** as a comparison of command-use or coding
success, rather than evidence that the guidance made the model faster or worse.
The local evidence path is
`/var/tmp/ash-coding-eval-20261109/gpu-event-log-command-guidance-v1/`.
The model server was stopped after evaluation.

## Follow-up: complete tools, incomplete specification (2026-11-10)

One additional **unmodified-current-runtime** `event-log` CLI trial ran on
`e14495c` with the same Qwen3.5-4B Q4_K_M GGUF, llama.cpp b12223 Vulkan
server, 80,000 cumulative turn-token budget, and 16,384-token request context.
It completed in **58.95s**, using **10 model requests**, 72,329 estimated
prompt tokens, and 1,915 estimated completion tokens. All six initial source
reads succeeded with project-relative paths, and there were **zero
out-of-scope path calls**. It changed all three required modules, preserved
all protected files, and passed the audit-integrity check. Its first test run
failed; after a further edit, the exact authorized
`python3 -B test_eventlog.py` command ran successfully. The CLI turn ended
normally with `stop_reason=completed` (not a context, token, or iteration
limit); the final model message asserted that all three modules were fixed.
Before its first successful edit, the model submitted identical old/new text to
`replace_file_content` and then used `write_file` on an existing file without
`overwrite=true`. Both calls were refused without rewriting the file, and the
model recovered. Their model-visible schema descriptions have since been
clarified; no overwrite or replacement behavior was changed.

**The externally graded task nevertheless failed.** The visible oracle passed,
but the hidden oracle failed when given JSON arrays: the resulting parser used
`if event and event.get('request_id')`, so a nonempty array raised
`AttributeError` rather than being ignored as the README required. The task's
explicit requirement to ignore non-object JSON was missed. A deterministic
regression now verifies that this near-correct, visible-test-passing parser
remains rejected by the hidden oracle. The saved evidence is private under
`/var/tmp/ash-coding-eval-20261110/gpu-event-log-current-main/`, and the
locally owned model server was shut down after the run.

This establishes a useful distinction: **path discovery, authorized command
execution, editing, and durable audit can all work while the model still misses
specified edge cases**. It does not demonstrate a general coding success-rate
improvement over earlier trials or prove a new Ash runtime defect. The
`e14495c` directory-scoped discovery change was not actually exercised by this
root-file-reading task, so it cannot be credited for the outcome. Ash already
instructs the model to inspect requirements and edge cases beyond passing
tests, and the session/activity APIs expose executed command outcomes. Do not
raise production iteration limits, add redundant completion gates, or make the
benchmark easier based on one 4B-model failure. A stronger-model or broader
multi-task comparison is required to justify runtime changes aimed at this
semantic completion gap.

## Edit-schema replay and native tool-call failure (2026-11-10)

One bounded follow-up on `b75a154` evaluated `two-file` and `event-log` with
the identical Qwen3.5-4B Q4_K_M GGUF, llama.cpp Vulkan b12223, 80k cumulative
turn budget, and 16k request context used in the previous trial. It used
separate disposable workspaces and retained its private evidence at
`/var/tmp/ash-coding-eval-20261110/gpu-edit-schema-b75a154/`. The model
server was stopped when both trials ended.

| Task | Scored result | Observed boundary |
|---|---|---|
| `two-file` | **Fail**, 2 requests, 21.99s | Four allowed source reads succeeded, then the provider emitted `arguments:value_error` while generating the next native tool call. Ash rejected the malformed stream; zero edits or test commands ran, no forbidden paths were accessed and the audit remained valid. |
| `event-log` | **Fail**, 10 requests, 58.71s | The model changed all three modules, used the exact allowed test command successfully, and passed the visible test, but the hidden oracle still rejected list-valued JSON because the parser called `.get()` on a non-object. Protected files and the audit remained valid. |

Critically, the `event-log` model **again** first submitted an identical
`target_content`/`replacement_content` no-op and then attempted `write_file`
on an existing file without `overwrite=true`. Both were safely refused. The
new edit-schema wording therefore did **not** eliminate those mistakes in
this single replay. Results are not statistically comparable success-rate
estimates; the two-file case failed due to a different provider-stream event
and cannot be interpreted as evidence of an editing regression. The event-log
case confirms the existing visible-test-versus-specification failure.

The two-file failure exposed an actionable CLI diagnostic gap: the specific
`arguments:value_error` error was classified under the generic provider remedy
to check API keys and connectivity. Ash now reports a targeted, content-free
native-tool-argument remedy for this exact failure. A real loopback
OpenAI-compatible CLI integration test verifies malformed tool JSON is refused
without leaking the model-supplied fragment, dispatching a tool, or silently
replaying the partial stream. This **improves error guidance only**, not model
tool-call correctness, recovery or coding-task success.

## Eager patch-tool exposure: controlled local comparison (2026-11-10)

The `fe2b9f4` change promoted the existing `apply_patch` tool into the initial
native-tool catalog when the effective input budget was at least 6,144 tokens.
The tool itself, its permissions, and its discoverability through `search_tools`
were already present. To evaluate whether the additional eager schema helped,
we compared the unchanged `23d93e7` runtime with `fe2b9f4` using separate
detached Git worktrees and isolated Ash homes, databases, and task workspaces.

Both revisions used the same Qwen3.5-4B Q4_K_M GGUF, llama.cpp Vulkan b12223
on the same local server, 16,384-token request context, 80,000-token turn
budget, 384-token completion cap, and the unchanged independent task oracles.
Two runs per task were followed by an alternating additional `two-file` run
against each revision. The results were:

| Task | `23d93e7` (deferred patch) | `fe2b9f4` (eager patch) |
|---|---|---|
| `two-file` | **3/3 passed**; successful edits, test, hidden oracle, and audit | **0/3 passed**; all three aborted with `arguments:value_error` immediately after four successful source reads, before edits or tests |
| `event-log` | **0/2 passed**; hidden/visible oracle failures | **0/2 passed**; visible test passed but hidden oracle failed |

No `apply_patch` calls were issued in the five eager-tool trials. The extra
schema did not reduce tool-discovery turns for these tasks, while the observed
`two-file` completion rate regressed. In all malformed-response trials Ash
refused the invalid provider output without tool dispatch or automatic replay.
The original patch tool remains available through `search_tools`; no policy
relaxation is needed to restore its deferred presentation.

These are **small, single-model, non-randomized** trials sharing a model server
and KV cache, not an estimate of general reliability or proof of a causal
mechanism. The comparison is nevertheless sufficient to reject eager patch
exposure as an *unverified default improvement* on the tested local-model
configuration. The change was reversed pending stronger-model and broader-task
evidence. Private receipts are retained under
`/var/tmp/ash-eval-20261110-head23d93e7/` on the test host; the evidence tree
and model-generated task files are not tracked.
After restoring the deferred-tool runtime on `main`, one additional `two-file`
CLI trial passed in 33.17s, including the authorized test command, hidden
oracle, and audit-integrity check. This confirms the restored user path works
in the tested environment, not that larger-model performance is established.

## Free remote coding baseline and one-shot exit status (2026-11-10)

With `5e878b5` checked out, Ash ran the real `event-log` coding task against
`openrouter/cohere/north-mini-code:free`. The public OpenRouter catalog
advertised zero prompt/completion prices, native tools, and sufficient context;
the credential was passed only in the isolated model process environment.
This was **one trial**, not a strong-model or cross-harness comparison.
The run used a 32,768-token request context, 80,000-token turn budget, and
2,048-token completion cap. It made 16 provider requests in 42.19s
(43.48s including setup and grading), using 61,436 prompt and 3,620
completion tokens. The model read requirements, attempted the authorized test,
edited two of the three required modules, and stopped before repairing the
renderer or verifying the changes. Both the visible and hidden oracles failed;
protected files and the audit remained intact. The score was **0/1 passed**.
Private evidence is under
`/var/tmp/ash-openrouter-cohere-eventlog-20261110/`.

This trial also exposed a separate **CLI automation defect**: the model hit
the configured 16-iteration limit and the JSON completion event correctly
reported `stop_reason=iteration_limit`, but the process exited with status 0.
A deterministic real-CLI regression then reproduced the same misleading exit
status in text, JSON, and streaming JSON. One-shot CLI exit status now reports
failure for iteration exhaustion, turn-token exhaustion, and circuit-breaker
stops, while preserving the terminal event, saved session, and audit.
This improves **completion signaling**, not model quality or coding-task success.
