from datetime import datetime, timezone

from ash.core.session import Message, SessionStore


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
