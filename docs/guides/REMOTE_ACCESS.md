# Remote access

Ash can run one dedicated headless runtime and control it from another terminal
or browser. The server is **loopback-only by default** and uses the same durable
workspace/session state as other Ash interfaces.

Remote control is intentionally a single-operator feature. One bearer token
represents one fully trusted controller: anyone holding it can run turns,
select sessions, steer work, and approve or deny tool calls. Ash does not claim
multi-user isolation, team authorization, or a hosted relay.

## Start a local server

Generate a strong token and keep it out of command-line arguments:

```bash
export ASH_SERVER_TOKEN="$(python -c 'import secrets; print(secrets.token_hex(32))')"
ash serve
```

The default listener is `127.0.0.1:8765`.

Open the browser control surface at:

```text
http://127.0.0.1:8765/ui
```

The page asks for the bearer token after loading. The token is kept only in the
live page state/password field; the UI does not write it to cookies,
`localStorage`, or `sessionStorage`.

The terminal client uses the same server:

```bash
ash remote chat http://127.0.0.1:8765
```

Both clients can inspect/select durable sessions, stream turns, steer a running
turn, and resolve live tool approvals. Browser **Stop** cancels the streaming
request, which propagates cancellation to the underlying turn. In the terminal,
`Ctrl-C` cancels the active remote command.

## Terminal control commands

```bash
ash remote status http://127.0.0.1:8765
ash remote sessions http://127.0.0.1:8765
ash remote new http://127.0.0.1:8765
ash remote resume http://127.0.0.1:8765 SESSION_ID
ash remote prompt http://127.0.0.1:8765 "review the current diff"
ash remote steer http://127.0.0.1:8765 "focus on the failing test"
ash remote approvals http://127.0.0.1:8765
ash remote approve http://127.0.0.1:8765 APPROVAL_ID
ash remote deny http://127.0.0.1:8765 APPROVAL_ID
```

`ash remote chat` is the normal interactive terminal attachment. During a
turn it long-polls the approval broker and prompts for pending decisions. The
standalone `steer`, `approve`, and `deny` commands are useful from a second
terminal.

The client accepts plaintext HTTP only for loopback origins, rejects
credential-bearing/query URLs, does not follow redirects, ignores ambient proxy
configuration for bearer-auth requests, and bounds JSON/SSE input.

## SSH tunnel: recommended across the Internet

Keep Ash bound to loopback on the remote development machine:

```bash
# remote development machine
export ASH_SERVER_TOKEN="..."
ash serve
```

From the operator machine, create an SSH tunnel:

```bash
ssh -N -L 8765:127.0.0.1:8765 user@development-host
```

Then use `http://127.0.0.1:8765/ui` or `ash remote chat
http://127.0.0.1:8765` locally. The Ash listener remains private to the remote
host; SSH supplies transport authentication and encryption.

## Private-network direct access

For a trusted VPN/private-network path, Ash can bind beyond loopback only with
an explicit opt-in and TLS:

```bash
ash serve \
  --host 0.0.0.0 \
  --allow-remote \
  --ssl-certfile /path/to/cert.pem \
  --ssl-keyfile /path/to/key.pem
```

The browser and terminal client then use the HTTPS origin:

```text
https://development-host:8765/ui
```

Ash requires a bearer token for every control/API route. Only `/health` and
the static control-UI files are public. Generated API documentation is disabled.
Authentication attempts and authenticated requests have separate bounded rate
limits, request bodies and live work are bounded, and Uvicorn connection
admission/backlog/keep-alive are capped.

## Direct public-Internet exposure

Directly exposing the built-in listener to the public Internet is **not a
supported deployment target**. Use a private VPN, SSH tunnel, or a hardened
reverse proxy that adds connection/TLS-handshake/header/request timeouts and
appropriate network-level abuse controls.

Ash deliberately does not ship a hosted relay or device-pairing service in this
phase. Those would create a separate service trust boundary and operations
burden rather than improving the local coding harness itself.

## Approval behavior

`ash serve` supplies the headless runtime with a bounded approval broker.
Operations that policy marks **ASK** pause until an authenticated operator
approves or denies them.

- pending approval capacity is bounded;
- arguments shown remotely use Ash's generic secret redaction plus each tool's
  declared sensitive argument fields;
- approval IDs are random and single-use;
- the default approval wait is 300 seconds and can be changed with
  `--approval-timeout` (1–3600 seconds);
- timeout, broker shutdown, or capacity exhaustion fails closed;
- no remote mode silently upgrades **ASK** to allow.

The bearer token therefore authorizes *the operator*, not individual tool calls.
Tool-policy decisions and explicit approvals remain separate.

## Session model

One `ash serve` process owns one runtime and one currently selected durable
session. Browser, terminal, REST, and JSON-RPC clients attached with the same
token control that shared runtime; they are not independent tenants.

Remote turn requests can bind an expected `session_id`. Selection of that
session and execution of the turn happen under the SDK's same turn lock, so a
second controller cannot switch the shared runtime between those two steps.
Session mutation and turns remain serialized while steering stays available
during a running turn. Do not use one server process as a multi-user service.
Run separate Ash processes/workspaces when independent operators or isolation
domains are required.

## A2A is different

The A2A server is for agent-to-agent task interoperability, not human remote
control. Its Agent Card is intentionally public for protocol discovery; task
and management routes require authentication. User-owned A2A configuration may
bind a bearer credential to a peer. Trusted project `.ash/a2a.json` files may
declare peers but cannot select or inherit host credential environment
variables.
