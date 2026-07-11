# Ash Agent Tasks v1

Agent Tasks v1 is the durable coordination contract beneath provider-backed
subagents. It prevents in-memory asyncio tasks from being the sole record of
ownership, capacity, budgets, and results. The contract is stored in the same
WAL-mode SQLite database as agent status and IPC.

## Task model

Each task records:

- a stable task ID, description, role, optional parent and sprint lineage;
- zero or more prerequisite task IDs forming an acyclic creation-order DAG;
- state: `queued`, `leased`, `running`, `succeeded`, `failed`, or `cancelled`;
- owner agent ID, opaque lease hash and expiration;
- current and maximum attempts;
- token and wall-clock budgets plus recorded token use;
- bounded JSON metadata and terminal result, or a bounded error;
- durable typed artifacts with URI, optional SHA-256, metadata, and timestamp.

Dependencies must already exist when a task is created, which makes cycles
impossible without a graph rewrite. A task becomes claimable only after every
dependency succeeds. Failed or cancelled prerequisites terminally fail queued
dependents instead of leaving them blocked forever.

## Claims and recovery

`AgentTaskStore.claim_task` uses `BEGIN IMMEDIATE`, so separate Ash processes
cannot claim the same task or exceed the configured live-agent capacity. A
successful claim returns the task and a random 256-bit bearer token; only its
SHA-256 hash is stored. Starting, renewing, accounting, completing, and failing
the task require that token and a non-expired lease.

Live workers renew at one third of the configured lease duration. Expired
leases are requeued while attempts remain and fail with `worker lease expired`
after exhaustion. An old worker cannot complete work after recovery or
reassignment because its token no longer matches. Cancellation clears ownership
and recursively cancels dependent tasks.

## Events

Every material task transition is written in the same SQLite transaction as
the state change. The append-only `agent_task_events` log uses the public v1
event envelope and records creation, claim, start, token accounting, retry,
recovery, terminal state, dependency propagation, cancellation, and artifact
registration. Event payloads are redacted before persistence. Lease renewals
are intentionally not logged because they are high-frequency heartbeats rather
than state transitions.

`spawn_agent` also publishes creation, claim, start, terminal state, and
artifact events to the active runtime event sink. Those session-scoped events
let streaming clients observe current activity; the SQLite task event log is
the durable source for cross-session and post-crash replay.

## Budgets

`ASH_MAX_CONCURRENT_AGENTS` defaults to 4 and is enforced transactionally across
processes. `ASH_AGENT_TOKEN_BUDGET` defaults to 4000 completion tokens;
provider-reported usage is preferred and deterministic estimates are used when
the provider omits usage. Exceeding the budget fails the task, agent status,
report, and tool result consistently. `ASH_AGENT_TIME_BUDGET_SECONDS` defaults
to 900 and cancels the model turn at the deadline. `ASH_AGENT_LEASE_SECONDS`
defaults to 30 and controls crash-detection latency. These security and resource
controls are user-owned and cannot be weakened by project configuration.

## Durable graph dispatch

`delegate_agents` accepts up to 32 keyed tasks in one call. Dependencies refer
to keys in that call. Ash validates every role, dependency, budget, identifier,
lineage edge, and cycle before writing, then creates all task rows, dependency
edges, and creation events in one transaction. A rejected graph leaves no
partial tasks.

The runtime-owned dispatcher claims every ready task through the same atomic
lease path as `spawn_agent`, runs independent tasks in parallel up to the
cross-process capacity limit, and waits for prerequisites before starting
dependents. Retryable failures are requeued up to `max_attempts`; each attempt
uses a fresh lease and agent ID. Starting or resuming an Ash runtime wakes the
dispatcher, so queued work and expired leases continue after a process restart.
Foreground delegation returns every terminal state, result, and error;
`background=true` returns after durable submission.
Graph cancellation is atomic, recursively covers graph dependents, revokes
active leases, and is observed by local workers at their next 100 ms control
poll rather than waiting for lease expiry.

Dependent tasks that need predecessor file changes should currently use
`isolation="shared"`. Isolated worktree commits are recorded as artifacts but
are not automatically accepted or merged into a dependent worktree.

## Live subagents

`spawn_agent` creates and immediately claims a durable task before worktree or
provider execution. If global capacity is full, the attempted task is cancelled
with an actionable error and no worker starts. Foreground and background runs
then heartbeat the lease, persist completion-token use, and commit the final
report into the task record. Stopping or shutting down a background worker
cancels its durable task. Isolated branch commits are registered as
`git-commit` artifacts.

Use the operator interface from any later process:

```console
ash agents tasks
ash agents tasks --state running --owner reviewer-1
ash agents tasks --json
ash agents tasks --graph GRAPH_ID --json
ash agents events --task TASK_ID
ash agents events --type agent.task.failed --after 100 --json
ash agents cancel GRAPH_ID --yes
```

The JSON output includes lineage, dependencies, attempts, budgets, result or
error, lease timestamps, metadata, and artifacts.

Embedded callers use the typed equivalents:

```python
tasks = client.agent_tasks(state="failed", limit=50)
artifacts = client.agent_artifacts(tasks[0].task_id)
events = client.agent_task_events(task_id=tasks[0].task_id, after_sequence=0)
graph = await client.delegate_agents(
    "review the change",
    [
        {"key": "inspect", "role": "researcher", "task": "inspect the code"},
        {
            "key": "review",
            "role": "reviewer",
            "task": "review the findings",
            "depends_on": ["inspect"],
        },
    ],
)
client.cancel_agent_graph(graph.graph_id, reason="superseded")
```

## Current boundary

This contract provides durable scheduling primitives, live single-task
subagents, and automatic dispatch of submitted DAGs. Dynamic dependency
insertion, artifact acceptance policies, graph-wide budget allocation, and
remote workers are intentionally separate future layers. They must build on
these ownership transitions rather than bypass them.
