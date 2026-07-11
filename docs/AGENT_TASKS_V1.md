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

## Budgets

`ASH_MAX_CONCURRENT_AGENTS` defaults to 4 and is enforced transactionally across
processes. `ASH_AGENT_TOKEN_BUDGET` defaults to 4000 completion tokens;
provider-reported usage is preferred and deterministic estimates are used when
the provider omits usage. Exceeding the budget fails the task, agent status,
report, and tool result consistently. `ASH_AGENT_TIME_BUDGET_SECONDS` defaults
to 900 and cancels the model turn at the deadline. `ASH_AGENT_LEASE_SECONDS`
defaults to 30 and controls crash-detection latency. These security and resource
controls are user-owned and cannot be weakened by project configuration.

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
```

The JSON output includes lineage, dependencies, attempts, budgets, result or
error, lease timestamps, metadata, and artifacts.

Embedded callers use the typed equivalents:

```python
tasks = client.agent_tasks(state="failed", limit=50)
artifacts = client.agent_artifacts(tasks[0].task_id)
```

## Current boundary

This contract provides durable scheduling primitives and integrates live
single-task subagents. Automatic dispatch of a queued multi-stage DAG, dynamic
dependency insertion, artifact acceptance policies, canonical task event
envelopes, and remote workers are intentionally separate future layers. They
must build on these ownership transitions rather than bypass them.
