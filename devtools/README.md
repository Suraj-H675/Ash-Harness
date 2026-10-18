# Ash Explorer

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
{"action":"step","label":"workspace boundary","prompt":"Try the requested read.","tool_call":{"name":"read_file","arguments":{"file_path":"../outside-marker.txt"}},"expect":{"kind":"expected_refusal","error_contains":"outside project scope"}}
{"action":"finish"}
```

The writable marker beside the disposable project is still inside the run's
private scratch directory. It lets a path-scope regression be observed without
reaching user files. A successful read must report the target's actual SHA-256
and leave it unchanged. The runner accepts only `write_file` and `read_file`;
it does not accept arbitrary Ash arguments, commands, plugins, MCP servers, or
provider URLs.

To reproduce a recorded sequence in a new disposable run:

```sh
.venv/bin/python devtools/ash_explorer.py replay \
  --steps /tmp/ash-explorer-run-01/actions.jsonl \
  --evidence /tmp/ash-explorer-replay-01 --seed 23
```

Evidence includes the accepted `actions.jsonl`, process argv/status, bounded
raw stdout and stderr, provider exchanges, Ash stream-JSON events, target
hashes, classifications, and a cleanup manifest. `expected_refusal` requires a
matching tool error and an unchanged target; process exit codes alone never
count as refusals. `observed` is never a pass. The fixture is loopback-only, but
the runner does not claim OS-level egress isolation. The manifest records the
runner SHA-256, Ash version, checkout revision, and Python executable/version.

The project, home, trust state, and database live in one owned temporary tree.
Catchable exits clean that tree. If the controller is killed before cleanup,
the manifest retains its path and pending-cleanup state; abandoned paths are
never scanned or deleted automatically.
