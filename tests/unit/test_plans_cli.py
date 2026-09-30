from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from ash.cli import main
from ash.commands.plans import (
    list_plans,
    render_plan_detail,
    render_plan_summaries,
    render_updated_plan_item,
    show_plan,
    update_plan_item,
)
from ash.core.session import SessionStorageError, SessionStore
from ash.core.sprint import (
    ChecklistItem,
    ChecklistStatus,
    SprintContract,
    SprintExecution,
    SprintState,
)


def _save_plan(store: SessionStore, project: Path, goal: str) -> str:
    session = store.create_session(str(project))
    execution = SprintExecution(contract=SprintContract(goal=goal))
    execution.set_items(
        [
            ChecklistItem(idx=1, section="Work", description="first"),
            ChecklistItem(idx=2, section="Work", description="second"),
        ]
    )
    store.save_sprint(session.session_id, execution)
    return execution.contract.contract_id


def test_load_latest_active_sprint_ignores_terminal_history(tmp_path: Path) -> None:
    store = SessionStore(tmp_path / "sessions.db")
    session = store.create_session(str(tmp_path))
    base = datetime(2026, 9, 30, tzinfo=timezone.utc)

    older_active = SprintExecution(
        contract=SprintContract(goal="older active"),
        state=SprintState.ACTIVE,
        created_at=base,
        started_at=base,
    )
    newest_nonterminal = SprintExecution(
        contract=SprintContract(goal="newest planning"),
        created_at=base + timedelta(seconds=1),
    )
    newer_terminal = SprintExecution(
        contract=SprintContract(goal="newer complete"),
        state=SprintState.COMPLETE,
        created_at=base + timedelta(seconds=2),
        started_at=base,
        completed_at=base + timedelta(seconds=2),
    )
    for execution in (older_active, newest_nonterminal, newer_terminal):
        store.save_sprint(session.session_id, execution)

    loaded = store.load_latest_active_sprint(session.session_id)

    assert loaded is not None
    assert loaded.contract.contract_id == newest_nonterminal.contract.contract_id
    assert loaded.state is SprintState.PLANNING

    newest_nonterminal.abort("done")
    older_active.complete()
    store.save_sprint(session.session_id, newest_nonterminal)
    store.save_sprint(session.session_id, older_active)
    assert store.load_latest_active_sprint(session.session_id) is None


def test_plan_summary_renderer_emits_json(tmp_path: Path) -> None:
    store = SessionStore(tmp_path / "sessions.db")
    sprint_id = _save_plan(store, tmp_path, "ship feature")

    plans = list_plans(store, project_path=str(tmp_path))
    payload = json.loads(render_plan_summaries(plans, json_output=True))

    assert payload["plans"][0]["sprint_id"] == sprint_id
    assert payload["plans"][0]["goal"] == "ship feature"
    assert payload["plans"][0]["total_items"] == 2
    assert payload["plans"][0]["completed_items"] == 0


def test_plan_list_limit_is_bounded(tmp_path: Path) -> None:
    store = SessionStore(tmp_path / "sessions.db")

    with pytest.raises(ValueError, match="limit must be between 1 and 1000"):
        list_plans(store, project_path=str(tmp_path), limit=1001)


def test_plan_list_rejects_session_database_link_swap(tmp_path: Path) -> None:
    database = tmp_path / "sessions.db"
    outside_database = tmp_path / "outside.db"
    store = SessionStore(database)
    outside = SessionStore(outside_database)
    _save_plan(outside, tmp_path / "outside", "outside-secret-plan")
    database.unlink()
    try:
        database.symlink_to(outside_database)
    except OSError as exc:
        pytest.skip(f"symlinks are unavailable: {exc}")

    with pytest.raises(SessionStorageError, match="symlink or junction"):
        list_plans(store, project_path=str(tmp_path), all_projects=True)


def test_plan_show_and_update_renderers_emit_json(tmp_path: Path) -> None:
    store = SessionStore(tmp_path / "sessions.db")
    sprint_id = _save_plan(store, tmp_path, "ship feature")

    item = update_plan_item(
        store,
        sprint_id,
        1,
        ChecklistStatus.DONE.value,
        notes="verified",
    )
    update_payload = json.loads(render_updated_plan_item(item, json_output=True))
    detail_payload = json.loads(
        render_plan_detail(show_plan(store, sprint_id), json_output=True)
    )

    assert update_payload["item"]["status"] == "done"
    assert detail_payload["plan"]["items"][0]["notes"] == "verified"


def test_plans_cli_lists_current_project_plans(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    db_dir = tmp_path / "db"
    store = SessionStore(db_dir / "sessions.db")
    current = _save_plan(store, tmp_path, "current")
    other = _save_plan(store, tmp_path / "other", "other")
    monkeypatch.chdir(tmp_path)

    assert main(["--db-directory", str(db_dir), "plans", "list", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)

    ids = {plan["sprint_id"] for plan in payload["plans"]}
    assert ids == {current}
    assert other not in ids


def test_plans_cli_shows_and_updates_plan_items(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    db_dir = tmp_path / "db"
    store = SessionStore(db_dir / "sessions.db")
    sprint_id = _save_plan(store, tmp_path, "current")
    monkeypatch.chdir(tmp_path)

    assert (
        main(
            [
                "--db-directory",
                str(db_dir),
                "plans",
                "update",
                sprint_id,
                "2",
                "in_progress",
                "--notes",
                "started",
                "--json",
            ]
        )
        == 0
    )
    update_payload = json.loads(capsys.readouterr().out)
    assert update_payload["item"]["status"] == "in_progress"

    assert (
        main(["--db-directory", str(db_dir), "plans", "show", sprint_id, "--json"]) == 0
    )
    show_payload = json.loads(capsys.readouterr().out)
    assert show_payload["plan"]["items"][1]["notes"] == "started"


def test_plans_cli_rejects_invalid_limit_and_item(
    tmp_path: Path,
    capsys,
) -> None:
    db_dir = tmp_path / "db"
    store = SessionStore(db_dir / "sessions.db")
    sprint_id = _save_plan(store, tmp_path, "current")

    assert main(["--db-directory", str(db_dir), "plans", "list", "--limit", "0"]) == 2
    assert "limit must be between 1 and 1000" in capsys.readouterr().err

    assert (
        main(
            ["--db-directory", str(db_dir), "plans", "list", "--limit", "1001"]
        )
        == 2
    )
    assert "limit must be between 1 and 1000" in capsys.readouterr().err

    assert (
        main(
            [
                "--db-directory",
                str(db_dir),
                "plans",
                "update",
                sprint_id,
                "99",
                "done",
            ]
        )
        == 2
    )
    assert "No checklist item" in capsys.readouterr().err
