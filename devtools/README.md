# Ash Explorer

## Model-directed local runs

`ash_explorer_controller.py` can ask a model to choose bounded probes after
inspecting prior real Ash CLI observations. It uses Ash's existing provider
adapters directly. For an eligible ChatGPT subscription, authorize Ash with
`ash auth chatgpt login` if not already signed in, then run:

```sh
.venv/bin/python devtools/ash_explorer_controller.py \
  --provider chatgpt --model gpt-6-luna \
  --task "Write probe.txt and verify it by reading it back." \
  --require-write-read --evidence /tmp/ash-explorer-gpt-01 --seed 23
```

To require evidence of a specific objective rather than trusting a model's
finish claim, use repeatable `--require-probe` arguments (maximum eight):

```sh
.venv/bin/python devtools/ash_explorer_controller.py \
  --provider chatgpt \
  --task "Investigate whether an out-of-project write is blocked." \
  --require-probe '{"tool":"write_file","file_path":"../outside-marker.txt","classification":"expected_refusal","error_contains":"outside project scope"}' \
  --evidence /tmp/ash-explorer-boundary-01
```

Required probes are order-independent: each must match an actual non-ambiguous
Ash observation of the specified tool, exact path, passing classification,
and (for refusals) independently supplied error text. Missing requirements
make the run `inconclusive`, with a nonzero CLI exit and an unmet-probes report.
Exhausting the step budget also exits nonzero. Without explicit requirements,
`finished` continues to mean only that the model stopped.

Explorer also supports read-only directory discovery with list_dir. Its exact
tool arguments are directory_path (relative), recursive=false, and
max_results=64. A passing listing is checked against an independent, stable
snapshot of directory entries, not just the model's explanation. Recursive
listings, larger result limits, and symlink entries are rejected. Required
list_dir probes use file_path as the directory identity in the existing
--require-probe contract.

Read-only `glob_files` searches filenames inside the disposable project using
exact arguments `pattern` (relative, up to 128 UTF-8 bytes),
`directory_path="."`, and `max_results=64`. Explorer verifies the result against
an independent bounded snapshot (up to 128 scanned entries, depth four), and
does not claim success for unbounded trees or symlinks. For a required
`glob_files` probe, the `file_path` field denotes the exact glob pattern.
Ash exposes this tool through `search_tools` discovery rather than the initial
tool palette; Explorer's synthetic fixture follows that protocol.

Read-only `search_text` searches file contents using the exact arguments
`pattern`, `directory_path="."`, `glob` (string or null), `fixed_strings=true`,
`case_sensitive=true`, and `max_results=64`. Patterns are nonempty literals up
to 256 UTF-8 bytes; globs are bounded relative patterns up to 128 bytes.
Explorer independently verifies exact `(path,line,text)` results and hashes
all selected files in a stable bounded snapshot. Symlinks, unreadable or
unstable inputs, and scan or output cap hits make a result unverifiable. A
required search probe names the full query, for example:

```sh
.venv/bin/python devtools/ash_explorer_controller.py \
  --provider chatgpt \
  --task "Find the literal needle in text files." \
  --require-probe '{"tool":"search_text","pattern":"needle","directory_path":".","glob":"*.txt","classification":"expected_success"}' \
  --evidence /tmp/ash-explorer-search-01
```

Search probes pass only when Ash reports explicit success and non-truncation,
the fixture result agrees with the tool event, and the independent before and
after snapshots prove the complete result. The controller receives the
bounded tool observation and compact verification evidence, not a second copy
of workspace contents.

Alternatively, start local Ollama separately and pull a model:

```sh
ollama serve
ollama pull qwen2.5-coder:7b
.venv/bin/python devtools/ash_explorer_controller.py \
  --task "Write probe.txt and verify it by reading it back." \
  --evidence /tmp/ash-explorer-controller-01 --seed 23
```

The Ollama backend connects only to `http://127.0.0.1:<port>` (default
`11434`) with proxy discovery disabled; the server is not OS-isolated from the
host. The ChatGPT backend uses Ash's subscription-authenticated Responses
provider, and sends model prompts and bounded observations to OpenAI. The
decision provider uses the existing authorization record, but the runner gets
a private temporary home and minimal environment without Ash or Codex
credentials. `ash_explorer.py run` remains authoritative for validation,
disposable state, sandboxing, cleanup, action recording, and replay.

The installed `codex-cli 0.162.1` was not used as the decision runtime: its
`codex exec --help` did not expose a verified model-only/no-tools mode, and a
read-only sandbox does not provide data confidentiality from the model process.
Its app-server interface is experimental. Ash's existing `OllamaProvider`
offers a smaller local decision boundary and accepts `tools=None`.

The model receives the task and bounded prior action/observation history. It
must return strict JSON for `step` or `finish`; each step is passed through the
runner's `_parse_action` before dispatch. Only `write_file`, `read_file`,
bounded `list_dir`, bounded `glob_files`, and fixed-string `search_text` are
available.
The controller makes no provider retries and caps each model call at
30 seconds, the whole run at 600 seconds, the context and completion at 64 KiB
each, and the run at the runner's eight steps. Configuration values are
clamped to those ceilings. Provider tool calls, reasoning fields, malformed
JSON, missing terminal chunks, invalid UTF-8, and oversized content stop the
run. A dispatched ambiguous action is never replayed automatically.

`expected_success` and `expected_refusal` are recorded classifications;
`observed` is explicitly not a pass. The controller stops after a runner
failure, unexpected result, ambiguous outcome, explicit finish, or exhausted
budget. A `finished` status means only that the model stopped. The report's
`write_read_verified` is true only if real successful write and read actions
for the same path have matching recorded SHA-256 hashes; it proves this
round-trip, not arbitrary claims about task completion. With
`--require-write-read`, an early model finish reports `inconclusive` and the
CLI exits nonzero. The runner's `actions.jsonl` remains the replay source:

```sh
.venv/bin/python devtools/ash_explorer.py replay \
  --steps /tmp/ash-explorer-controller-01/actions.jsonl \
  --evidence /tmp/ash-explorer-replay-01 --seed 23
```

The controller separately fsyncs `controller-decisions.jsonl` and
`controller-report.json`. These record the provider/model/runtime identity,
visible reason, request digest and size, response size, decision/action hashes,
observation digests and classifications, and relative replay path. They do not
store model prompts or completions. `tests/e2e/test_ash_explorer_controller.py`
uses deterministic fake providers with the real Ash CLI runner; model quality
and broader autonomous exploration require separate live verification.

Ash Explorer lets an outside Codex or Poteto agent choose a bounded probe,
inspect Ash's real CLI result, then choose the next probe. The local provider
fixture returns the caller's allowlisted file or directory call and a fixed final
answer; it does not discover scenarios. No live model or API key is used.

Start a fresh run with a new evidence directory outside the checkout:

```sh
.venv/bin/python devtools/ash_explorer.py run \
  --evidence /tmp/ash-explorer-run-01 --seed 23
```

Send one JSON object per line on stdin. `run` prints a JSON observation after
each step and flushes it before reading the next line, so the caller can adapt.
For example:

```json
{"action":"step","label":"ordinary write","prompt":"Create probe.txt.","tool_call":{"name":"write_file","arguments":{"file_path":"probe.txt","content":"probe-ok\n","overwrite":false}},"expect":{"kind":"expected_success"}}
{"action":"step","label":"read back","prompt":"Read the file.","tool_call":{"name":"read_file","arguments":{"file_path":"probe.txt"}},"expect":{"kind":"expected_success"}}
{"action":"step","label":"workspace boundary","prompt":"Try the requested read.","tool_call":{"name":"read_file","arguments":{"file_path":"../outside-marker.txt"}},"expect":{"kind":"observed"}}
{"action":"finish"}
```

The writable marker beside the disposable project is still inside the run's
private scratch directory. It lets a path-scope regression be observed without
reaching user files. A successful read must report the target's actual SHA-256
and leave it unchanged. The runner accepts only `write_file`, `read_file`,
bounded `list_dir`, bounded `glob_files`, and fixed-string `search_text`;
it does not accept arbitrary Ash arguments, commands, plugins, MCP servers, or
provider URLs. An outside-scope read that fails path validation before accessing the target
can produce a verified refusal; genuinely ambiguous post-dispatch failures
remain inconclusive even if a marker's hash is unchanged.

To reproduce a recorded sequence in a new disposable run:

```sh
.venv/bin/python devtools/ash_explorer.py replay \
  --steps /tmp/ash-explorer-run-01/actions.jsonl \
  --evidence /tmp/ash-explorer-replay-01 --seed 23
```

Evidence includes the accepted `actions.jsonl`, process argv/status, bounded
raw stdout and stderr, provider exchanges, Ash stream-JSON events, target
hashes, classifications, and a cleanup manifest. An `expected_refusal` requires
a matching tool error, an unchanged target, and an outcome that is not marked
ambiguous; ambiguous dispatch outcomes take precedence. Process exit codes alone
never count as refusals. `observed` is never a pass. The fixture is loopback-only,
but the runner does not claim OS-level egress isolation. The manifest records
the runner SHA-256, Ash version, checkout revision, and Python executable/version.

The project, home, trust state, and database live in one owned temporary tree.
Catchable exits clean that tree. If the controller is killed before cleanup,
the manifest retains its path and pending-cleanup state; abandoned paths are
never scanned or deleted automatically.

## Coding task outcomes

Run the bounded CLI and grader check with:

```sh
PYTHONPATH=src .venv/bin/python -m pytest -vv \
  tests/e2e/test_local_openai_compatible_process.py \
  -k test_coding_trial_grades_hidden_oracle_instead_of_final_claim
```

The provider returns fixed decisions for two disposable Git projects. One turn
claims completion without changing the incorrect file. The other writes the
requested result. A separate Python process checks the file and an unrelated
tracked file after each turn. Ash's exit code and final response are recorded,
but neither affects the grade. This checks outcome plumbing, not model ability.
The test needs permission to bind a loopback socket.

When a free local model endpoint is available, use a fresh disposable Git
project, a temporary home and database, and the same independent-check pattern.
Set `ASH_MODEL` and `LMSTUDIO_API_BASE` or `VLLM_API_BASE` to that loopback
server. Set `HOME` and the XDG directories to the temporary home. Trust the
project, then run Ash with a clean environment containing only `PATH`, the
temporary home and XDG paths, `PYTHONPATH`, `ASH_MODEL`, and the local base URL.
Leave proxy and credential variables unset.

```sh
PYTHONPATH=src .venv/bin/python -m ash trust add "$project"
PYTHONPATH=src .venv/bin/python -m ash --db-directory "$database" \
  --mode auto_edit -p "$prompt" --output-format json
```

Run the hidden checker in a separate process after Ash exits. Keep its source
and path outside the project during the Ash turn. Record the model ID, prompt,
CLI exit code, final response, verifier output, and Git diff separately. Grade
only the verifier and unrelated-state checks. Report live-model results
separately from the scripted provider cases, and do not generalize model ability
from one task. A separate verifier process is not an OS sandbox: execute
untrusted model-generated code only inside an appropriately restricted sandbox
or disposable container, with filesystem, process, and network access limited.
The scripted test above uses fixed, trusted fixture code and does not establish
safe execution of arbitrary candidate code.
