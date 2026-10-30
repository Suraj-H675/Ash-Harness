"""Native command path for explicit, read-only coworker turns."""

from __future__ import annotations

import argparse
import asyncio
import difflib
import os
import signal
import sys
from datetime import datetime, timezone
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
    CoworkerApprovalInboxEntry,
    CoworkerDirectory,
    CoworkerDirectoryError,
    CoworkerFileApproval,
    CoworkerHandle,
    CoworkerModelReadinessError,
    CoworkerModelReadinessResult,
    CoworkerModelTurnResult,
    CoworkerNotFound,
    CoworkerResponsibility,
    CoworkerResponsibilityRun,
    CoworkerResponsibilityStatus,
    CoworkerRunInboxEntry,
    CoworkerRuntimeUnavailable,
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
    if action == "show":
        return _show(args)
    if action == "inbox":
        return _inbox(args)
    if action == "brief":
        return _brief(args)
    if action in {"pause", "resume", "archive"}:
        return _lifecycle(args, action)
    if action == "check-model":
        return _check_model(args)
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
    if action == "worker":
        return _worker(args)
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

    if identity.lifecycle_status == "active":
        _write_line("Coworker ready.")
    else:
        _write_line(
            "Existing coworker found. Lifecycle was not changed from "
            f"{identity.lifecycle_status}."
        )
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


def _brief(args: argparse.Namespace) -> int:
    try:
        handle = _open_coworker_handle(args)
        if args.crew_brief_action == "set":
            brief = handle.set_brief(args.text)
        elif args.crew_brief_action == "clear":
            brief = handle.set_brief("")
        else:
            brief = handle.read_brief()
    except (
        CoworkerDirectoryError, CoworkerNotFound, CoworkerRuntimeUnavailable,
        OSError, SafetyViolation, SessionStorageError, ValueError,
    ) as exc:
        _write_error(f"Coworker brief unavailable: {_safe_error(exc)}")
        return 2
    _write_line(f"Brief revision: {brief.revision}")
    if brief.text:
        _write_line("Owner-curated context:")
        _write_line(_safe_text(brief.text))
    else:
        _write_line("No coworker brief saved.")
    _write_line(
        "This context is untrusted reference data in future coworker model turns, "
        "not a permission or instruction grant."
    )
    return 0


def _show(args: argparse.Namespace) -> int:
    try:
        if not 1 <= args.limit <= 20:
            raise ValueError("show limit must be between 1 and 20")
        handle = _open_coworker_handle(args)
        identity = handle.require_owner_binding()
        responsibilities = handle.list_responsibilities()
        activity = CoworkerDirectory.open().list_inbox(
            owner_id=args.owner,
            coworker_id=identity.coworker_id,
            limit=args.limit,
        )
    except (
        CoworkerDirectoryError, CoworkerNotFound, CoworkerRuntimeUnavailable,
        OSError, SafetyViolation, SessionStorageError, ValueError,
    ) as exc:
        _write_error(f"Coworker inspection is unavailable: {_safe_error(exc)}")
        return 2
    except Exception as exc:
        _write_error(f"Coworker inspection is unavailable ({type(exc).__name__}).")
        return 2

    _write_identity(identity)
    if identity.lifecycle_status != "active":
        _write_line("New work and new approval effects are disabled for this coworker.")
        _write_line("Previously claimed turns may still finish; inspect their outcome below.")
    _write_line("Responsibilities:")
    if not responsibilities:
        _write_line("  None.")
    for responsibility in responsibilities[:args.limit]:
        _write_line(
            f"  {_safe_text(responsibility.responsibility_id, single_line=True)}: "
            f"{responsibility.status} "
            f"({_safe_text(responsibility.scope, single_line=True)})"
        )
        if responsibility.schedule is not None:
            _write_line(f"    Schedule: {render_schedule(responsibility.schedule)}")
            if responsibility.next_fire_at is not None:
                _write_line(
                    f"    Stored next occurrence: {responsibility.next_fire_at.isoformat()}"
                )
    if len(responsibilities) > args.limit:
        _write_line("  More: use `ash crew responsibility list` for the full view.")

    _write_line("Recent local activity (no remote notifications were delivered):")
    if not activity:
        _write_line("  None.")
    for entry in activity:
        _write_line(f"Entry ID: {_safe_text(entry.entry_id, single_line=True)}")
        _write_line(f"Cause: {_safe_text(entry.cause, single_line=True)}")
        if isinstance(entry, CoworkerRunInboxEntry):
            _render_inbox_run(entry)
        elif isinstance(entry, CoworkerApprovalInboxEntry):
            _render_inbox_approval(entry)
        _write_line("")
    _write_line(
        "Schedules run through an explicit tick or started Crew worker; "
        "there is no automatically installed background service or "
        "remote notification channel."
    )
    return 0


def _inbox(args: argparse.Namespace) -> int:
    try:
        entries = CoworkerDirectory.open().list_inbox(
            owner_id=args.owner,
            limit=args.limit,
            include_skipped=args.include_skipped,
        )
    except (
        CoworkerDirectoryError,
        CoworkerNotFound,
        CoworkerRuntimeUnavailable,
        OSError,
        ValueError,
    ) as exc:
        _write_error(f"Local activity inbox is unavailable: {_safe_error(exc)}")
        return 2
    except Exception as exc:
        _write_error(f"Local activity inbox is unavailable ({type(exc).__name__}).")
        return 2

    _write_line("Local Crew activity only. No remote message was sent or delivered.")
    if not entries:
        _write_line("No Crew activity found for this owner label.")
        return 0
    for entry in entries:
        _write_line(f"Entry ID: {_safe_text(entry.entry_id, single_line=True)}")
        _write_line(
            "Coworker: "
            f"{_safe_text(entry.coworker_name, single_line=True)} "
            f"({_safe_text(entry.coworker_id, single_line=True)})"
        )
        _write_line(
            f"Lifecycle: {_safe_text(entry.lifecycle_status, single_line=True)}"
        )
        _write_line(f"Cause: {_safe_text(entry.cause, single_line=True)}")
        if entry.responsibility_id is not None:
            _write_line(
                "Responsibility ID: "
                f"{_safe_text(entry.responsibility_id, single_line=True)}"
            )
        if isinstance(entry, CoworkerRunInboxEntry):
            _render_inbox_run(entry)
        elif isinstance(entry, CoworkerApprovalInboxEntry):
            _render_inbox_approval(entry)
        _write_line("")
    return 0


def _render_inbox_run(entry: CoworkerRunInboxEntry) -> None:
    _write_line(f"Run ID: {_safe_text(entry.run_id, single_line=True)}")
    if entry.session_id is not None:
        _write_line(f"Session ID: {_safe_text(entry.session_id, single_line=True)}")
    if entry.request_id is not None:
        _write_line(f"Request ID: {_safe_text(entry.request_id, single_line=True)}")
    if entry.event_key is not None:
        _write_line(f"Event key: {_safe_text(entry.event_key, single_line=True)}")
    if entry.scheduled_for is not None:
        _write_line(f"Scheduled for: {entry.scheduled_for.isoformat()}")
    if entry.skip_reason is not None:
        _write_line(
            f"Skip reason: {_safe_text(entry.skip_reason, single_line=True)}"
        )
    _write_line(f"Execution: {_safe_text(entry.execution_kind, single_line=True)}")
    _write_line(f"Occurred: {entry.occurred_at.isoformat()}")
    if entry.status == "claimed":
        status = "Outcome unknown; this claim will not be replayed."
    elif entry.status == "skipped":
        status = "Skipped because its scheduled start window expired."
    else:
        status = "Completed."
    _write_line(f"Status: {_safe_text(status, single_line=True)}")


def _render_inbox_approval(entry: CoworkerApprovalInboxEntry) -> None:
    _write_line(f"Approval ID: {_safe_text(entry.approval_id, single_line=True)}")
    _write_line(f"Source run ID: {_safe_text(entry.source_run_id, single_line=True)}")
    _write_line(
        f"Source session ID: {_safe_text(entry.source_session_id, single_line=True)}"
    )
    _write_line(
        f"Source request ID: {_safe_text(entry.source_request_id, single_line=True)}"
    )
    if entry.event_key is not None:
        _write_line(f"Event key: {_safe_text(entry.event_key, single_line=True)}")
    if entry.target_path:
        _write_line(f"Target: {_safe_text(entry.target_path, single_line=True)}")
    _write_line(f"Action: {_safe_text(entry.action, single_line=True)}")
    _write_line(f"Occurred: {entry.occurred_at.isoformat()}")
    _write_line(f"Expires: {entry.expires_at.isoformat()}")

    now = datetime.now(timezone.utc)
    if entry.status in {"pending", "approved"} and entry.expires_at <= now:
        status = (
            "Deadline passed; the expiration transition is not yet recorded. "
            "Inspect with `ash crew approval show`."
        )
    elif entry.status == "pending":
        status = "Waiting for explicit owner approval."
    elif entry.status == "approved":
        status = "Approved; waiting for an explicit local apply."
    elif entry.status == "denied":
        status = "Denied."
    elif entry.status == "expired":
        status = "Expired."
    elif entry.status == "revoked":
        status = "Revoked."
    elif entry.status == "dispatching":
        status = (
            "File effect outcome unknown; inspect with `ash crew approval show`. "
            "Do not replay."
        )
    elif entry.status == "succeeded":
        status = "Applied."
    elif entry.status == "failed":
        status = "Failed. Inspect with `ash crew approval show`."
    elif entry.status == "blocked":
        status = "Blocked. Inspect with `ash crew approval show`."
    else:
        status = "Outcome ambiguous; inspect with `ash crew approval show`."
    if entry.lifecycle_status != "active" and entry.status in {"pending", "approved"}:
        status += (
            " New approval grants and file effects are blocked while the coworker is "
            f"{entry.lifecycle_status}."
        )
    _write_line(f"Status: {_safe_text(status, single_line=True)}")


def _lifecycle(args: argparse.Namespace, action: str) -> int:
    try:
        handle = CoworkerDirectory.open().open_coworker(
            owner_id=args.owner,
            coworker_id=args.coworker_id,
        )
        if action == "pause":
            identity = handle.pause()
        elif action == "resume":
            identity = handle.resume()
        elif action == "archive":
            identity = handle.archive()
        else:
            raise ValueError("unsupported coworker lifecycle action")
    except (
        CoworkerDirectoryError,
        CoworkerNotFound,
        CoworkerRuntimeUnavailable,
        OSError,
        ValueError,
    ) as exc:
        _write_error(f"Coworker lifecycle could not change: {_safe_error(exc)}")
        return 2
    except Exception as exc:  # noqa: BLE001 - keep CLI failures secret-free
        _write_error(
            "Coworker lifecycle could not change "
            f"({type(exc).__name__})."
        )
        return 2

    _write_identity(identity)
    if identity.lifecycle_status == "paused":
        _write_line(
            "New work is paused. Scheduled entries remain stored but disabled."
        )
        _write_line(
            "Turns claimed before this lifecycle change may still dispatch or finish. "
            "Dispatched provider requests cannot be canceled. Inspect history or "
            "responsibility runs for their outcome."
        )
    elif identity.lifecycle_status == "archived":
        _write_line(
            "Coworker archived. New work is permanently disabled; history and "
            "decisions remain available for inspection."
        )
        _write_line(
            "Turns claimed before this lifecycle change may still dispatch or finish. "
            "Dispatched provider requests cannot be canceled. Inspect history or "
            "responsibility runs for their outcome."
        )
    else:
        _write_line("Coworker active. New work is enabled.")
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
        result = asyncio.run(
            handle.run_readonly_turn(request, check_model=args.check_model)
        )
    except CoworkerRuntimeUnavailable as exc:
        if isinstance(exc, CoworkerModelReadinessError):
            if not args.check_model and exc.failure_code == "provider_unavailable":
                _write_error(f"Blocked. {_safe_error(exc)}. No provider request was sent.")
            else:
                _write_model_readiness_failure(exc, check_requested=args.check_model)
            return 2
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


def _check_model(args: argparse.Namespace) -> int:
    try:
        handle = _open_coworker_handle(args)
        result = asyncio.run(handle.check_model_readiness())
    except CoworkerModelReadinessError as exc:
        _write_model_readiness_failure(exc, check_requested=True)
        return 2
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
        _write_error(f"Model check blocked before provider dispatch: {_safe_error(exc)}")
        return 2
    except Exception as exc:  # noqa: BLE001 - never print provider details
        _write_error(
            "Model check failed before provider dispatch "
            f"({type(exc).__name__}); no provider request was sent."
        )
        return 2

    _write_model_readiness_success(result)
    return 0


def _write_model_readiness_success(result: CoworkerModelReadinessResult) -> None:
    _write_line(
        "Synthetic model readiness probe passed for "
        f"{_safe_text(result.model, single_line=True)}."
    )
    _write_line("One provider request was attempted. No Crew run claim or history was created.")
    _write_line(
        "This probe is an advisory signal and does not guarantee the next provider request."
    )


def _write_model_readiness_failure(
    exc: CoworkerModelReadinessError,
    *,
    check_requested: bool,
) -> None:
    if exc.failure_code == "already_claimed":
        request_status = (
            "A provider request was attempted during preflight"
            if exc.provider_request_attempted
            else "No provider request was attempted"
        )
        _write_error(
            "This deterministic Crew run ID was already claimed. "
            f"{request_status}; the existing claim was left unchanged and no new "
            "Crew run claim was made."
        )
        return
    label = (
        "Model readiness check failed"
        if check_requested
        else "Crew model selection was blocked"
    )
    request_status = (
        "A provider request was attempted"
        if exc.provider_request_attempted
        else "No provider request was sent"
    )
    _write_error(
        f"{label} (failure_code={exc.failure_code}). {request_status}; "
        "no Crew run claim was made."
    )


def _history(args: argparse.Namespace) -> int:
    try:
        handle = CoworkerDirectory.open().open_coworker(
            owner_id=args.owner,
            coworker_id=args.coworker_id,
        )
        handle.require_owner_binding()
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
                    check_model=args.check_model,
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
        if isinstance(exc, CoworkerModelReadinessError):
            if not args.check_model and exc.failure_code == "provider_unavailable":
                _write_model_admission_error(exc, scheduled=False)
            else:
                _write_model_readiness_failure(exc, check_requested=args.check_model)
            return 2
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
                check_model=args.check_model,
                draft_correction=args.draft_correction,
            )
        )
        _render_model_turn(result)
        return 0
    except CoworkerRuntimeUnavailable as exc:
        if isinstance(exc, CoworkerModelReadinessError):
            if not args.check_model and exc.failure_code == "provider_unavailable":
                _write_model_admission_error(exc, scheduled=False)
            else:
                _write_model_readiness_failure(exc, check_requested=args.check_model)
            return 2
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
        result = asyncio.run(
            handle.run_due_model_responsibilities_once(
                check_model=args.check_model,
            )
        )
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
        if isinstance(exc, CoworkerModelReadinessError):
            if not args.check_model and exc.failure_code == "provider_unavailable":
                _write_model_admission_error(exc, scheduled=True)
            else:
                _write_model_readiness_failure(exc, check_requested=args.check_model)
            return 2
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


def _worker(args: argparse.Namespace) -> int:
    """Run one explicitly selected coworker's stored schedules until stopped."""
    if not 10 <= args.poll_seconds <= 3600:
        _write_error("Worker polling interval must be between 10 and 3600 seconds.")
        return 2
    try:
        handle = _open_coworker_handle(args)
        if handle.require_owner_binding().lifecycle_status == "archived":
            _write_error("Archived coworkers cannot start a scheduled worker.")
            return 2
        asyncio.run(_run_crew_worker(handle, args.poll_seconds))
        _write_line("Crew worker stopped.")
        return 0
    except KeyboardInterrupt:
        _write_error(
            "Crew worker interrupted. An in-flight provider outcome may be unknown; "
            "inspect Crew responsibility runs before retrying."
        )
        return 130
    except (CoworkerDirectoryError, CoworkerNotFound, CoworkerRuntimeUnavailable,
            OSError, SafetyViolation, SessionStorageError, ValueError) as exc:
        _write_error(
            "Crew worker stopped ("
            f"{type(exc).__name__}). Inspect Crew show and inbox."
        )
        return 2
    except Exception as exc:
        _write_error(f"Crew worker stopped ({type(exc).__name__}).")
        return 2


async def _run_crew_worker(
    handle: CoworkerHandle,
    poll_seconds: int,
    *,
    stop: asyncio.Event | None = None,
) -> None:
    """One serial, bounded Crew model tick per poll; no independent dispatch path."""
    stopping = stop if stop is not None else asyncio.Event()
    loop = asyncio.get_running_loop()
    registered_term = False
    if stop is None:
        try:
            loop.add_signal_handler(signal.SIGTERM, stopping.set)
            registered_term = True
        except (NotImplementedError, RuntimeError):
            pass
    _write_line(
        "Crew worker started for coworker "
        f"{_safe_text(handle.identity.coworker_id, single_line=True)}. "
        "Only stored, due responsibilities will run. "
        "Stop with Ctrl-C or SIGTERM; inspect the Crew inbox for outcomes."
    )
    sys.stdout.flush()
    try:
        while not stopping.is_set():
            identity = handle.require_owner_binding()
            if identity.lifecycle_status == "archived":
                _write_line("Coworker archived; worker will not dispatch new work.")
                return
            if identity.lifecycle_status == "active":
                try:
                    result = await handle.run_due_model_responsibilities_once()
                    if result.status == "completed":
                        if result.turn is None:
                            raise CoworkerDirectoryError(
                                "completed scheduled turn has no persisted result"
                            )
                        _write_line(
                            "Scheduled Crew turn completed; "
                            f"responsibility {result.responsibility_id}, "
                            f"event {_safe_text(result.event_key or '', single_line=True)}. "
                            "Inspect Crew inbox for details."
                        )
                    elif result.status == "skipped":
                        _write_line(
                            "Scheduled Crew occurrence skipped after misfire grace; "
                            f"responsibility {result.responsibility_id}."
                        )
                except CoworkerModelReadinessError as exc:
                    _write_error(
                        "Scheduled Crew turn blocked by provider readiness "
                        f"({exc.failure_code}); inspect Crew show."
                    )
                except CoworkerRuntimeUnavailable:
                    _write_error(
                        "Scheduled Crew turn was blocked or its outcome may be unknown; "
                        "inspect Crew responsibility runs before retrying."
                    )
                except (CoworkerDirectoryError, AshError, OSError, SafetyViolation,
                        SessionStorageError, ValueError):
                    _write_error(
                        "Scheduled Crew turn unavailable; inspect Crew show "
                        "and the local inbox."
                    )
            if not stopping.is_set():
                try:
                    await asyncio.wait_for(stopping.wait(), timeout=poll_seconds)
                except TimeoutError:
                    pass
    finally:
        if registered_term:
            loop.remove_signal_handler(signal.SIGTERM)


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
            "coworker is paused",
            "coworker is archived",
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
    handle.require_owner_binding()
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
    handle.require_owner_binding()
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
            from_response=args.from_response,
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
    if approval.replacement_source_path == "<atlas-response>":
        _write_line("Replacement origin: Atlas draft (untrusted; owner review required)")
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
    _write_line(
        f"Lifecycle: {_safe_text(identity.lifecycle_status, single_line=True)}"
    )


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
