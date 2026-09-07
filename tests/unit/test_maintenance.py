from datetime import datetime, timedelta, timezone

import pytest

from ash.cli import main
from ash.commands.reset import reset_local_state
from ash.core.session import SessionStore, get_db_connection


def test_session_retention_and_selective_reset(tmp_path, monkeypatch) -> None:
    store = SessionStore(tmp_path / "sessions.db")
    old = store.create_session(str(tmp_path))
    recent = store.create_session(str(tmp_path))
    connection = get_db_connection(store.db_path)
    with connection:
        connection.execute(
            "UPDATE sessions SET updated_at = ? WHERE session_id = ?",
            (
                (datetime.now(timezone.utc) - timedelta(days=40)).isoformat(),
                old.session_id,
            ),
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


def test_reset_rejects_symlinked_ash_state_directory(tmp_path, monkeypatch) -> None:
    home = tmp_path / "home"
    outside = tmp_path / "outside"
    home.mkdir()
    outside.mkdir()
    victim = outside / ".env"
    victim.write_text("KEEP=value\n", encoding="utf-8")
    try:
        (home / ".ash").symlink_to(outside, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"symlink creation is unavailable: {exc}")
    monkeypatch.setenv("HOME", str(home))

    with pytest.raises(ValueError, match="symlinked Ash state directory"):
        reset_local_state(config=True, sessions=False, cache=False, confirmed=True)

    assert victim.read_text(encoding="utf-8") == "KEEP=value\n"


def test_reset_cli_reports_symlinked_state_without_traceback(
    tmp_path, monkeypatch, capsys
) -> None:
    home = tmp_path / "home"
    outside = tmp_path / "outside"
    home.mkdir()
    outside.mkdir()
    victim = outside / ".env"
    victim.write_text("KEEP=value\n", encoding="utf-8")
    try:
        (home / ".ash").symlink_to(outside, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"symlink creation is unavailable: {exc}")
    monkeypatch.setenv("HOME", str(home))

    assert main(["reset", "--config", "--yes"]) == 2

    captured = capsys.readouterr()
    assert "symlinked Ash state directory" in captured.err
    assert "Traceback" not in captured.err
    assert victim.read_text(encoding="utf-8") == "KEEP=value\n"
