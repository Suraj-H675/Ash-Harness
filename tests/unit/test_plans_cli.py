from __future__ import annotations

import json
from pathlib import Path

from ash.cli import main
from cli.plans import (
    list_plans,
    render_plan_detail,
    render_plan_summaries,
    render_updated_plan_item,
    show_plan,
    update_plan_item,
)
from core.session import SessionStore
from core.sprint import ChecklistItem, ChecklistStatus, SprintContract, SprintExecution


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


def test_plan_summary_renderer_emits_json(tmp_path: Path) -> None:
    store = SessionStore(tmp_path / "sessions.db")
    sprint_id = _save_plan(store, tmp_path, "ship feature")

    plans = list_plans(store, project_path=str(tmp_path))
    payload = json.loads(render_plan_summaries(plans, json_output=True))

    assert payload["plans"][0]["sprint_id"] == sprint_id
    assert payload["plans"][0]["goal"] == "ship feature"
    assert payload["plans"][0]["total_items"] == 2
    assert payload["plans"][0]["completed_items"] == 0


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
    assert "limit must be positive" in capsys.readouterr().err

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
