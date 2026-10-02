from ash.core.session import SessionStore


def test_session_token_totals_accumulate(tmp_path) -> None:
    store = SessionStore(tmp_path / "sessions.db")
    session = store.create_session(str(tmp_path))
    store.save_session_token_stats(
        session.session_id,
        10,
        5,
        0.01,
        cache_read_tokens=6,
        cache_write_tokens=2,
        estimated_prompt_tokens=4,
        estimated_completion_tokens=1,
        estimated_cost_usd=0.004,
    )
    store.save_session_token_stats(
        session.session_id,
        20,
        7,
        0.02,
        cache_read_tokens=5,
        cache_write_tokens=3,
        estimated_prompt_tokens=0,
        estimated_completion_tokens=2,
        estimated_cost_usd=0.006,
        cost_known=False,
    )

    from ash.core.session import get_db_connection

    connection = get_db_connection(store.db_path)
    try:
        row = connection.execute(
            "SELECT total_tokens, total_prompt_tokens, total_completion_tokens, "
            "total_cache_read_tokens, total_cache_write_tokens, total_cost_usd "
            ", estimated_prompt_tokens, estimated_completion_tokens, estimated_cost_usd, "
            "pricing_unknown_turns "
            "FROM sessions WHERE session_id = ?",
            (session.session_id,),
        ).fetchone()
    finally:
        connection.close()
    assert row["total_tokens"] == 42
    assert row["total_prompt_tokens"] == 30
    assert row["total_completion_tokens"] == 12
    assert row["total_cache_read_tokens"] == 11
    assert row["total_cache_write_tokens"] == 5
    assert row["total_cost_usd"] == 0.03
    assert row["estimated_prompt_tokens"] == 4
    assert row["estimated_completion_tokens"] == 3
    assert row["estimated_cost_usd"] == 0.01
    assert row["pricing_unknown_turns"] == 1
    usage = store.get_session_usage(session.session_id)
    assert usage.total_tokens == 42
    assert usage.prompt_tokens == 30
    assert usage.completion_tokens == 12
    assert usage.cache_read_tokens == 11
    assert usage.cache_write_tokens == 5
    assert usage.cost_usd == 0.03
    assert usage.estimated_prompt_tokens == 4
    assert usage.estimated_completion_tokens == 3
    assert usage.estimated_cost_usd == 0.01
    assert usage.has_estimates is True
    assert usage.cost_known is False


def test_session_usage_pricing_known_when_all_turns_are_priced(tmp_path) -> None:
    store = SessionStore(tmp_path / "known.db")
    session = store.create_session(str(tmp_path))

    store.save_session_token_stats(session.session_id, 4, 2, 0.001, cost_known=True)

    usage = store.get_session_usage(session.session_id)
    assert usage.pricing_unknown_turns == 0
    assert usage.cost_known is True
