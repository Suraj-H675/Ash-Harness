import json
import time
from pathlib import Path

import pytest

from ash.agents.shared_state import SharedState
from ash.agents.tasks import (
    AgentGraphBudget,
    AgentTaskBudgetExceeded,
    AgentTaskCreate,
    AgentTaskError,
)


@pytest.fixture
def state(tmp_path: Path):
    value = SharedState(tmp_path / "agents.db")
    yield value
    value.close()


def test_task_dependencies_claim_in_ready_order(state: SharedState) -> None:
    first = state.tasks.create_task("inspect", task_id="inspect")
    second = state.tasks.create_task(
        "implement",
        task_id="implement",
        dependencies=[first.task_id],
    )

    lease = state.tasks.claim_task("worker-a")

    assert lease is not None
    assert lease.task.task_id == "inspect"
    state.tasks.start_task("inspect", lease.token)
    state.tasks.complete_task("inspect", lease.token, {"summary": "done"})
    next_lease = state.tasks.claim_task("worker-b")
    assert next_lease is not None
    assert next_lease.task.task_id == second.task_id


def test_task_graph_is_created_atomically_in_any_definition_order(
    state: SharedState,
) -> None:
    tasks = state.tasks.create_tasks(
        [
            AgentTaskCreate(
                "review",
                task_id="review",
                parent_task_id="implement",
                dependencies=("implement",),
            ),
            AgentTaskCreate("implement", task_id="implement"),
        ]
    )

    assert [task.task_id for task in tasks] == ["review", "implement"]
    assert state.tasks.get_task("review").parent_task_id == "implement"
    lease = state.tasks.claim_task("worker")
    assert lease is not None
    assert lease.task.task_id == "implement"


def test_task_graph_rejects_cycles_and_rolls_back_whole_batch(
    state: SharedState,
) -> None:
    with pytest.raises(ValueError, match="dependency cycle"):
        state.tasks.create_tasks(
            [
                AgentTaskCreate("one", task_id="one", dependencies=("two",)),
                AgentTaskCreate("two", task_id="two", dependencies=("one",)),
            ]
        )
    assert state.tasks.list_tasks() == []

    with pytest.raises(AgentTaskError, match="unknown dependency"):
        state.tasks.create_tasks(
            [
                AgentTaskCreate("valid", task_id="valid"),
                AgentTaskCreate(
                    "invalid", task_id="invalid", dependencies=("missing",)
                ),
            ]
        )
    assert state.tasks.list_tasks() == []


def test_task_graph_collision_does_not_partially_insert(state: SharedState) -> None:
    state.tasks.create_task("existing", task_id="existing")

    with pytest.raises(AgentTaskError, match="already exists"):
        state.tasks.create_tasks(
            [
                AgentTaskCreate("new", task_id="new"),
                AgentTaskCreate("collision", task_id="existing"),
            ]
        )

    assert {task.task_id for task in state.tasks.list_tasks()} == {"existing"}


def test_atomic_capacity_limit_is_shared_across_connections(tmp_path: Path) -> None:
    first_state = SharedState(tmp_path / "agents.db")
    second_state = SharedState(tmp_path / "agents.db")
    try:
        first_state.tasks.create_task("one", task_id="one")
        first_state.tasks.create_task("two", task_id="two")

        first = first_state.tasks.claim_task("worker-a", max_active=1)
        second = second_state.tasks.claim_task("worker-b", max_active=1)

        assert first is not None
        assert second is None
    finally:
        first_state.close()
        second_state.close()


def test_stale_lease_is_requeued_then_exhausted(
    state: SharedState, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = [time.time()]
    monkeypatch.setattr("ash.agents.tasks.time.time", lambda: clock[0])
    state.tasks.create_task("retry", task_id="retry", max_attempts=2)
    first = state.tasks.claim_task("worker-a", task_id="retry", lease_seconds=1)
    assert first is not None
    state.tasks.start_task("retry", first.token)
    clock[0] += 2

    assert state.tasks.recover_expired() == ["retry"]
    assert state.tasks.get_task("retry").state == "queued"
    second = state.tasks.claim_task("worker-b", task_id="retry", lease_seconds=1)
    assert second is not None
    clock[0] += 2

    assert state.tasks.recover_expired() == ["retry"]
    exhausted = state.tasks.get_task("retry")
    assert exhausted.state == "failed"
    assert exhausted.error == "worker lease expired"


def test_stale_or_wrong_owner_cannot_complete(state: SharedState) -> None:
    state.tasks.create_task("owned", task_id="owned")
    lease = state.tasks.claim_task("worker", task_id="owned")
    assert lease is not None

    with pytest.raises(AgentTaskError, match="another lease"):
        state.tasks.complete_task("owned", "wrong-token", {})

    state.tasks.cancel_task("owned")
    with pytest.raises(AgentTaskError, match="not actively leased"):
        state.tasks.complete_task("owned", lease.token, {})


def test_token_budget_fails_task_and_revokes_lease(state: SharedState) -> None:
    state.tasks.create_task("budgeted", task_id="budgeted", token_budget=10)
    lease = state.tasks.claim_task("worker", task_id="budgeted")
    assert lease is not None
    state.tasks.start_task("budgeted", lease.token)
    state.tasks.record_tokens("budgeted", lease.token, 8)

    with pytest.raises(AgentTaskBudgetExceeded, match="exceeded"):
        state.tasks.record_tokens("budgeted", lease.token, 3)

    task = state.tasks.get_task("budgeted")
    assert task.state == "failed"
    assert task.used_tokens == 11


def test_cancellation_cascades_to_dependent_tasks(state: SharedState) -> None:
    state.tasks.create_task("parent", task_id="parent")
    state.tasks.create_task("child", task_id="child", dependencies=["parent"])
    state.tasks.create_task("grandchild", task_id="grand", dependencies=["child"])

    cancelled = state.tasks.cancel_task("parent", reason="user stopped plan")

    assert set(cancelled) == {"parent", "child", "grand"}
    assert {task.state for task in state.tasks.list_tasks()} == {"cancelled"}
    first_events = state.tasks.list_events(event_type="agent.task.cancelled")
    state.tasks.cancel_task("parent", reason="repeated cancellation")
    assert state.tasks.list_events(event_type="agent.task.cancelled") == first_events


def test_graph_cancellation_is_atomic_filterable_and_idempotent(
    state: SharedState,
) -> None:
    graph_id = "graph-cancel"
    state.tasks.create_tasks(
        [
            AgentTaskCreate(
                "one",
                task_id="graph-one",
                metadata={"graph_id": graph_id},
            ),
            AgentTaskCreate(
                "two",
                task_id="graph-two",
                dependencies=("graph-one",),
                metadata={"graph_id": graph_id},
            ),
        ]
    )
    state.tasks.create_task("unrelated", task_id="unrelated")
    lease = state.tasks.claim_task("worker", task_id="graph-one")
    assert lease is not None
    state.tasks.start_task("graph-one", lease.token)

    cancelled = state.tasks.cancel_graph(graph_id, reason="operator stopped graph")

    assert set(cancelled) == {"graph-one", "graph-two"}
    assert {task.state for task in state.tasks.list_tasks(graph_id=graph_id)} == {
        "cancelled"
    }
    assert state.tasks.get_task("unrelated").state == "queued"
    first_events = state.tasks.list_events(event_type="agent.task.cancelled")
    state.tasks.cancel_graph(graph_id, reason="repeated")
    assert state.tasks.list_events(event_type="agent.task.cancelled") == first_events

    with pytest.raises(AgentTaskError, match="unknown task graph"):
        state.tasks.cancel_graph("missing-graph")


def test_graph_token_budget_fails_consuming_task_and_exposes_usage(
    state: SharedState,
) -> None:
    graph_id = "budgeted-graph"
    tasks = state.tasks.create_tasks(
        [
            AgentTaskCreate(
                "one",
                task_id="budget-one",
                metadata={"graph_id": graph_id},
                    token_budget=15,
                graph_token_budget=20,
            ),
            AgentTaskCreate(
                "two",
                task_id="budget-two",
                dependencies=("budget-one",),
                metadata={"graph_id": graph_id},
                token_budget=15,
                graph_token_budget=20,
            ),
        ]
    )
    first = state.tasks.claim_task("worker", task_id="budget-one")
    assert first is not None
    state.tasks.start_task("budget-one", first.token)
    state.tasks.record_tokens("budget-one", first.token, 12)

    second = state.tasks.get_task("budget-two")
    assert second is not None and second.state == "queued"
    state.tasks.fail_task("budget-one", first.token, "operator stopped")

    budget = state.tasks.get_graph_budget(graph_id)
    assert budget == (
        AgentGraphBudget(
            graph_id=graph_id,
            token_budget=20,
            used_tokens=12,
            remaining_tokens=8,
            task_count=2,
        )
    )
    assert [task.used_tokens for task in tasks] == [0, 0]

    with pytest.raises(AgentTaskError, match="unknown task graph"):
        state.tasks.get_graph_budget("missing-graph")


def test_graph_token_budget_exceeded_fails_owner_atomically(state: SharedState) -> None:
    graph_id = "over-budget-graph"
    state.tasks.create_tasks(
        [
            AgentTaskCreate(
                "one",
                task_id="over-budget-one",
                metadata={"graph_id": graph_id},
                token_budget=100,
                graph_token_budget=20,
            ),
            AgentTaskCreate(
                "two",
                task_id="over-budget-two",
                metadata={"graph_id": graph_id},
                token_budget=100,
                graph_token_budget=20,
            ),
        ]
    )
    lease = state.tasks.claim_task("worker", task_id="over-budget-one")
    assert lease is not None
    state.tasks.start_task("over-budget-one", lease.token)
    state.tasks.record_tokens("over-budget-one", lease.token, 15)

    with pytest.raises(AgentTaskBudgetExceeded, match="graph token budget"):
        state.tasks.record_tokens("over-budget-one", lease.token, 10)

    failed = state.tasks.get_task("over-budget-one")
    assert failed is not None
    assert failed.state == "failed"
    assert failed.used_tokens == 25
    assert failed.error == "graph token budget exceeded: 25 > 20"
    events = state.tasks.list_events(event_type="agent.task.failed")
    assert len(events) == 1
    assert events[0].event["reason"] == "graph_token_budget_exceeded"
    assert events[0].event["graph_used_tokens"] == 25


def test_failed_dependency_is_terminally_propagated(state: SharedState) -> None:
    state.tasks.create_task("parent", task_id="parent")
    state.tasks.create_task("child", task_id="child", dependencies=["parent"])
    lease = state.tasks.claim_task("worker", task_id="parent")
    assert lease is not None
    state.tasks.fail_task("parent", lease.token, "broken")

    assert state.tasks.claim_task("other") is None
    child = state.tasks.get_task("child")
    assert child.state == "failed"
    assert child.error == "dependency did not succeed"


def test_artifacts_are_durable_and_digest_validated(
    tmp_path: Path,
) -> None:
    state = SharedState(tmp_path / "agents.db")
    state.tasks.create_task("produce", task_id="produce")
    artifact = state.tasks.add_artifact(
        "produce",
        kind="git-commit",
        uri="refs/heads/ash-agent/worker",
        sha256="a" * 64,
        metadata={"commit": "abc"},
    )
    state.close()

    reopened = SharedState(tmp_path / "agents.db")
    try:
        loaded = reopened.tasks.list_artifacts("produce")
        assert loaded == [artifact]
        with pytest.raises(ValueError, match="sha256"):
            reopened.tasks.add_artifact(
                "produce", kind="file", uri="output.txt", sha256="bad"
            )
    finally:
        reopened.close()


def test_task_contract_rejects_missing_dependencies_and_non_json_metadata(
    state: SharedState,
) -> None:
    with pytest.raises(AgentTaskError, match="unknown dependency"):
        state.tasks.create_task("bad", dependencies=["missing"])
    with pytest.raises(ValueError, match="JSON"):
        state.tasks.create_task("bad", metadata={"value": float("nan")})
    with pytest.raises(ValueError, match="portable identifier"):
        state.tasks.create_task("bad", task_id="not portable")


def test_task_events_are_ordered_redacted_and_cursor_replayable(
    tmp_path: Path,
) -> None:
    state = SharedState(tmp_path / "events.db")
    task = state.tasks.create_task(
        "eventful",
        task_id="eventful",
        metadata={"api_key": "sk-abcdefghijklmnop"},
    )
    lease = state.tasks.claim_task("worker", task_id=task.task_id)
    assert lease is not None
    state.tasks.start_task(task.task_id, lease.token)
    state.tasks.record_tokens(task.task_id, lease.token, 7)
    state.tasks.fail_task(
        task.task_id,
        lease.token,
        "provider exposed sk-abcdefghijklmnop",
        retryable=False,
    )

    events = state.tasks.list_events(task_id=task.task_id)
    assert [item.event["type"] for item in events] == [
        "agent.task.created",
        "agent.task.leased",
        "agent.task.running",
        "agent.task.tokens_recorded",
        "agent.task.failed",
    ]
    assert [item.sequence for item in events] == sorted(
        item.sequence for item in events
    )
    assert all(item.event["schema_version"] == 1 for item in events)
    assert all(item.event["source"]["type"] == "agent_task" for item in events)
    assert events[0].event["token_budget"] == 4000
    assert "sk-abcdefghijklmnop" not in json.dumps(events[-1].event)
    replay = state.tasks.list_events(
        after_sequence=events[1].sequence,
        event_type="agent.task.failed",
    )
    assert [item.sequence for item in replay] == [events[-1].sequence]


def test_task_events_cover_retry_dependency_failure_and_artifacts(
    tmp_path: Path,
) -> None:
    state = SharedState(tmp_path / "events.db")
    parent = state.tasks.create_task("parent", task_id="parent", max_attempts=2)
    state.tasks.create_task("child", task_id="child", dependencies=[parent.task_id])
    lease = state.tasks.claim_task("worker", task_id=parent.task_id)
    assert lease is not None
    state.tasks.start_task(parent.task_id, lease.token)
    state.tasks.fail_task(parent.task_id, lease.token, "transient", retryable=True)
    retry = state.tasks.claim_task("worker-2", task_id=parent.task_id)
    assert retry is not None
    state.tasks.start_task(parent.task_id, retry.token)
    state.tasks.fail_task(parent.task_id, retry.token, "terminal")
    assert state.tasks.claim_task("worker-3") is None
    state.tasks.add_artifact(parent.task_id, kind="report", uri="report.json")

    event_types = [item.event["type"] for item in state.tasks.list_events()]
    assert "agent.task.retrying" in event_types
    assert event_types.count("agent.task.failed") == 2
    assert "agent.task.artifact.created" in event_types


def test_task_event_replay_validates_cursor_filter_and_limit(tmp_path: Path) -> None:
    state = SharedState(tmp_path / "events.db")
    state.tasks.create_task("one", task_id="one")
    with pytest.raises(ValueError, match="after_sequence"):
        state.tasks.list_events(after_sequence=-1)
    with pytest.raises(ValueError, match="limit"):
        state.tasks.list_events(limit=0)
    with pytest.raises(ValueError, match="portable identifier"):
        state.tasks.list_events(event_type="not valid")
