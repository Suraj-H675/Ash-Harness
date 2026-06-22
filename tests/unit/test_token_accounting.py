from core.session import SessionStore


def test_session_token_totals_accumulate(tmp_path) -> None:
    store = SessionStore(tmp_path / "sessions.db")
    session = store.create_session(str(tmp_path))
    store.save_session_token_stats(session.session_id, 10, 5, 0.01)
    store.save_session_token_stats(session.session_id, 20, 7, 0.02)

    from core.session import get_db_connection

    connection = get_db_connection(store.db_path)
    try:
        row = connection.execute(
            "SELECT total_tokens, total_prompt_tokens, total_completion_tokens, "
            "total_cost_usd FROM sessions WHERE session_id = ?",
            (session.session_id,),
        ).fetchone()
    finally:
        connection.close()
    assert row["total_tokens"] == 42
    assert row["total_prompt_tokens"] == 30
    assert row["total_completion_tokens"] == 12
    assert row["total_cost_usd"] == 0.03
    usage = store.get_session_usage(session.session_id)
    assert usage.total_tokens == 42
    assert usage.prompt_tokens == 30
    assert usage.completion_tokens == 12
    assert usage.cost_usd == 0.03
