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
