from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any

import pytest

from ash.core.session import (
    Message,
    SessionForkConflictError,
    SessionStorageError,
    SessionStore,
)


def _recovery_commit_args(turn_id: str) -> dict[str, Any]:
    call = SimpleNamespace(
        call_id="call-1",
        tool_name="run_command",
        error="outcome unknown",
        dispatched=True,
        ambiguous=True,
        success=False,
        output="",
        assistant_message_id=None,
        assistant_arguments=None,
        arguments={"command_line": "echo hello"},
    )
    return {
        "call_errors": {"call-1": "outcome unknown"},
        "restored_checkpoint_ids": [],
        "recovery": {
            "turn_id": turn_id,
            "status": "needs_attention",
            "unknown_calls": [{"call_id": "call-1", "tool": "run_command"}],
            "unresolved_files": [],
            "recovered_calls": [{"call_id": "call-1"}],
        },
        "recovered_calls": [call],
    }


def test_turn_journal_recovery_and_rewind(tmp_path) -> None:
    store = SessionStore(tmp_path / "sessions.db")
    session = store.create_session(str(tmp_path))
    store.start_turn(session.session_id, "turn-1", "work")
    assert store.reconcile_interrupted_turns(session.session_id) == 1
    assert store.reconcile_interrupted_turns(session.session_id) == 0

    for content in ("one", "two", "three"):
        store.save_message(
            session.session_id,
            Message(
                role="user",
                content=content,
                timestamp=datetime.now(timezone.utc),
            ),
        )
    rewound = store.rewind_session(session.session_id, 1)
    assert [message.content for message in rewound.messages] == ["one"]
    assert rewound.context_summary == ""


def test_interrupt_marks_only_selected_started_turn(tmp_path) -> None:
    store = SessionStore(tmp_path / "sessions.db")
    session = store.create_session(str(tmp_path))
    store.start_turn(session.session_id, "turn-one", "first")
    store.start_turn(session.session_id, "turn-two", "second")

    store.interrupt_turn("turn-one")

    assert store.reconcile_interrupted_turns(session.session_id) == 1
    assert store.reconcile_interrupted_turns(session.session_id) == 0


def test_interrupted_turn_cannot_be_completed_to_bypass_restart_receipt(
    tmp_path,
) -> None:
    store = SessionStore(tmp_path / "sessions.db")
    session = store.create_session(str(tmp_path))
    store.start_turn(session.session_id, "turn-interrupted", "unfinished")
    store.interrupt_turn("turn-interrupted")

    with pytest.raises(SessionForkConflictError, match="quiescent"):
        store.restart_context_session(
            session.session_id,
            expected_project_path=tmp_path,
            user_handoff="Inspect and verify the unfinished work",
        )

    with pytest.raises(SessionStorageError, match="interrupted"):
        store.complete_turn("turn-interrupted")

    with store._connect() as conn:
        row = conn.execute(
            "SELECT status, recovery_json FROM turn_journal WHERE turn_id = ?",
            ("turn-interrupted",),
        ).fetchone()
    assert row["status"] == "interrupted"
    assert row["recovery_json"] == "{}"

    with pytest.raises(SessionForkConflictError, match="quiescent"):
        store.restart_context_session(
            session.session_id,
            expected_project_path=tmp_path,
            user_handoff="Inspect and verify the unfinished work",
        )


def test_complete_turn_is_idempotent_and_rejects_missing_turn(
    tmp_path, monkeypatch
) -> None:
    store = SessionStore(tmp_path / "sessions.db")
    session = store.create_session(str(tmp_path))
    store.start_turn(session.session_id, "turn-completed", "finished")
    completion_times = iter(
        (
            datetime(2026, 11, 18, 10, 0, tzinfo=timezone.utc),
            datetime(2026, 11, 18, 10, 1, tzinfo=timezone.utc),
            datetime(2026, 11, 18, 10, 2, tzinfo=timezone.utc),
        )
    )
    monkeypatch.setattr("ash.core.session._utc_now", lambda: next(completion_times))

    store.complete_turn("turn-completed")
    with store._connect() as conn:
        first = conn.execute(
            "SELECT status, completed_at FROM turn_journal WHERE turn_id = ?",
            ("turn-completed",),
        ).fetchone()

    store.complete_turn("turn-completed")
    with store._connect() as conn:
        second = conn.execute(
            "SELECT status, completed_at FROM turn_journal WHERE turn_id = ?",
            ("turn-completed",),
        ).fetchone()

    assert first["status"] == second["status"] == "completed"
    assert first["completed_at"] == second["completed_at"]

    with pytest.raises(KeyError, match="Turn not found"):
        store.complete_turn("missing-turn")


def test_interrupted_recovery_receipt_is_committed_exactly_once(tmp_path) -> None:
    store = SessionStore(tmp_path / "sessions.db")
    session = store.create_session(str(tmp_path))
    store.start_turn(session.session_id, "turn-recovery", "uncertain action")
    store.interrupt_turn("turn-recovery")
    recovery_args = _recovery_commit_args("turn-recovery")

    store.finalize_interrupted_recovery(
        session.session_id, "turn-recovery", **recovery_args
    )
    first_audit = store.list_audit_logs(session.session_id)
    first_session = store.load_session(session.session_id)

    with pytest.raises(SessionStorageError, match="already finalized"):
        store.finalize_interrupted_recovery(
            session.session_id, "turn-recovery", **recovery_args
        )

    second_audit = store.list_audit_logs(session.session_id)
    second_session = store.load_session(session.session_id)
    assert len(first_audit) == len(second_audit) == 1
    assert sum(message.role == "tool" for message in first_session.messages) == 1
    assert sum(message.role == "tool" for message in second_session.messages) == 1
    assert len(store.interrupted_recovery_reports(session.session_id)) == 1


def test_interrupted_recovery_rejects_completed_turn_before_writes(tmp_path) -> None:
    store = SessionStore(tmp_path / "sessions.db")
    session = store.create_session(str(tmp_path))
    store.start_turn(session.session_id, "turn-completed", "finished")
    store.complete_turn("turn-completed")
    recovery_args = _recovery_commit_args("turn-completed")

    with pytest.raises(SessionStorageError, match="completed"):
        store.finalize_interrupted_recovery(
            session.session_id,
            "turn-completed",
            **recovery_args,
        )

    assert store.list_audit_logs(session.session_id) == []
    loaded = store.load_session(session.session_id)
    assert all(message.role != "tool" for message in loaded.messages)
    with store._connect() as conn:
        row = conn.execute(
            "SELECT status, recovery_json FROM turn_journal WHERE turn_id = ?",
            ("turn-completed",),
        ).fetchone()
    assert row["status"] == "completed"
    assert row["recovery_json"] == "{}"



@pytest.mark.parametrize(
    ("recovery", "message"),
    [
        ({}, "must not be empty"),
        ({"turn_id": "another-turn"}, "does not match"),
    ],
)
def test_recovery_receipt_is_validated_before_turn_mutation(
    tmp_path, recovery, message
) -> None:
    store = SessionStore(tmp_path / "sessions.db")
    session = store.create_session(str(tmp_path))
    store.start_turn(session.session_id, "turn-interrupted", "unfinished")
    store.interrupt_turn("turn-interrupted")

    with pytest.raises(ValueError, match=message):
        store.finalize_interrupted_recovery(
            session.session_id,
            "turn-interrupted",
            call_errors={},
            restored_checkpoint_ids=[],
            recovery=recovery,
            recovered_calls=[],
        )

    with store._connect() as conn:
        row = conn.execute(
            "SELECT status, recovery_json FROM turn_journal WHERE turn_id = ?",
            ("turn-interrupted",),
        ).fetchone()
    assert row["status"] == "interrupted"
    assert row["recovery_json"] == "{}"
    assert store.list_audit_logs(session.session_id) == []


def test_recovery_cannot_mark_another_turns_checkpoint_restored(tmp_path) -> None:
    store = SessionStore(tmp_path / "sessions.db")
    session = store.create_session(str(tmp_path))
    store.start_turn(session.session_id, "turn-recovering", "recover this turn")
    store.interrupt_turn("turn-recovering")
    store.start_turn(session.session_id, "turn-other", "leave this turn alone")
    store.save_file_checkpoints(
        session.session_id,
        "turn-other",
        "write_file",
        [(str(tmp_path / "other.py"), True, b"before", 0o644)],
        call_id="other-call",
    )
    checkpoint = store.unmatched_incomplete_checkpoints(
        session.session_id, "turn-other", []
    )[0]

    with pytest.raises(SessionStorageError, match="checkpoint"):
        store.finalize_interrupted_recovery(
            session.session_id,
            "turn-recovering",
            call_errors={},
            restored_checkpoint_ids=[int(checkpoint["checkpoint_id"])],
            recovery={
                "turn_id": "turn-recovering",
                "status": "interrupted",
                "unknown_calls": [],
                "unresolved_files": [],
                "recovered_calls": [],
            },
            recovered_calls=[],
        )

    with store._connect() as conn:
        row = conn.execute(
            "SELECT status, recovery_json FROM turn_journal WHERE turn_id = ?",
            ("turn-recovering",),
        ).fetchone()
        checkpoint_row = conn.execute(
            "SELECT restored FROM file_checkpoints WHERE checkpoint_id = ?",
            (int(checkpoint["checkpoint_id"]),),
        ).fetchone()
    assert row["status"] == "interrupted"
    assert row["recovery_json"] == "{}"
    assert checkpoint_row["restored"] == 0
