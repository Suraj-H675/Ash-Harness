import asyncio
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest

from ash.core.session import (
    Message,
    SessionResolutionError,
    SessionStore,
    SessionStorageError,
    ToolCallRecord,
    get_db_connection,
    write_transaction,
)


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
            == 14
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
    with get_db_connection(db_path) as conn, conn:
        conn.execute("DROP TABLE mcp_tasks")
        conn.execute("DELETE FROM schema_migrations WHERE version >= 12")

    SessionStore(db_path)

    assert len(list(tmp_path.glob("v11.db.before-v14-migration.*.backup"))) == 1
    with get_db_connection(db_path) as conn:
        assert conn.execute(
            "SELECT MAX(version) FROM schema_migrations"
        ).fetchone()[0] == 14
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
    with get_db_connection(db_path) as conn, conn:
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

    assert len(list(tmp_path.glob("v12.db.before-v14-migration.*.backup"))) == 1
    with get_db_connection(db_path) as conn:
        columns = {
            row["name"] for row in conn.execute("PRAGMA table_info(mcp_tasks)")
        }
        assert "server_fingerprint" in columns
        assert conn.execute(
            "SELECT MAX(version) FROM schema_migrations"
        ).fetchone()[0] == 14


def test_v14_migration_scopes_tool_call_and_event_ids_to_sessions(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "v13.db"
    store = SessionStore(db_path)
    first = store.create_session(project_path="/workspace/first")
    second = store.create_session(project_path="/workspace/second")
    with get_db_connection(db_path) as conn, conn:
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

    assert len(list(tmp_path.glob("v13.db.before-v14-migration.*.backup"))) == 1
    with get_db_connection(db_path) as conn:
        assert conn.execute(
            "SELECT MAX(version) FROM schema_migrations"
        ).fetchone()[0] == 14
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
    backups = list(tmp_path.glob("legacy.db.before-v14-migration.*.backup"))
    assert len(backups) == 1
    with sqlite3.connect(backups[0]) as conn:
        assert conn.execute("SELECT session_id FROM sessions").fetchone()[0] == "legacy"


def test_v7_migration_preserves_checkpoints_and_adds_call_granularity(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "v6.db"
    store = SessionStore(db_path)
    session = store.create_session(str(tmp_path))
    with get_db_connection(db_path) as conn, conn:
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
    assert len(list(tmp_path.glob("v6.db.before-v14-migration.*.backup"))) == 1


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
            "output": "OPENAI_API_KEY=sk-proj-abcdefghijklmnopqrstuvwxyz",
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
    real_connect = sqlite3.connect
    swapped = False

    def swap_source_before_connect(database, *args, **kwargs):
        nonlocal swapped
        database_text = str(database)
        if (
            not swapped
            and (
                Path(database_text) == source
                or database_text.startswith("file:/dev/fd/")
            )
        ):
            swapped = True
            source.unlink()
            try:
                source.symlink_to(replacement)
            except OSError as exc:
                pytest.skip(f"symlinks are unavailable: {exc}")
        return real_connect(database, *args, **kwargs)

    monkeypatch.setattr(session_module.sqlite3, "connect", swap_source_before_connect)

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
    assert SessionStore(replacement).load_session(replacement_session.session_id).session_id == (
        replacement_session.session_id
    )
    assert source_session.session_id != replacement_session.session_id


def test_manual_backup_includes_committed_live_wal_contents(tmp_path: Path) -> None:
    source = tmp_path / "sessions.db"
    store = SessionStore(source)
    destination = tmp_path / "manual.backup"

    with get_db_connection(source) as writer:
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

        assert store.backup(destination) == destination

    assert SessionStore(destination).load_session(session_id).title == "from wal"


def test_manual_backup_fails_closed_without_secure_source_descriptor_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import ash.core.session as session_module

    store = SessionStore(tmp_path / "sessions.db")
    store.create_session("/workspace")
    destination = tmp_path / "manual.backup"
    monkeypatch.setattr(session_module, "descriptor_path", lambda descriptor: None)

    with pytest.raises(
        SessionStorageError,
        match="Secure session backup source opening is unavailable",
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


def test_connection_uses_wal_pragmas_and_foreign_keys(tmp_path: Path) -> None:
    db_path = tmp_path / "session_store.db"

    conn = get_db_connection(db_path)
    try:
        assert conn.execute("PRAGMA journal_mode;").fetchone()[0].lower() == "wal"
        assert conn.execute("PRAGMA synchronous;").fetchone()[0] == 1
        assert conn.execute("PRAGMA foreign_keys;").fetchone()[0] == 1
    finally:
        conn.close()


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
    foreign = store.create_session(str(tmp_path / "other"))
    store.rename_session(first.session_id, "duplicate")
    store.rename_session(second.session_id, "DUPLICATE")

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
