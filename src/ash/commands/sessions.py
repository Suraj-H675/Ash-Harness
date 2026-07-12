"""Session listing helpers for the top-level CLI."""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from ash.core.session import SessionLineage, SessionStore, SessionSummary


@dataclass(frozen=True)
class StartupSessionSelection:
    session_id: str | None
    cancelled: bool = False


async def pick_session(
    store: SessionStore,
    *,
    project_path: str,
    initial_query: str = "",
    limit: int = 200,
) -> str | None:
    """Open the metadata-only session picker for one project."""

    from ash.ui.session_picker import SessionPicker

    sessions = store.list_sessions(project_path=project_path, limit=limit)
    if not sessions:
        raise ValueError("no sessions found in this project")
    return await SessionPicker(
        sessions,
        load_session=store.load_session,
        initial_query=initial_query,
    ).run()


async def select_startup_session(
    store: SessionStore,
    *,
    project_path: str,
    continue_session: bool = False,
    resume: str | None = None,
    legacy_session_id: str | None = None,
    fork_session: bool = False,
    interactive: bool = False,
    picker: Callable[[], Awaitable[str | None]] | None = None,
) -> StartupSessionSelection:
    """Resolve startup continuation flags before provider initialization."""

    session_id = legacy_session_id
    if continue_session:
        latest = store.latest_session(project_path)
        if latest is None:
            raise ValueError("no session found to continue in this project")
        session_id = latest.session_id
    elif resume is not None:
        if resume:
            session_id = store.resolve_session(resume, project_path).session_id
        else:
            if not interactive:
                raise ValueError(
                    "--resume without a session requires an interactive terminal"
                )
            selected = await (
                picker()
                if picker is not None
                else pick_session(store, project_path=project_path)
            )
            if selected is None:
                return StartupSessionSelection(None, cancelled=True)
            session_id = selected

    if fork_session:
        if session_id is None:
            raise ValueError(
                "--fork-session requires --continue, --resume, or --session"
            )
        session_id = store.fork_session(session_id).session_id
    return StartupSessionSelection(session_id)


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


def render_session_tree(
    tree: list[SessionLineage],
    *,
    json_output: bool = False,
) -> str:
    if json_output:
        return json.dumps(
            {"sessions": [node.model_dump(mode="json") for node in tree]},
            sort_keys=True,
        )
    lines: list[str] = []
    for node in tree:
        label = node.branch_name or (
            "root" if node.parent_session_id is None else "branch"
        )
        lines.append(f"{'  ' * node.depth}{node.session_id}  {label}")
    return "\n".join(lines)
