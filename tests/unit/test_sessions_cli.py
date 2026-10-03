from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from ash.cli import main
from ash.commands.sessions import (
    list_session_summaries,
    parse_session_retention_days,
    render_recovery_reports,
    render_session_summaries,
    render_session_tree,
    select_startup_session,
)
from ash.core.checkpoints import recover_interrupted_turns
from ash.core.session import Message, SessionStore, ToolCallRecord
from ash.safety.guard import SafetyGuard


def test_session_summary_renderer_emits_json(tmp_path: Path) -> None:
    store = SessionStore(tmp_path / "sessions.db")
    session = store.create_session(str(tmp_path), model="openai/gpt-5.2")
    store.rename_session(session.session_id, "Feature Work")
    store.save_message(
        session.session_id,
        Message(role="user", content="hello", timestamp=datetime.now(timezone.utc)),
    )

    summaries = list_session_summaries(store, project_path=str(tmp_path))
    payload = json.loads(render_session_summaries(summaries, json_output=True))

    assert payload["sessions"][0]["session_id"] == session.session_id
    assert payload["sessions"][0]["title"] == "Feature Work"
    assert payload["sessions"][0]["message_count"] == 1
    assert payload["sessions"][0]["model"] == "openai/gpt-5.2"


def test_session_human_renderers_neutralize_persisted_terminal_controls(
    tmp_path: Path,
) -> None:
    store = SessionStore(tmp_path / "sessions.db")
    session = store.create_session(
        str(tmp_path / "project\u202ehidden\u202c"),
        model="openai/model\x1b]0;owned\x07",
    )
    store.rename_session(session.session_id, "title\x1b[2J\nforged")
    child = store.fork_session(
        session.session_id,
        branch_name="branch\x1b[3J\u202ehidden\u202c",
    )

    summaries = list_session_summaries(store, project_path=None, all_projects=True)
    summary_text = render_session_summaries(summaries)
    tree_text = render_session_tree(store.session_tree(session.session_id))

    assert "\x1b" not in summary_text
    assert "\x1b" not in tree_text
    assert "\u202e" not in summary_text
    assert "\u202e" not in tree_text
    assert "title\\x1b[2J forged" in summary_text
    assert "openai/model\\x1b]0;owned\\x07" in summary_text
    assert "branch\\x1b[3J\\u202ehidden\\u202c" in tree_text
    assert child.session_id in tree_text

    payload = json.loads(render_session_summaries(summaries, json_output=True))
    stored = next(item for item in payload["sessions"] if item["session_id"] == session.session_id)
    assert stored["title"] == "title\x1b[2J forged"
    assert stored["model"] == "openai/model\x1b]0;owned\x07"


def test_recovery_renderer_explains_attention_items() -> None:
    reports = [
        {
            "turn_id": "turn-1",
            "status": "needs_attention",
            "compensated_calls": ["call-edit"],
            "unknown_calls": [
                {"call_id": "call-command", "tool": "run_command"},
            ],
            "unresolved_files": ["src/app.py"],
            "recovered_calls": [
                {
                    "call_id": "call-command",
                    "tool": "run_command",
                    "error": "Tool outcome is ambiguous; inspect before retrying.",
                    "dispatched": True,
                    "ambiguous": True,
                }
            ],
        }
    ]

    rendered = render_recovery_reports(reports)

    assert "needs_attention" in rendered
    assert "Compensated tool calls: 1" in rendered
    assert "inspect the external system before retrying" in rendered
    assert "src/app.py" in rendered
    assert "inspect the items above before manually retrying" in rendered

    payload = json.loads(render_recovery_reports(reports, json_output=True))
    assert payload["reports"][0]["turn_id"] == "turn-1"


def test_sessions_cli_lists_current_project_sessions(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    db_dir = tmp_path / "db"
    store = SessionStore(db_dir / "sessions.db")
    current = store.create_session(str(tmp_path), model="anthropic/claude-sonnet-4-6")
    store.rename_session(current.session_id, "Current Project")
    other = store.create_session(str(tmp_path / "other"))
    monkeypatch.chdir(tmp_path)

    status = main(
        [
            "--db-directory",
            str(db_dir),
            "sessions",
            "--json",
        ]
    )

    assert status == 0
    payload = json.loads(capsys.readouterr().out)
    ids = {session["session_id"] for session in payload["sessions"]}
    assert ids == {current.session_id}
    assert other.session_id not in ids


def test_sessions_cli_filters_query_and_all_projects(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    db_dir = tmp_path / "db"
    store = SessionStore(db_dir / "sessions.db")
    first = store.create_session(str(tmp_path))
    second = store.create_session(str(tmp_path / "other"))
    store.rename_session(first.session_id, "frontend fix")
    store.rename_session(second.session_id, "backend fix")
    monkeypatch.chdir(tmp_path)

    status = main(
        [
            "--db-directory",
            str(db_dir),
            "sessions",
            "list",
            "--all-projects",
            "--query",
            "backend",
            "--json",
        ]
    )

    assert status == 0
    payload = json.loads(capsys.readouterr().out)
    assert [session["session_id"] for session in payload["sessions"]] == [
        second.session_id
    ]


def test_sessions_cli_renders_branch_tree_by_title(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    db_dir = tmp_path / "db"
    store = SessionStore(db_dir / "sessions.db")
    root = store.create_session(str(tmp_path))
    store.rename_session(root.session_id, "feature work")
    child = store.fork_session(root.session_id, branch_name="alternative")
    monkeypatch.chdir(tmp_path)

    status = main(
        [
            "--db-directory",
            str(db_dir),
            "sessions",
            "tree",
            "--session",
            "FEATURE WORK",
            "--json",
        ]
    )

    assert status == 0
    payload = json.loads(capsys.readouterr().out)
    assert [node["session_id"] for node in payload["sessions"]] == [
        root.session_id,
        child.session_id,
    ]
    assert payload["sessions"][1]["branch_name"] == "alternative"
    assert "alternative" in render_session_tree(store.session_tree(root.session_id))


def test_sessions_cli_renders_persisted_recovery_report(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    db_dir = tmp_path / "db"
    store = SessionStore(db_dir / "sessions.db")
    session = store.create_session(str(tmp_path))
    store.rename_session(session.session_id, "recovery target")
    turn_id = "turn-interrupted"
    store.start_turn(session.session_id, turn_id, "run command")
    store.save_tool_call(
        session.session_id,
        ToolCallRecord(
            call_id="call-command",
            tool_name="run_command",
            arguments={"command_line": "build"},
            approved=True,
            executed=False,
            dispatched=True,
            timestamp=datetime.now(timezone.utc),
        ),
        turn_id=turn_id,
    )
    store.interrupt_turn(turn_id)
    summary = recover_interrupted_turns(
        store,
        SafetyGuard(tmp_path),
        session.session_id,
    )
    assert summary.needs_attention is True
    monkeypatch.chdir(tmp_path)

    status = main(
        [
            "--db-directory",
            str(db_dir),
            "sessions",
            "recovery",
            "--session",
            "RECOVERY TARGET",
            "--json",
        ]
    )

    assert status == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["reports"][0]["turn_id"] == turn_id
    assert payload["reports"][0]["status"] == "needs_attention"
    assert payload["reports"][0]["unknown_calls"] == [
        {"call_id": "call-command", "tool": "run_command"}
    ]


def test_sessions_cli_rejects_invalid_limit(tmp_path: Path, capsys) -> None:
    db_dir = tmp_path / "db"
    SessionStore(db_dir / "sessions.db")

    status = main(
        [
            "--db-directory",
            str(db_dir),
            "sessions",
            "--limit",
            "0",
        ]
    )

    assert status == 2
    assert "limit must be positive" in capsys.readouterr().err


@pytest.mark.parametrize(
    "raw",
    ["0", "-1", "²", "9" * 5000],
)
def test_session_prune_days_reject_malformed_or_unbounded_values(raw: str) -> None:
    with pytest.raises(ValueError, match="positive integer"):
        parse_session_retention_days(raw)


def test_session_prune_days_accept_positive_decimal() -> None:
    assert parse_session_retention_days("30") == 30


def test_sessions_cli_rejects_tree_only_session_option(tmp_path: Path, capsys) -> None:
    db_dir = tmp_path / "db"
    SessionStore(db_dir / "sessions.db")

    status = main(
        [
            "--db-directory",
            str(db_dir),
            "sessions",
            "list",
            "--session",
            "unused",
        ]
    )

    assert status == 2
    assert "requires 'sessions tree' or 'sessions recovery'" in capsys.readouterr().err


def test_startup_continue_selects_latest_project_session(tmp_path: Path) -> None:
    store = SessionStore(tmp_path / "sessions.db")
    first = store.create_session(str(tmp_path))
    store.create_session(str(tmp_path))
    store.rename_session(first.session_id, "most recently touched")

    selection = asyncio.run(
        select_startup_session(
            store,
            project_path=str(tmp_path),
            continue_session=True,
        )
    )

    assert selection.session_id == first.session_id
    assert selection.cancelled is False


def test_startup_resume_supports_name_and_fork(tmp_path: Path) -> None:
    store = SessionStore(tmp_path / "sessions.db")
    original = store.create_session(str(tmp_path))
    store.rename_session(original.session_id, "auth refactor")

    selection = asyncio.run(
        select_startup_session(
            store,
            project_path=str(tmp_path),
            resume="AUTH REFACTOR",
            fork_session=True,
        )
    )

    assert selection.session_id != original.session_id
    assert store.load_session(selection.session_id).title == "auth refactor (fork)"


def test_bare_resume_requires_tty_and_honors_picker_cancel(tmp_path: Path) -> None:
    store = SessionStore(tmp_path / "sessions.db")
    store.create_session(str(tmp_path))

    with pytest.raises(ValueError, match="interactive terminal"):
        asyncio.run(
            select_startup_session(
                store,
                project_path=str(tmp_path),
                resume="",
                interactive=False,
            )
        )

    async def cancel() -> None:
        return None

    selection = asyncio.run(
        select_startup_session(
            store,
            project_path=str(tmp_path),
            resume="",
            interactive=True,
            picker=cancel,
        )
    )
    assert selection.cancelled is True


def test_continue_reports_empty_project(tmp_path: Path) -> None:
    store = SessionStore(tmp_path / "sessions.db")

    with pytest.raises(ValueError, match="no session found"):
        asyncio.run(
            select_startup_session(
                store,
                project_path=str(tmp_path),
                continue_session=True,
            )
        )
