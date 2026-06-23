"""Session listing helpers for the top-level CLI."""

from __future__ import annotations

import json

from core.session import SessionStore, SessionSummary


def list_session_summaries(
    store: SessionStore,
    *,
    project_path: str,
    all_projects: bool = False,
    limit: int = 20,
    query: str = "",
) -> list[SessionSummary]:
    return store.list_sessions(
        project_path=None if all_projects else project_path,
        limit=limit,
        query=query,
    )


def render_session_summaries(
    sessions: list[SessionSummary],
    *,
    json_output: bool = False,
) -> str:
    if json_output:
        return json.dumps(
            {"sessions": [session.model_dump(mode="json") for session in sessions]},
            sort_keys=True,
        )
    if not sessions:
        return "No matching sessions."
    lines: list[str] = []
    for session in sessions:
        title = session.title or "(untitled)"
        model = session.model or "unknown"
        lines.append(
            f"{session.session_id}  {title}  {session.message_count} messages  "
            f"{model}  {session.updated_at.isoformat()}  {session.project_path}"
        )
    return "\n".join(lines)
