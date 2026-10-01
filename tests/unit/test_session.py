import asyncio
import json
import os
import sqlite3
import subprocess
import sys
import threading
import time
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

import pytest

from ash.core.goals import GoalState
from ash.core.session import (
    Message,
    SessionResolutionError,
    SessionStore,
    SessionStorageError,
    ToolCallRecord,
    get_db_connection,
    write_transaction,
)
from ash.sqlite_utils import preferred_sqlite_journal_mode


def test_session_creation_initializes_required_tables(tmp_path: Path) -> None:
    db_path = tmp_path / "session_store.db"
    store = SessionStore(db_path)

    session = store.create_session(project_path=str(tmp_path))
    loaded = store.load_session(session.session_id)

    assert loaded.session_id == session.session_id
    assert loaded.project_path == str(tmp_path)
    assert loaded.created_at == session.created_at
    assert loaded.messages == []
    assert loaded.tool_calls == []

    with get_db_connection(db_path) as conn:
        table_names = {
            row["name"]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
        index_names = {
            row["name"]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'index'"
            ).fetchall()
        }

    assert {
        "sessions",
        "messages",
        "tool_calls",
        "audit_logs",
        "runtime_events",
    }.issubset(table_names)
    assert {
        "idx_messages_session",
        "idx_tool_calls_session",
        "idx_audit_session",
        "idx_runtime_events_session_sequence",
        "idx_runtime_events_turn_sequence",
    }.issubset(index_names)
    with get_db_connection(db_path) as conn:
        assert (
            conn.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0]
            == 16
        )
        assert "mcp_tasks" in table_names
        assert {
            "idx_mcp_tasks_session_status",
            "idx_mcp_tasks_session_turn",
        }.issubset(index_names)
        audit_columns = {
            row["name"] for row in conn.execute("PRAGMA table_info(audit_logs)")
        }
        assert "previous_hash" in audit_columns
        session_columns = {
            row["name"] for row in conn.execute("PRAGMA table_info(sessions)")
        }
        assert "total_cache_read_tokens" in session_columns
        assert "context_summary_message_count" in session_columns
        assert "total_cache_write_tokens" in session_columns
        assert "estimated_prompt_tokens" in session_columns
        assert "estimated_completion_tokens" in session_columns
        assert "estimated_cost_usd" in session_columns
        assert "project_key" in session_columns
        assert {
            "parent_session_id",
            "root_session_id",
            "fork_message_count",
            "branch_name",
            "branch_summary",
            "depth",
        }.issubset(session_columns)
        assert {"idx_sessions_parent", "idx_sessions_root_depth"}.issubset(index_names)
        assert "idx_sprints_session_state_created" in index_names
        assert "idx_goals_session_updated" in index_names
        assert "idx_goals_one_current_per_session" in index_names
        assert "goals" in table_names
        assert "turn_id" in {
            row["name"] for row in conn.execute("PRAGMA table_info(messages)")
        }
        assert "turn_id" in {
            row["name"] for row in conn.execute("PRAGMA table_info(tool_calls)")
        }
        assert "usage_json" in {
            row["name"] for row in conn.execute("PRAGMA table_info(turn_journal)")
        }
        assert "recovery_json" in {
            row["name"] for row in conn.execute("PRAGMA table_info(turn_journal)")
        }
        assert "call_id" in {
            row["name"] for row in conn.execute("PRAGMA table_info(file_checkpoints)")
        }


def test_v15_migration_adds_context_summary_message_count(tmp_path: Path) -> None:
    db_path = tmp_path / "v14.db"
    store = SessionStore(db_path)
    session = store.create_session(str(tmp_path))
    store.save_context_summary(session.session_id, "legacy summary")

    with closing(get_db_connection(db_path)) as conn, conn:
        conn.execute("ALTER TABLE sessions DROP COLUMN context_summary_message_count")
        conn.execute("DROP INDEX IF EXISTS idx_sprints_session_state_created")
        conn.execute("DELETE FROM schema_migrations WHERE version >= 15")

    migrated = SessionStore(db_path)

    assert len(list(tmp_path.glob("v14.db.before-v16-migration.*.backup"))) == 1
    with get_db_connection(db_path) as conn:
        assert conn.execute(
            "SELECT MAX(version) FROM schema_migrations"
        ).fetchone()[0] == 16
        columns = {
            row["name"] for row in conn.execute("PRAGMA table_info(sessions)")
        }
        assert "context_summary_message_count" in columns
        indexes = {
            row["name"]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'index'"
            )
        }
        assert "idx_sprints_session_state_created" in indexes
    loaded = migrated.load_session(session.session_id)
    assert loaded.context_summary == "legacy summary"
    assert loaded.context_summary_message_count == 0


def test_v16_migration_adds_durable_goals(tmp_path: Path) -> None:
    db_path = tmp_path / "v15.db"
    store = SessionStore(db_path)
    session = store.create_session(str(tmp_path))

    with closing(get_db_connection(db_path)) as conn, conn:
        conn.execute("DROP TABLE goals")
        conn.execute("DELETE FROM schema_migrations WHERE version >= 16")

    migrated = SessionStore(db_path)

    assert len(list(tmp_path.glob("v15.db.before-v16-migration.*.backup"))) == 1
    with get_db_connection(db_path) as conn:
        assert conn.execute(
            "SELECT MAX(version) FROM schema_migrations"
        ).fetchone()[0] == 16
        assert conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'goals'"
        ).fetchone() is not None
    goal = migrated.create_goal(
        session.session_id,
        "Finish the migration",
        max_continuations=3,
    )
    assert goal.state is GoalState.ACTIVE


def test_goal_lifecycle_is_durable_bounded_and_redacted(tmp_path: Path) -> None:
    store = SessionStore(tmp_path / "goals.db")
    session = store.create_session(str(tmp_path))
    goal = store.create_goal(
        session.session_id,
        "Fix auth with OPENAI_API_KEY=sk-proj-abcdefghijklmnopqrstuvwxyz",
        max_continuations=2,
    )

    assert goal.state is GoalState.ACTIVE
    assert "sk-proj-" not in goal.objective
    assert store.load_current_goal(session.session_id) == goal
    with pytest.raises(ValueError, match="already has a current Goal"):
        store.create_goal(session.session_id, "Second Goal", max_continuations=2)

    goal = store.record_goal_progress(
        goal.goal_id,
        "Observed OPENAI_API_KEY=sk-proj-abcdefghijklmnopqrstuvwxyz in fixture",
    )
    assert "sk-proj-" not in goal.last_evidence

    goal = store.claim_goal_continuation(goal.goal_id)
    assert goal.continuations_used == 1
    assert goal.state is GoalState.ACTIVE
    goal = store.claim_goal_continuation(goal.goal_id)
    assert goal.continuations_used == 2
    assert goal.state is GoalState.ACTIVE
    goal = store.claim_goal_continuation(goal.goal_id)
    assert goal.state is GoalState.BUDGET_LIMITED

    goal = store.resume_current_goal(session.session_id)
    assert goal.state is GoalState.ACTIVE
    assert goal.continuations_used == 0
    goal = store.pause_current_goal(session.session_id)
    assert goal is not None
    assert goal.state is GoalState.PAUSED
    goal = store.resume_current_goal(session.session_id)
    assert goal.state is GoalState.ACTIVE

    completed = store.record_goal_progress(
        goal.goal_id,
        "Targeted tests pass",
        complete=True,
    )
    assert completed.state is GoalState.COMPLETE
    assert completed.completed_at is not None
    assert store.load_current_goal(session.session_id) is None


def test_session_preserves_opaque_provider_replay_state(tmp_path: Path) -> None:
    store = SessionStore(tmp_path / "provider-state.db")
    session = store.create_session(str(tmp_path))
    now = datetime.now(timezone.utc)
    replay_state = [
        {
            "type": "reasoning",
            "id": "rs_1",
            "summary": [],
            "status": "completed",
            "encrypted_content": "opaque-encrypted-reasoning",
        }
    ]

    store.save_message(
        session.session_id,
        Message(
            role="assistant",
            content="",
            timestamp=now,
            metadata={"provider_state": replay_state},
        ),
    )

    loaded = store.load_session(session.session_id)
    assert loaded.messages[-1].metadata["provider_state"] == replay_state

    replacement = store.create_goal(
        session.session_id,
        "Follow-up cleanup",
        max_continuations=1,
    )
    cleared = store.clear_current_goal(session.session_id)
    assert cleared.goal_id == replacement.goal_id
    assert cleared.state is GoalState.CLEARED
    assert store.load_current_goal(session.session_id) is None


def test_runtime_session_load_uses_persisted_compaction_window(tmp_path: Path) -> None:
    store = SessionStore(tmp_path / "sessions.db")
    session = store.create_session(str(tmp_path))
    for index in range(6):
        store.save_message(
            session.session_id,
            Message(
                role="user" if index % 2 == 0 else "assistant",
                content=f"message-{index}",
                timestamp=datetime.now(timezone.utc),
            ),
        )
    store.save_tool_call(
        session.session_id,
        ToolCallRecord(
            call_id="call-history",
            tool_name="read_file",
            arguments={"file_path": "README.md"},
            approved=True,
            executed=True,
            result="ok",
            timestamp=datetime.now(timezone.utc),
        ),
    )
    store.save_context_summary(
        session.session_id,
        "summary of the first four messages",
        summarized_message_count=4,
    )

    full = store.load_session(session.session_id)
    runtime = store.load_session(session.session_id, runtime_window=True)

    assert [message.content for message in full.messages] == [
        f"message-{index}" for index in range(6)
    ]
    assert len(full.tool_calls) == 1
    assert full.resident_message_offset == 0
    assert full.context_summary_message_count == 4
    assert [message.content for message in runtime.messages] == [
        "message-4",
        "message-5",
    ]
    assert [call.call_id for call in runtime.tool_calls] == ["call-history"]
    assert runtime.resident_message_offset == 4
    assert runtime.context_summary_message_count == 4
    assert runtime.resident_history_is_windowed is True


def test_runtime_session_load_bounds_recent_tool_call_tail(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import ash.core.session as session_module

    monkeypatch.setattr(session_module, "MAX_RUNTIME_SESSION_TOOL_CALLS", 2)
    store = SessionStore(tmp_path / "sessions.db")
    session = store.create_session(str(tmp_path))
    for index in range(4):
        store.save_tool_call(
            session.session_id,
            ToolCallRecord(
                call_id=f"call-{index}",
                tool_name="read_file",
                arguments={"file_path": f"{index}.txt"},
                approved=True,
                executed=True,
                result=f"result-{index}",
                timestamp=datetime(2026, 1, 1, 0, 0, index, tzinfo=timezone.utc),
            ),
        )

    runtime = store.load_session(session.session_id, runtime_window=True)
    full = store.load_session(session.session_id)

    assert [call.call_id for call in runtime.tool_calls] == ["call-2", "call-3"]
    assert [call.call_id for call in full.tool_calls] == [
        "call-0",
        "call-1",
        "call-2",
        "call-3",
    ]


def test_mcp_task_state_is_durable_and_updatable(tmp_path: Path) -> None:
    db_path = tmp_path / "session_store.db"
    store = SessionStore(db_path)
    session = store.create_session(project_path=str(tmp_path))
    initial = {
        "taskId": "task-1",
        "status": "working",
        "createdAt": "2026-09-20T00:00:00Z",
        "lastUpdatedAt": "2026-09-20T00:00:01Z",
        "ttlMs": 60_000,
    }
    store.save_mcp_task(
        task_id="task-1",
        session_id=session.session_id,
        turn_id="turn-1",
        call_id="call-1",
        server_name="server",
        remote_tool_name="slow",
        contract_fingerprint="contract",
        server_fingerprint="server-fingerprint",
        protocol_version="2026-07-28",
        task=initial,
        answered_inputs={},
    )
    with get_db_connection(db_path) as conn:
        created_at = conn.execute(
            "SELECT created_at FROM mcp_tasks WHERE task_id = 'task-1'"
        ).fetchone()[0]

    completed = {
        **initial,
        "status": "completed",
        "lastUpdatedAt": "2026-09-20T00:00:02Z",
        "result": {"content": [{"type": "text", "text": "done"}]},
    }
    store.save_mcp_task(
        task_id="task-1",
        session_id=session.session_id,
        turn_id="turn-1",
        call_id="call-1",
        server_name="server",
        remote_tool_name="slow",
        contract_fingerprint="contract",
        server_fingerprint="server-fingerprint",
        protocol_version="2026-07-28",
        task=completed,
        answered_inputs={"approve": "fingerprint"},
    )

    rows = store.list_mcp_tasks(session.session_id)
    assert len(rows) == 1
    row = rows[0]
    assert row["task_id"] == "task-1"
    assert row["status"] == "completed"
    assert row["created_at"] == created_at
    assert row["server_name"] == "server"
    assert row["remote_tool_name"] == "slow"
    assert row["contract_fingerprint"] == "contract"
    assert row["server_fingerprint"] == "server-fingerprint"
    assert row["protocol_version"] == "2026-07-28"
    assert row["task_json"] == json.dumps(
        completed, ensure_ascii=False, sort_keys=True
    )
    assert row["answered_inputs_json"] == json.dumps(
        {"approve": "fingerprint"}, ensure_ascii=False, sort_keys=True
    )

    store.delete_mcp_task("server", "task-1")
    assert store.list_mcp_tasks(session.session_id) == []


def test_mcp_task_durable_state_has_count_and_payload_limits(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import ash.core.session as session_module

    monkeypatch.setattr(session_module, "MAX_DURABLE_MCP_TASKS_PER_SESSION", 2)
    monkeypatch.setattr(session_module, "MAX_DURABLE_MCP_TASK_STATE_BYTES", 256)
    monkeypatch.setattr(
        session_module,
        "MAX_DURABLE_MCP_ANSWERED_INPUTS_BYTES",
        128,
    )
    monkeypatch.setattr(session_module, "MAX_DURABLE_MCP_ANSWERED_INPUTS", 2)
    store = SessionStore(tmp_path / "sessions.db")
    session = store.create_session(project_path=str(tmp_path))
    base_task = {
        "taskId": "task",
        "status": "working",
        "createdAt": "2026-09-20T00:00:00Z",
        "lastUpdatedAt": "2026-09-20T00:00:01Z",
    }

    def save(task_id: str, call_id: str, *, task=None, answered=None) -> None:
        payload = dict(base_task if task is None else task)
        payload["taskId"] = task_id
        store.save_mcp_task(
            task_id=task_id,
            session_id=session.session_id,
            turn_id=f"turn-{call_id}",
            call_id=call_id,
            server_name="server",
            remote_tool_name="slow",
            contract_fingerprint="contract",
            server_fingerprint="server-fingerprint",
            protocol_version="2026-07-28",
            task=payload,
            answered_inputs={} if answered is None else answered,
        )

    save("task-1", "call-1")
    save("task-2", "call-2")
    # Updating an existing row must not consume another capacity slot.
    save("task-1", "call-1", answered={"approve": "yes"})

    with pytest.raises(ValueError, match="capacity exceeded"):
        save("task-3", "call-3")
    with pytest.raises(ValueError, match="task state exceeds 256"):
        save(
            "task-1",
            "call-1",
            task={**base_task, "padding": "x" * 300},
        )
    with pytest.raises(ValueError, match="answered-input history exceeds 2 entries"):
        save(
            "task-1",
            "call-1",
            answered={"a": "1", "b": "2", "c": "3"},
        )
    with pytest.raises(ValueError, match="answered-input history exceeds 128"):
        save("task-1", "call-1", answered={"a": "x" * 200})
    with pytest.raises(ValueError, match="strict JSON"):
        save("task-1", "call-1", task={**base_task, "value": float("nan")})

    # Simulate an oversized legacy/corrupt row set that predates the write cap.
    with closing(get_db_connection(store.db_path)) as conn, conn:
        conn.execute(
            """
            INSERT INTO mcp_tasks (
                task_id, session_id, turn_id, call_id, server_name,
                remote_tool_name, contract_fingerprint, server_fingerprint,
                protocol_version, status, task_json, answered_inputs_json,
                created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "task-legacy",
                session.session_id,
                "turn-legacy",
                "call-legacy",
                "legacy-server",
                "slow",
                "contract",
                "legacy-fingerprint",
                "2026-07-28",
                "working",
                json.dumps({**base_task, "taskId": "task-legacy"}),
                "{}",
                "2026-09-20T00:00:00Z",
                "2026-09-20T00:00:01Z",
            ),
        )

    with pytest.raises(SessionStorageError, match="task count exceeds"):
        store.list_mcp_tasks(session.session_id)


def test_mcp_task_ids_are_namespaced_by_server_and_cannot_be_reassigned(
    tmp_path: Path,
) -> None:
    store = SessionStore(tmp_path / "sessions.db")
    first = store.create_session(project_path=str(tmp_path))
    second = store.create_session(project_path=str(tmp_path))
    task = {
        "taskId": "shared-id",
        "status": "working",
        "createdAt": "2026-09-20T00:00:00Z",
        "lastUpdatedAt": "2026-09-20T00:00:01Z",
        "ttlMs": None,
    }
    for server_name, session_id, call_id in (
        ("alpha", first.session_id, "call-a"),
        ("beta", second.session_id, "call-b"),
    ):
        store.save_mcp_task(
            task_id="shared-id",
            session_id=session_id,
            turn_id=f"turn-{call_id}",
            call_id=call_id,
            server_name=server_name,
            remote_tool_name="slow",
            contract_fingerprint="contract",
            server_fingerprint=f"fingerprint-{server_name}",
            protocol_version="2026-07-28",
            task=task,
            answered_inputs={},
        )

    assert len(store.list_mcp_tasks(first.session_id)) == 1
    assert len(store.list_mcp_tasks(second.session_id)) == 1

    with pytest.raises(ValueError, match="reused a durable taskId"):
        store.save_mcp_task(
            task_id="shared-id",
            session_id=second.session_id,
            turn_id="turn-other",
            call_id="call-other",
            server_name="alpha",
            remote_tool_name="slow",
            contract_fingerprint="contract",
            server_fingerprint="fingerprint-alpha",
            protocol_version="2026-07-28",
            task=task,
            answered_inputs={},
        )

    with pytest.raises(ValueError, match="server identity"):
        store.save_mcp_task(
            task_id="shared-id",
            session_id=first.session_id,
            turn_id="turn-call-a",
            call_id="call-a",
            server_name="alpha",
            remote_tool_name="slow",
            contract_fingerprint="contract",
            server_fingerprint="replacement-fingerprint",
            protocol_version="2026-07-28",
            task=task,
            answered_inputs={},
        )

    row = store.list_mcp_tasks(first.session_id)[0]
    assert row["server_fingerprint"] == "fingerprint-alpha"


def test_v12_migration_adds_mcp_task_table_with_backup(tmp_path: Path) -> None:
    db_path = tmp_path / "v11.db"
    SessionStore(db_path)
    with closing(get_db_connection(db_path)) as conn, conn:
        conn.execute("DROP TABLE mcp_tasks")
        conn.execute("DELETE FROM schema_migrations WHERE version >= 12")

    SessionStore(db_path)

    assert len(list(tmp_path.glob("v11.db.before-v16-migration.*.backup"))) == 1
    with get_db_connection(db_path) as conn:
        assert conn.execute(
            "SELECT MAX(version) FROM schema_migrations"
        ).fetchone()[0] == 16
        assert conn.execute(
            "SELECT COUNT(*) FROM sqlite_master "
            "WHERE type = 'table' AND name = 'mcp_tasks'"
        ).fetchone()[0] == 1
        assert "server_fingerprint" in {
            row["name"] for row in conn.execute("PRAGMA table_info(mcp_tasks)")
        }


def test_v13_migration_binds_existing_mcp_task_table_to_server_identity(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "v12.db"
    SessionStore(db_path)
    with closing(get_db_connection(db_path)) as conn, conn:
        conn.execute("DROP TABLE mcp_tasks")
        conn.executescript(
            """
            CREATE TABLE mcp_tasks (
                task_id TEXT NOT NULL,
                session_id TEXT NOT NULL,
                turn_id TEXT NOT NULL,
                call_id TEXT NOT NULL,
                server_name TEXT NOT NULL,
                remote_tool_name TEXT NOT NULL,
                contract_fingerprint TEXT NOT NULL,
                protocol_version TEXT NOT NULL,
                status TEXT NOT NULL,
                task_json TEXT NOT NULL,
                answered_inputs_json TEXT NOT NULL DEFAULT '{}',
                created_at TIMESTAMP NOT NULL,
                updated_at TIMESTAMP NOT NULL,
                PRIMARY KEY(server_name, task_id),
                UNIQUE(session_id, call_id)
            );
            """
        )
        conn.execute("DELETE FROM schema_migrations WHERE version >= 13")

    SessionStore(db_path)

    assert len(list(tmp_path.glob("v12.db.before-v16-migration.*.backup"))) == 1
    with get_db_connection(db_path) as conn:
        columns = {
            row["name"] for row in conn.execute("PRAGMA table_info(mcp_tasks)")
        }
        assert "server_fingerprint" in columns
        assert conn.execute(
            "SELECT MAX(version) FROM schema_migrations"
        ).fetchone()[0] == 16


def test_v14_migration_scopes_tool_call_and_event_ids_to_sessions(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "v13.db"
    store = SessionStore(db_path)
    first = store.create_session(project_path="/workspace/first")
    second = store.create_session(project_path="/workspace/second")
    with closing(get_db_connection(db_path)) as conn, conn:
        conn.execute("DROP TABLE tool_calls")
        conn.execute("DROP TABLE runtime_events")
        conn.executescript(
            """
            CREATE TABLE tool_calls (
                call_id TEXT PRIMARY KEY,
                session_id TEXT NOT NULL,
                tool_name TEXT NOT NULL,
                arguments_json TEXT NOT NULL,
                approved INTEGER CHECK(approved IN (0, 1)) DEFAULT 0,
                executed INTEGER CHECK(executed IN (0, 1)) DEFAULT 0,
                dispatched INTEGER CHECK(dispatched IN (0, 1)) DEFAULT 0,
                result TEXT,
                error TEXT,
                timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                turn_id TEXT,
                FOREIGN KEY(session_id) REFERENCES sessions(session_id) ON DELETE CASCADE
            );
            CREATE TABLE runtime_events (
                sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                event_id TEXT NOT NULL UNIQUE,
                session_id TEXT NOT NULL,
                turn_id TEXT,
                operation_id TEXT,
                event_type TEXT NOT NULL,
                schema_version INTEGER NOT NULL,
                timestamp TIMESTAMP NOT NULL,
                event_json TEXT NOT NULL,
                FOREIGN KEY(session_id) REFERENCES sessions(session_id) ON DELETE CASCADE
            );
            """
        )
        conn.execute(
            """
            INSERT INTO tool_calls (
                call_id, session_id, tool_name, arguments_json, approved,
                executed, dispatched, timestamp, turn_id
            ) VALUES (?, ?, ?, ?, 1, 0, 1, ?, ?)
            """,
            (
                "shared-call-id",
                first.session_id,
                "write_file",
                '{"path":"first.txt"}',
                "2026-06-02T11:00:00+00:00",
                "turn-first",
            ),
        )
        first_event = {
            "schema_version": 1,
            "event_id": "shared-event-id",
            "timestamp": "2026-07-10T00:00:00+00:00",
            "source": {"type": "runtime", "id": "ash"},
            "session_id": first.session_id,
            "turn_id": "turn-first",
            "operation_id": None,
            "parent_event_id": None,
            "type": "turn.started",
        }
        conn.execute(
            """
            INSERT INTO runtime_events (
                event_id, session_id, turn_id, operation_id, event_type,
                schema_version, timestamp, event_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "shared-event-id",
                first.session_id,
                "turn-first",
                None,
                "turn.started",
                1,
                "2026-07-10T00:00:00+00:00",
                json.dumps(first_event, sort_keys=True, separators=(",", ":")),
            ),
        )
        conn.execute("DELETE FROM schema_migrations WHERE version >= 14")

    migrated = SessionStore(db_path)

    assert len(list(tmp_path.glob("v13.db.before-v16-migration.*.backup"))) == 1
    with get_db_connection(db_path) as conn:
        assert conn.execute(
            "SELECT MAX(version) FROM schema_migrations"
        ).fetchone()[0] == 16
    assert migrated.load_session(first.session_id).tool_calls[0].call_id == (
        "shared-call-id"
    )
    assert migrated.list_runtime_events(first.session_id)[0].event["event_id"] == (
        "shared-event-id"
    )

    migrated.save_tool_call(
        second.session_id,
        ToolCallRecord(
            call_id="shared-call-id",
            tool_name="read_file",
            arguments={"file_path": "second.txt"},
            approved=False,
            executed=False,
            timestamp=datetime(2026, 6, 2, 11, 1, tzinfo=timezone.utc),
        ),
        turn_id="turn-second",
    )
    assert migrated.save_runtime_events(
        [
            {
                **first_event,
                "session_id": second.session_id,
                "turn_id": "turn-second",
                "type": "turn.completed",
            }
        ]
    ) == 1
    assert migrated.load_session(first.session_id).tool_calls[0].tool_name == "write_file"
    assert migrated.load_session(second.session_id).tool_calls[0].tool_name == "read_file"
    assert [
        item.event["type"] for item in migrated.list_runtime_events(second.session_id)
    ] == ["turn.completed"]


def test_session_store_rejects_linked_database_file_and_parent(tmp_path: Path) -> None:
    target = tmp_path / "target.db"
    with sqlite3.connect(target) as connection:
        connection.execute("CREATE TABLE marker(value TEXT)")
    linked_file = tmp_path / "sessions.db"
    outside = tmp_path / "outside"
    outside.mkdir()
    linked_parent = tmp_path / "linked-db"
    try:
        linked_file.symlink_to(target)
        linked_parent.symlink_to(outside, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"symlinks are unavailable: {exc}")

    for database in (linked_file, linked_parent / "sessions.db"):
        with pytest.raises(SessionStorageError, match="symlink or junction"):
            SessionStore(database)

    assert not (outside / "sessions.db").exists()
    with sqlite3.connect(target) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='sessions'"
        ).fetchone()[0] == 0


def test_session_store_rejects_database_identity_replacement(tmp_path: Path) -> None:
    database = tmp_path / "sessions.db"
    replacement = tmp_path / "replacement.db"
    moved = tmp_path / "sessions-original.db"
    store = SessionStore(database)
    session = store.create_session("/workspace")
    replacement_store = SessionStore(replacement)
    replacement_store.create_session("/replacement")

    try:
        database.rename(moved)
        replacement.rename(database)

        with pytest.raises(SessionStorageError, match="file identity changed"):
            store.load_session(session.session_id)
    finally:
        if database.exists() and moved.exists():
            database.unlink()
            moved.rename(database)


def test_legacy_database_is_backed_up_and_migrated(tmp_path: Path) -> None:
    db_path = tmp_path / "legacy.db"
    with sqlite3.connect(db_path) as conn:
        conn.executescript(
            """
            CREATE TABLE sessions (
                session_id TEXT PRIMARY KEY,
                project_path TEXT NOT NULL,
                created_at TIMESTAMP
            );
            CREATE TABLE messages (
                message_id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL,
                role TEXT,
                content TEXT NOT NULL,
                timestamp TIMESTAMP,
                metadata_json TEXT
            );
            INSERT INTO sessions VALUES ('legacy', '/workspace', '2026-01-01T00:00:00+00:00');
            """
        )

    store = SessionStore(db_path)

    assert store.load_session("legacy").session_id == "legacy"
    backups = list(tmp_path.glob("legacy.db.before-v16-migration.*.backup"))
    assert len(backups) == 1
    with sqlite3.connect(backups[0]) as conn:
        assert conn.execute("SELECT session_id FROM sessions").fetchone()[0] == "legacy"


def test_v7_migration_preserves_checkpoints_and_adds_call_granularity(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "v6.db"
    store = SessionStore(db_path)
    session = store.create_session(str(tmp_path))
    with closing(get_db_connection(db_path)) as conn, conn:
        conn.execute("DROP INDEX IF EXISTS idx_file_checkpoints_call")
        conn.execute("DROP TABLE file_checkpoints")
        conn.executescript(
            """
            CREATE TABLE file_checkpoints (
                checkpoint_id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL,
                turn_id TEXT NOT NULL,
                tool_name TEXT NOT NULL,
                path TEXT NOT NULL,
                existed INTEGER NOT NULL CHECK(existed IN (0, 1)),
                before_content BLOB,
                before_mode INTEGER,
                after_sha256 TEXT,
                restored INTEGER NOT NULL DEFAULT 0 CHECK(restored IN (0, 1)),
                created_at TIMESTAMP NOT NULL,
                UNIQUE(session_id, turn_id, path)
            );
            """
        )
        conn.execute(
            "INSERT INTO file_checkpoints "
            "(session_id, turn_id, tool_name, path, existed, before_content, "
            "created_at) VALUES (?, 'turn-1', 'whole_edit', 'file.txt', 1, ?, ?)",
            (session.session_id, b"before", datetime.now(timezone.utc).isoformat()),
        )
        conn.execute("DELETE FROM schema_migrations WHERE version >= 7")

    migrated = SessionStore(db_path)

    rows = migrated.file_checkpoints_for_turns(session.session_id, ["turn-1"])
    assert len(rows) == 1
    assert rows[0]["before_content"] == b"before"
    assert rows[0]["call_id"] == ""
    migrated.save_file_checkpoint(
        session.session_id,
        "turn-1",
        "whole_edit",
        "file.txt",
        existed=True,
        before_content=b"second",
        before_mode=None,
        call_id="call-2",
    )
    assert len(migrated.file_checkpoints_for_turns(session.session_id, ["turn-1"])) == 2
    assert len(list(tmp_path.glob("v6.db.before-v16-migration.*.backup"))) == 1


def test_session_forks_form_a_durable_redacted_tree(tmp_path: Path) -> None:
    store = SessionStore(tmp_path / "tree.db")
    root = store.create_session(str(tmp_path), model="test/model")
    now = datetime.now(timezone.utc)
    store.save_message(
        root.session_id,
        Message(role="user", content="one", timestamp=now),
        token_count=3,
        prompt_tokens=2,
        turn_id="turn-root",
    )
    store.save_message(
        root.session_id,
        Message(role="assistant", content="answer", timestamp=now),
        token_count=4,
        completion_tokens=3,
        turn_id="turn-root",
    )

    child = store.fork_session(
        root.session_id,
        message_count=2,
        branch_name="  alternate   design  ",
        branch_summary="Use OPENAI_API_KEY=sk-proj-abcdefghijklmnopqrstuvwxyz",
    )
    grandchild = store.fork_session(child.session_id, branch_name="second pass")
    sibling = store.fork_session(root.session_id, branch_name="different path")
    tree = store.session_tree(grandchild.session_id)

    assert [node.session_id for node in tree] == [
        root.session_id,
        child.session_id,
        grandchild.session_id,
        sibling.session_id,
    ]
    assert [node.depth for node in tree] == [0, 1, 2, 1]
    assert child.parent_session_id == root.session_id
    assert child.root_session_id == root.session_id
    assert child.fork_message_count == 2
    assert child.branch_name == "alternate design"
    assert "sk-proj" not in child.branch_summary
    assert "REDACTED" in child.branch_summary
    assert tree[0].children == (child.session_id, sibling.session_id)
    assert tree[1].children == (grandchild.session_id,)
    assert tree[2].children == ()
    assert [message.content for message in grandchild.messages] == ["one", "answer"]
    with get_db_connection(store.db_path) as conn:
        copied = conn.execute(
            "SELECT token_count, prompt_tokens, completion_tokens, turn_id "
            "FROM messages WHERE session_id = ? ORDER BY message_id",
            (child.session_id,),
        ).fetchall()
    assert [row["token_count"] for row in copied] == [3, 4]
    assert [row["prompt_tokens"] for row in copied] == [2, 0]
    assert [row["completion_tokens"] for row in copied] == [0, 3]
    assert [row["turn_id"] for row in copied] == [None, None]


def test_session_fork_rejects_incomplete_tool_and_turn_boundaries(
    tmp_path: Path,
) -> None:
    store = SessionStore(tmp_path / "boundaries.db")
    session = store.create_session(str(tmp_path))
    now = datetime.now(timezone.utc)
    store.save_message(
        session.session_id,
        Message(
            role="assistant",
            content="",
            timestamp=now,
            metadata={"tool_calls": [{"id": "call-1"}]},
        ),
    )
    store.save_message(
        session.session_id,
        Message(role="tool", content="done", timestamp=now),
    )

    with pytest.raises(ValueError, match="assistant/tool-call pair"):
        store.fork_session(session.session_id, message_count=1)

    turn_session = store.create_session(str(tmp_path))
    store.save_message(
        turn_session.session_id,
        Message(role="user", content="work", timestamp=now),
        turn_id="turn-1",
    )
    store.save_message(
        turn_session.session_id,
        Message(role="assistant", content="done", timestamp=now),
        turn_id="turn-1",
    )
    with pytest.raises(ValueError, match="splits an Ash turn"):
        store.fork_session(turn_session.session_id, message_count=1)


def test_fork_session_does_not_preload_source_history(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = SessionStore(tmp_path / "fork-no-preload.db")
    source = store.create_session(str(tmp_path))
    now = datetime.now(timezone.utc)
    for content in ("one", "two", "three"):
        store.save_message(
            source.session_id,
            Message(role="user", content=content, timestamp=now),
        )
    original_load = store.load_session
    loaded_ids: list[str] = []

    def tracking_load(session_id: str, **kwargs):
        loaded_ids.append(session_id)
        return original_load(session_id, **kwargs)

    monkeypatch.setattr(store, "load_session", tracking_load)

    child = store.fork_session(source.session_id, message_count=2)

    assert loaded_ids == [child.session_id]
    assert [message.content for message in child.messages] == ["one", "two"]


def test_rewind_session_does_not_preload_full_history(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = SessionStore(tmp_path / "rewind-no-preload.db")
    session = store.create_session(str(tmp_path))
    now = datetime.now(timezone.utc)
    for content in ("one", "two", "three"):
        store.save_message(
            session.session_id,
            Message(role="user", content=content, timestamp=now),
        )
    original_load = store.load_session
    loaded_ids: list[str] = []

    def tracking_load(session_id: str, **kwargs):
        loaded_ids.append(session_id)
        return original_load(session_id, **kwargs)

    monkeypatch.setattr(store, "load_session", tracking_load)

    rewound = store.rewind_session(session.session_id, 1)

    assert loaded_ids == [session.session_id]
    assert [message.content for message in rewound.messages] == ["one"]


def test_session_cleanup_deletes_only_complete_inactive_trees(tmp_path: Path) -> None:
    store = SessionStore(tmp_path / "cleanup.db")
    root = store.create_session(str(tmp_path))
    child = store.fork_session(root.session_id, branch_name="active")
    stale = "2020-01-01T00:00:00+00:00"
    with get_db_connection(store.db_path) as conn, conn:
        conn.execute(
            "UPDATE sessions SET updated_at = ? WHERE session_id = ?",
            (stale, root.session_id),
        )

    assert store.cleanup_sessions(30) == 0
    assert len(store.session_tree(root.session_id)) == 2

    with get_db_connection(store.db_path) as conn, conn:
        conn.execute(
            "UPDATE sessions SET updated_at = ? WHERE root_session_id = ?",
            (stale, root.session_id),
        )
    assert store.cleanup_sessions(30) == 2
    with pytest.raises(KeyError, match="Session not found"):
        store.load_session(child.session_id)


def test_session_cleanup_treats_unrepresentable_retention_as_noop(
    tmp_path: Path,
) -> None:
    store = SessionStore(tmp_path / "cleanup.db")
    session = store.create_session(str(tmp_path))

    assert store.cleanup_sessions(10**12) == 0
    assert store.load_session(session.session_id).session_id == session.session_id


def test_session_fork_rolls_back_the_entire_child_on_copy_failure(
    tmp_path: Path,
) -> None:
    store = SessionStore(tmp_path / "rollback.db")
    root = store.create_session(str(tmp_path))
    store.save_message(
        root.session_id,
        Message(
            role="user",
            content="cannot copy",
            timestamp=datetime.now(timezone.utc),
        ),
    )
    with get_db_connection(store.db_path) as conn, conn:
        conn.execute(
            f"CREATE TRIGGER reject_branch_copy BEFORE INSERT ON messages "
            f"WHEN NEW.session_id != '{root.session_id}' "
            "BEGIN SELECT RAISE(ABORT, 'forced copy failure'); END"
        )

    with pytest.raises(sqlite3.IntegrityError, match="forced copy failure"):
        store.fork_session(root.session_id, branch_name="must rollback")

    assert [item.session_id for item in store.list_sessions()] == [root.session_id]
    assert store.get_session_lineage(root.session_id).children == ()


def test_discard_unchanged_leaf_fork_is_narrow_and_fail_closed(tmp_path: Path) -> None:
    store = SessionStore(tmp_path / "discard-fork.db")
    root = store.create_session(str(tmp_path))

    disposable = store.fork_session(root.session_id, branch_name="disposable")
    assert store.discard_unchanged_leaf_fork(
        disposable.session_id,
        parent_session_id=root.session_id,
        created_at=disposable.created_at,
        updated_at=disposable.updated_at,
    ) is True
    with pytest.raises(KeyError, match="Session not found"):
        store.load_session(disposable.session_id)
    assert store.get_session_lineage(root.session_id).children == ()

    modified = store.fork_session(root.session_id, branch_name="modified")
    store.save_message(
        modified.session_id,
        Message(
            role="user",
            content="keep me",
            timestamp=datetime.now(timezone.utc),
        ),
    )
    assert store.discard_unchanged_leaf_fork(
        modified.session_id,
        parent_session_id=root.session_id,
        created_at=modified.created_at,
        updated_at=modified.updated_at,
    ) is False
    assert store.load_session(modified.session_id).messages[0].content == "keep me"

    audited = store.fork_session(root.session_id, branch_name="audited")
    store.append_audit_log(
        audited.session_id,
        action_type="command_run",
        target_resource="rollback-proof",
        details={"source": "test"},
        result="SUCCESS",
    )
    assert store.discard_unchanged_leaf_fork(
        audited.session_id,
        parent_session_id=root.session_id,
        created_at=audited.created_at,
        updated_at=audited.updated_at,
    ) is False
    assert len(store.list_audit_logs(audited.session_id)) == 1

    tooled = store.fork_session(root.session_id, branch_name="tooled")
    store.save_tool_call(
        tooled.session_id,
        ToolCallRecord(
            call_id="rollback-proof-call",
            tool_name="read_file",
            arguments={"file_path": "README.md"},
            approved=True,
            executed=False,
            timestamp=datetime.now(timezone.utc),
        ),
    )
    assert store.discard_unchanged_leaf_fork(
        tooled.session_id,
        parent_session_id=root.session_id,
        created_at=tooled.created_at,
        updated_at=tooled.updated_at,
    ) is False
    assert store.load_session(tooled.session_id).tool_calls[0].call_id == (
        "rollback-proof-call"
    )

    parent = store.fork_session(root.session_id, branch_name="has-child")
    child = store.fork_session(parent.session_id, branch_name="descendant")
    assert store.discard_unchanged_leaf_fork(
        parent.session_id,
        parent_session_id=root.session_id,
        created_at=parent.created_at,
        updated_at=parent.updated_at,
    ) is False
    assert store.load_session(child.session_id).parent_session_id == parent.session_id

    assert store.discard_unchanged_leaf_fork(
        parent.session_id,
        parent_session_id="wrong-parent",
        created_at=parent.created_at,
        updated_at=parent.updated_at,
    ) is False


def test_runtime_event_log_is_ordered_idempotent_and_redacted(tmp_path: Path) -> None:
    store = SessionStore(tmp_path / "events.db")
    session = store.create_session(str(tmp_path))
    github_token = "ghp_" + "A" * 36
    base = {
        "schema_version": 1,
        "timestamp": "2026-07-10T00:00:00+00:00",
        "source": {"type": "runtime", "id": "ash"},
        "session_id": session.session_id,
        "turn_id": "turn-1",
        "operation_id": None,
        "parent_event_id": None,
    }
    events = [
        {**base, "event_id": "event-1", "type": "turn.started"},
        {
            **base,
            "event_id": "event-2",
            "type": "tool.completed",
            "output": (
                "OPENAI_API_KEY=sk-proj-abcdefghijklmnopqrstuvwxyz "
                f"github={github_token}"
            ),
        },
    ]

    assert store.save_runtime_events(events) == 2
    assert store.save_runtime_events(events) == 0
    replay = store.list_runtime_events(session.session_id, limit=1)
    remainder = store.list_runtime_events(
        session.session_id, after_sequence=replay[-1].sequence
    )

    assert [item.event["type"] for item in [*replay, *remainder]] == [
        "turn.started",
        "tool.completed",
    ]
    assert "sk-proj" not in remainder[0].event["output"]
    assert github_token not in remainder[0].event["output"]
    assert "REDACTED" in remainder[0].event["output"]


def test_runtime_event_ids_are_scoped_per_session(tmp_path: Path) -> None:
    store = SessionStore(tmp_path / "events.db")
    first = store.create_session(str(tmp_path / "first"))
    second = store.create_session(str(tmp_path / "second"))

    def event(session_id: str, event_type: str) -> dict[str, object]:
        return {
            "schema_version": 1,
            "event_id": "shared-event-id",
            "timestamp": "2026-07-10T00:00:00+00:00",
            "source": {"type": "runtime", "id": "ash"},
            "session_id": session_id,
            "turn_id": "turn-1",
            "operation_id": None,
            "parent_event_id": None,
            "type": event_type,
        }

    first_event = event(first.session_id, "turn.started")
    second_event = event(second.session_id, "turn.completed")

    assert store.save_runtime_events([first_event]) == 1
    assert store.save_runtime_events([second_event]) == 1
    assert store.save_runtime_events([first_event]) == 0
    assert store.save_runtime_events([second_event]) == 0

    assert [item.event["type"] for item in store.list_runtime_events(first.session_id)] == [
        "turn.started"
    ]
    assert [item.event["type"] for item in store.list_runtime_events(second.session_id)] == [
        "turn.completed"
    ]


def test_runtime_event_replay_validates_cursor_and_limit(tmp_path: Path) -> None:
    store = SessionStore(tmp_path / "events.db")

    with pytest.raises(ValueError, match="after_sequence"):
        store.list_runtime_events("missing", after_sequence=-1)
    with pytest.raises(ValueError, match="limit"):
        store.list_runtime_events("missing", limit=0)


def test_newer_database_schema_is_refused(tmp_path: Path) -> None:
    db_path = tmp_path / "future.db"
    with sqlite3.connect(db_path) as conn:
        conn.executescript(
            """
            CREATE TABLE schema_migrations (
                version INTEGER PRIMARY KEY,
                applied_at TIMESTAMP NOT NULL
            );
            INSERT INTO schema_migrations VALUES (999, '2026-01-01T00:00:00+00:00');
            """
        )

    with pytest.raises(SessionStorageError, match="newer"):
        SessionStore(db_path)


def test_manual_backup_is_consistent_and_never_overwrites(tmp_path: Path) -> None:
    store = SessionStore(tmp_path / "sessions.db")
    session = store.create_session("/workspace")
    destination = tmp_path / "manual.backup"

    assert store.backup(destination) == destination
    assert (
        SessionStore(destination).load_session(session.session_id).session_id
        == session.session_id
    )
    with pytest.raises(SessionStorageError, match="already exists"):
        store.backup(destination)


def test_manual_backup_rejects_plain_parent_directory_swap(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import ash.core.session as session_module

    source_directory = tmp_path / "db"
    target_directory = tmp_path / "target"
    saved_directory = tmp_path / "target-original"
    replacement_directory = tmp_path / "replacement"
    source_directory.mkdir()
    target_directory.mkdir()
    replacement_directory.mkdir()
    store = SessionStore(source_directory / "sessions.db")
    store.create_session("/workspace")
    real_open = session_module.AnchoredDirectory.open
    swapped = False

    def open_then_swap(path, **kwargs):
        nonlocal swapped
        directory = real_open(path, **kwargs)
        if Path(path) == target_directory and not swapped:
            swapped = True
            target_directory.rename(saved_directory)
            replacement_directory.rename(target_directory)
        return directory

    monkeypatch.setattr(
        session_module.AnchoredDirectory,
        "open",
        staticmethod(open_then_swap),
    )

    with pytest.raises(SessionStorageError):
        store.backup(target_directory / "manual.backup")

    assert swapped is True
    assert not (target_directory / "manual.backup").exists()
    assert not (saved_directory / "manual.backup").exists()


def test_manual_backup_does_not_follow_destination_swapped_to_symlink(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import ash.core.session as session_module

    store = SessionStore(tmp_path / "sessions.db")
    source_session = store.create_session("/source")
    destination = tmp_path / "manual.backup"
    victim = tmp_path / "victim.db"
    with sqlite3.connect(victim) as connection:
        connection.execute("CREATE TABLE victim_marker(value TEXT)")
        connection.execute("INSERT INTO victim_marker VALUES ('do-not-touch')")

    real_connect = sqlite3.connect
    swapped = False

    def swap_before_destination_open(database, *args, **kwargs):
        nonlocal swapped
        if Path(str(database)) == destination and not swapped:
            swapped = True
            try:
                destination.symlink_to(victim)
            except OSError as exc:
                pytest.skip(f"symlinks are unavailable: {exc}")
        return real_connect(database, *args, **kwargs)

    monkeypatch.setattr(session_module.sqlite3, "connect", swap_before_destination_open)

    assert store.backup(destination) == destination
    assert swapped is False
    monkeypatch.setattr(session_module.sqlite3, "connect", real_connect)
    with real_connect(victim) as connection:
        assert connection.execute("SELECT value FROM victim_marker").fetchone() == (
            "do-not-touch",
        )
        assert (
            connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='sessions'"
            ).fetchone()
            is None
        )
    assert SessionStore(destination).load_session(source_session.session_id).session_id == (
        source_session.session_id
    )
    assert source_session.session_id in {
        item.session_id for item in store.list_sessions(limit=10)
    }


def test_manual_backup_does_not_follow_source_swapped_to_symlink(
    tmp_path: Path,
) -> None:
    source = tmp_path / "sessions.db"
    store = SessionStore(source)
    source_session = store.create_session("/source")
    replacement = tmp_path / "replacement.db"
    replacement_store = SessionStore(replacement)
    replacement_session = replacement_store.create_session("/replacement")
    destination = tmp_path / "manual.backup"
    source.unlink()
    try:
        source.symlink_to(replacement)
    except OSError as exc:
        pytest.skip(f"symlinks are unavailable: {exc}")

    with pytest.raises(SessionStorageError):
        store.backup(destination)

    assert not destination.exists()
    assert SessionStore(replacement).load_session(replacement_session.session_id).session_id == (
        replacement_session.session_id
    )
    assert source_session.session_id != replacement_session.session_id


def test_manual_backup_rejects_regular_file_replacement_before_backup(
    tmp_path: Path,
) -> None:
    source = tmp_path / "sessions.db"
    store = SessionStore(source)
    source_session = store.create_session("/source")
    original = tmp_path / "sessions-original.db"
    source.rename(original)

    replacement_store = SessionStore(source)
    replacement_session = replacement_store.create_session("/replacement")
    destination = tmp_path / "manual.backup"

    with pytest.raises(SessionStorageError, match="file identity changed"):
        store.backup(destination)

    assert not destination.exists()
    assert source_session.session_id != replacement_session.session_id
    assert SessionStore(source).load_session(replacement_session.session_id).session_id == (
        replacement_session.session_id
    )


def test_manual_backup_never_copies_replacement_during_regular_file_aba_swap(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import ash.core.session as session_module

    source = tmp_path / "sessions.db"
    store = SessionStore(source)
    source_session = store.create_session("/source")
    replacement = tmp_path / "replacement.db"
    replacement_store = SessionStore(replacement)
    replacement_session = replacement_store.create_session("/replacement")
    destination = tmp_path / "manual.backup"
    original_hold = tmp_path / "original-held.db"
    real_copy = session_module._copy_descriptor
    swapped = False

    def swap_away_and_back(source_descriptor, destination_descriptor):
        nonlocal swapped
        if not swapped:
            swapped = True
            source.replace(original_hold)
            replacement.replace(source)
            try:
                real_copy(source_descriptor, destination_descriptor)
            finally:
                source.replace(replacement)
                original_hold.replace(source)
            return
        real_copy(source_descriptor, destination_descriptor)

    monkeypatch.setattr(session_module, "_copy_descriptor", swap_away_and_back)

    try:
        created = store.backup(destination)
    except SessionStorageError:
        assert not destination.exists()
    else:
        backup_ids = {
            item.session_id for item in SessionStore(created).list_sessions(limit=10)
        }
        assert source_session.session_id in backup_ids
        assert replacement_session.session_id not in backup_ids

    assert swapped is True
    assert SessionStore(source).load_session(source_session.session_id).session_id == (
        source_session.session_id
    )
    assert SessionStore(replacement).load_session(replacement_session.session_id).session_id == (
        replacement_session.session_id
    )


@pytest.mark.skipif(
    preferred_sqlite_journal_mode() != "WAL",
    reason="runtime intentionally avoids WAL because its SQLite version is vulnerable",
)
def test_manual_backup_rejects_live_uncoordinated_wal_then_succeeds_after_close(
    tmp_path: Path,
) -> None:
    source = tmp_path / "sessions.db"
    store = SessionStore(source)
    destination = tmp_path / "manual.backup"

    writer = sqlite3.connect(source)
    try:
        writer.execute("PRAGMA wal_autocheckpoint=0")
        session_id = "wal-session"
        writer.execute(
            """
            INSERT INTO sessions (
                session_id, project_path, created_at, title, updated_at, model
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                session_id,
                "/wal",
                "2026-09-23T00:00:00+00:00",
                "from wal",
                "2026-09-23T00:00:00+00:00",
                "provider/model",
            ),
        )
        writer.commit()
        assert Path(f"{source}-wal").exists()

        with pytest.raises(SessionStorageError, match="WAL sidecar"):
            store.backup(destination)
        assert not destination.exists()
    finally:
        writer.close()

    assert store.backup(destination) == destination
    assert SessionStore(destination).load_session(session_id).title == "from wal"


def test_manual_backup_quiesces_concurrent_session_writer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import ash.core.session as session_module

    source = tmp_path / "sessions.db"
    store = SessionStore(source)
    original = store.create_session("/before-backup")
    destination = tmp_path / "manual.backup"
    copy_entered = threading.Event()
    writer_started = threading.Event()
    writer_done = threading.Event()
    writer_reached_acquire = threading.Event()
    writer_session_id: list[str] = []
    writer_errors: list[BaseException] = []
    completed_during_copy: list[bool] = []
    real_copy = session_module._copy_descriptor
    real_acquire = session_module._acquire_database_coordination

    def observe_acquire(
        db_path,
        *,
        exclusive,
        coordination_parent_descriptor=None,
    ):
        if threading.current_thread().name == "backup-writer" and not exclusive:
            writer_reached_acquire.set()
        return real_acquire(
            db_path,
            exclusive=exclusive,
            coordination_parent_descriptor=coordination_parent_descriptor,
        )

    def writer() -> None:
        assert copy_entered.wait(5)
        writer_started.set()
        try:
            session = store.create_session("/during-backup")
            writer_session_id.append(session.session_id)
        except BaseException as exc:
            writer_errors.append(exc)
        finally:
            writer_done.set()

    def observe_copy(*args, **kwargs):
        copy_entered.set()
        assert writer_started.wait(5)
        assert writer_reached_acquire.wait(5)
        completed_during_copy.append(writer_done.wait(0.25))
        return real_copy(*args, **kwargs)

    monkeypatch.setattr(
        session_module,
        "_acquire_database_coordination",
        observe_acquire,
    )
    monkeypatch.setattr(session_module, "_copy_descriptor", observe_copy)
    writer_thread = threading.Thread(target=writer, name="backup-writer")
    writer_thread.start()
    try:
        assert store.backup(destination) == destination
        writer_thread.join(5)
    finally:
        copy_entered.set()
        writer_thread.join(5)

    assert not writer_thread.is_alive()
    assert completed_during_copy == [False]
    assert writer_errors == []
    assert len(writer_session_id) == 1
    backup_ids = {
        item.session_id for item in SessionStore(destination).list_sessions(limit=10)
    }
    live_ids = {item.session_id for item in SessionStore(source).list_sessions(limit=10)}
    assert original.session_id in backup_ids
    assert writer_session_id[0] not in backup_ids
    assert writer_session_id[0] in live_ids


@pytest.mark.skipif(os.name != "posix", reason="cross-process flock is POSIX-only")
def test_manual_backup_quiesces_cross_process_session_connection(
    tmp_path: Path,
) -> None:
    source = tmp_path / "sessions.db"
    store = SessionStore(source)
    original = store.create_session("/before-backup")
    destination = tmp_path / "manual.backup"
    attempt = tmp_path / "backup-attempt"
    result = tmp_path / "backup-result"
    held_connection = get_db_connection(source)
    child: subprocess.Popen[str] | None = None

    child_code = """
from pathlib import Path
import sys
import ash.core.session as session_module
from ash.core.session import SessionStore

database = Path(sys.argv[1])
destination = Path(sys.argv[2])
attempt = Path(sys.argv[3])
result = Path(sys.argv[4])
real_acquire = session_module._acquire_database_coordination

def marked_acquire(
    db_path,
    *,
    exclusive,
    coordination_parent_descriptor=None,
):
    if exclusive:
        attempt.write_text("waiting", encoding="utf-8")
    return real_acquire(
        db_path,
        exclusive=exclusive,
        coordination_parent_descriptor=coordination_parent_descriptor,
    )

session_module._acquire_database_coordination = marked_acquire
store = SessionStore(database)
try:
    store.backup(destination)
except BaseException as exc:
    result.write_text("error:" + repr(exc), encoding="utf-8")
    raise
else:
    result.write_text("ok", encoding="utf-8")
"""

    try:
        child = subprocess.Popen(
            [
                sys.executable,
                "-c",
                child_code,
                str(source),
                str(destination),
                str(attempt),
                str(result),
            ],
            text=True,
        )
        deadline = time.monotonic() + 5
        while not attempt.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert attempt.exists()
        time.sleep(0.25)
        assert child.poll() is None
        assert not result.exists()
    finally:
        held_connection.close()

    assert child is not None
    child.wait(timeout=5)
    assert child.returncode == 0
    assert result.read_text(encoding="utf-8") == "ok"
    backup_ids = {
        item.session_id for item in SessionStore(destination).list_sessions(limit=10)
    }
    assert original.session_id in backup_ids


def test_manual_backup_rejects_reentrant_open_connection(
    tmp_path: Path,
) -> None:
    source = tmp_path / "sessions.db"
    store = SessionStore(source)
    store.create_session("/workspace")
    destination = tmp_path / "manual.backup"

    with get_db_connection(source):
        with pytest.raises(SessionStorageError, match="current thread holds"):
            store.backup(destination)

    assert not destination.exists()


def test_nested_database_read_completes_while_writer_is_queued(tmp_path: Path) -> None:
    import ash.core.session as session_module

    source = tmp_path / "sessions.db"
    store = SessionStore(source)
    first_opened = threading.Event()
    attempt_nested = threading.Event()
    nested_done = threading.Event()
    release_first = threading.Event()
    writer_done = threading.Event()
    errors: list[BaseException] = []

    def reader() -> None:
        first = None
        try:
            first = get_db_connection(source)
            first_opened.set()
            assert attempt_nested.wait(5)
            with closing(get_db_connection(source)) as second:
                assert second.execute("SELECT 1").fetchone()[0] == 1
            nested_done.set()
            assert release_first.wait(5)
        except BaseException as exc:
            errors.append(exc)
        finally:
            if first is not None:
                first.close()

    def writer() -> None:
        try:
            with session_module.exclusive_database_access(source):
                pass
        except BaseException as exc:
            errors.append(exc)
        finally:
            writer_done.set()

    reader_thread = threading.Thread(target=reader)
    reader_thread.start()
    assert first_opened.wait(5)

    writer_thread = threading.Thread(target=writer)
    writer_thread.start()
    state = session_module._database_coordination_state(store.db_path)
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        with state.condition:
            if state.waiting_writers:
                break
        time.sleep(0.005)
    else:
        release_first.set()
        reader_thread.join(5)
        writer_thread.join(5)
        pytest.fail("writer never entered the coordination queue")

    attempt_nested.set()
    try:
        assert nested_done.wait(2), "nested reader blocked behind the queued writer"
        assert not writer_done.is_set()
    finally:
        release_first.set()
        reader_thread.join(5)
        writer_thread.join(5)

    assert not reader_thread.is_alive()
    assert not writer_thread.is_alive()
    assert writer_done.is_set()
    assert errors == []


@pytest.mark.skipif(os.name == "nt", reason="descriptor-anchored POSIX regression")
def test_exclusive_database_access_parent_swap_fails_without_writing_replacement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import ash.core.session as session_module
    import ash.safety.anchored_fs as anchored_fs

    state_root = tmp_path / "state"
    state_root.mkdir()
    source = state_root / "db" / "sessions.db"
    saved_root = tmp_path / "state-saved"
    replacement = tmp_path / "replacement"
    replacement.mkdir()
    real_open_or_create = anchored_fs._open_or_create_directory
    swapped = False

    def open_or_create_then_swap(
        parent_descriptor,
        name,
        *,
        create,
        expected=None,
    ):
        nonlocal swapped
        if name == "db" and not swapped:
            state_root.rename(saved_root)
            state_root.symlink_to(replacement, target_is_directory=True)
            swapped = True
        return real_open_or_create(
            parent_descriptor,
            name,
            create=create,
            expected=expected,
        )

    monkeypatch.setattr(
        anchored_fs,
        "_open_or_create_directory",
        open_or_create_then_swap,
    )

    with pytest.raises(SessionStorageError):
        with session_module.exclusive_database_access(source):
            pass

    assert swapped is True
    assert not (replacement / "db").exists()


def test_manual_backup_fails_closed_without_descriptor_validation_support(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import ash.core.session as session_module

    store = SessionStore(tmp_path / "sessions.db")
    store.create_session("/workspace")
    destination = tmp_path / "manual.backup"

    real_connect = session_module.sqlite3.connect

    class ConnectionWithoutDeserialize:
        def close(self) -> None:
            return None

    def connect_without_deserialize(database, *args, **kwargs):
        if database == ":memory:":
            return ConnectionWithoutDeserialize()
        return real_connect(database, *args, **kwargs)

    monkeypatch.setattr(session_module.sqlite3, "connect", connect_without_deserialize)

    with pytest.raises(
        SessionStorageError,
        match="Secure session backup validation is unavailable",
    ):
        store.backup(destination)

    assert not destination.exists()


def test_message_storage_round_trips_in_insert_order(tmp_path: Path) -> None:
    db_path = tmp_path / "session_store.db"
    store = SessionStore(db_path)
    session = store.create_session(project_path="/workspace")

    first = Message(
        role="user",
        content="Build the config loader.",
        timestamp=datetime(2026, 6, 2, 10, 0, tzinfo=timezone.utc),
        metadata={"turn": 1},
    )
    second = Message(
        role="assistant",
        content="Done.",
        timestamp=datetime(2026, 6, 2, 10, 1, tzinfo=timezone.utc),
        metadata={"tokens": 42},
    )

    store.save_message(session.session_id, first)
    store.save_message(session.session_id, second)

    loaded = store.load_session(session.session_id)

    assert loaded.messages == [first, second]


def test_tool_call_storage_inserts_and_updates_records(tmp_path: Path) -> None:
    db_path = tmp_path / "session_store.db"
    store = SessionStore(db_path)
    session = store.create_session(project_path="/workspace")

    record = ToolCallRecord(
        call_id="call-1",
        tool_name="write_file",
        arguments={"path": "ash/config.py"},
        approved=True,
        executed=False,
        timestamp=datetime(2026, 6, 2, 11, 0, tzinfo=timezone.utc),
    )
    updated = record.model_copy(
        update={
            "executed": True,
            "result": "SUCCESS",
            "timestamp": datetime(2026, 6, 2, 11, 1, tzinfo=timezone.utc),
        }
    )

    store.save_tool_call(session.session_id, record)
    store.save_tool_call(session.session_id, updated)

    loaded = store.load_session(session.session_id)

    assert loaded.tool_calls == [updated]


def test_tool_call_ids_are_scoped_per_session(tmp_path: Path) -> None:
    db_path = tmp_path / "session_store.db"
    store = SessionStore(db_path)
    first = store.create_session(project_path="/workspace/first")
    second = store.create_session(project_path="/workspace/second")
    first_record = ToolCallRecord(
        call_id="shared-call-id",
        tool_name="write_file",
        arguments={"path": "first.txt"},
        approved=True,
        executed=False,
        dispatched=True,
        timestamp=datetime(2026, 6, 2, 11, 0, tzinfo=timezone.utc),
    )
    second_record = ToolCallRecord(
        call_id="shared-call-id",
        tool_name="read_file",
        arguments={"file_path": "second.txt"},
        approved=False,
        executed=False,
        dispatched=False,
        timestamp=datetime(2026, 6, 2, 11, 1, tzinfo=timezone.utc),
    )

    store.save_tool_call(first.session_id, first_record, turn_id="turn-first")
    store.save_tool_call(second.session_id, second_record, turn_id="turn-second")

    assert store.load_session(first.session_id).tool_calls == [first_record]
    assert store.load_session(second.session_id).tool_calls == [second_record]
    assert (
        store.tool_call_for_recovery(
            first.session_id,
            "turn-first",
            "shared-call-id",
        )["tool_name"]
        == "write_file"
    )
    assert (
        store.tool_call_for_recovery(
            second.session_id,
            "turn-second",
            "shared-call-id",
        )["tool_name"]
        == "read_file"
    )


def test_audit_log_hash_chain_detects_tampering(tmp_path: Path) -> None:
    db_path = tmp_path / "session_store.db"
    store = SessionStore(db_path)
    session = store.create_session(project_path="/workspace")

    first = store.append_audit_log(
        session.session_id,
        action_type="user_approval",
        target_resource="write_file",
        details={"call_id": "call-1", "path": "a.py"},
        result="APPROVED",
    )
    second = store.append_audit_log(
        session.session_id,
        action_type="file_write",
        target_resource="write_file",
        details={"call_id": "call-1", "success": True},
        result="SUCCESS",
    )

    assert first.previous_hash == ""
    assert second.previous_hash == first.sha256_hash
    assert store.verify_audit_log(session.session_id) == []

    with get_db_connection(db_path) as conn, conn:
        conn.execute(
            "UPDATE audit_logs SET details_json = ? WHERE log_id = ?",
            ('{"call_id":"call-1","success":false}', second.log_id),
        )

    assert store.verify_audit_log(session.session_id) == [
        f"audit log {second.log_id} sha256_hash mismatch"
    ]


def test_audit_append_serializes_chain_tail_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = tmp_path / "session_store.db"
    first_store = SessionStore(db_path)
    session = first_store.create_session(project_path=str(tmp_path))
    second_store = SessionStore(db_path)
    original_append = SessionStore._append_audit_log_in_connection
    first_entered = threading.Event()
    release_first = threading.Event()
    second_entered = threading.Event()
    counter_lock = threading.Lock()
    calls = 0

    def controlled_append(self, conn, session_id, **kwargs):
        nonlocal calls
        with counter_lock:
            calls += 1
            call_number = calls
        if call_number == 1:
            first_entered.set()
            assert release_first.wait(timeout=2)
        elif call_number == 2:
            second_entered.set()
        return original_append(self, conn, session_id, **kwargs)

    monkeypatch.setattr(
        SessionStore,
        "_append_audit_log_in_connection",
        controlled_append,
    )
    errors: list[BaseException] = []

    def append(store: SessionStore, target: str) -> None:
        try:
            store.append_audit_log(
                session.session_id,
                action_type="tool_call",
                target_resource=target,
                details={"target": target},
                result="SUCCESS",
            )
        except BaseException as exc:
            errors.append(exc)

    first = threading.Thread(target=append, args=(first_store, "first"))
    second = threading.Thread(target=append, args=(second_store, "second"))
    first.start()
    assert first_entered.wait(timeout=2)
    second.start()
    try:
        assert second_entered.wait(timeout=0.1) is False
    finally:
        release_first.set()
    first.join(timeout=2)
    second.join(timeout=2)

    assert not first.is_alive()
    assert not second.is_alive()
    assert errors == []
    assert first_store.verify_audit_log(session.session_id) == []


def test_connection_uses_safe_journal_mode_and_foreign_keys(tmp_path: Path) -> None:
    db_path = tmp_path / "session_store.db"

    conn = get_db_connection(db_path)
    try:
        assert (
            conn.execute("PRAGMA journal_mode;").fetchone()[0].upper()
            == preferred_sqlite_journal_mode()
        )
        assert conn.execute("PRAGMA synchronous;").fetchone()[0] == 1
        assert conn.execute("PRAGMA foreign_keys;").fetchone()[0] == 1
    finally:
        conn.close()


@pytest.mark.skipif(os.name != "posix", reason="POSIX advisory lock regression")
def test_session_runtime_lease_rejects_second_active_owner(tmp_path: Path) -> None:
    db_path = tmp_path / "session_store.db"
    first_store = SessionStore(db_path)
    session = first_store.create_session(project_path=str(tmp_path))
    second_store = SessionStore(db_path)

    first_lease = first_store.acquire_session_runtime_lease(session.session_id)
    try:
        with pytest.raises(SessionStorageError, match="active in another Ash process"):
            second_store.acquire_session_runtime_lease(session.session_id)
    finally:
        first_lease.close()

    second_lease = second_store.acquire_session_runtime_lease(session.session_id)
    second_lease.close()


@pytest.mark.asyncio
async def test_write_transaction_serializes_concurrent_writes(tmp_path: Path) -> None:
    db_path = tmp_path / "session_store.db"

    with get_db_connection(db_path) as conn:
        conn.execute(
            """
            CREATE TABLE writes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                label TEXT NOT NULL
            )
            """
        )

    gate = asyncio.Event()
    entered: list[str] = []

    async def write_first() -> None:
        async with write_transaction(db_path) as conn:
            entered.append("first-start")
            await gate.wait()
            conn.execute("INSERT INTO writes (label) VALUES (?)", ("first",))
            entered.append("first-end")

    async def write_second() -> None:
        async with write_transaction(db_path) as conn:
            entered.append("second-start")
            conn.execute("INSERT INTO writes (label) VALUES (?)", ("second",))
            entered.append("second-end")

    first_task = asyncio.create_task(write_first())
    await asyncio.sleep(0)
    second_task = asyncio.create_task(write_second())
    await asyncio.sleep(0.05)

    assert entered == ["first-start"]

    gate.set()
    await asyncio.gather(first_task, second_task)

    with get_db_connection(db_path) as conn:
        labels = [
            row["label"]
            for row in conn.execute(
                "SELECT label FROM writes ORDER BY id ASC"
            ).fetchall()
        ]

    assert entered == ["first-start", "first-end", "second-start", "second-end"]
    assert labels == ["first", "second"]


def test_write_transaction_can_contend_across_sequential_event_loops(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "session_store.db"
    SessionStore(db_path)

    async def contend() -> None:
        entered = asyncio.Event()
        release = asyncio.Event()

        async def first() -> None:
            async with write_transaction(db_path):
                entered.set()
                await release.wait()

        async def second() -> None:
            await entered.wait()
            async with write_transaction(db_path):
                return

        first_task = asyncio.create_task(first())
        second_task = asyncio.create_task(second())
        await entered.wait()
        await asyncio.sleep(0)
        release.set()
        await asyncio.gather(first_task, second_task)

    asyncio.run(contend())
    asyncio.run(contend())


def test_get_recent_session_summaries(tmp_path: Path) -> None:
    store = SessionStore(tmp_path / "test.db")
    s1 = store.create_session(str(tmp_path))
    s2 = store.create_session(str(tmp_path))

    store.save_message(
        s1.session_id,
        Message(role="user", content="hello", timestamp=datetime.now(timezone.utc)),
    )
    store.save_message(
        s2.session_id,
        Message(role="user", content="goodbye", timestamp=datetime.now(timezone.utc)),
    )

    summaries = store.get_recent_session_summaries(str(tmp_path), limit=2)
    assert len(summaries) == 2
    assert any("hello" in s for s in summaries)
    assert any("goodbye" in s for s in summaries)


def test_recent_session_summaries_are_bounded_recent_tails(tmp_path: Path) -> None:
    store = SessionStore(tmp_path / "test.db")
    session = store.create_session(str(tmp_path))
    for index in range(40):
        store.save_message(
            session.session_id,
            Message(
                role="user",
                content=f"message-{index:02d} " + ("x" * 180),
                timestamp=datetime.now(timezone.utc),
            ),
        )

    summaries = store.get_recent_session_summaries(str(tmp_path), limit=1)

    assert len(summaries) == 1
    assert len(summaries[0]) <= 2000
    assert "message-39" in summaries[0]
    assert "message-00" not in summaries[0]


def test_session_scope_resolves_equivalent_project_paths(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    alias = tmp_path / "alias"
    try:
        alias.symlink_to(project, target_is_directory=True)
    except OSError:
        pytest.skip("directory symlinks are unavailable")
    store = SessionStore(tmp_path / "test.db")
    session = store.create_session(str(alias))

    listed = store.list_sessions(project_path=str(project))

    assert [item.session_id for item in listed] == [session.session_id]
    assert session.project_path == str(project.resolve())


def test_session_resolution_supports_exact_id_and_case_insensitive_title(
    tmp_path: Path,
) -> None:
    store = SessionStore(tmp_path / "test.db")
    older = store.create_session(str(tmp_path))
    store.create_session(str(tmp_path))
    store.rename_session(older.session_id, "Auth Refactor")

    assert (
        store.resolve_session(older.session_id, str(tmp_path)).session_id
        == older.session_id
    )
    assert (
        store.resolve_session("auth refactor", str(tmp_path)).session_id
        == older.session_id
    )
    assert store.latest_session(str(tmp_path)).session_id == older.session_id


def test_session_resolution_rejects_ambiguous_and_cross_project_references(
    tmp_path: Path,
) -> None:
    store = SessionStore(tmp_path / "test.db")
    first = store.create_session(str(tmp_path))
    second = store.create_session(str(tmp_path))
    third = store.create_session(str(tmp_path))
    foreign = store.create_session(str(tmp_path / "other"))
    store.rename_session(first.session_id, "duplicate")
    store.rename_session(second.session_id, "DUPLICATE")
    store.rename_session(third.session_id, "Duplicate")

    with pytest.raises(SessionResolutionError, match="ambiguous"):
        store.resolve_session("duplicate", str(tmp_path))
    with pytest.raises(SessionResolutionError, match="different project"):
        store.resolve_session(foreign.session_id, str(tmp_path))
    with pytest.raises(SessionResolutionError, match="no session"):
        store.resolve_session("missing", str(tmp_path))


def test_list_and_rename_sessions(tmp_path: Path) -> None:
    store = SessionStore(tmp_path / "sessions.db")
    first = store.create_session(str(tmp_path))
    second = store.create_session(str(tmp_path / "other"))
    store.save_message(
        first.session_id,
        Message(role="user", content="hello", timestamp=datetime.now(timezone.utc)),
    )
    store.rename_session(first.session_id, "  feature   work  ")

    project_sessions = store.list_sessions(project_path=str(tmp_path))
    assert [item.session_id for item in project_sessions] == [first.session_id]
    assert project_sessions[0].title == "feature work"
    assert project_sessions[0].message_count == 1
    assert store.list_sessions(query="feature")[0].session_id == first.session_id
    assert second.session_id in {
        item.session_id for item in store.list_sessions(limit=10)
    }


def test_session_list_limit_is_bounded(tmp_path: Path) -> None:
    store = SessionStore(tmp_path / "sessions.db")

    with pytest.raises(ValueError, match="limit must be between 1 and 1000"):
        store.list_sessions(limit=1001)


def test_session_tree_and_lineage_fail_closed_above_node_limit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import ash.core.session as session_module

    monkeypatch.setattr(session_module, "MAX_SESSION_TREE_NODES", 2)
    store = SessionStore(tmp_path / "sessions.db")
    root = store.create_session(str(tmp_path))
    first = store.fork_session(root.session_id, branch_name="first")
    store.fork_session(root.session_id, branch_name="second")
    store.fork_session(root.session_id, branch_name="third")
    store.fork_session(first.session_id, branch_name="grandchild")

    with pytest.raises(SessionStorageError, match="child-node limit"):
        store.get_session_lineage(root.session_id)
    with pytest.raises(SessionStorageError, match="session tree exceeds"):
        store.session_tree(first.session_id)


def test_rename_session_rejects_oversized_title_without_mutation(tmp_path: Path) -> None:
    store = SessionStore(tmp_path / "sessions.db")
    session = store.create_session(str(tmp_path))
    store.rename_session(session.session_id, "Original")

    with pytest.raises(ValueError, match="session title cannot exceed 256 characters"):
        store.rename_session(session.session_id, "x" * 257)

    assert store.load_session(session.session_id).title == "Original"


def test_fork_title_suffix_stays_within_session_title_limit(tmp_path: Path) -> None:
    store = SessionStore(tmp_path / "sessions.db")
    session = store.create_session(str(tmp_path))
    store.rename_session(session.session_id, "x" * 256)

    fork = store.fork_session(session.session_id)

    assert fork.title.endswith(" (fork)")
    assert len(fork.title) <= 256


def test_create_session_rejects_oversized_model_metadata(tmp_path: Path) -> None:
    store = SessionStore(tmp_path / "sessions.db")

    with pytest.raises(ValueError, match="session model cannot exceed 512 UTF-8 bytes"):
        store.create_session(str(tmp_path), model="x" * 513)

    assert store.list_sessions(limit=10) == []


def test_session_summary_search_matches_redacted_context_summary(
    tmp_path: Path,
) -> None:
    store = SessionStore(tmp_path / "sessions.db")
    session = store.create_session(str(tmp_path))
    store.save_context_summary(session.session_id, "Reviewed authentication flow")

    matching = store.list_sessions(project_path=str(tmp_path), query="authentication")
    non_matching = store.list_sessions(project_path=str(tmp_path), query="payments")

    assert [item.session_id for item in matching] == [session.session_id]
    assert matching[0].context_summary == "Reviewed authentication flow"
    assert non_matching == []


def test_rename_unknown_session_fails(tmp_path: Path) -> None:
    store = SessionStore(tmp_path / "sessions.db")
    with pytest.raises(KeyError, match="Session not found"):
        store.rename_session("missing", "name")
