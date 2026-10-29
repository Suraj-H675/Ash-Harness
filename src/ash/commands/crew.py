"""Native command path for explicit, read-only coworker turns."""

from __future__ import annotations

import argparse
import asyncio
import difflib
import os
import sys
from pathlib import Path
from uuid import UUID, uuid4, uuid5

from ash.automation.schedules import build_schedule, render_schedule
from ash.crew_events import MAX_CREW_EVENT_BYTES
from ash.core.redaction import redact_known_secrets
from ash.coworker_evidence import (
    read_explicit_evidence,
    serialize_evidence,
)
from ash.core.session import SessionStorageError
from ash.coworkers import (
    Coworker,
    CoworkerDirectory,
    CoworkerDirectoryError,
    CoworkerFileApproval,
    CoworkerHandle,
    CoworkerNotFound,
    CoworkerRuntimeUnavailable,
    CoworkerModelTurnResult,
    CoworkerResponsibility,
    CoworkerResponsibilityRun,
    CoworkerResponsibilityStatus,
    CoworkerTurnRequest,
)
from ash.exceptions import AshError
from ash.safety.guard import SafetyGuard, SafetyViolation
from ash.safety.scoped_io import ScopedIOError
from ash.ui.safe_text import terminal_safe_text


_MAX_HISTORY_LIMIT = 100
def run_crew_command(args: argparse.Namespace) -> int:
    if not _current_uid_available():
        _write_error("Blocked. The current local owner UID cannot be verified.")
        return 2
    action = args.crew_action
    if action == "create":
        return _create(args)
    if action == "list":
        return _list(args)
    if action == "ask":
        return _ask(args)
    if action == "history":
        return _history(args)
    if action == "responsibility":
        return _responsibility(args)
    if action == "event":
        return _event(args)
    if action == "tick":
        return _tick(args)
    if action == "propose":
        return _propose(args)
    if action == "approvals":
        return _approvals(args)
    if action == "approval" and args.crew_approval_action == "show":
        return _approval_show(args)
    if action in {"approve", "deny", "revoke", "apply"}:
        return _approval_decision(args, action)
    _write_error("Unsupported Crew command.")
    return 2


def _create(args: argparse.Namespace) -> int:
    try:
        identity = CoworkerDirectory.open().create(
            owner_id=args.owner,
            display_name=args.name,
            profile_name=args.crew_profile,
            workspace=args.workspace,
        )
    except (
        CoworkerDirectoryError,
        CoworkerNotFound,
        CoworkerRuntimeUnavailable,
        AshError,
        OSError,
        SafetyViolation,
        SessionStorageError,
        ValueError,
    ) as exc:
        _write_error(f"Coworker could not be created: {_safe_error(exc)}")
        return 2
    except Exception as exc:  # noqa: BLE001 - keep CLI failures secret-free
        _write_error(f"Coworker could not be created ({type(exc).__name__}).")
        return 2

    _write_line("Coworker ready.")
    _write_identity(identity)
    return 0


def _list(args: argparse.Namespace) -> int:
    ash_dir = Path.home() / ".ash"
    if not ash_dir.exists() and not ash_dir.is_symlink():
        _write_line("No coworkers found for this owner label.")
        return 0
    try:
        coworkers = CoworkerDirectory.open().list_for_local_owner(
            owner_id=args.owner
        )
    except (
        CoworkerDirectoryError,
        CoworkerRuntimeUnavailable,
        OSError,
        SafetyViolation,
        SessionStorageError,
        ValueError,
    ) as exc:
        _write_error(f"Coworker list is unavailable: {_safe_error(exc)}")
        return 2
    except Exception as exc:  # noqa: BLE001 - keep CLI failures secret-free
        _write_error(f"Coworker list is unavailable ({type(exc).__name__}).")
        return 2
    if not coworkers:
        _write_line("No coworkers found for this owner label.")
        return 0
    for identity in coworkers:
        _write_identity(identity)
    return 0


def _ask(args: argparse.Namespace) -> int:
    turn_id = args.turn_id or str(uuid4())
    _write_line(f"Turn ID: {_safe_text(turn_id, single_line=True)}")
    try:
        handle = CoworkerDirectory.open().open_coworker(
            owner_id=args.owner,
            coworker_id=args.coworker_id,
        )
        identity = handle.require_local_owner()
        workspace_guard = SafetyGuard(identity.workspace)
        workspace_identity = workspace_guard.project_root_identity
        if workspace_identity is None:
            raise ValueError("workspace identity cannot be bound for this turn")
        evidence_files = read_explicit_evidence(
            identity.workspace,
            tuple(args.evidence),
            guard=workspace_guard,
        )
        evidence = serialize_evidence(evidence_files)
        request = CoworkerTurnRequest(
            turn_id=turn_id,
            question=args.question,
            evidence=evidence,
            workspace_identity=workspace_identity,
        )
    except (
        CoworkerDirectoryError,
        CoworkerNotFound,
        CoworkerRuntimeUnavailable,
        AshError,
        OSError,
        SafetyViolation,
        SessionStorageError,
        ScopedIOError,
        ValueError,
    ) as exc:
        _write_error(f"Blocked before model dispatch: {_safe_error(exc)}")
        return 2
    except Exception as exc:  # noqa: BLE001 - no pre-dispatch details are needed
        _write_error(
            "Blocked before model dispatch "
            f"({type(exc).__name__}); no provider request was sent."
        )
        return 2

    for item in evidence_files:
        _write_line(
            "Evidence: "
            f"{_safe_text(item.relative_path, single_line=True)} "
            f"({item.size_bytes} bytes, SHA-256 {item.sha256})"
        )
    _write_line(
        f"Coworker ID: {_safe_text(identity.coworker_id, single_line=True)}"
    )
    _write_line("Status: attempting one read-only model turn.")
    try:
        result = asyncio.run(handle.run_readonly_turn(request))
    except CoworkerRuntimeUnavailable as exc:
        message = _safe_error(exc)
        if "configured profile provider is unavailable" in str(exc):
            _write_error(f"Blocked. {message}. No provider request was sent.")
        elif "already used" in str(exc) or "will not be replayed" in str(exc):
            _write_error(
                f"Turn is non-replayable. {message}. Inspect `ash crew history` before another ask."
            )
        else:
            _write_error(
                f"Turn failed or its outcome is unknown. {message}. "
                "Do not reuse this turn ID; inspect `ash crew history`."
            )
        return 2
    except ValueError:
        _write_error(
            "Turn failed or its outcome is unknown (ValueError). "
            "Do not reuse this turn ID; inspect `ash crew history`."
        )
        return 2
    except KeyboardInterrupt:
        _write_error(
            "Turn interrupted. Its outcome may be unknown. Do not reuse this turn ID; "
            "inspect `ash crew history`."
        )
        return 130
    except Exception as exc:  # noqa: BLE001 - never print provider or filesystem details
        _write_error(
            "Turn failed or its outcome is unknown "
            f"({type(exc).__name__}). Do not reuse this turn ID; inspect `ash crew history`."
        )
        return 2

    _write_line("Status: completed.")
    _write_line(f"Turn ID: {_safe_text(result.turn_id, single_line=True)}")
    _write_line(f"Run ID: {result.session_ref.run_id}")
    _write_line(f"Session ID: {result.session_ref.session_id}")
    _write_line(f"Request ID: {uuid5(UUID(result.session_ref.run_id), 'request')}")
    _write_line(f"Model: {_safe_text(result.model, single_line=True)}")
    _write_line("Response:")
    _write_line(_safe_text(result.response))
    _write_line("Proposal only. No files were changed.")
    return 0


def _history(args: argparse.Namespace) -> int:
    try:
        handle = CoworkerDirectory.open().open_coworker(
            owner_id=args.owner,
            coworker_id=args.coworker_id,
        )
        handle.require_local_owner()
        entries = handle.list_readonly_turn_history(limit=args.limit)
    except (
        CoworkerDirectoryError,
        CoworkerNotFound,
        CoworkerRuntimeUnavailable,
        OSError,
        SafetyViolation,
        SessionStorageError,
        ValueError,
    ) as exc:
        _write_error(f"Coworker history is unavailable: {_safe_error(exc)}")
        return 2
    except Exception as exc:  # noqa: BLE001 - keep CLI failures secret-free
        _write_error(f"Coworker history is unavailable ({type(exc).__name__}).")
        return 2
    if not entries:
        _write_line("No model turns recorded for this coworker.")
        return 0

    for entry in entries:
        _write_line(
            f"Status: {entry.status}"
            if entry.status == "completed"
            else "Status: outcome unknown; claimed turns cannot be replayed."
        )
        _write_line(f"Run ID: {entry.session_ref.run_id}")
        _write_line(f"Session ID: {entry.session_ref.session_id}")
        _write_line(f"Created: {_safe_text(entry.created_at, single_line=True)}")
        if entry.completed_at is not None:
            _write_line(
                f"Completed: {_safe_text(entry.completed_at, single_line=True)}"
            )
        if entry.model is not None:
            _write_line(f"Model: {_safe_text(entry.model, single_line=True)}")
        if entry.response is not None:
            _write_line("Response:")
            _write_line(_safe_text(entry.response))
            _write_line("Proposal only. No files were changed.")
        _write_line("")
    return 0


def _responsibility(args: argparse.Namespace) -> int:
    action = args.crew_responsibility_action
    try:
        handle = _open_coworker_handle(args)
        if action == "create":
            schedule = None
            if any(value is not None for value in (args.at, args.every, args.cron)):
                schedule = build_schedule(
                    at=args.at,
                    every=args.every,
                    cron=args.cron,
                    timezone_name=args.timezone,
                )
            responsibility = handle.create_responsibility(
                responsibility_id=args.responsibility_id,
                scope=args.scope,
                objective=args.objective,
                success_criteria=args.success_criterion,
                evidence_paths=tuple(args.evidence),
                schedule=schedule,
                misfire_grace_seconds=args.misfire_grace_seconds,
            )
            _write_line("Responsibility created.")
            _render_responsibility(responsibility)
            return 0
        if action == "list":
            responsibilities = handle.list_responsibilities()
            if not responsibilities:
                _write_line("No responsibilities found for this coworker.")
                return 0
            for responsibility in responsibilities:
                _render_responsibility(responsibility)
            return 0
        if action in {"pause", "resume", "complete"}:
            status_by_action: dict[str, CoworkerResponsibilityStatus] = {
                "pause": "paused",
                "resume": "active",
                "complete": "completed",
            }
            status = status_by_action[action]
            responsibility = handle.set_responsibility_status(
                args.responsibility_id,
                status,
            )
            _write_line(f"Responsibility {status}.")
            _render_responsibility(responsibility)
            return 0
        if action == "run":
            result = asyncio.run(
                handle.run_model_responsibility(
                    args.responsibility_id,
                    event_id=args.event_id,
                )
            )
            _render_model_turn(result)
            return 0
        if action == "runs":
            runs = handle.list_responsibility_runs(
                args.responsibility_id,
                limit=args.limit,
            )
            if not runs:
                _write_line("No runs recorded for this responsibility.")
                return 0
            for run in runs:
                _render_responsibility_run(handle, run)
            return 0
        _write_error("Unsupported responsibility command.")
        return 2
    except CoworkerRuntimeUnavailable as exc:
        _write_model_admission_error(exc, scheduled=False)
        return 2
    except (
        CoworkerDirectoryError,
        CoworkerNotFound,
        AshError,
        OSError,
        SafetyViolation,
        SessionStorageError,
        ValueError,
    ) as exc:
        if action == "run" and any(
            marker in str(exc).casefold()
            for marker in ("evidence", "prompt exceeds")
        ):
            _write_error(f"Blocked before provider dispatch: {_safe_error(exc)}")
        elif action == "run":
            _write_error(
                "Responsibility turn failed or its outcome may be unknown. "
                "Inspect responsibility runs before retrying: "
                f"{_safe_error(exc)}"
            )
        else:
            _write_error(f"Responsibility command failed: {_safe_error(exc)}")
        return 2
    except KeyboardInterrupt:
        _write_error(
            "Responsibility turn interrupted. Its outcome may be unknown. "
            "Inspect `ash crew responsibility runs` before retrying."
        )
        return 130
    except Exception as exc:
        if action == "run":
            _write_error(
                "Responsibility turn failed or its outcome may be unknown "
                f"({type(exc).__name__}). Inspect responsibility runs before retrying."
            )
        else:
            _write_error(f"Responsibility command failed ({type(exc).__name__}).")
        return 2


def _event(args: argparse.Namespace) -> int:
    try:
        handle = _open_coworker_handle(args)
        envelope = _read_event_envelope(args.input)
        result = asyncio.run(
            handle.run_signed_event(
                envelope,
                key_file=args.key_file,
                responsibility_id=args.responsibility_id,
            )
        )
        _render_model_turn(result)
        return 0
    except CoworkerRuntimeUnavailable as exc:
        _write_model_admission_error(exc, scheduled=False)
        return 2
    except (CoworkerNotFound, ValueError) as exc:
        _write_error(f"Blocked before model dispatch: {_safe_error(exc)}")
        return 2
    except (
        CoworkerDirectoryError,
        AshError,
        OSError,
        SafetyViolation,
        SessionStorageError,
    ) as exc:
        if "evidence" in str(exc).casefold():
            _write_error(f"Signed event blocked before provider dispatch: {_safe_error(exc)}")
        else:
            _write_error(
                "Signed event blocked or failed; its provider outcome may be unknown. "
                "Inspect responsibility runs before retrying: "
                f"{_safe_error(exc)}"
            )
        return 2
    except KeyboardInterrupt:
        _write_error(
            "Signed event turn interrupted. Its outcome may be unknown. "
            "Inspect `ash crew responsibility runs` before retrying."
        )
        return 130
    except Exception as exc:
        _write_error(
            "Signed event turn failed or its outcome is unknown "
            f"({type(exc).__name__}). Inspect responsibility runs before retrying."
        )
        return 2


def _tick(args: argparse.Namespace) -> int:
    try:
        handle = _open_coworker_handle(args)
        result = asyncio.run(handle.run_due_model_responsibilities_once())
        if result.status == "idle":
            _write_line("No due scheduled responsibility.")
            return 0
        if result.status == "skipped":
            _write_line("Status: skipped after the configured misfire grace.")
            _write_line(f"Responsibility ID: {result.responsibility_id}")
            _write_line(f"Event key: {_safe_text(result.event_key or '', single_line=True)}")
            return 0
        if result.turn is None:
            raise CoworkerDirectoryError("completed model schedule tick has no turn")
        _render_model_turn(result.turn)
        return 0
    except CoworkerRuntimeUnavailable as exc:
        _write_model_admission_error(exc, scheduled=True)
        return 2
    except (CoworkerNotFound, ValueError) as exc:
        _write_error(f"Scheduled turn blocked before provider dispatch: {_safe_error(exc)}")
        return 2
    except (
        CoworkerDirectoryError,
        AshError,
        OSError,
        SafetyViolation,
        SessionStorageError,
    ) as exc:
        if "evidence" in str(exc).casefold():
            _write_error(
                "Scheduled turn blocked before provider dispatch. "
                "The occurrence remains due: "
                f"{_safe_error(exc)}"
            )
        else:
            _write_error(
                "Scheduled turn failed or its outcome may be unknown. "
                "Inspect responsibility runs before retrying: "
                f"{_safe_error(exc)}"
            )
        return 2
    except KeyboardInterrupt:
        _write_error(
            "Scheduled turn interrupted. Its outcome may be unknown. "
            "Inspect `ash crew responsibility runs` before retrying."
        )
        return 130
    except Exception as exc:
        _write_error(
            "Scheduled turn failed or its outcome is unknown "
            f"({type(exc).__name__}). Inspect responsibility runs before retrying."
        )
        return 2


def _read_event_envelope(source: str) -> bytes:
    if source == "-":
        content = sys.stdin.buffer.read(MAX_CREW_EVENT_BYTES + 1)
    else:
        with Path(source).open("rb") as stream:
            content = stream.read(MAX_CREW_EVENT_BYTES + 1)
    if len(content) > MAX_CREW_EVENT_BYTES:
        raise ValueError("signed event exceeds the 16 KiB input limit")
    return content


def _write_model_admission_error(
    exc: CoworkerRuntimeUnavailable,
    *,
    scheduled: bool,
) -> None:
    detail = _safe_error(exc)
    if "configured profile provider is unavailable" in str(exc):
        timing = (
            "The scheduled occurrence remains due. "
            if scheduled
            else "No provider request was sent. "
        )
        _write_error(
            "Blocked. The selected profile provider is unavailable. "
            + timing
            + detail
        )
    elif (
        "will not be replayed" in str(exc)
        or "already claimed" in str(exc)
        or "already used" in str(exc)
    ):
        _write_error(
            "This run is non-replayable. Its provider outcome may be unknown. "
            "Inspect `ash crew responsibility runs`. "
            + detail
        )
    elif any(
        marker in str(exc)
        for marker in (
            "not active",
            "target does not match",
            "owner identity could not be verified",
            "workspace identity cannot be bound",
            "responsibility evidence",
            "binding changed before claim",
            "prompt changed before claim",
        )
    ):
        _write_error(f"Blocked before provider dispatch: {detail}")
    else:
        _write_error(
            "Responsibility turn failed or its provider outcome may be unknown. "
            "Inspect responsibility runs before retrying: "
            f"{detail}"
        )


def _open_coworker_handle(args: argparse.Namespace) -> CoworkerHandle:
    handle = CoworkerDirectory.open().open_coworker(
        owner_id=args.owner,
        coworker_id=args.coworker_id,
    )
    handle.require_local_owner()
    return handle


def _render_responsibility(responsibility: CoworkerResponsibility) -> None:
    _write_line(f"Responsibility ID: {responsibility.responsibility_id}")
    _write_line(f"Coworker ID: {responsibility.coworker_id}")
    _write_line(f"Status: {responsibility.status}")
    _write_line(f"Scope: {_safe_text(responsibility.scope, single_line=True)}")
    _write_line(f"Objective: {_safe_text(responsibility.objective, single_line=True)}")
    for criterion in responsibility.success_criteria:
        _write_line(f"Success criterion: {_safe_text(criterion, single_line=True)}")
    if responsibility.evidence_paths:
        for path in responsibility.evidence_paths:
            _write_line(f"Evidence grant: {_safe_text(path, single_line=True)}")
    else:
        _write_line("Evidence grant: none")
    if responsibility.schedule is not None:
        _write_line(f"Schedule: {render_schedule(responsibility.schedule)}")
        if responsibility.next_fire_at is not None:
            _write_line(f"Next fire: {responsibility.next_fire_at.isoformat()}")


def _render_model_turn(result: CoworkerModelTurnResult) -> None:
    _write_line("Status: completed.")
    if result.responsibility_id is not None:
        _write_line(f"Responsibility ID: {result.responsibility_id}")
    if result.trigger_type is not None:
        _write_line(f"Trigger: {result.trigger_type}")
    if result.event_key is not None:
        _write_line(f"Event key: {_safe_text(result.event_key, single_line=True)}")
    if result.responsibility_id is not None:
        if result.evidence_provenance:
            for evidence in result.evidence_provenance:
                _write_line(
                    "Evidence used: "
                    f"{_safe_text(evidence.relative_path, single_line=True)} "
                    f"({evidence.size_bytes} bytes, SHA-256 {evidence.sha256})"
                )
        else:
            _write_line("Evidence used: none granted")
    _write_line(f"Run ID: {result.session_ref.run_id}")
    _write_line(f"Session ID: {result.session_ref.session_id}")
    if result.request_id is not None:
        _write_line(f"Request ID: {result.request_id}")
    _write_line(f"Model: {_safe_text(result.model, single_line=True)}")
    _write_line("Response:")
    _write_line(_safe_text(result.response))
    _write_line("Read-only turn. No files were changed.")


def _render_responsibility_run(
    handle: CoworkerHandle,
    run: CoworkerResponsibilityRun,
) -> None:
    _write_line(f"Responsibility ID: {run.responsibility_id}")
    _write_line(f"Status: {run.status}")
    _write_line(f"Trigger: {run.trigger_type}")
    _write_line(f"Execution: {run.execution_kind}")
    _write_line(f"Event key: {_safe_text(run.event_key, single_line=True)}")
    _write_line(f"Run ID: {run.run_id}")
    if run.session_id is not None:
        _write_line(f"Session ID: {run.session_id}")
    if run.request_id is not None:
        _write_line(f"Request ID: {run.request_id}")
    if run.scheduled_for is not None:
        _write_line(f"Scheduled for: {run.scheduled_for.isoformat()}")
    if run.skip_reason is not None:
        _write_line(f"Skip reason: {run.skip_reason}")
    if run.status == "claimed":
        _write_line("Outcome may be unknown. This claim cannot be replayed.")
    if run.execution_kind == "model" and run.status == "completed" and run.session_id:
        session, _ = handle.load_session(run.session_id)
        _write_line(f"Model: {_safe_text(session.model, single_line=True)}")
        if session.messages and session.messages[-1].role == "assistant":
            _write_line("Response:")
            _write_line(_safe_text(session.messages[-1].content))
    _write_line("")


def _open_approval_handle(args: argparse.Namespace) -> CoworkerHandle:
    handle = CoworkerDirectory.open().open_coworker(
        owner_id=args.owner,
        coworker_id=args.coworker_id,
    )
    handle.require_local_owner()
    return handle


def _propose(args: argparse.Namespace) -> int:
    try:
        handle = _open_approval_handle(args)
        approval = handle.prepare_file_approval(
            run_id=args.run_id,
            session_id=args.session_id,
            request_id=args.request_id,
            target_path=args.target,
            replacement_file_path=args.replacement_file,
        )
    except (
        CoworkerDirectoryError,
        CoworkerNotFound,
        CoworkerRuntimeUnavailable,
        OSError,
        SafetyViolation,
        SessionStorageError,
        ScopedIOError,
        ValueError,
    ) as exc:
        _write_error(f"File correction could not be staged: {_safe_error(exc)}")
        return 2
    except Exception as exc:
        _write_error(f"File correction could not be staged ({type(exc).__name__}).")
        return 2
    _render_file_approval(approval)
    _write_line("Status: pending explicit local owner approval; no file was changed.")
    return 0


def _approvals(args: argparse.Namespace) -> int:
    try:
        handle = _open_approval_handle(args)
        entries = handle.list_file_approvals(limit=args.limit)
    except (
        CoworkerDirectoryError,
        CoworkerNotFound,
        CoworkerRuntimeUnavailable,
        OSError,
        ValueError,
    ) as exc:
        _write_error(f"File approval list is unavailable: {_safe_error(exc)}")
        return 2
    except Exception as exc:
        _write_error(f"File approval list is unavailable ({type(exc).__name__}).")
        return 2
    if not entries:
        _write_line("No file approvals found for this coworker.")
        return 0
    for approval in entries:
        _write_line(
            f"{approval.status}: {approval.approval_id} "
            f"{_safe_text(approval.action, single_line=True)} "
            f"{_safe_text(approval.target_path, single_line=True)}"
        )
        _write_line(f"  Approval digest: {approval.approval_digest}")
        _write_line(f"  Expected prior SHA-256: {approval.expected_sha256}")
        _write_line(f"  Replacement SHA-256: {approval.replacement_sha256}")
    return 0


def _approval_show(args: argparse.Namespace) -> int:
    try:
        handle = _open_approval_handle(args)
        approval, events = handle.inspect_file_approval(args.approval_id)
    except (
        CoworkerDirectoryError,
        CoworkerNotFound,
        CoworkerRuntimeUnavailable,
        OSError,
        SafetyViolation,
        SessionStorageError,
        ScopedIOError,
        ValueError,
    ) as exc:
        _write_error(f"File approval could not be inspected: {_safe_error(exc)}")
        return 2
    except Exception as exc:
        _write_error(f"File approval could not be inspected ({type(exc).__name__}).")
        return 2
    _render_file_approval(approval)
    _write_line("History:")
    for event in events:
        actor = (
            f" UID {event.actor_uid}"
            if event.actor_uid is not None
            else ""
        )
        _write_line(
            f"  {event.occurred_at} {event.status}{actor} "
            f"({_safe_text(event.detail, single_line=True)})"
        )
    return 0


def _approval_decision(args: argparse.Namespace, action: str) -> int:
    try:
        handle = _open_approval_handle(args)
        approval, _ = handle.inspect_file_approval(args.approval_id)
    except (
        CoworkerDirectoryError,
        CoworkerNotFound,
        CoworkerRuntimeUnavailable,
        OSError,
        SafetyViolation,
        SessionStorageError,
        ScopedIOError,
        ValueError,
    ) as exc:
        _write_error(f"File approval is unavailable: {_safe_error(exc)}")
        return 2
    except Exception as exc:
        _write_error(f"File approval is unavailable ({type(exc).__name__}).")
        return 2
    _render_file_approval(approval)
    expected = f"{action.upper()} {approval.approval_id} {approval.approval_digest}"
    if not _require_tty_confirmation(expected):
        _write_error(
            "Blocked. This decision requires an interactive local TTY and the exact challenge."
        )
        return 2
    try:
        if action == "approve":
            result = handle._approve_file_approval(approval.approval_id)
        elif action == "deny":
            result = handle._deny_file_approval(approval.approval_id)
        elif action == "revoke":
            result = handle._revoke_file_approval(approval.approval_id)
        elif action == "apply":
            result = handle._apply_file_approval(approval.approval_id)
        else:
            raise ValueError("unsupported file approval action")
    except (
        CoworkerDirectoryError,
        CoworkerNotFound,
        CoworkerRuntimeUnavailable,
        OSError,
        SafetyViolation,
        SessionStorageError,
        ScopedIOError,
        ValueError,
    ) as exc:
        _write_error(f"File approval action was refused: {_safe_error(exc)}")
        return 2
    except Exception as exc:
        _write_error(f"File approval action could not complete ({type(exc).__name__}).")
        return 2
    _write_line(f"Status: {result.status}.")
    _write_line(f"Approval ID: {result.approval_id}")
    if action == "approve":
        _write_line(
            "Decision recorded durably. Run `ash crew apply` with this exact ID "
            "to make the one file effect."
        )
    elif action == "apply":
        _write_line(
            "The one-shot file action is recorded. Inspect it with `ash crew approval show`."
        )
    return 0


def _require_tty_confirmation(challenge: str) -> bool:
    try:
        if not sys.stdin.isatty() or not sys.stdout.isatty():
            return False
        _write_line(f"Type exactly: {challenge}")
        sys.stdout.flush()
        answer = sys.stdin.readline()
    except (AttributeError, OSError, ValueError):
        return False
    return answer.rstrip("\r\n") == challenge


def _render_file_approval(approval: CoworkerFileApproval) -> None:
    _write_line(f"Approval ID: {approval.approval_id}")
    _write_line(f"Status: {approval.status}")
    _write_line(f"Target: {_safe_text(approval.target_path, single_line=True)}")
    _write_line(f"Action: {approval.action}")
    _write_line(f"Source run: {approval.source_run_id}")
    _write_line(f"Source session: {approval.source_session_id}")
    _write_line(f"Source request: {approval.source_request_id}")
    _write_line(f"Expected prior SHA-256: {approval.expected_sha256}")
    _write_line(f"Replacement SHA-256: {approval.replacement_sha256}")
    _write_line(f"Approval digest: {approval.approval_digest}")
    _write_line(f"Expires: {_safe_text(approval.expires_at, single_line=True)}")
    if approval.observed_sha256 is not None:
        _write_line(f"Last observed target SHA-256: {approval.observed_sha256}")
    if approval.failure_code is not None:
        _write_line(f"Outcome code: {approval.failure_code}")
    before = approval.expected_content.decode("utf-8")
    after = approval.replacement_content.decode("utf-8")
    diff = "".join(
        difflib.unified_diff(
            before.splitlines(keepends=True),
            after.splitlines(keepends=True),
            fromfile=f"a/{approval.target_path}",
            tofile=f"b/{approval.target_path}",
        )
    )
    if diff:
        _write_line("Proposed diff:")
        _write_line(_safe_text(diff))


def _current_uid_available() -> bool:
    get_effective_uid = getattr(os, "geteuid", None)
    if not callable(get_effective_uid):
        return False
    try:
        return type(get_effective_uid()) is int
    except OSError:
        return False


def _write_identity(identity: Coworker) -> None:
    _write_line(f"ID: {_safe_text(identity.coworker_id, single_line=True)}")
    _write_line(f"Name: {_safe_text(identity.display_name, single_line=True)}")
    _write_line(f"Owner: {_safe_text(identity.owner_id, single_line=True)}")
    _write_line(f"Profile: {_safe_text(identity.profile_name, single_line=True)}")
    _write_line(f"Workspace: {_safe_text(str(identity.workspace), single_line=True)}")
    _write_line(f"State: {_safe_text(identity.state, single_line=True)}")


def _safe_error(exc: Exception) -> str:
    if isinstance(exc, CoworkerNotFound):
        return "no coworker matches the supplied owner label and ID"
    if isinstance(exc, (ScopedIOError, SafetyViolation, OSError)):
        return "the selected workspace file could not be read safely"
    message = str(exc)
    if not message:
        return type(exc).__name__
    return _safe_text(message, single_line=True)


def _safe_text(value: str, *, single_line: bool = False) -> str:
    return terminal_safe_text(
        redact_known_secrets(value),
        single_line=single_line,
    )


def _write_line(value: str) -> None:
    print(value)


def _write_error(value: str) -> None:
    print(f"Error: {value}", file=sys.stderr)
