import json
from datetime import datetime, timezone

import pytest

from ash.core.session import Message, SessionStore


def test_session_jsonl_import_rejects_duplicate_fields(tmp_path) -> None:
    store = SessionStore(tmp_path / "sessions.db")
    content = (
        '{"schema_version":2,"schema_version":1,"type":"session",'
        '"title":"Imported","model":"provider/model"}\n'
        '{"schema_version":1,"type":"message","role":"user",'
        '"role":"assistant","content":"ambiguous",'
        '"timestamp":"2026-01-01T00:00:00+00:00","metadata":{}}\n'
    )

    try:
        store.import_session_jsonl(content, project_path=str(tmp_path / "new"))
    except ValueError as exc:
        assert "duplicate JSON object key" in str(exc)
    else:
        raise AssertionError("duplicate JSONL fields must be rejected")


def test_session_jsonl_import_failure_is_atomic(tmp_path) -> None:
    store = SessionStore(tmp_path / "sessions.db")
    content = (
        '{"schema_version":1,"type":"session","title":"Broken",'
        '"model":"provider/model"}\n'
        '{"schema_version":1,"type":"message","role":"user",'
        '"content":"must not persist","timestamp":"2026-01-01T00:00:00+00:00",'
        '"metadata":{}}\n'
        '{"schema_version":1,"type":"message","role":"invalid",'
        '"content":"bad","timestamp":"2026-01-01T00:00:01+00:00",'
        '"metadata":{}}\n'
    )

    with pytest.raises(ValueError, match="invalid imported message role"):
        store.import_session_jsonl(content, project_path=str(tmp_path / "new"))

    assert store.list_sessions(limit=10) == []


def test_session_jsonl_import_rejects_unknown_message_schema_atomically(tmp_path) -> None:
    store = SessionStore(tmp_path / "sessions.db")
    content = (
        '{"schema_version":1,"type":"session","title":"Imported",'
        '"model":"provider/model"}\n'
        '{"schema_version":999,"type":"message","role":"user",'
        '"content":"must not persist","timestamp":"2026-01-01T00:00:00+00:00",'
        '"metadata":{}}\n'
    )

    with pytest.raises(ValueError, match="message schema"):
        store.import_session_jsonl(content, project_path=str(tmp_path / "new"))

    assert store.list_sessions(limit=10) == []


@pytest.mark.parametrize(
    "record",
    [
        (
            '{"schema_version":1,"type":"message","role":"assistant",'
            '"content":"bad tool metadata",'
            '"timestamp":"2026-01-01T00:00:00+00:00",'
            '"metadata":{"tool_calls":"not-a-list"}}'
        ),
        (
            '{"schema_version":1,"type":"message","role":"tool",'
            '"content":"orphan tool result",'
            '"timestamp":"2026-01-01T00:00:00+00:00","metadata":{}}'
        ),
        (
            '{"schema_version":1,"type":"message","role":"assistant",'
            '"content":"bad falsy tool metadata",'
            '"timestamp":"2026-01-01T00:00:00+00:00",'
            '"metadata":{"tool_calls":{}}}'
        ),
        (
            '{"schema_version":1,"type":"message","role":"assistant",'
            '"content":"bad falsy tool metadata",'
            '"timestamp":"2026-01-01T00:00:00+00:00",'
            '"metadata":{"tool_calls":""}}'
        ),
        (
            '{"schema_version":1,"type":"message","role":"assistant",'
            '"content":"bad falsy tool metadata",'
            '"timestamp":"2026-01-01T00:00:00+00:00",'
            '"metadata":{"tool_calls":0}}'
        ),
        (
            '{"schema_version":1,"type":"message","role":"assistant",'
            '"content":"bad falsy tool metadata",'
            '"timestamp":"2026-01-01T00:00:00+00:00",'
            '"metadata":{"tool_calls":false}}'
        ),
    ],
)
def test_session_jsonl_import_rejects_provider_invalid_tool_metadata_atomically(
    tmp_path,
    record,
) -> None:
    store = SessionStore(tmp_path / "sessions.db")
    content = (
        '{"schema_version":1,"type":"session","title":"Imported",'
        '"model":"provider/model"}\n'
        + record
        + "\n"
    )

    with pytest.raises(ValueError, match="provider message"):
        store.import_session_jsonl(content, project_path=str(tmp_path / "new"))

    assert store.list_sessions(limit=10) == []


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("title", '{"nested":true}'),
        ("model", '["not","a","model"]'),
        ("content", '{"not":"text"}'),
    ],
)
def test_session_jsonl_import_rejects_non_string_text_fields_atomically(
    tmp_path,
    field,
    value,
) -> None:
    store = SessionStore(tmp_path / "sessions.db")
    header_title = value if field == "title" else '"Imported"'
    header_model = value if field == "model" else '"provider/model"'
    message_content = value if field == "content" else '"hello"'
    content = (
        f'{{"schema_version":1,"type":"session","title":{header_title},'
        f'"model":{header_model}}}\n'
        '{"schema_version":1,"type":"message","role":"user",'
        f'"content":{message_content},'
        '"timestamp":"2026-01-01T00:00:00+00:00","metadata":{}}\n'
    )

    with pytest.raises(ValueError, match="must be a string"):
        store.import_session_jsonl(content, project_path=str(tmp_path / "new"))

    assert store.list_sessions(limit=10) == []


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


def test_session_jsonl_import_accepts_valid_tool_call_round_trip(tmp_path) -> None:
    store = SessionStore(tmp_path / "sessions.db")
    source = store.create_session(str(tmp_path), model="provider/model")
    timestamp = datetime.now(timezone.utc)
    store.save_message(
        source.session_id,
        Message(
            role="assistant",
            content="",
            timestamp=timestamp,
            metadata={
                "tool_calls": [
                    {
                        "call_id": "call-1",
                        "name": "read_file",
                        "arguments": {"file_path": "README.md"},
                    }
                ]
            },
        ),
    )
    store.save_message(
        source.session_id,
        Message(
            role="tool",
            content="contents",
            timestamp=timestamp,
            metadata={"call_id": "call-1"},
        ),
    )

    exported = store.export_session(source.session_id, format="jsonl")
    imported = store.import_session_jsonl(
        exported,
        project_path=str(tmp_path / "imported"),
    )

    assert len(imported.messages) == 2
    assert imported.messages[0].metadata["tool_calls"][0]["call_id"] == "call-1"
    assert imported.messages[1].metadata["call_id"] == "call-1"
