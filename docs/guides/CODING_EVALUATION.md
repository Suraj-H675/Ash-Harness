# Coding-task evaluation (local, free remote, or authorized ChatGPT plan)

For the cross-scenario decision process, existing-task inventory and the
separation between synthetic smoke tasks and pinned real-repository tasks,
see [Coding evaluation portfolio](../benchmarks/CODING_PORTFOLIO.md).

`devtools/coding_benchmark.py` is a small, repeatable real-model evaluation of
Ash's **actual one-shot CLI**, not a mocked provider test. It uses a local
loopback model by default; explicitly opted-in zero-cost OpenRouter and
authorized ChatGPT-plan routes exist for stronger-model investigations. It
creates a separate home, Ash database, and workspace for each task, checks the resulting files with
an independent oracle, reads the persisted tool/audit record, and removes the
temporary workspace. Its evidence directory retains task results, model output,
tool-call summaries, and the source revision.

The initial tasks intentionally cover distinct workflow contracts: exact file
creation; creating and executing a Python program; reading and repairing
a failing Python test; and finding a percentage-calculation bug across a small
multi-file package. That original `multi-file` case **requires editing only one
module**, despite reading multiple files. The newer `two-file` case requires
separate fixes in both billing modules; both the visible test and a hidden
behavioral oracle must pass, and both original source files must actually change.
The other repair tasks also have hidden behavioral checks. This is still a
**smoke-scale baseline**, not a
statistically meaningful agent leaderboard, general model-quality claim, or
replacement for larger, independently authored coding tasks. Run repeated trials
before claiming an improvement in completion rate or latency.

For pinned **real upstream Git repositories** rather than synthetic fixture
projects, see [Real-repository coding evaluation](REAL_REPOSITORY_CODING_EVALUATION.md).
That separate runner keeps independent hidden behavioral checks and protected
source boundaries without putting the later upstream fix in the model's checkout.

## Prerequisites

- An Ash development environment (`.venv` with Python 3.12–3.14).
- Linux with working Bubblewrap. The independent code oracle executes model-
  authored Python inside a read-only, networkless sandbox; it never executes
  that code unsandboxed.
- For the default local path: a running OpenAI-compatible model server with
  native tools at `127.0.0.1`. Explicit capability metadata is configured only
  inside the disposable Ash home. Wire compatibility alone is not evidence of
  tool-call support.
- For the **opt-in remote path only**: an authorized `OPENROUTER_API_KEY` already
  available in the calling environment. Do not put keys in shell arguments,
  benchmark configuration, Git, or evidence. The runner uses Ash's existing
  OpenRouter provider. It checks the official public catalog **without the key**
  first and rejects unknown IDs, non-`:free` IDs, listed nonzero or conditional
  pricing, missing native-tool support, and insufficient context. Failed
  preflight means no coding inference is sent.
- For the **opt-in ChatGPT plan path**: an already-authorized
  `ash auth chatgpt status` registration, with an access token valid long
  enough to cover the trial, and an available model listed by
  `ash auth chatgpt models`. Linux `/dev/shm` must be mounted as `tmpfs`.
  The benchmark reads the existing registration without changing it, stages
  **only an access token** (not refresh or identity tokens) in a private,
  memory-backed disposable Ash home, and destroys that home after the trial.
  This avoids recording credentials in the benchmark evidence or normal Ash
  configuration; an expired access token must be refreshed separately.

For example, start a locally installed llama.cpp server on a free loopback port
(in a separate terminal). Replace the paths with your own *authorized local*
GGUF and server. A model must be selected **and kept constant** across an
experiment:

```sh
/path/to/llama-server --model /path/to/model.gguf \
  --host 127.0.0.1 --port 18105 --threads 8 \
  --ctx-size 16384 --parallel 1
```

From the Ash checkout:

```sh
PYTHONPATH=src .venv/bin/python devtools/coding_benchmark.py \
  --model-file /path/to/model.gguf \
  --evidence-dir /var/tmp/ash-coding-trial-001
```

For the free remote path, first configure an OpenRouter API key privately in
the calling shell (never paste it into commands, logs, or chat). Once
`OPENROUTER_API_KEY` is present, explicitly select **one or two** tasks:

```sh
PYTHONPATH=src .venv/bin/python devtools/coding_benchmark.py \
  --openrouter-free-model cohere/north-mini-code:free \
  --task two-file --task event-log \
  --context-tokens 16384 --turn-token-budget 80000 \
  --evidence-dir /var/tmp/ash-free-coding-trial-001
```

The selected ID is pinned in the evidence. The live free catalog changes;
the example is not a guarantee of future availability or model quality.
Each remote trial is limited to 16 model-turn iterations; provider retries and
rate limits can be stricter. Remote inference sends only synthetic benchmark
prompts and code to the selected model provider; review its data policies.
The runner never silently substitutes a paid model or a different free model.

For an already authorized ChatGPT plan, select a model that is currently
available in your account:

```sh
PYTHONPATH=src .venv/bin/python devtools/coding_benchmark.py \
  --chatgpt-model gpt-6-luna --task two-file \
  --context-tokens 32768 --turn-token-budget 120000 \
  --timeout 210 --evidence-dir /var/tmp/ash-chatgpt-trial-001
```

This exercises Ash's ChatGPT-plan Responses provider, **not** the separate
OpenAI API-key route. It permits up to 24 model-request cycles per trial and
at most two selected trials per invocation. The plan provider currently
ignores the provider-neutral per-request `max_completion_tokens` setting, so
the recorded completion-token cap is **not enforced** on this route. Do not
compare token budgets or model-provider timings as if this setting were
identical across local, OpenRouter, and ChatGPT models. The trial does not
alter the user's normal Ash trust, permissions, or session database.

Use a **new evidence path** for every invocation. `--task marker`,
`--task build-run`, `--task repair-test`, `--task multi-file`,
`--task two-file`, or `--task event-log` can select a single case, and
`--task` can be repeated to
select cases. In local mode, `--repeat 3` uses three isolated Ash workspaces
and sessions per case. Those trials **share the model server and any of its
prompt/KV caches**; they are not independent inference environments. Free
remote and ChatGPT-plan modes allow at most two selected trials per invocation
to bound model usage. A before/after comparison should use equivalent selected models,
model-serving conditions, sampling, context, and output budgets, and balance
task order before interpreting small latency differences.
The benchmark deliberately limits cumulative input plus output to **40,000
tokens per trial**, unlike Ash's regular default of **no turn-wide token cap**.
Use `--turn-token-budget 80000` for a bounded budget-sensitivity probe; this
changes the experiment, not Ash's production defaults. The chosen value is
written to both `summary.json` and each task result. Never compare runs with
different budgets as if they had identical constraints.
The benchmark independently defaults to **8,192 tokens of per-request context**
even when the local model server is started with `--ctx-size 16384`. A
context-sensitivity probe may use `--context-tokens 16384` **only when** the
serving model/runtime supports that window. Free remote trials can request up
to 65,536 context tokens after a catalog window check. Each local request
defaults to 384 completion tokens, versus 4,096 for the remote route; set
`--completion-tokens` explicitly for matched-budget comparisons. These
limits are recorded in each result. A turn that stops on `context` is not
evidence that its *turn-total* budget was exhausted: these are independent
constraints. Ash's production
context policy has not been changed by either benchmark option.
The `event-log` task is an **original, synthetic three-module integration
repair**, not an upstream bug report or a substitute for a repository-scale
benchmark. It combines malformed JSON handling, aggregation correctness, and
report ordering. The grader requires all three modules to change, passes both
visible and independent hidden input variants through an isolated Python
oracle, and rejects modifications to fixture/test files. That improves our
workflow coverage without implying real-world or multi-language success.
The default local mode never launches cloud inference. Opt-in free remote mode
uses a named OpenRouter model, verified public pricing, and the key provided
only through the isolated child environment; it does not write that key to
configuration, command arguments, or benchmark metadata. ChatGPT-plan mode
uses a short-lived access-only credential in private tmpfs. None of the modes
modify the user's live Ash profile or execute code in the project being developed.
The code tasks execute in `auto_edit` mode with no general command approval;
only the exact
fixture test command is granted when needed. Ash's own command sandbox is
explicitly set to `native`.

## Reading the scores

`summary.json` records the Ash HEAD, model SHA-256 (when supplied), Python
interpreter, endpoint, timeout, and per-task outcomes. A task passes only when
all of the following are true: the CLI completes without timeout; the external
oracle passes; required tools have actually succeeded; protected files are
unchanged; all case-specific editable source files have changed; and the session
audit verifies. The oracle, tool activity, and final
model claim are recorded separately so a confident answer cannot substitute
for a completed edit or successful test.

Each trial also includes `setup_seconds`, `cli_seconds`, and `performance`, a
bounded projection of **persisted, content-free runtime event fields**. The
projection includes request counts, completed provider-request durations,
successful edit/command timing, and an event timeline capped at 300 entries.
Terminal model/tool events also include their individual `duration_seconds`
when a matching start event exists on the inspected page. The aggregates
`completed_model_seconds`, `unsuccessful_model_seconds`, and
`completed_tool_seconds` distinguish successful model completions and tool
executions from failed or cancelled provider attempts.
For new runtime events, `model.request.first_output` records the first retained
provider output (text, tool call, reasoning, or replay state) **without its
content**. Requests with a matching start and completion gain
`first_output_seconds` (waiting for initial output) and
`after_first_output_seconds` (remaining request duration). Their aggregate
seconds and matched-request count are exposed separately. This is a
first-output measurement, **not** a guaranteed first visible text token or
model-prefill-only measurement; it cannot distinguish network latency from
provider queueing or prefill. Older sessions and incomplete event pages may
lack the intermediate event, so missing split timings must not be treated as
zero.
It never copies model text, command output, tool arguments, workspace paths,
credentials, or free-form error messages into that projection. Raw stdout,
stderr, and tool records elsewhere in the evidence directory remain private.

Use these timings to separate provider latency and tool execution from a slow
final response. A timed-out trial can have correct code and a successful test
without a terminal `turn.completed` event. An open model request in the event
projection means a request had no persisted terminal event, **not** proof the
model itself hung; interruptions and bounded event persistence are confounders.

For ordinary Ash sessions, the same content-free projection is available
without running a benchmark or replaying a model prompt:

```sh
ash sessions activity --limit 100
ash sessions activity --session SESSION_ID --limit 100 --json
ash sessions activity --session SESSION_ID --after-sequence CURSOR --json
```

The inspection is read-only and scoped to the current project. `next_cursor`
and `has_more` allow bounded pagination; a page's relative timings start at
its first recorded event, so a page that omits a request start cannot measure
that request's full latency. This is intentionally **not** a transcript or
command-output inspection surface.

Keep the server runtime build, model decoding/chat-template settings, host
resource contention, and task budgets in the experiment notes: the evaluation
script does not control them. It does not benchmark real hosted model quality,
end-user consent flows, crash recovery, malicious-code resistance of the model,
multi-hour repository work, or long-session memory. Use the existing
`skills/verify-ash` recipes and appropriate integration/security tests for
those distinct contracts.

The evidence tree may contain model-generated files, source paths, and model
output. Treat it as private local development data; review before sharing or
tracking results. Never commit credentials or unrelated user workspace state.
