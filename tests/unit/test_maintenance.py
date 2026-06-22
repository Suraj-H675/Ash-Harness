from datetime import datetime, timedelta, timezone

from cli.reset import reset_local_state
from core.session import SessionStore, get_db_connection


def test_session_retention_and_selective_reset(tmp_path, monkeypatch) -> None:
    store = SessionStore(tmp_path / "sessions.db")
    old = store.create_session(str(tmp_path))
    recent = store.create_session(str(tmp_path))
    connection = get_db_connection(store.db_path)
    with connection:
        connection.execute(
            "UPDATE sessions SET updated_at = ? WHERE session_id = ?",
            ((datetime.now(timezone.utc) - timedelta(days=40)).isoformat(), old.session_id),
        )
    connection.close()
    assert store.cleanup_sessions(30, project_path=str(tmp_path)) == 1
    assert store.load_session(recent.session_id).session_id == recent.session_id

    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    (home / ".ash").mkdir(parents=True)
    (home / ".ash" / ".env").write_text("SECRET=value")
    removed = reset_local_state(
        config=True, sessions=False, cache=False, confirmed=True
    )
    assert home / ".ash" / ".env" in removed
    assert not (home / ".ash" / ".env").exists()
