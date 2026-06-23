"""Top-level persisted sprint plan inspection helpers."""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from typing import Any

from core.session import SessionStore
from core.sprint import ChecklistStatus


@dataclass(frozen=True)
class PlanSummary:
    sprint_id: str
    session_id: str
    goal: str
    state: str
    created_at: str
    total_items: int
    completed_items: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "sprint_id": self.sprint_id,
            "session_id": self.session_id,
            "goal": self.goal,
            "state": self.state,
            "created_at": self.created_at,
            "total_items": self.total_items,
            "completed_items": self.completed_items,
        }


def list_plans(
    store: SessionStore,
    *,
    project_path: str,
    all_projects: bool = False,
    limit: int = 20,
) -> list[PlanSummary]:
    if limit < 1:
        raise ValueError("limit must be positive")
    where = "" if all_projects else "WHERE sessions.project_path = ?"
    params: tuple[Any, ...] = (limit,) if all_projects else (project_path, limit)
    with closing(sqlite3.connect(store.db_path)) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            f"""
            SELECT
                sprints.sprint_id,
                sprints.session_id,
                sprints.goal,
                sprints.state,
                sprints.created_at,
                COUNT(checklist_items.idx) AS total_items,
                SUM(
                    CASE
                        WHEN checklist_items.status IN ('done', 'skipped') THEN 1
                        ELSE 0
                    END
                ) AS completed_items
            FROM sprints
            JOIN sessions ON sessions.session_id = sprints.session_id
            LEFT JOIN checklist_items
                ON checklist_items.sprint_id = sprints.sprint_id
            {where}
            GROUP BY sprints.sprint_id
            ORDER BY sprints.created_at DESC
            LIMIT ?
            """,
            params,
        ).fetchall()
    return [
        PlanSummary(
            sprint_id=row["sprint_id"],
            session_id=row["session_id"],
            goal=row["goal"],
            state=row["state"],
            created_at=row["created_at"],
            total_items=int(row["total_items"] or 0),
            completed_items=int(row["completed_items"] or 0),
        )
        for row in rows
    ]


def show_plan(store: SessionStore, sprint_id: str) -> dict[str, Any]:
    execution = store.load_sprint(sprint_id)
    return execution.to_dict()


def update_plan_item(
    store: SessionStore,
    sprint_id: str,
    item_idx: int,
    status: str,
    *,
    notes: str = "",
) -> dict[str, Any]:
    if item_idx < 1:
        raise ValueError("item index must be positive")
    execution = store.load_sprint(sprint_id)
    session_id = _plan_session_id(store, sprint_id)
    try:
        checklist_status = ChecklistStatus(status)
    except ValueError as exc:
        valid = ", ".join(item.value for item in ChecklistStatus)
        raise ValueError(f"invalid status {status!r}; expected one of: {valid}") from exc
    item = execution.set_item_status(item_idx, checklist_status, notes)
    store.save_sprint(session_id, execution)
    return item.to_dict()


def _plan_session_id(store: SessionStore, sprint_id: str) -> str:
    with closing(sqlite3.connect(store.db_path)) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT session_id FROM sprints WHERE sprint_id = ?",
            (sprint_id,),
        ).fetchone()
    if row is None:
        raise KeyError(f"Sprint not found: {sprint_id}")
    return str(row["session_id"])


def render_plan_summaries(
    plans: list[PlanSummary],
    *,
    json_output: bool = False,
) -> str:
    if json_output:
        return json.dumps(
            {"plans": [plan.to_dict() for plan in plans]},
            sort_keys=True,
        )
    if not plans:
        return "No persisted sprint plans."
    return "\n".join(
        f"{plan.sprint_id} [{plan.state}] {plan.completed_items}/{plan.total_items} "
        f"{plan.goal} ({plan.session_id})"
        for plan in plans
    )


def render_plan_detail(plan: dict[str, Any], *, json_output: bool = False) -> str:
    if json_output:
        return json.dumps({"plan": plan}, sort_keys=True)
    contract = plan["contract"]
    done = sum(
        1 for item in plan["items"] if item["status"] in {"done", "skipped"}
    )
    lines = [
        f"{contract['contract_id']} [{plan['state']}] {done}/{len(plan['items'])}",
        contract["goal"],
    ]
    for item in plan["items"]:
        lines.append(
            f"{item['idx']}. [{item['status']}] {item['section']}: "
            f"{item['description']}"
        )
    return "\n".join(lines)


def render_updated_plan_item(
    item: dict[str, Any],
    *,
    json_output: bool = False,
) -> str:
    if json_output:
        return json.dumps({"item": item}, sort_keys=True)
    return f"Updated item {item['idx']} to {item['status']}."
