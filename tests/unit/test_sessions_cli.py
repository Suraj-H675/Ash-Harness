from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path
import threading
import time

import pytest
from prompt_toolkit.input.defaults import create_pipe_input
from prompt_toolkit.output import DummyOutput

from ash.cli import main
from ash.commands.sessions import (
    list_session_summaries,
    parse_session_retention_days,
    pick_session,
    render_recovery_reports,
    render_session_summaries,
    render_session_tree,
    select_startup_session,
)
from ash.core.checkpoints import recover_interrupted_turns
from ash.core.session import (
    Message,
    SessionStore,
    SessionSummary,
    ToolCallRecord,
    get_db_connection,
)
from ash.safety.guard import SafetyGuard


@pytest.mark.parametrize(
    "query", ("needle-only-in-archived-session", "archived-heading-unique")
)
@pytest.mark.asyncio
async def test_pick_session_can_select_old_project_scoped_transcript_match(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    query: str,
) -> None:
    from ash.ui.session_picker import SessionPicker

    project = tmp_path / "project"
    project.mkdir()
    store = SessionStore(tmp_path / "sessions.db")
    archived = store.create_session(str(project))
    store.save_message(
        archived.session_id,
        Message(
            role="user",
            content="needle-only-in-archived-session",
            timestamp=datetime.now(timezone.utc),
        ),
    )
    store.rename_session(archived.session_id, "archived-heading-unique")
    for _ in range(200):
        store.create_session(str(project))

    assert archived.session_id not in {
        summary.session_id
        for summary in store.list_sessions(project_path=str(project), limit=200)
    }
    assert store.search_session_messages(
        project_path=str(project),
        query="needle-only-in-archived-session",
        limit=50,
    )

    with create_pipe_input() as pipe:
        original_picker = SessionPicker

        def picker_with_test_io(*args, **kwargs):
            return original_picker(
                *args,
                input=pipe,
                output=DummyOutput(),
                **kwargs,
            )

        monkeypatch.setattr("ash.ui.session_picker.SessionPicker", picker_with_test_io)
        pending = asyncio.create_task(
            pick_session(store, project_path=str(project))
        )
        await asyncio.sleep(0)
        pipe.send_text(f"{query}\r")

        assert await pending == archived.session_id


@pytest.mark.asyncio
async def test_pick_session_loads_initial_sessions_without_blocking_event_loop(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    store = SessionStore(tmp_path / "sessions.db")
    session = store.create_session(str(project))
    original_list_sessions = store.list_sessions
    release_read = threading.Event()

    def blocked_list_sessions(
        *, project_path: str, limit: int
    ) -> list[SessionSummary]:
        release_read.wait(timeout=1)
        return original_list_sessions(project_path=project_path, limit=limit)

    class ImmediatePicker:
        def __init__(
            self,
            sessions: list[SessionSummary],
            **_kwargs: object,
        ) -> None:
            self.sessions = sessions

        async def run(self) -> str:
            return self.sessions[0].session_id

    monkeypatch.setattr(store, "list_sessions", blocked_list_sessions)
    monkeypatch.setattr("ash.ui.session_picker.SessionPicker", ImmediatePicker)

    loop = asyncio.get_running_loop()
    heartbeat: asyncio.Future[float] = loop.create_future()
    started_at = loop.time()
    released_at: float | None = None

    def mark_heartbeat() -> None:
        heartbeat.set_result(loop.time())

    def release_blocked_read() -> None:
        nonlocal released_at
        released_at = time.monotonic()
        release_read.set()

    timer = threading.Timer(0.2, release_blocked_read)
    timer.start()
    loop.call_later(0.01, mark_heartbeat)
    pending = asyncio.create_task(pick_session(store, project_path=str(project)))
    try:
        heartbeat_at = await heartbeat
        selected = await pending
        assert released_at is not None
        assert heartbeat_at < released_at
        assert selected == session.session_id
        assert heartbeat_at > started_at
    finally:
        release_read.set()
        timer.cancel()


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


def test_sessions_cli_searches_current_project_transcript_content(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    db_dir = tmp_path / "db"
    project = tmp_path / "project"
    other = tmp_path / "other"
    project.mkdir()
    other.mkdir()
    store = SessionStore(db_dir / "sessions.db")
    current = store.create_session(str(project))
    external = store.create_session(str(other))
    timestamp = datetime.now(timezone.utc)
    store.save_message(
        current.session_id,
        Message(
            role="user",
            content="diagnose websocket reconnect regression",
            timestamp=timestamp,
        ),
    )
    store.save_message(
        external.session_id,
        Message(
            role="user",
            content="websocket reconnect from another project",
            timestamp=timestamp,
        ),
    )
    monkeypatch.chdir(project)

    status = main(
        [
            "--db-directory",
            str(db_dir),
            "sessions",
            "search",
            "--query",
            "websocket reconnect",
            "--json",
        ]
    )

    assert status == 0
    payload = json.loads(capsys.readouterr().out)
    assert [item["session_id"] for item in payload["matches"]] == [
        current.session_id
    ]


def test_sessions_cli_search_rejects_cross_project_scope(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    db_dir = tmp_path / "db"
    SessionStore(db_dir / "sessions.db")
    monkeypatch.chdir(tmp_path)

    assert (
        main(
            [
                "--db-directory",
                str(db_dir),
                "sessions",
                "search",
                "--query",
                "needle",
                "--all-projects",
            ]
        )
        == 2
    )
    assert "scoped to the current project" in capsys.readouterr().err


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


@pytest.mark.parametrize(
    ("selection_args", "protect_target"),
    [
        (["--session"], True),
        (["--resume"], True),
        (["--continue"], False),
    ],
)
def test_startup_retention_preserves_explicit_target_tree_only(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    selection_args: list[str],
    protect_target: bool,
) -> None:
    home = tmp_path / "home"
    db_dir = tmp_path / "db"
    workspace = tmp_path / "workspace"
    other_workspace = tmp_path / "other-workspace"
    workspace.mkdir()
    other_workspace.mkdir()
    store = SessionStore(db_dir / "sessions.db")
    target = store.create_session(str(workspace), model="lmstudio/local-model")
    store.rename_session(target.session_id, "Explicit target")
    branch = store.fork_session(target.session_id, branch_name="work branch")
    unrelated = store.create_session(str(workspace), model="lmstudio/local-model")
    foreign = store.create_session(str(other_workspace), model="lmstudio/local-model")
    with get_db_connection(store.db_path) as conn, conn:
        conn.execute(
            "UPDATE sessions SET updated_at = ?",
            ("2000-01-01T00:00:00+00:00",),
        )

    monkeypatch.chdir(workspace)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("ASH_MODEL", "lmstudio/local-model")
    monkeypatch.setenv("ASH_SESSION_RETENTION_DAYS", "1")

    class RuntimeReached(RuntimeError):
        pass

    def stop_after_selection(*args, **kwargs) -> None:
        raise RuntimeReached("startup selection completed")

    monkeypatch.setattr("ash.runtime.build_runtime", stop_after_selection)
    arguments = ["--db-directory", str(db_dir), *selection_args]
    if selection_args == ["--session"]:
        arguments.append(target.session_id)
    elif selection_args == ["--resume"]:
        arguments.append("Explicit target")

    if protect_target:
        with pytest.raises(RuntimeReached, match="startup selection completed"):
            main(arguments)
        assert store.load_session(target.session_id).session_id == target.session_id
        assert store.load_session(branch.session_id).session_id == branch.session_id
        with pytest.raises(KeyError, match="Session not found"):
            store.load_session(unrelated.session_id)
    else:
        assert main(arguments) == 1
        assert "no session found to continue" in capsys.readouterr().err
        with pytest.raises(KeyError, match="Session not found"):
            store.load_session(target.session_id)
        with pytest.raises(KeyError, match="Session not found"):
            store.load_session(branch.session_id)
        with pytest.raises(KeyError, match="Session not found"):
            store.load_session(unrelated.session_id)

    assert store.load_session(foreign.session_id).session_id == foreign.session_id


@pytest.mark.parametrize(
    "selection_args",
    [
        ["--session", "missing-session-id"],
        ["--resume", "missing title"],
        ["--resume", "Duplicate title"],
    ],
)
def test_invalid_explicit_resume_does_not_delete_expired_sessions(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    selection_args: list[str],
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    store = SessionStore(tmp_path / "db" / "sessions.db")
    sessions = [store.create_session(str(workspace)) for _ in range(2)]
    if selection_args[-1] == "Duplicate title":
        for session in sessions:
            store.rename_session(session.session_id, "Duplicate title")
    with get_db_connection(store.db_path) as conn, conn:
        conn.execute(
            "UPDATE sessions SET updated_at = ?",
            ("2000-01-01T00:00:00+00:00",),
        )

    monkeypatch.chdir(workspace)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("ASH_MODEL", "lmstudio/local-model")
    monkeypatch.setenv("ASH_SESSION_RETENTION_DAYS", "1")

    assert main(["--db-directory", str(tmp_path / "db"), *selection_args]) != 0
    assert all(store.session_exists(session.session_id) for session in sessions)


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


def test_bare_resume_rejects_full_screen_picker_in_screen_reader_mode(
    tmp_path: Path,
) -> None:
    store = SessionStore(tmp_path / "sessions.db")
    store.create_session(str(tmp_path))

    async def forbidden_picker() -> None:
        raise AssertionError("screen-reader mode must not open the session picker")

    with pytest.raises(ValueError, match="screen-reader mode"):
        asyncio.run(
            select_startup_session(
                store,
                project_path=str(tmp_path),
                resume="",
                interactive=True,
                screen_reader_mode=True,
                picker=forbidden_picker,
            )
        )


def test_bare_resume_rejects_full_screen_picker_in_limited_terminal_mode(
    tmp_path: Path,
) -> None:
    store = SessionStore(tmp_path / "sessions.db")
    store.create_session(str(tmp_path))

    async def forbidden_picker() -> None:
        raise AssertionError("limited terminal mode must not open the session picker")

    with pytest.raises(ValueError, match="limited terminal mode"):
        asyncio.run(
            select_startup_session(
                store,
                project_path=str(tmp_path),
                resume="",
                interactive=False,
                limited_terminal_mode=True,
                picker=forbidden_picker,
            )
        )


def test_startup_bare_resume_lists_sessions_linearly_in_screen_reader_mode(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    home = tmp_path / "home"
    db_dir = tmp_path / "db"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    store = SessionStore(db_dir / "sessions.db")
    session = store.create_session(str(workspace), model="ollama/test-model")
    store.rename_session(session.session_id, "Accessible Session")
    monkeypatch.chdir(workspace)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("ASH_MODEL", "ollama/test-model")
    monkeypatch.setenv("ASH_SCREEN_READER_MODE", "true")

    status = main(
        [
            "--db-directory",
            str(db_dir),
            "--resume",
        ]
    )

    captured = capsys.readouterr()
    assert status == 1
    assert session.session_id in captured.out
    assert "Accessible Session" in captured.out
    assert "screen-reader mode" in captured.err.casefold()
    assert "--resume SESSION" in captured.err


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
