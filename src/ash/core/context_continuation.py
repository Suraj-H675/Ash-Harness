"""Conservative, user-opted-in reasoning-window continuation for one-shot coding.

This is a new agent turn in a fresh session, not provider-state compaction or
replay of interrupted tool calls. Uncertain effects fail closed at the durable
session boundary. The original user task is retained without model summaries.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from ash.context.history import ContextBudgetExceededError
from ash.core.checkpoints import recover_interrupted_turns
from ash.core.session import SessionForkConflictError, validate_context_restart_handoff

if TYPE_CHECKING:
    from ash.core.loop import AshLoop
    from ash.core.session import Session, SessionStore
    from ash.safety.guard import SafetyGuard


MAX_CONTEXT_CONTINUATIONS = 2
MAX_HANDOFF_TASK_BYTES = 8_192
_CONTINUATION_INSTRUCTION = (
    "Resume the original coding objective from the context-restart handoff. "
    "This is a fresh reasoning window, not a continuation of the earlier "
    "model's private reasoning. Inspect the actual workspace before any edit; "
    "partial edits may be present. Do not blindly repeat earlier actions "
    "or assume commands ran. Recheck the requested behavior and run permitted focused "
    "tests before claiming success. If safe verification is impossible, "
    "report the blocker."
)


@dataclass(frozen=True)
class ContinuationResult:
    response: str
    session_id: str
    context_continuations: int
    tool_free_finalized: bool = False
    tool_free_finalization_usage: dict[str, int] | None = None


def restart_context_with_handoff(
    store: SessionStore,
    guard: SafetyGuard,
    session_id: str,
    handoff: str,
) -> Session:
    """Recover locally and fork an explicit handoff without starting a runtime."""

    validate_context_restart_handoff(handoff)
    store.require_session_project(session_id, guard.ensure_project_root_current())
    lease = store.acquire_session_runtime_lease(session_id)
    try:
        for task in store.list_mcp_tasks(session_id):
            call = store.tool_call_for_recovery(
                session_id, task["turn_id"], task["call_id"]
            )
            if (
                task["status"] not in {"completed", "cancelled", "failed"}
                or call is None
                or not call["dispatched"]
                or not call["executed"]
                or call["success"] not in (0, 1)
            ):
                raise SessionForkConflictError(
                    "session has persisted MCP tasks without known local outcomes. "
                    "Resume their recovery before starting a fresh reasoning window."
                )
        recover_interrupted_turns(
            store, guard, session_id, include_quiescent_interrupted=True
        )
        return store.restart_context_session(
            session_id,
            expected_project_path=guard.ensure_project_root_current(),
            user_handoff=handoff,
        )
    finally:
        lease.close()


async def run_with_context_continuation(
    loop: AshLoop,
    original_task: str,
    *,
    max_continuations: int,
    allow_tool_free_finalization: bool = False,
) -> ContinuationResult:
    """Run once, then restart only on safe, opaque-state context overflow.

    The caller must use a new one-shot session without attachments. A restart
    is attempted only for a provider reasoning-state context failure, never
    for a provider outage, deadline, generic tool failure or token cap.
    """
    if not 0 <= max_continuations <= MAX_CONTEXT_CONTINUATIONS:
        raise ValueError("context continuations must be between 0 and 2")
    if max_continuations and (
        not original_task.strip()
        or len(original_task.encode("utf-8")) > MAX_HANDOFF_TASK_BYTES
    ):
        raise ValueError("automatic context continuation requires a bounded user task")
    if loop.current_session is None:
        raise RuntimeError("context continuation requires an active session")
    requested_task = original_task
    completed_restarts = 0
    while True:
        try:
            response = await loop.run_turn(requested_task)
        except ContextBudgetExceededError as error:
            if (
                not error.provider_replay_state_preserved
                or completed_restarts >= max_continuations
            ):
                raise
            parent = loop.current_session
            if parent is None:
                raise
            lease = loop.session_store.acquire_session_runtime_lease(
                parent.session_id
            )
            try:
                # run_turn has closed the failed-turn lease and persisted
                # its interrupted journal before reaching this boundary.
                await loop._recover_current_session_before_turn()
                recovery = recover_interrupted_turns(
                    loop.session_store,
                    loop.safety_guard,
                    parent.session_id,
                    include_quiescent_interrupted=True,
                )
                if recovery.needs_attention:
                    raise RuntimeError(
                        "automatic context continuation blocked by unknown "
                        "actions or unresolved files; inspect session recovery"
                    )
                handoff = (
                    "Original user coding task (preserved verbatim):\n"
                    + original_task
                    + "\n\nThe earlier reasoning window exhausted its "
                    "input budget. Inspect current workspace state before "
                    "continuing; no prior tool calls or approvals are "
                    "authorized for replay."
                )
                try:
                    child = loop.session_store.restart_context_session(
                        parent.session_id,
                        expected_project_path=loop.project_root,
                        user_handoff=handoff,
                        automatic=True,
                    )
                except SessionForkConflictError:
                    if not allow_tool_free_finalization:
                        raise
                    evidence = loop.session_store.tool_free_finalization_evidence(
                        parent.session_id, expected_project_path=loop.project_root
                    )
                    if (
                        evidence is None
                        or loop.session_store.verify_audit_log(parent.session_id)
                    ):
                        raise
                    from ash.core.tool_free_finalization import finalize_tool_free

                    source_turn_id, actions = evidence
                    report = await finalize_tool_free(
                        loop, original_task, source_turn_id, actions
                    )
                    return ContinuationResult(
                        response=report.response,
                        session_id=parent.session_id,
                        context_continuations=completed_restarts,
                        tool_free_finalized=True,
                        tool_free_finalization_usage=report.usage,
                    )
            finally:
                lease.close()
            await loop.start_session(child.session_id)
            requested_task = _CONTINUATION_INSTRUCTION
            completed_restarts += 1
        else:
            active = loop.current_session
            if active is None:
                raise RuntimeError("context continuation ended without a session")
            return ContinuationResult(
                response=response,
                session_id=active.session_id,
                context_continuations=completed_restarts,
            )
