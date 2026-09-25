# Durable Automation

Ash persists scheduled prompts independently from any terminal session. A
worker claims due runs atomically, executes them through the normal Ash runtime,
and records their terminal state and bounded result. One database can contain
jobs for multiple workspaces, but each worker only claims its canonical current
workspace.

## Create and operate jobs

The workspace must exist and be trusted before a job can be created, resumed,
claimed, or executed:

```bash
cd /absolute/path/to/project
ash trust add .

# One future instant. The timestamp must include an explicit UTC offset.
ash cron add release-check --prompt 'Check release readiness' \
  --at '2026-08-01T09:00:00+05:30'

# Fixed elapsed interval anchored when the job is created.
ash cron add health-review --prompt 'Review project health' --every 6h

# Five-field cron in an IANA time zone. Weekdays use names, not numbers.
ash cron add weekday-review --prompt 'Review open risks' \
  --cron '30 9 * * mon-fri' --timezone Asia/Kolkata

# Deliver terminal run results to one HTTPS webhook. The signing secret is
# read from the worker environment when delivery happens and is not stored.
export ASH_AUTOMATION_WEBHOOK_SECRET='replace-me'
ash cron add release-watch --prompt 'Check release status' --every 30m \
  --webhook-url 'https://hooks.example.com/ash/result' \
  --webhook-secret-env ASH_AUTOMATION_WEBHOOK_SECRET
```

Use either the job ID or its case-insensitive name:

```bash
ash cron list --all
ash cron show weekday-review
ash cron pause weekday-review
ash cron resume weekday-review
ash cron run weekday-review
ash cron history weekday-review --limit 20
ash cron deliveries weekday-review --limit 20
ash cron cancel RUN_ID
ash cron remove weekday-review --yes
```

Every command that reads or changes structured state also supports `--json`.
Prompts can be read from standard input with `--prompt -`. Active jobs have a
case-insensitive unique name within one workspace.

### Webhook result delivery

Webhook delivery is an optional durable outbox attached to a job. Ash snapshots
the configured target when a run becomes terminal, so a worker restart after
the model turn does not silently lose a result notification. The public
contract is intentionally narrow:

- targets must use HTTPS and a public host;
- embedded URL credentials, query strings, and fragments are rejected;
- redirects are not followed;
- DNS is resolved with a bounded pre-dispatch deadline and the connection is
  pinned to those accepted public addresses, preventing private-address and
  DNS-rebinding pivots after the dispatch fence;
- request bodies are bounded JSON and never include the stored automation
  prompt or local workspace path;
- `Idempotency-Key` and `X-Ash-Delivery-Id` carry one stable delivery ID;
- `--webhook-secret-env NAME` adds
  `X-Ash-Signature: sha256=<HMAC-SHA256(body)>` using the value of `NAME` from
  the worker environment. Only the environment-variable name is stored. Use a
  dedicated secret variable: Ash removes that variable from the unattended model
  child's environment and fails closed for collisions with known provider, command,
  and built-in runtime variables the child must preserve. On Linux, while that
  child is alive, the
  worker also disables process dumpability so the child cannot recover the
  signing secret from the worker parent via `/proc/<pid>/environ`. Because Ash
  does not yet have an equivalent parent-process environment isolation primitive
  on macOS or Windows, unattended jobs configured with `--webhook-secret-env`
  fail closed there; unsigned webhook delivery remains cross-platform. Do not
  reuse the signing variable for provider, command, web search, A2A, MCP, plugin,
  or other tool credentials.

Webhook URLs themselves are stored verbatim because the worker needs the exact
target. Treat them as sensitive configuration: some providers encode tokens in
the URL path. Routine CLI and model-facing output therefore shows only the
redacted target origin/path marker rather than echoing the full path.

Ash does **not** assume that receiving servers honor the idempotency header.
Immediately before the POST, the worker persists a dispatch fence. If the
worker dies, times out, or loses the connection after that fence without a
provable HTTP response, delivery becomes `ambiguous` and is not automatically
replayed. A lease that expires before the fence is safe to requeue. Known 2xx
responses become `delivered`; known non-2xx responses become `failed`.
`ash cron deliveries [JOB]` shows these states. Delivery state never rewrites
the underlying automation run's success or failure.

## Run a worker

Creating a job does not create a hidden background process. Run one continuous
worker for each workspace:

```bash
cd /absolute/path/to/project
ash cron worker
```

For an existing system scheduler, `ash cron worker --once` claims one due batch,
waits for it, and exits. Atomic claims make overlapping worker processes safe at
the durable lease layer: one job has at most one run record in the `running`
state at a time. An expired lease is recovered as `interrupted` rather than
replaying that same occurrence; a later scheduled occurrence may then be
claimed. Different jobs can run concurrently up to
`automation_max_concurrent_runs` per worker. The command exits `1` if any
claimed run fails or becomes interrupted, and its `--json` summary reports each
terminal status. A continuous worker writes every terminal run to standard
output; `--json` makes that stream JSON Lines for log collectors.

For a Linux user service, first use `command -v ash` to find the absolute
executable path, then create `~/.config/systemd/user/ash-cron-project.service`:

```ini
[Unit]
Description=Ash automation worker for project
After=network-online.target

[Service]
Type=simple
WorkingDirectory=/absolute/path/to/project
ExecStart=/home/USER/.local/bin/ash cron worker
Restart=on-failure
RestartSec=5

[Install]
WantedBy=default.target
```

Load and inspect it with:

```bash
systemctl --user daemon-reload
systemctl --user enable --now ash-cron-project.service
systemctl --user status ash-cron-project.service
```

On macOS use a per-user `launchd` agent, and on Windows use Task Scheduler or a
service manager, with the project as the working directory and `ash cron
worker` as the command. Do not rely on a terminal window remaining open.

`ash cron status` reports enabled jobs, active runs, and fresh worker
heartbeats. `ash doctor` performs read-only SQLite integrity and foreign-key
checks and warns when enabled jobs have no live worker. A stale heartbeat is not
proof that the old process is dead; leases, rather than heartbeat rows, decide
run ownership.

## Safety and permissions

The worker revalidates the workspace directory and persistent trust before
claiming work and again before runtime creation. Revoking trust stops new work:

```bash
ash trust remove /absolute/path/to/project
```

Scheduled turns use the same central permission policy, sandbox, hooks, MCP,
skills, plugins, and provider configuration as ordinary Ash turns. There is no
interactive approval callback. Read-only tools and matching persistent allow
rules can execute; an `ask` decision is denied. Add narrowly scoped persistent
rules with `ash permissions allow`, inspect them with `ash permissions status`,
and retain deny rules for sensitive paths and commands. `auto_approve` still
requires Ash's ordinary sandbox safety checks and should not be enabled merely
to make a schedule pass.

Long-running workers reload user configuration before claims and immediately
before runtime creation. Disabling automation or tightening permissions,
sandbox, network, plugin, provider, or environment settings therefore applies
without restarting the service. A continuous worker removes its heartbeat and
pauses while automation is disabled or workspace trust is absent, then resumes
when the condition is corrected. Changing `db_directory` requires restarting
the worker so one process never splits state across databases. On POSIX, each
production turn runs in an isolated process group and carries a worker lifeline:
if a live runner observes abrupt worker death, Ash hard-stops that runner group.
Ordinary child commands that inherit the group are therefore covered by timeout,
cancellation, shutdown, and the abrupt-worker lifeline.

That process-group boundary is not a portable container. A permitted command
that deliberately daemonizes, creates a new session/process group (for example
via `setsid()`), or starts an independently managed service can outlive the run.
Lease expiry also proves that durable ownership expired, not that every external
side effect stopped. Do not use unattended automation to launch detached
services unless their lifecycle and idempotency are managed separately. Ash
does not claim crash-safe non-overlap for arbitrary external side effects.

Automation settings are user-owned. A repository `.ash/config.toml` cannot
enable automation, increase worker concurrency or leases, choose persistence
paths, or weaken permissions. Relevant user TOML or `ASH_*` settings are:

```toml
automation_enabled = true
automation_max_concurrent_runs = 2
automation_poll_seconds = 1.0
automation_lease_seconds = 60.0
automation_run_retention_days = 30
```

The database is `automation.db` under `db_directory`; schema migrations are
applied transactionally when a worker or cron command opens it. The database
and its WAL and shared-memory sidecars are mode `0600` on POSIX. Prompts are
stored verbatim, so do not put credentials in them. Bounded redacted responses,
usage, errors, and lifecycle events are also persisted. Credentials are loaded
at execution and are never copied into a job record.

Webhook signing secrets follow the same rule: the database stores only their
environment-variable name. Pending or actively leased webhook outbox rows keep
their terminal run row from being removed by retention maintenance; once
delivery is terminal (`delivered`, `failed`, or `ambiguous`), ordinary run
retention applies.

## Timing and recovery contract

- Intervals are elapsed-time schedules anchored at creation. If several fires
  are missed, Ash runs at most one eligible occurrence and advances directly to
  the next future occurrence.
- Cron uses five fields and an IANA timezone. During a fall-back repeated hour,
  both real instants fire. A nonexistent spring-forward wall time follows
  wall-clock normalization, for example `02:30` executes at the first valid
  local time after the gap. Daily times outside the gap are not skipped when
  the UTC offset changes.
- `misfire_grace_seconds` defaults to one day. A due occurrence older than its
  grace is recorded as `skipped`; recurring schedules advance and one-shots
  become terminal.
- The next scheduled time is committed before model execution. If a worker
  loses its renewable lease, the run becomes `interrupted` and is never
  replayed because its external side effects are ambiguous.
- One-shot jobs disable when claimed. A completed or expired one-shot cannot be
  resumed; create a new one instead.
- `timeout_seconds` covers runtime initialization and the complete agent turn.
  `token_budget` also bounds aggregate prompt plus completion usage across the
  complete turn. Exhaustion stops before a pending tool call can cause side
  effects and is persisted as a failed run. Output and error fields are
  redacted and size bounded.
- Cancellation requests are detected on the lease-renewal interval, no slower
  than 20 seconds with supported settings, then terminate the isolated runtime
  process. Worker shutdown cancels and finalizes owned tasks before removing
  its heartbeat.
- Terminal run rows expire according to `automation_run_retention_days` during
  hourly maintenance while no runs are active. The worker prunes through its
  already-open automation database connection instead of reopening
  `automation.db` by pathname; the database operation runs off the event loop
  and is settled before worker cancellation completes. Lifecycle events remain
  as the audit ledger after bulky run output is pruned.
- The worker records the visible main `automation.db` file identity at startup
  and requires restart if a later loop observes that file missing, non-regular,
  unavailable, or replaced. This is a fail-closed restart detector for a
  persistent identity change, not a guarantee against a transient same-account
  file swap that occurs entirely between validation checks.
- `session_retention_days` remains enforced by normal Ash runtime/CLI startup.
  The durable automation worker does not independently reopen `sessions.db` for
  destructive retention because it does not own a stable session-database
  connection; when session retention is enabled it preserves those sessions and
  emits a one-time warning instead of risking cleanup against a replaced file.

Because interrupted outcomes are deliberately not retried, inspect the
workspace and any external system before manually running that job again.

## Python SDK

An `AshClient` uses its configured database and workspace boundary:

```python
job = client.create_automation(
    "weekday review",
    "Review open risks",
    cron="30 9 * * mon-fri",
    timezone="Asia/Kolkata",
    webhook_url="https://hooks.example.com/ash/result",
    webhook_secret_env="ASH_AUTOMATION_WEBHOOK_SECRET",
)
client.pause_automation(job.job_id)
client.resume_automation(job.job_id)
runs = client.automation_runs(job.job_id, limit=20)
deliveries = client.automation_deliveries(job.job_id, limit=20)
```

`claim_automation()` is the low-level external-executor API. It returns a
capability token that must be renewed and finalized through `AutomationStore`;
losing it intentionally produces an interrupted, non-replayed outcome. Most
applications should operate the standard worker instead.
