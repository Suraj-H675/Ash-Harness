# Exploratory h11 harness comparison — 2026-11-11

**Status: first matched requested model/effort, not a controlled harness
ranking.** Both agents worked on the real
[python-hyper/h11](https://github.com/python-hyper/h11) source pinned to
`31e626c64e1e28db3cd73a6aa0ac057f1b915c18`, immediately before
the historical fix `dff7cc397a26ed4acdedd92d1bda6c8f18a6ed9f`.
The original failed both independent behavioral oracles; the upstream fix
passed both. Neither agent received the fixed checkout or hidden tests.

| Measure | Ash CLI | Codex CLI + Poteto |
|---|---|---|
| Requested model | GPT-6 Luna | GPT-6 Luna |
| Requested reasoning effort | xhigh (new one-shot `--reasoning-effort`) | xhigh (`model_reasoning_effort`) |
| Task | Historical h11 chunk footer validation | Same pinned bug and visible test |
| Run deadline | 170 seconds | 170 seconds |
| Final source scope | Only `h11/_readers.py` | Only `h11/_readers.py` |
| Visible regression | Pass | Pass |
| Hidden regression | Pass | Pass |
| CLI terminal success | **Exit 0; completed** | **Timed out**; no terminal event |
| Total wall-clock time | **83.90 seconds** | **170.00 seconds** |
| Usage reporting | 94,278 prompt / 2,654 completion tokens; 10 model requests | Not available; no completion receipt |

Ash executed its exact authorized `python3 -B qa_visible.py` command;
one file-edit attempt failed and was recovered. The session audit verified,
all protected files remained unchanged, and isolated tmpfs access-only
credentials and scratch workspaces were deleted.

Codex was launched with `codex exec --ephemeral`, workspace-write
sandboxing, a requested network-off setting and no automatic approvals. The
workspace was prepared separately from the same pinned upstream source;
Codex followed the locally installed **Poteto mode** workflow and executed
read-only inspection commands in addition to the visible test. It produced
correct source and passed both oracles **despite being terminated at the
deadline**. Its lack of a completed turn must not be misrepresented as a
coding-quality failure, and there is no trustworthy comparable prompt-token
total for Codex in this receipt.

The setup is **not controlled for** system prompts, installed skills,
multi-agent routing, tool schema/command affordances, full execution
containment, or request-context/turn-token limits. Both runs requested the
same model/effort, but provider inference internals and user-session
conditions were not independently verified to be identical. Only one
matched-requested-model trial per harness is available. This is useful
end-to-end evidence, **not** proof that Ash is faster or more capable in
general.

Evidence, retained privately and outside Git:
`/var/tmp/ash-h11-luna-xhigh-comparator-20261111/result.json` and
`/var/tmp/ash-codex-h11-comparator-20261111/grade.json` plus the separately
stored Codex command event log. The latter is not a full Ash-equivalent audit.

**Next gate:** For a stronger head-to-head, standardize tools and execution
policy as far as each CLI permits, control skill/plugin activation and
subagents explicitly, use identical prompt wording, and run multiple fixed
tasks with repeated trials. Keep scored coding correctness separate from
terminal completion and provider usage.

## Reusable comparison check

Run the local read-only receipt comparison without starting either model:

```sh
PYTHONPATH=src .venv/bin/python devtools/coding_comparison.py \
  --ash-evidence /var/tmp/ash-h11-luna-xhigh-comparator-20261111 \
  --codex-evidence /var/tmp/ash-codex-h11-comparator-20261111
```

The checker refuses mismatched task identifiers or upstream revision pins,
separates source correctness from CLI completion and requested model settings,
and makes unresolved parity conditions explicit. It prints only numeric and
boolean metadata plus a hash of the Codex prompt, not agent messages or
source content. This is **not a Codex runner** and cannot retroactively
verify the Codex sandbox, prompt equivalence, sealed-oracle access or actual
inference route. Those require a controlled future execution setup.

After this original hand-assembled experiment, the dedicated
devtools/codex_repository_benchmark.py runner was added to make future
Codex evaluations reproducible, including a preflight-only mode that
does not contact a model. It now defaults to plain Codex.
This does **not** retroactively upgrade the original Codex receipt's
trustworthiness or erase the differences in tool and sandbox policy.

**Updated comparison protocol:** Following the owner's explicit fairness
requirement, new trials use plain Codex without Poteto, ignore Codex
user configuration/rules, request suppression of host skill discovery,
and send the exact same user task bytes as an Ash run with `--fair-prompt`.
Both retain their native runtime/system instructions and tool safety
boundaries; the comparison receipt verifies actual user-prompt fingerprints.
All earlier Poteto results remain historical exploratory evidence only.

**First plain-Codex h11 trial (2026-11-11):** The pinned h11 fixture
passed red-original/green-reference preflight and was repaired by
plain Codex CLI using requested GPT-6 Luna xhigh, a 170-second deadline,
ignored user config and exec rules, and requested host-skill discovery
suppression. The full user-input SHA-256 was
`5567c1b06b83aa604c1b38bbaa6e60d32c1f0f05c8b3bc27e7b6554c92bf592e`.
It exited **0 in 56.61 seconds**, passed both external oracles, changed
only `h11/_readers.py`, executed the visible test successfully, and
cleaned the temporary source checkout. The JSONL included four completed
shell calls, two of which were not the exact test command; no Poteto
commands were observed. At that checkpoint a matching Ash run with
`--fair-prompt` had not yet been performed, so the 56.61-second
outcome must not be
compared as a controlled speed result against the older Ash 83.90-second
trial using different user input. Private evidence:
`/var/tmp/ash-plain-codex-h11-luna-xhigh-20261111/grade.json`.

**First paired identical-user-input run (2026-11-11):** A stricter
plain-Codex evaluation used the **native binary directly** and an
isolated, disposable HOME/XDG/CODEX_HOME with only an authentication
copy staged on Linux tmpfs. The installed account overlay, personal
plugins, skills, MCP configuration, rules and user-home instructions
were excluded. The source authentication file was not changed, and
the temporary private credential directory was deleted. Ash was run
in its existing isolated benchmark home with the exact same user
input (SHA-256
`5567c1b06b83aa604c1b38bbaa6e60d32c1f0f05c8b3bc27e7b6554c92bf592e`),
the same pinned h11 revision, GPT-6 Luna xhigh requested on both,
and a **170-second deadline**.

| Scored outcome | Ash | Isolated plain Codex |
|---|---|---|
| Completed CLI and both independent oracles | PASS | PASS |
| Protected files unchanged | PASS | PASS |
| Required source changed | `h11/_readers.py` | `h11/_readers.py` |
| CLI execution interval | **51.80 seconds** | **46.52 seconds** |
| Original `elapsed_seconds` receipt | 56.57 seconds (includes preflight and grading) | 46.52 seconds (CLI only) |
| CLI timeout | No | No |

**Timing correction:** the original 10.05-second gap compared Ash's
end-to-end interval with Codex's CLI-only interval. The comparable CLI
intervals show Codex **about 5.28 seconds faster** on this
specific task, with both solutions correct. The Codex CLI timer on the
historical run also included a small isolated-profile setup/cleanup span;
future runs start/stop the timer at the process boundaries. This single
pair does **not** establish
an overall speed or reliability advantage. Ash's exact-command
authorization, tool interfaces, model context budget and token
accounting are not mirrored by Codex's native shell tools; both
products retain their own system prompts. The request-context and
turn-token limits are not independently matched.

Private evidence:
`/var/tmp/ash-fair-h11-luna-xhigh-20261111/result.json` and
`/var/tmp/ash-isolated-codex-h11-luna-xhigh-20261111/grade.json`.

## Second paired-task attempt: excluded, not a competitor result

On 2026-11-11 a second model/effort-matched attempt used the pinned
`urllib3-proxy-redirect` two-module task, identical user input
(SHA-256 `6a7c43d46d7a51ccc76c311d26ec637efaf05a5ef45b6613cf828b9e81d83416`),
the same 65,536-token *requested* context window, GPT-6 Luna xhigh, and
180-second subprocess deadlines. The identical hidden/visible-oracle source
fingerprints were checked by `devtools/coding_comparison.py`. Both runners
created disposable source checkouts and retained independent receipts.

| Outcome | Ash | Isolated plain Codex |
|---|---|---|
| Graded completion | FAIL / deadline | FAIL / deadline |
| Source edits | Both designated modules | None |
| Tool calls | 21 | Zero shell commands |
| Visible test | Not executed | Not executed |
| Model/provider state | Nine model requests; eight completed | Nine routing-retry errors; no usable model execution |
| External oracles | Not passing | Not passing (untouched baseline) |
| **Count as a coding contest?** | **No** | **No** |

**Exclusion reason:** the private Codex JSONL contains repeated
`workspace routing discovery failed` / `workspace routing discovery timed out`
errors, and native Codex's model-catalog refresh timed out. Codex never got
to interact with the repository. This is an infrastructure-unavailable trial,
not evidence that Ash outperformed Codex. Its original `grade.json` records
the unsuccessful run without a routing-failure classification because that
metadata was added to the comparator afterward; historical receipts are not
retroactively altered. The local source and events were independently read.

Ash's failure remains actionable on its own: about 138,233 prompt tokens,
5,076 completion tokens, 177.66 seconds of completed model request time and
0.25 seconds of tool execution, with the last edit at 178.6 seconds. The
required visible test did not run and the unchanged oracles failed. This
suggests investigating model-request count, read/search trajectory and
context reuse, but does not yet identify a harness-only regression or prove
that a particular optimization is correct. Retest without changing the task
or its oracles, then validate any improvement on held-out tasks.

Private receipts:
`/var/tmp/ash-fair-urllib3-proxy-luna-65536-20261111-b2/result.json` and
`/var/tmp/ash-plain-codex-urllib3-proxy-luna-65536-20261111-b1/grade.json`.

## 2026-11-12: bounded parallel-scenario proof

The new `devtools/parallel_repository_benchmarks.py` coordinates **at most
two independent pairs** of the existing, independently graded runners. Each
pair executes Ash and native *plain Codex* sequentially in independently
provisioned source and authentication directories; pairs may overlap. It does
not use a supervisory Codex model, Poteto, model-generated grading, shared
worktrees, or unscored task prose. It preserves protected-path checks and
verifies shared task, prompt, and visible/hidden oracle fingerprints. It
alternates engine starting order across tasks and repeat indices.

The initial pilot ran before the task-order alternation improvement. Both
Ash runners started first, each followed by its own Codex runner. Both
requested GPT-6 Luna xhigh, a 32,768-token context window, and a
160-second child deadline. All four attempts passed their independent
visible and hidden oracles, and both pairs passed receipt consistency checks:

| Real-repo scenario | Ash CLI | Plain Codex CLI | Both independently pass? |
|---|---:|---:|---|
| `h11-chunk-footer` | 64.54s | 49.60s | Yes |
| `urllib3-header-sanitization` | 54.87s | 42.22s | Yes |

**Do not use these durations to rank engines.** Concurrent provider load
is uncontrolled, as is the difference in each runner's system instructions,
command tools, sandbox implementation, and model token accounting. Pair
execution order was not balanced in this first pilot. The result demonstrates
correctness, reproducible graded evidence, bounded concurrency, and working
isolation, not a controlled performance winner. For latency comparisons use
`--parallel 1`, repeat and alternate the order, and disclose the remaining
model-routing and tool-parity limitations.

Example, explicitly opt-in only (not a CI job or a background service):

```sh
PYTHONPATH=src .venv/bin/python devtools/parallel_repository_benchmarks.py \
  --task h11-chunk-footer --task urllib3-header-sanitization \
  --parallel 2 --repetitions 1 --timeout 160 --context-tokens 32768
```

Private pilot evidence:
`/tmp/ash-paired-batch-9d62499f9fb0fad094d5/batch.json`.
No credentials, source fixtures, or raw model transcripts are part of the
tracked report. The benchmark remains optional and local-first.

## 2026-11-12: two-file proxy-redirect diagnosis (not yet accepted)

The earlier 180-second proxy-redirect comparison was unusable as a paired
coding result because plain Codex failed provider routing before making any
repository edits. A new **sequential**, same-user-prompt, same-oracle,
GPT-6 Luna xhigh attempt gave both agents a 240-second child deadline and
requested 65,536 context tokens. The independent plain Codex runner now
completed correctly, while Ash did not:

| Metric | Ash, old 120K cumulative cap | Plain Codex |
|---|---:|---:|
| CLI interval | 154.72s | 147.74s |
| Visible and hidden oracles | Fail | Pass |
| Required visible test run | No | Yes |
| Timed out | No | No |
| Terminal | Nonzero, `token_budget` | Completed, exit zero |

The Ash turn used 73,953 cumulative tokens and could not admit the next
estimated 54,607-token input within its benchmark-only 120K limit. Native
plain Codex had no corresponding configured cumulative turn cap. The two
task inputs and both oracle-source fingerprints matched; execution policies
and cumulative limits did not. Do **not** claim a fair capability ranking
from this pair. The receipt comparison now discloses the asymmetric Ash cap,
and the parallel coordinator refuses to certify a capped pair as comparable.

**Corrected runner configuration:** The Ash repository benchmark and
paired-scenario coordinator now use `max_turn_total_tokens = 0`, matching
Ash's normal product default, rather than imposing an artificial 120K cap.
They still enforce per-request input and output bounds, model iteration
limits, exact permitted tool grants, sandbox and process isolation, and
the 240-second deadline. Explicit finite turn budgets remain available for
users deliberately measuring resource-constrained tasks.

An Ash-only retest with the **unchanged proxy task, identical prompt and
oracle source, 65,536 requested context, GPT-6 Luna xhigh and the same
240-second deadline**, but with the cumulative cap disabled, still failed:

| Metric | Ash, 120K capped | Ash, uncapped retest | Plain Codex, prior pair |
|---|---:|---:|---:|
| CLI interval | 154.72s | 202.12s | 147.74s |
| Visible/hidden independent oracles | Fail/Fail | Fail/Fail | Pass/Pass |
| Required test executed | No | No | Yes |
| Context/terminal blocker | Cumulative turn cap | Opaque reasoning input overflow | None |

The second Ash attempt made seven model requests, performed 15 tool calls
(14 reads/searches and one completed edit), and hit a fail-closed
`ContextBudgetExceededError` after the edit: the next protected provider
history required approximately **72,904 input tokens** versus an effective
**59,141-token** input limit. The independent grader observed a missing
`_is_same_host` helper in the partially modified source. That is an
incomplete repair, not a successful solution, and not a deadline expiry.

This reveals a real harness capability gap: long multi-file tasks may
require a new reasoning window before finishing a turn. On the ChatGPT-plan
route, opaque reasoning replay cannot be rewritten or locally compacted
without losing required provider state. The existing `/restart-context`
correctly refuses unfinished turns and ambiguous side effects, and native
Responses compaction is not supported on this route. Silently discarding
provider state or auto-replaying actions would weaken fundamental safety
guarantees. As an immediate user-facing correction, context-error guidance
now explains safe workspace inspection, quiescent explicit handoff and
provider-native/larger-context alternatives, rather than recommending
unsafe generic compaction. **Automatic safe mid-task continuation remains
unimplemented and unverified**; no throughput or quality win is claimed.

The next engineering task is to design and test an explicit, bounded
continuation checkpoint for unfinished *coding* turns that preserves
pending/unknown side-effect guards, audit and approval boundaries, user
objective, verified workspace facts, and session lineage without replaying
opaque reasoning data. Evaluate alternatives against real Codex behavior,
then retest this same frozen proxy task and held-out multi-file tasks.

Private evidence: original pair
`/tmp/ash-paired-batch-a0020a4ba87c22d54c6b/` and corrected Ash attempt
`/var/tmp/ash-urllib3-proxy-uncapped-20261112-r1/result.json`.
