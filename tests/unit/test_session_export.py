import json
from datetime import datetime, timezone

from core.session import Message, SessionStore


def test_fork_and_redacted_exports(tmp_path) -> None:
    store = SessionStore(tmp_path / "sessions.db")
    source = store.create_session(str(tmp_path), model="provider/model")
    store.rename_session(source.session_id, "Original")
    messages = [
        Message(role="user", content="hello", timestamp=datetime.now(timezone.utc)),
        Message(
            role="assistant",
            content="token=super-secret-value",
            timestamp=datetime.now(timezone.utc),
            metadata={"api_key": "sk-project-secret-value"},
        ),
    ]
    for message in messages:
        store.save_message(source.session_id, message)

    fork = store.fork_session(source.session_id, message_count=1)
    assert fork.model == "provider/model"
    assert len(fork.messages) == 1
    assert fork.title == "Original (fork)"

    jsonl = store.export_session(source.session_id, format="jsonl")
    records = [json.loads(line) for line in jsonl.splitlines()]
    assert records[0]["schema_version"] == 1
    assert "super-secret-value" not in jsonl
    assert "sk-project-secret-value" not in jsonl
    assert records[2]["metadata"]["api_key"] == "[REDACTED]"

    markdown = store.export_session(source.session_id, format="markdown")
    assert "## User" in markdown
    assert "super-secret-value" not in markdown

    imported = store.import_session_jsonl(jsonl, project_path=str(tmp_path / "new"))
    assert imported.project_path == str(tmp_path / "new")
    assert imported.title == "Original (imported)"
    assert len(imported.messages) == 2
