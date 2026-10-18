# Ash Explorer

## Model-directed local runs

`ash_explorer_controller.py` can ask a local Ollama model to choose each
bounded probe after inspecting the previous real Ash CLI observation. It uses
Ash's existing provider adapter directly and does not need an API key or new
credentials. Start Ollama separately and pull a model before running:

```sh
ollama serve
ollama pull qwen2.5-coder:7b
.venv/bin/python devtools/ash_explorer_controller.py \
  --task "Write probe.txt and verify it by reading it back." \
  --evidence /tmp/ash-explorer-controller-01 --seed 23
```

The controller connects only to `http://127.0.0.1:<port>` (default port
`11434`) with proxy environment discovery disabled. The Ollama server is a
separate process and is not OS-isolated from the host. The controller does not
read Ash or Codex configuration or forward credential environment values. It
gives the runner a minimal environment with a private temporary home and
invokes `ash_explorer.py run` with fixed arguments, leaving the runner
authoritative for validation, disposable state, sandboxing, cleanup, action
recording, and replay.

The installed `codex-cli 0.162.1` was not used as the decision runtime: its
`codex exec --help` did not expose a verified model-only/no-tools mode, and a
read-only sandbox does not provide data confidentiality from the model process.
Its app-server interface is experimental. Ash's existing `OllamaProvider`
offers a smaller local decision boundary and accepts `tools=None`.

The model receives the task and bounded prior action/observation history. It
must return strict JSON for `step` or `finish`; each step is passed through the
runner's `_parse_action` before dispatch. Only `write_file` and `read_file` are
available. The controller makes no provider retries, caps each model call at
30 seconds, the whole run at 600 seconds, the context and completion at 64 KiB
each, and the run at the runner's eight steps. Configuration values are
clamped to those ceilings. Provider tool calls, reasoning fields, malformed
JSON, missing terminal chunks, invalid UTF-8, and oversized content stop the
run. A dispatched ambiguous action is never replayed automatically.

`expected_success` and `expected_refusal` are recorded classifications;
`observed` is explicitly not a pass. The controller stops after a runner
failure, unexpected result, ambiguous outcome, explicit finish, or exhausted
budget. The runner's `actions.jsonl` remains the replay source:

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
uses only a deterministic fake provider and the real Ash CLI runner. No live
model or provider call was performed; autonomous behavior is **not yet
autonomously verified**.

Ash Explorer lets an outside Codex or Poteto agent choose a bounded probe,
inspect Ash's real CLI result, then choose the next probe. The local provider
fixture returns the caller's `write_file` or `read_file` call and a fixed final
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
and leave it unchanged. The runner accepts only `write_file` and `read_file`;
it does not accept arbitrary Ash arguments, commands, plugins, MCP servers, or
provider URLs. An outside-scope read can return an access-denied tool error
after dispatch, which Ash classifies as `ambiguous_outcome`; the error text and
unchanged marker do not make it an `expected_refusal`.

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
