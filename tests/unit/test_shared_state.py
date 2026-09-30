# tests/unit/test_shared_state.py
import asyncio
import sqlite3
import threading
import pytest
import ash.agents.shared_state as shared_state_module
from ash.agents.shared_state import SharedState
from ash.agents.tasks import AgentTaskStore
import tempfile
from pathlib import Path


@pytest.fixture
def state() -> SharedState:
    with tempfile.TemporaryDirectory() as tmpdir:
        shared = SharedState(Path(tmpdir) / "test.db")
        try:
            yield shared
        finally:
            shared.close()


def test_shared_state_closes_connection_when_schema_init_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeConnection:
        def __init__(self) -> None:
            self.row_factory = None
            self.closed = False

        def close(self) -> None:
            self.closed = True

    connection = FakeConnection()

    class FakeDatabase:
        path = tmp_path / "agents.db"

        def connect(self, **kwargs):
            del kwargs
            return connection

    class FakePinnedSQLiteDatabase:
        @staticmethod
        def prepare(*args, **kwargs):
            del args, kwargs
            return FakeDatabase()

    monkeypatch.setattr(
        shared_state_module,
        "PinnedSQLiteDatabase",
        FakePinnedSQLiteDatabase,
    )
    monkeypatch.setattr(
        SharedState,
        "_init_db",
        lambda self: (_ for _ in ()).throw(RuntimeError("schema init failed")),
    )

    with pytest.raises(RuntimeError, match="schema init failed"):
        SharedState(tmp_path / "agents.db")

    assert connection.closed is True


def test_open_existing_uses_parent_initialized_schema_without_ddl(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "agents.db"
    parent = SharedState(database)
    task = parent.tasks.create_task("existing durable task", task_id="existing-task")

    def unexpected_init(*args, **kwargs):
        del args, kwargs
        raise AssertionError("open_existing must not run schema initialization")

    monkeypatch.setattr(SharedState, "_init_db", unexpected_init)
    monkeypatch.setattr(AgentTaskStore, "_init_db", unexpected_init)
    child = SharedState.open_existing(database)
    try:
        loaded = child.tasks.get_task(task.task_id)
        assert loaded is not None
        assert loaded.description == "existing durable task"
    finally:
        child.close()
        parent.close()


def test_open_existing_does_not_create_missing_agent_database(tmp_path: Path) -> None:
    database = tmp_path / "missing" / "agents.db"

    with pytest.raises(ValueError, match="does not exist"):
        SharedState.open_existing(database)

    assert not database.exists()
    assert not database.parent.exists()


def test_shared_state_rollback_journal_supports_multiple_owners(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import ash.sqlite_utils as sqlite_utils

    monkeypatch.setattr(
        sqlite_utils,
        "preferred_sqlite_journal_mode",
        lambda version=None: "DELETE",
    )
    database = tmp_path / "agents.db"
    first = SharedState(database)
    second = SharedState(database)
    try:
        task = first.tasks.create_task(
            "rollback journal task",
            task_id="rollback-journal-task",
        )
        loaded = second.tasks.get_task(task.task_id)

        assert first._conn.execute("PRAGMA journal_mode").fetchone()[0].upper() == "DELETE"
        assert loaded is not None
        assert loaded.description == "rollback journal task"
    finally:
        second.close()
        first.close()


@pytest.mark.asyncio
async def test_concurrent_status_updates_do_not_race(state):
    """Multiple concurrent update_status calls should not corrupt state."""
    # Register agents first (update_status does UPDATE not INSERT)
    state.register_agent("agent-a", role="general")
    state.register_agent("agent-b", role="general")
    state.register_agent("agent-c", role="general")

    async def update_many(agent_id, count):
        for i in range(count):
            await state.update_status_async(
                agent_id, "working", current_task=f"task-{i}"
            )

    await asyncio.gather(
        update_many("agent-a", 10),
        update_many("agent-b", 10),
        update_many("agent-c", 10),
    )

    agents = {st.agent_id: st for st in state.list_agents()}
    assert len(agents) == 3
    # All agents should have completed without exceptions
    for agent_id in ["agent-a", "agent-b", "agent-c"]:
        assert agent_id in agents


@pytest.mark.asyncio
async def test_concurrent_send_and_fetch(state):
    """Concurrent IPC send + fetch should not lose messages."""

    async def send_messages(sender, count):
        for i in range(count):
            await state.send_message_async(sender, "lead", "test", {"message": f"msg-{i}"})

    await asyncio.gather(
        send_messages("agent-1", 5),
        send_messages("agent-2", 5),
    )

    messages = state.fetch_messages("lead", undelivered_only=False)
    assert len(messages) == 10


def test_fetch_messages_rejects_unbounded_limits(state) -> None:
    with pytest.raises(ValueError, match="between 1 and 10000"):
        state.fetch_messages("lead", limit=10_001)
    with pytest.raises(ValueError, match="between 1 and 10000"):
        state.fetch_messages("lead", limit=0)


@pytest.mark.asyncio
async def test_sync_failure_cannot_be_committed_by_concurrent_async_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first_executed = threading.Event()
    release_first = threading.Event()
    second_executed = threading.Event()

    class RaceConnection(sqlite3.Connection):
        armed = False

        def execute(self, sql, parameters=(), /):
            cursor = super().execute(sql, parameters)
            if self.armed and "UPDATE agent_status" in " ".join(sql.split()):
                agent_id = parameters[-1]
                if agent_id == "sync-agent":
                    first_executed.set()
                    if not release_first.wait(5):
                        raise RuntimeError("test did not release first transaction")
                    raise RuntimeError("synthetic failure after sync write")
                if agent_id == "async-agent":
                    second_executed.set()
            return cursor

    original_connect = sqlite3.connect

    def race_connect(*args, **kwargs):
        kwargs["factory"] = RaceConnection
        return original_connect(*args, **kwargs)

    with monkeypatch.context() as scoped:
        scoped.setattr(shared_state_module.sqlite3, "connect", race_connect)
        state = SharedState(tmp_path / "transaction-race.db")

    try:
        state.register_agent("sync-agent")
        state.register_agent("async-agent")
        assert isinstance(state._conn, RaceConnection)
        state._conn.armed = True
        sync_errors: list[BaseException] = []

        def sync_writer() -> None:
            try:
                state.update_status("sync-agent", "working", "sync-write")
            except BaseException as exc:
                sync_errors.append(exc)

        thread = threading.Thread(target=sync_writer, name="sync-writer")
        thread.start()
        assert await asyncio.to_thread(first_executed.wait, 5)

        async_write = asyncio.create_task(
            state.update_status_async("async-agent", "working", "async-write")
        )
        await asyncio.sleep(0.05)
        assert second_executed.is_set() is False

        release_first.set()
        await async_write
        thread.join(5)

        assert len(sync_errors) == 1
        assert isinstance(sync_errors[0], RuntimeError)
        assert state.get_status("sync-agent").status == "idle"
        assert state.get_status("async-agent").status == "working"
    finally:
        release_first.set()
        state.close()


def test_agent_to_agent_messages(state):
    state.register_agent("agent-a", role="general")
    state.register_agent("agent-b", role="general")

    state.send_to_agent("agent-a", "agent-b", "test", "hello from a")
    messages = state.fetch_messages("agent-b", undelivered_only=False)
    assert len(messages) == 1
    assert messages[0].content == {"content": "hello from a"}


def test_workspace_bound_shared_state_isolates_agent_identity_ipc_and_sprints(
    tmp_path: Path,
) -> None:
    database = tmp_path / "agents.db"
    workspace_a = tmp_path / "workspace-a"
    workspace_b = tmp_path / "workspace-b"
    workspace_a.mkdir()
    workspace_b.mkdir()
    first = SharedState(database, workspace=workspace_a)
    second = SharedState(database, workspace=workspace_b)
    try:
        first.register_agent("worker", metadata={"source": "a"})
        first.update_status("worker", "working", "from-a")
        second.register_agent("worker", metadata={"source": "b"})
        second.update_status("worker", "idle", "from-b")

        first_status = first.get_status("worker")
        second_status = second.get_status("worker")
        assert first_status is not None
        assert second_status is not None
        assert first_status.agent_id == "worker"
        assert second_status.agent_id == "worker"
        assert first_status.current_task == "from-a"
        assert second_status.current_task == "from-b"
        assert first_status.metadata == {
            "source": "a",
            "workspace": str(workspace_a.resolve()),
        }
        assert second_status.metadata == {
            "source": "b",
            "workspace": str(workspace_b.resolve()),
        }
        assert [item.agent_id for item in first.list_agents()] == ["worker"]
        assert [item.agent_id for item in second.list_agents()] == ["worker"]

        first_message = first.send_message(
            "worker",
            "lead",
            "test",
            {"workspace": "a"},
        )
        second_message = second.send_message(
            "worker",
            "lead",
            "test",
            {"workspace": "b"},
        )

        assert [m.message_id for m in first.fetch_messages("lead")] == [
            first_message
        ]
        assert [m.message_id for m in second.fetch_messages("lead")] == [
            second_message
        ]
        assert second.mark_delivered([first_message]) == 0
        assert first.fetch_messages("lead")[0].delivered is False
        assert first.mark_delivered([first_message]) == 1

        first_sprint = first.create_sprint("lead", "workspace a")
        second_sprint = second.create_sprint("lead", "workspace b")
        assert first.get_sprint(first_sprint) is not None
        assert first.get_sprint(second_sprint) is None
        assert second.get_sprint(second_sprint) is not None
        assert second.get_sprint(first_sprint) is None
        assert [item.sprint_id for item in first.list_sprints()] == [first_sprint]
        assert [item.sprint_id for item in second.list_sprints()] == [second_sprint]
    finally:
        first.close()
        second.close()


def test_workspace_bound_approval_resolution_cannot_cross_workspace(
    tmp_path: Path,
) -> None:
    database = tmp_path / "agents.db"
    workspace_a = tmp_path / "workspace-a"
    workspace_b = tmp_path / "workspace-b"
    workspace_a.mkdir()
    workspace_b.mkdir()
    first = SharedState(database, workspace=workspace_a)
    second = SharedState(database, workspace=workspace_b)
    try:
        first.tasks.create_task(
            "approval task",
            task_id="approval-a",
            metadata={"workspace": str(workspace_a)},
        )
        lease = first.tasks.claim_task("worker", task_id="approval-a")
        assert lease is not None
        first.tasks.start_task("approval-a", lease.token)
        request_id = first.send_message(
            "worker",
            "lead",
            "approval_request",
            {
                "task_id": "approval-a",
                "attempt": lease.task.attempt,
                "agent_id": "worker",
                "tool_name": "write_file",
                "arguments_sha256": "a" * 64,
                "arguments_preview": "{}",
            },
        )

        with pytest.raises(ValueError, match="unknown approval request"):
            second.resolve_approval_request(request_id, approved=True)

        resolution = first.resolve_approval_request(request_id, approved=True)

        assert resolution["approved"] is True
        assert resolution["agent_id"] == "worker"
        assert second.fetch_messages("worker", undelivered_only=False) == []
        responses = first.fetch_messages("worker", undelivered_only=False)
        assert [message.message_type for message in responses] == [
            "approval_response"
        ]
    finally:
        first.close()
        second.close()


def test_ipc_payload_is_bounded_and_requires_valid_json(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(shared_state_module, "MAX_IPC_MESSAGE_BYTES", 32)
    state = SharedState(tmp_path / "agents.db", workspace=tmp_path)
    try:
        with pytest.raises(ValueError, match="IPC content exceeds"):
            state.send_message("worker", "lead", "test", {"value": "x" * 64})
        with pytest.raises(ValueError, match="valid JSON values"):
            state.send_message("worker", "lead", "test", {"value": float("nan")})

        assert state.fetch_messages("lead", undelivered_only=False) == []
    finally:
        state.close()


def test_delivered_ipc_history_prunes_per_workspace_without_dropping_pending(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        shared_state_module,
        "MAX_DELIVERED_IPC_HISTORY_PER_WORKSPACE",
        2,
    )
    database = tmp_path / "agents.db"
    workspace_a = tmp_path / "workspace-a"
    workspace_b = tmp_path / "workspace-b"
    workspace_a.mkdir()
    workspace_b.mkdir()
    first = SharedState(database, workspace=workspace_a)
    second = SharedState(database, workspace=workspace_b)
    try:
        delivered = [
            first.send_message("worker", "lead", "test", {"index": index})
            for index in range(3)
        ]
        pending = first.send_message("worker", "lead", "test", {"pending": True})
        other_workspace = second.send_message(
            "worker",
            "lead",
            "test",
            {"workspace": "b"},
        )

        assert first.mark_delivered(delivered) == 3

        first_messages = first.fetch_messages("lead", undelivered_only=False)
        second_messages = second.fetch_messages("lead", undelivered_only=False)

        assert [message.message_id for message in first_messages] == [
            delivered[1],
            delivered[2],
            pending,
        ]
        assert first.fetch_messages("lead")[-1].message_id == pending
        assert [message.message_id for message in second_messages] == [
            other_workspace
        ]
        assert second_messages[0].delivered is False
    finally:
        first.close()
        second.close()


def test_pending_ipc_backlog_is_bounded_per_recipient_and_reopens_after_delivery(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        shared_state_module,
        "MAX_PENDING_IPC_MESSAGES_PER_RECIPIENT",
        2,
    )
    state = SharedState(tmp_path / "agents.db", workspace=tmp_path)
    try:
        first = state.send_message("worker", "lead", "test", {"index": 1})
        state.send_message("worker", "lead", "test", {"index": 2})

        with pytest.raises(ValueError, match="pending-message limit reached"):
            state.send_message("worker", "lead", "test", {"index": 3})

        assert state.mark_delivered([first]) == 1
        third = state.send_message("worker", "lead", "test", {"index": 3})

        assert [message.message_id for message in state.fetch_messages("lead")] == [
            first + 1,
            third,
        ]
    finally:
        state.close()


def test_terminal_agent_status_history_is_bounded_without_pruning_live_agents(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        shared_state_module,
        "MAX_TERMINAL_AGENT_STATUS_PER_WORKSPACE",
        2,
    )
    state = SharedState(tmp_path / "agents.db", workspace=tmp_path)
    try:
        for agent_id in ("done-a", "done-b", "done-c"):
            state.register_agent(agent_id)
            state.update_status(agent_id, "completed", agent_id)
        state.register_agent("live")
        state.update_status("live", "working", "still running")

        statuses = state.list_agents()
        terminal = [item for item in statuses if item.status == "completed"]
        live = [item for item in statuses if item.status == "working"]

        assert len(terminal) == 2
        assert [item.agent_id for item in live] == ["live"]
        assert state.get_status("live") is not None
    finally:
        state.close()


def test_terminal_sprint_history_is_bounded_without_pruning_active_sprints(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        shared_state_module,
        "MAX_TERMINAL_SPRINT_HISTORY_PER_WORKSPACE",
        2,
    )
    state = SharedState(tmp_path / "agents.db", workspace=tmp_path)
    try:
        terminal_ids = []
        for index in range(3):
            sprint_id = state.create_sprint("lead", f"terminal-{index}")
            state.update_sprint_state(sprint_id, "complete")
            terminal_ids.append(sprint_id)
        active_id = state.create_sprint("lead", "active")
        state.update_sprint_state(active_id, "active")

        sprints = state.list_sprints()
        terminal = [item for item in sprints if item.state == "complete"]
        active = [item for item in sprints if item.state == "active"]

        assert len(terminal) == 2
        assert [item.sprint_id for item in active] == [active_id]
        assert state.get_sprint(active_id) is not None
        assert sum(
            state.get_sprint(sprint_id) is not None for sprint_id in terminal_ids
        ) == 2
    finally:
        state.close()


def test_broadcast(state):
    state.register_agent("agent-a", role="general")
    state.register_agent("agent-b", role="general")
    state.register_agent("agent-c", role="general")

    state.broadcast("agent-a", "ping", {"content": "checkin"})
    for agent_id in ["agent-b", "agent-c"]:
        msgs = state.fetch_messages(agent_id, undelivered_only=False)
        assert len(msgs) == 1
        assert msgs[0].content == {"content": "checkin"}


def test_close_is_idempotent(tmp_path: Path) -> None:
    state = SharedState(tmp_path / "agents.db")

    state.close()
    state.close()


def test_close_attempts_all_owned_connections_and_retries_after_failure() -> None:
    class FlakyTasks:
        def __init__(self) -> None:
            self.close_calls = 0

        def close(self) -> None:
            self.close_calls += 1
            if self.close_calls == 1:
                raise RuntimeError("task-store close failed once")

    class TrackingConnection:
        def __init__(self) -> None:
            self.close_calls = 0

        def close(self) -> None:
            self.close_calls += 1

    state = object.__new__(SharedState)
    state._conn_lock = threading.RLock()
    state._closed = False
    state.tasks = FlakyTasks()
    state._conn = TrackingConnection()

    with pytest.raises(RuntimeError, match="task-store close failed once"):
        state.close()

    assert state.tasks.close_calls == 1
    assert state._conn.close_calls == 1
    assert state._closed is False

    state.close()

    assert state.tasks.close_calls == 2
    assert state._conn.close_calls == 2
    assert state._closed is True


def test_context_manager_preserves_primary_error_when_close_fails() -> None:
    class FailingTasks:
        def close(self) -> None:
            raise RuntimeError("state cleanup failed")

    class TrackingConnection:
        def close(self) -> None:
            pass

    state = object.__new__(SharedState)
    state._conn_lock = threading.RLock()
    state._closed = False
    state.tasks = FailingTasks()
    state._conn = TrackingConnection()

    with pytest.raises(RuntimeError, match="primary query failure") as captured:
        with state:
            raise RuntimeError("primary query failure")

    assert state._closed is False
    assert any(
        "agent shared-state cleanup failed" in note
        for note in captured.value.__notes__
    )


def test_retire_stale_approval_requests_preserves_active_and_retires_retried(
    tmp_path: Path,
) -> None:
    state = SharedState(tmp_path / "approval-recovery.db")
    state.tasks.create_task(
        "approval recovery",
        task_id="approval-recovery",
        max_attempts=2,
    )
    lease = state.tasks.claim_task("worker-a", task_id="approval-recovery")
    assert lease is not None
    state.tasks.start_task("approval-recovery", lease.token)
    request_id = state.send_message(
        "worker-a",
        "lead",
        "approval_request",
        {
            "task_id": "approval-recovery",
            "attempt": lease.task.attempt,
            "agent_id": "worker-a",
            "tool_name": "write_file",
            "arguments_sha256": "b" * 64,
            "arguments_preview": "{}",
        },
    )

    assert state.retire_stale_approval_requests() == []
    retried = state.tasks.fail_task(
        "approval-recovery",
        lease.token,
        "retry",
        retryable=True,
    )
    assert retried.state == "queued"
    lease2 = state.tasks.claim_task("worker-b", task_id="approval-recovery")
    assert lease2 is not None
    state.tasks.start_task("approval-recovery", lease2.token)

    assert state.retire_stale_approval_requests() == [request_id]
    request = state.fetch_messages(
        "lead",
        undelivered_only=False,
        message_type="approval_request",
    )[0]
    assert request.delivered is True
    state.close()
