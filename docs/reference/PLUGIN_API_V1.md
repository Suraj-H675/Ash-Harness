# Ash Plugin API v1

Plugin API v1 lets a local Ash plugin contribute executable tools without
importing Ash internals or running code during discovery. Ash starts one shared
child process per executable plugin on its first approved call. Declarative
plugins remain valid and do not need a runtime.

## Manifest

An executable plugin declares a runtime and at least one tool in `plugin.json`:

```json
{
  "schemaVersion": 1,
  "name": "text-utils",
  "version": "1.0.0",
  "description": "Deterministic text utilities",
  "runtime": {
    "command": ["python3", "runtime.py"],
    "protocolVersion": 1,
    "timeoutSeconds": 30
  },
  "tools": [
    {
      "name": "normalize",
      "description": "Normalize whitespace in supplied text",
      "inputSchema": {
        "type": "object",
        "properties": {"text": {"type": "string"}},
        "required": ["text"],
        "additionalProperties": false
      }
    }
  ]
}
```

`command` is an argv array and is never interpreted by a shell. Commands should
be portable within the selected sandbox; relative files resolve from the plugin
root. A plugin can declare at most 64 tools. Tool names start with a letter and
contain only letters, numbers, `_`, or `-`. Input schemas must be valid JSON
Schema Draft 2020-12 schemas whose root type is `object`.

Ash exposes the example as `plugin_10_text-utils__normalize`. The length prefix
keeps namespace encoding injective. Names are normalized for provider
portability and capped at 64 characters; installation or assembly fails on an
overlong name or collision.

## Transport

The host and plugin exchange one UTF-8 JSON-RPC 2.0 message per line over stdio.
Messages are limited to 1 MiB. Request IDs are integers and responses must echo
the exact ID. The plugin must reserve stdout for protocol messages; diagnostics
belong on stderr. Ash drains stderr continuously and retains a bounded 64 KiB
tail for errors.

Ash sends `initialize` before any tool call:

```json
{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocol_version":1,"plugin":{"name":"text-utils","version":"1.0.0"}}}
```

The plugin acknowledges the negotiated version:

```json
{"jsonrpc":"2.0","id":1,"result":{"protocol_version":1}}
```

Ash invokes a declared tool with `tool/call`:

```json
{"jsonrpc":"2.0","id":2,"method":"tool/call","params":{"name":"normalize","arguments":{"text":"a  b"}}}
```

The result object has this closed contract:

```json
{
  "success": true,
  "output": "a b",
  "error": null,
  "token_count": 2,
  "truncated": false
}
```

`success` and `output` are required. `error` is a string or null;
`token_count` is a non-negative integer; `truncated` is a boolean. Output is
limited to 768 KiB and error text to 64 KiB. Unknown result fields are rejected.
JSON-RPC errors use the standard object form with a string `message`.

On clean shutdown Ash may send `shutdown` with an empty params object. The host
should reply normally and may exit. Ash then terminates any remaining process
tree. Shutdown is idempotent.

## Minimal Python host

This example uses only the Python standard library:

```python
import json
import re
import sys


def respond(request, result):
    print(
        json.dumps({"jsonrpc": "2.0", "id": request["id"], "result": result}),
        flush=True,
    )


for line in sys.stdin:
    request = json.loads(line)
    method = request["method"]
    if method == "initialize":
        respond(request, {"protocol_version": 1})
    elif method == "shutdown":
        respond(request, {})
        break
    elif method == "tool/call":
        params = request["params"]
        if params["name"] != "normalize":
            raise ValueError("unknown tool")
        text = params["arguments"]["text"]
        respond(
            request,
            {
                "success": True,
                "output": re.sub(r"\s+", " ", text).strip(),
                "error": None,
                "token_count": 0,
                "truncated": False,
            },
        )
```

## Lifecycle crash recovery

Managed user-plugin install, replacement, update, and uninstall operations are
serialized by the plugin-root lifecycle lock and use a durable transaction
journal. The journal binds the plugin root, any moved/staged/live tree inodes,
the exact before/after managed provenance, and any managed activation-state
transition. Plugin lifecycle persistence requires successful file and directory
`fsync` checkpoints before advancing the durable decision.

There is one durable decision point: before the journal is marked committed,
restart recovery compensates back to the previous tree/provenance/activation
state. After the committed journal is durable, recovery preserves the new
state and only finishes transaction cleanup. Recovery runs before normal user
plugin discovery and before a later lifecycle mutation. If a recorded inode,
plugin root, activation-state parent, provenance value, or activation value can
no longer be proven to match, recovery fails closed rather than deleting or
overwriting an unknown entry.

Ash-generated staging, backup, quarantine, and rollback-conflict directories
are not plugin discovery roots. Unrecognized residue is preserved; recovery
only removes entries whose persisted inode identity proves that Ash owns that
transaction artifact.

## Isolation and policy

Executable plugins are denied by default unless Bubblewrap or the configured
local Docker sandbox is available. macOS `sandbox-exec` is not sufficient for
plugins because it cannot provide the required host-read isolation; macOS
therefore uses Docker for executable plugins. WSL2 follows the Linux host path.
Native Windows is not currently supported. The installed plugin root is mounted read-only,
temporary storage is isolated, the network is disabled, and the environment
contains only operational values such as `PATH`, `HOME`, locale, and Python I/O
settings. Ash credentials and arbitrary host environment variables are not
forwarded; only isolated temporary storage is writable.

For emergency compatibility, a user may set
`ASH_ALLOW_UNSAFE_PLUGIN_RUNTIME=true` or the equivalent user configuration.
This is deliberately not accepted from project `.ash/config.toml`. It runs
plugin code as the current user with host access and should only be used for
code the user fully trusts.

Isolation does not replace Ash policy. Each namespaced plugin tool follows the
same permission decision, user approval, hook lifecycle, middleware, audit log,
runtime event, dry-run denial, and session persistence path as a built-in tool.
Unknown tools require approval outside full-auto mode.

## Failure semantics

- Input is validated in Ash before the process starts or receives a request.
- A timeout, crash, malformed response, oversized message, mismatched ID, or
  protocol violation fails the current call and tears down the host.
- Ash never automatically replays that call. This prevents duplicated external
  side effects when delivery succeeded but the response was lost.
- A later, separately approved call may start a fresh host.
- Calls to tools from the same plugin are serialized through its shared host.
- Reload closes the old host before installing new proxies; loop shutdown closes
  every host and descendant process deterministically.

Plugin API v1 intentionally has no network grants, secret injection, in-process
Python ABI, or runtime dependency installer. Network services should use MCP,
and plugin dependencies should be installed and pinned by the plugin author or
operator before activation.
