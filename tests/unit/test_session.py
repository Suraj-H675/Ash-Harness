import asyncio
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest

from ash.core.session import (
    Message,
    SessionStore,
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

    assert {"sessions", "messages", "tool_calls", "audit_logs"}.issubset(table_names)
    assert {"idx_messages_session", "idx_tool_calls_session", "idx_audit_session"}.issubset(
        index_names
    )


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
            for row in conn.execute("SELECT label FROM writes ORDER BY id ASC").fetchall()
        ]

    assert entered == ["first-start", "first-end", "second-start", "second-end"]
    assert labels == ["first", "second"]
