# Local coding-task evaluation

`devtools/coding_benchmark.py` is a small, repeatable real-model evaluation of
Ash's **actual one-shot CLI**, not a mocked provider test. It creates a separate
home, Ash database, and workspace for each task, checks the resulting files with
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

## Prerequisites

- An Ash development environment (`.venv` with Python 3.12–3.14).
- Linux with working Bubblewrap. The independent code oracle executes model-
  authored Python inside a read-only, networkless sandbox; it never executes
  that code unsandboxed.
- A locally running OpenAI-compatible model server supporting native tools at
  `127.0.0.1`. Explicit capability metadata is configured only inside each
  disposable Ash home. Use a model/server combination that genuinely supports
  those calls; a generic compatible API does not guarantee tool calling.

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

Use a **new evidence path** for every invocation. `--task marker`,
`--task build-run`, `--task repair-test`, `--task multi-file`, or
`--task two-file` can select a single case, and `--task` can be repeated to
select cases. Use `--repeat 3` for three isolated Ash workspaces and sessions
per case. Those trials **share the model server and any of its prompt/KV caches**;
they are not independent inference environments. A before/after comparison
should use the same serving binary, device, sampling configuration, and model,
and should balance task order before interpreting small latency differences.
The tool never launches
a cloud inference route, passes through personal credentials, alters the user's
Ash profile, or runs anything in the project being developed. The code tasks
execute in `auto_edit` mode with no general command approval; only the exact
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
