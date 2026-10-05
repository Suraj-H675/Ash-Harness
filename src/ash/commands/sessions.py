"""Session listing helpers for the top-level CLI."""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from ash.core.redaction import redact_text
from ash.core.session import (
    SessionLineage,
    SessionSearchHit,
    SessionStore,
    SessionSummary,
)
from ash.ui.safe_text import terminal_safe_text


@dataclass(frozen=True)
class StartupSessionSelection:
    session_id: str | None
    cancelled: bool = False


def parse_session_retention_days(raw: str) -> int:
    """Parse the positive day count accepted by ``/sessions prune``."""

    if not raw.isdigit():
        raise ValueError("retention days must be a positive integer")
    try:
        retention_days = int(raw)
    except ValueError as exc:
        raise ValueError("retention days must be a positive integer") from exc
    if retention_days < 1:
        raise ValueError("retention days must be a positive integer")
    return retention_days


async def pick_session(
    store: SessionStore,
    *,
    project_path: str,
    initial_query: str = "",
    limit: int = 200,
    theme: str = "dark",
    no_color: bool = False,
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
        theme=theme,
        no_color=no_color,
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
    screen_reader_mode: bool = False,
    limited_terminal_mode: bool = False,
    picker: Callable[[], Awaitable[str | None]] | None = None,
    theme: str = "dark",
    no_color: bool = False,
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
            if screen_reader_mode:
                raise ValueError(
                    "--resume without a session uses a full-screen picker, which is "
                    "unavailable in screen-reader mode; run `ash sessions list`, then "
                    "use `ash --resume SESSION`"
                )
            if limited_terminal_mode:
                raise ValueError(
                    "--resume without a session uses a full-screen picker, which is "
                    "unavailable in limited terminal mode; run `ash sessions list`, "
                    "then use `ash --resume SESSION`"
                )
            if not interactive:
                raise ValueError(
                    "--resume without a session requires an interactive terminal"
                )
            selected = await (
                picker()
                if picker is not None
                else pick_session(
                    store,
                    project_path=project_path,
                    theme=theme,
                    no_color=no_color,
                )
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
        session_id = terminal_safe_text(session.session_id, single_line=True)
        title = terminal_safe_text(session.title or "(untitled)", single_line=True)
        model = terminal_safe_text(session.model or "unknown", single_line=True)
        project_path = terminal_safe_text(session.project_path, single_line=True)
        lines.append(
            f"{session_id}  {title}  {session.message_count} messages  "
            f"{model}  {session.updated_at.isoformat()}  {project_path}"
        )
    return "\n".join(lines)


def render_session_search_hits(
    hits: list[SessionSearchHit],
    *,
    json_output: bool = False,
) -> str:
    if json_output:
        return json.dumps(
            {"matches": [hit.model_dump(mode="json") for hit in hits]},
            sort_keys=True,
        )
    if not hits:
        return "No matching session messages."
    lines: list[str] = []
    for hit in hits:
        title = terminal_safe_text(hit.title or "(untitled)", single_line=True)
        excerpt = terminal_safe_text(redact_text(hit.excerpt), single_line=True)
        lines.append(
            f"{hit.session_id}  {title}  {hit.role}  "
            f"{hit.timestamp.isoformat()}  {excerpt}"
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
        session_id = terminal_safe_text(node.session_id, single_line=True)
        label = terminal_safe_text(
            node.branch_name or (
                "root" if node.parent_session_id is None else "branch"
            ),
            single_line=True,
        )
        lines.append(f"{'  ' * node.depth}{session_id}  {label}")
    return "\n".join(lines)


def render_recovery_reports(
    reports: list[dict[str, object]],
    *,
    json_output: bool = False,
) -> str:
    """Render persisted interrupted-turn recovery decisions for one session."""

    if json_output:
        return json.dumps({"reports": reports}, sort_keys=True)
    if not reports:
        return "No interrupted-turn recovery reports for this session."

    lines: list[str] = []
    for index, report in enumerate(reports, 1):
        turn_id = terminal_safe_text(
            str(report.get("turn_id", "unknown")),
            single_line=True,
        )
        status = terminal_safe_text(
            str(report.get("status", "interrupted")),
            single_line=True,
        )
        if index > 1:
            lines.append("")
        lines.append(f"Recovery {index}: turn {turn_id} — {status}")

        compensated = report.get("compensated_calls")
        if isinstance(compensated, list) and compensated:
            lines.append(f"  Compensated tool calls: {len(compensated)}")

        unknown = report.get("unknown_calls")
        if isinstance(unknown, list) and unknown:
            lines.append("  Unknown external outcomes:")
            for item in unknown:
                if not isinstance(item, dict):
                    continue
                tool = terminal_safe_text(
                    str(item.get("tool", "tool")),
                    single_line=True,
                )
                call_id = terminal_safe_text(
                    str(item.get("call_id", "")),
                    single_line=True,
                )
                lines.append(
                    f"    - {tool} ({call_id}): inspect the external system "
                    "before retrying."
                )

        unresolved = report.get("unresolved_files")
        if isinstance(unresolved, list) and unresolved:
            lines.append("  Files needing inspection:")
            for path in unresolved:
                lines.append(
                    "    - "
                    + terminal_safe_text(str(path), single_line=True)
                )

        recovered = report.get("recovered_calls")
        if isinstance(recovered, list) and recovered:
            lines.append("  Recovered tool decisions:")
            for item in recovered:
                if not isinstance(item, dict):
                    continue
                tool = terminal_safe_text(
                    str(item.get("tool", "tool")),
                    single_line=True,
                )
                ambiguous = bool(item.get("ambiguous"))
                outcome = "ambiguous" if ambiguous else "recorded"
                error = redact_text(str(item.get("error", "") or ""))
                suffix = (
                    ": " + terminal_safe_text(error, single_line=True)
                    if error
                    else ""
                )
                lines.append(f"    - {tool}: {outcome}{suffix}")

        if status == "needs_attention":
            lines.append(
                "  Next: inspect the items above before manually retrying any "
                "side effect."
            )
    return "\n".join(lines)
