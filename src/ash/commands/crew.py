"""Native command path for explicit, read-only coworker turns."""

from __future__ import annotations

import argparse
import asyncio
import difflib
import json
import os
import signal
import sys
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID, uuid4, uuid5

from ash.automation.schedules import build_schedule, render_schedule
from ash.crew_events import (
    MAX_CREW_EVENT_BYTES, CrewEventError, validate_crew_event_key_file,
)
from ash.crew_ingress import (
    CrewIngressReceipt, CrewIngressRefusal, serve_crew_ingress,
)
from ash.core.redaction import redact_known_secrets
from ash.coworker_evidence import (
    evidence_metadata_manifest,
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
    CoworkerRemovalPreview,
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
    if action == "rename":
        return _rename(args)
    if action == "inbox":
        return _inbox(args)
    if action == "remove":
        return _remove(args)
    if action == "usage":
        return _usage(args)
    if action == "budget":
        return _budget(args)
    if action == "brief":
        return _brief(args)
    if action in {"pause", "resume", "archive"}:
        return _lifecycle(args, action)
    if action == "check-model":
        return _check_model(args)
    if action in {"ask", "chat"}:
        return _ask(args)
    if action == "syntax":
        return _syntax(args)
    if action == "git-diff":
        return _git_diff(args)
    if action == "investigate":
        return _investigate(args)
    if action == "history":
        return _history(args)
    if action == "responsibility":
        return _responsibility(args)
    if action == "event":
        return _event(args)
    if action == "listen":
        return _listen(args)
    if action == "intake":
        return _intake(args)
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
        if args.crew_brief_action == "adopt":
            return _adopt_brief(args, handle)
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


def _adopt_brief(args: argparse.Namespace, handle: CoworkerHandle) -> int:
    """One owner-confirmed promotion of model text, never automatic memory."""

    try:
        preview = handle.preview_brief_adoption(
            run_id=args.run_id,
            session_id=args.session_id,
            request_id=args.request_id,
            excerpt=args.excerpt,
        )
    except (
        CoworkerDirectoryError, CoworkerNotFound, CoworkerRuntimeUnavailable,
        OSError, SafetyViolation, SessionStorageError, ValueError,
    ) as exc:
        _write_error(f"Model finding could not be reviewed: {_safe_error(exc)}")
        return 2
    _write_line(f"Source run: {args.run_id}")
    _write_line(f"Source response SHA-256: {preview.source_response_sha256}")
    _write_line(f"Current brief revision: {preview.expected_revision}")
    _write_line("Proposed full coworker brief:")
    _write_line(_safe_text(preview.text))
    _write_line(
        "This is model-generated, unverified reference data, not an instruction "
        "or permission grant."
    )
    challenge = (
        f"ADOPT {args.run_id} {preview.expected_revision} "
        f"{preview.source_response_sha256}"
    )
    if not _require_tty_confirmation(challenge):
        _write_error(
            "Blocked. Adoption requires an interactive local TTY and the exact challenge."
        )
        return 2
    try:
        saved = handle.adopt_brief_from_run(
            run_id=args.run_id, session_id=args.session_id,
            request_id=args.request_id, excerpt=args.excerpt,
            expected_revision=preview.expected_revision,
            expected_response_sha256=preview.source_response_sha256,
        )
    except (
        CoworkerDirectoryError, CoworkerNotFound, CoworkerRuntimeUnavailable,
        OSError, SafetyViolation, SessionStorageError, ValueError,
    ) as exc:
        _write_error(f"Model finding was not saved: {_safe_error(exc)}")
        return 2
    _write_line(f"Brief updated. New revision: {saved.revision}")
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


def _budget(args: argparse.Namespace) -> int:
    """Conservative lifetime model-turn cap, not a dollar spending promise."""

    try:
        handle = _open_coworker_handle(args)
        action = args.crew_budget_action
        if action == "show":
            budget = handle.model_budget()
        else:
            budget = handle.set_model_budget(
                max_attempts=args.max_attempts if action == "set" else None,
                expected_revision=args.expected_revision,
            )
    except (
        CoworkerDirectoryError, CoworkerNotFound, CoworkerRuntimeUnavailable,
        SessionStorageError, OSError, ValueError,
    ) as exc:
        _write_error(f"Coworker model budget is unavailable: {_safe_error(exc)}")
        return 2
    except Exception as exc:
        _write_error(f"Coworker model budget is unavailable ({type(exc).__name__}).")
        return 2
    _write_line(f"Budget revision: {budget.revision}")
    _write_line(
        "Lifetime model attempt cap: "
        + ("unlimited" if budget.max_attempts is None else str(budget.max_attempts))
    )
    _write_line(f"Durably claimed model attempts: {budget.used_attempts}")
    _write_line(
        "Remaining attempts: "
        + (
            "unlimited" if budget.remaining_attempts is None
            else str(budget.remaining_attempts)
        )
    )
    _write_line(
        "Claimed and uncertain turns consume a slot. Scheduled skips do not. "
        "This is a lifetime admission cap, not a USD/token limit; changing it "
        "cannot cancel a dispatched turn. Explicit readiness tests and native "
        "tool-only operations have separate accounting."
    )
    return 0


def _usage(args: argparse.Namespace) -> int:
    """Summarize only durable, owner-bound model session accounting."""

    try:
        handle = _open_coworker_handle(args)
        summary = handle.model_usage_summary()
    except (
        CoworkerDirectoryError, CoworkerNotFound, CoworkerRuntimeUnavailable,
        SessionStorageError, OSError, ValueError,
    ) as exc:
        _write_error(f"Coworker usage is unavailable: {_safe_error(exc)}")
        return 2
    except Exception as exc:
        _write_error(
            f"Coworker usage is unavailable ({type(exc).__name__})."
        )
        return 2
    _write_line(f"Model attempts: {summary.model_attempts}")
    _write_line(f"Completed: {summary.completed_runs}")
    _write_line(f"Claimed / uncertain: {summary.uncertain_runs}")
    _write_line(f"Scheduled skips without a model call: {summary.skipped_occurrences}")
    _write_line(f"Unmetered completed turns: {summary.unmetered_completed_runs}")
    _write_line(f"Recorded tokens: {summary.total_tokens}")
    _write_line(
        f"  Prompt: {summary.prompt_tokens}; "
        f"completion: {summary.completion_tokens}"
    )
    _write_line(
        f"  Cache read: {summary.cache_read_tokens}; "
        f"cache write: {summary.cache_write_tokens}"
    )
    _write_line(
        "Estimated token subset of recorded totals: "
        f"prompt {summary.estimated_prompt_tokens}, "
        f"completion {summary.estimated_completion_tokens}"
    )
    _write_line(f"Recorded priced cost (USD): {summary.known_cost_usd:.6f}")
    _write_line(f"Estimated cost (USD): {summary.estimated_cost_usd:.6f}")
    _write_line(f"Turns with unknown pricing: {summary.pricing_unknown_turns}")
    if not summary.full_cost_known:
        _write_line(
            "Total actual cost is UNKNOWN: incomplete/uncertain runs, "
            "missing token accounting, or missing pricing prevent a reliable total."
        )
    else:
        _write_line(
            "Recorded cost is fully priced for the accounted model turns only."
        )
    _write_line(
        "This is historical local session accounting, not a provider invoice, "
        "live balance, spending limit, or guarantee of zero charges. "
        "Preflight readiness probes and native tool-only sessions are excluded."
    )
    return 0


def _inbox(args: argparse.Namespace) -> int:
    if getattr(args, "crew_inbox_action", None) == "ack":
        return _inbox_ack(args)
    owner_id = getattr(args, "owner", None)
    if not isinstance(owner_id, str) or not owner_id:
        _write_error("Local activity inbox requires an explicit owner label.")
        return 2
    try:
        entries = CoworkerDirectory.open().list_inbox(
            owner_id=owner_id,
            limit=args.limit,
            include_skipped=args.include_skipped,
            unacknowledged_only=args.unacknowledged,
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
        if isinstance(entry, CoworkerApprovalInboxEntry):
            _write_line(f"Entry ID: {_safe_text(entry.entry_id, single_line=True)}")
        _write_line(f"Inbox event ID: {_safe_text(entry.event_id, single_line=True)}")
        if entry.acknowledged_at is None:
            _write_line("Acknowledged: no")
        else:
            _write_line(f"Acknowledged: {entry.acknowledged_at.isoformat()}")
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


def _inbox_ack(args: argparse.Namespace) -> int:
    try:
        acknowledged_at = CoworkerDirectory.open().acknowledge_inbox_event(
            owner_id=args.owner,
            event_id=args.event_id,
        )
    except (
        CoworkerDirectoryError,
        CoworkerNotFound,
        CoworkerRuntimeUnavailable,
        OSError,
        ValueError,
    ) as exc:
        _write_error(f"Inbox event could not be acknowledged: {_safe_error(exc)}")
        return 2
    except Exception as exc:
        _write_error(f"Inbox event acknowledgement failed ({type(exc).__name__}).")
        return 2
    _write_line(f"Acknowledged inbox event: {_safe_text(args.event_id, single_line=True)}")
    _write_line(f"Acknowledged at: {acknowledged_at.isoformat()}")
    _write_line("Run and approval lifecycle state was not changed.")
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


def _rename(args: argparse.Namespace) -> int:
    """Rename only the owner-bound display label, never the stable coworker ID."""

    try:
        handle = _open_coworker_handle(args)
        if handle.require_owner_binding().display_name != args.expected_name:
            raise CoworkerRuntimeUnavailable(
                "coworker display name changed since owner review"
            )
        renamed = handle.rename(args.name)
    except (
        CoworkerDirectoryError, CoworkerNotFound, CoworkerRuntimeUnavailable,
        SessionStorageError, OSError, ValueError,
    ) as exc:
        _write_error(f"Coworker rename blocked: {_safe_error(exc)}")
        return 2
    except Exception as exc:
        _write_error(f"Coworker rename blocked ({type(exc).__name__}).")
        return 2
    _write_identity(renamed)
    _write_line(
        "Only the display name changed; coworker ID, profile, workspace, "
        "sessions and responsibilities remain bound to their original identity."
    )
    return 0


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


def _remove(args: argparse.Namespace) -> int:
    try:
        directory = CoworkerDirectory.open()
        preview = directory.preview_archived_removal(
            owner_id=args.owner,
            coworker_id=args.coworker_id,
        )
    except (
        CoworkerDirectoryError,
        CoworkerNotFound,
        CoworkerRuntimeUnavailable,
        OSError,
        SessionStorageError,
        ValueError,
    ) as exc:
        _write_error(f"Coworker removal preview failed: {_safe_error(exc)}")
        return 2
    except Exception as exc:
        _write_error(f"Coworker removal preview failed ({type(exc).__name__}).")
        return 2

    _render_removal_preview(preview)
    if preview.operation_state == "completed":
        _write_line("Logical removal was already completed; no state was changed.")
        _write_removal_retention_note()
        return 0

    challenge = f"REMOVE {preview.plan.coworker_id} {preview.plan.sha256}"
    if not _require_tty_confirmation(challenge):
        _write_error(
            "Coworker removal requires that exact confirmation in an interactive "
            "local TTY; no state was changed."
        )
        return 2

    try:
        removed = directory.remove_archived_coworker(
            owner_id=args.owner,
            coworker_id=args.coworker_id,
            expected_plan_sha256=preview.plan.sha256,
        )
    except (
        CoworkerDirectoryError,
        CoworkerNotFound,
        CoworkerRuntimeUnavailable,
        OSError,
        SessionStorageError,
        ValueError,
    ) as exc:
        _write_error(f"Coworker removal did not complete: {_safe_error(exc)}")
        return 2
    except Exception as exc:
        _write_error(f"Coworker removal did not complete ({type(exc).__name__}).")
        return 2

    _write_line(
        f"Logical removal completed for coworker {removed.coworker_id}."
    )
    _write_removal_retention_note()
    return 0


def _render_removal_preview(preview: CoworkerRemovalPreview) -> None:
    plan = preview.plan
    state = {
        "new": "new removal plan",
        "pending": "resume of a previously confirmed interrupted removal",
        "completed": "completed removal",
    }[preview.operation_state]
    _write_line(f"Coworker removal preview ({state}; logical deletion only):")
    _write_line(f"Owner label: {_safe_text(plan.owner_id, single_line=True)}")
    _write_line(
        f"Coworker: {_safe_text(plan.display_name, single_line=True)} "
        f"({plan.coworker_id})"
    )
    _write_line(f"Profile: {_safe_text(plan.profile_name, single_line=True)}")
    _write_line(f"Workspace retained: {_safe_text(plan.workspace_path, single_line=True)}")
    _write_line(
        "Coworker directory rows to remove: "
        f"{sum(count for _table, count in plan.row_counts)}"
    )
    for table, count in plan.row_counts:
        _write_line(f"  {table}: {count}")
    profile_state = "present" if preview.profile_sessions_present else "already removed"
    profile_count = preview.current_retained_profile_session_count
    native_count = preview.current_retained_native_session_count
    count_time = "at confirmation" if profile_count is None else "currently"
    _write_line(
        f"Coworker-owned profile sessions: {len(plan.profile_session_ids)} "
        f"({profile_state}); unrelated profile sessions {count_time}: "
        f"{plan.retained_profile_session_count if profile_count is None else profile_count}"
    )
    native_state = "present" if preview.native_sessions_present else "already removed"
    native_count_time = "at confirmation" if native_count is None else "currently"
    _write_line(
        f"Coworker-owned native tool sessions: {len(plan.native_session_ids)} "
        f"({native_state}); unrelated native sessions {native_count_time}: "
        f"{plan.retained_native_session_count if native_count is None else native_count}"
    )
    if preview.operation_state == "pending" and (
        profile_count != plan.retained_profile_session_count
        or native_count != plan.retained_native_session_count
    ):
        _write_line(
            "Unrelated session counts changed after confirmation; "
            "these sessions are outside the removal targets."
        )
    _write_line(f"Exact confirmation digest: {plan.sha256}")
    _write_line(
        "Deleted local records include stored briefs, run history, inbox "
        "acknowledgements, and any staged pending file approvals. Pending approvals "
        "are discarded, never executed."
    )
    _write_line(
        "Retained: shared profile and workspace files, credentials, external accounts, "
        "unrelated sessions, and a small immutable removal record."
    )


def _write_removal_retention_note() -> None:
    _write_line(
        "This removes the listed SQLite rows and exact Coworker-owned session IDs. "
        "It does not securely erase SQLite WAL pages, backups, snapshots, or other "
        "filesystem copies."
    )


def _git_diff(args: argparse.Namespace) -> int:
    """Keep the explicit owner TTY decision separate from model turns."""

    run_id = args.run_id
    source_ids = (
        getattr(args, "source_run_id", None),
        getattr(args, "source_session_id", None),
        getattr(args, "source_request_id", None),
    )
    from_model = any(source_ids)
    source_values = tuple(str(value) for value in source_ids)
    if args.crew_git_action == "run":
        if from_model and (
            not all(source_ids) or args.file is not None or run_id is not None
        ):
            _write_error(
                "Use exactly three model source IDs without --file or --run-id."
            )
            return 2
        if not from_model and not args.file:
            _write_error("Select --file or a complete model proposal source.")
            return 2
        if run_id is None and not from_model:
            run_id = str(uuid4())
    if run_id is not None:
        _write_line(f"Native tool run ID: {_safe_text(run_id, single_line=True)}")
    try:
        handle = CoworkerDirectory.open().open_coworker(
            owner_id=args.owner, coworker_id=args.coworker_id,
        )
        if args.crew_git_action == "status":
            receipt = handle.native_git_diff_status(run_id)
            _write_line(f"Status: {receipt.status}")
            _write_line(f"Path: {_safe_text(receipt.path, single_line=True)}")
            _write_line(f"Owner-granted SHA-256: {receipt.source_sha256}")
            if receipt.tool_session_id:
                _write_line(f"Native tool session: {receipt.tool_session_id}")
            if receipt.output_sha256:
                _write_line(f"Output SHA-256: {receipt.output_sha256}")
            if receipt.status in {"claimed", "unknown"}:
                _write_line(
                    "Outcome uncertain; do not retry this run ID. "
                    "Inspect the separate native tool journal."
                )
            if args.show_output:
                _write_line("Recorded Git patch (no new execution):")
                recorded = handle.native_git_diff_output(run_id)
                _write_line(
                    _safe_text(recorded) if recorded else "(no unstaged change)"
                )
            return 0
        if from_model:
            proposal = handle.preview_git_diff_proposal(
                run_id=source_values[0],
                session_id=source_values[1],
                request_id=source_values[2],
            )
            run_id = proposal.task_run_id
            selected_path, sha256 = proposal.path, proposal.sha256
            _write_line(f"Native tool run ID: {run_id}")
            _write_line(f"Model source run: {proposal.source_run_id}")
            _write_line(f"Model response SHA-256: {proposal.response_sha256}")
            _write_line(
                "This untrusted model choice is not owner permission. "
                "Only an exact read-only Git diff of the selected path is possible."
            )
        else:
            current = handle.require_local_owner()
            guard = SafetyGuard(current.workspace)
            evidence, = read_explicit_evidence(
                current.workspace, (args.file,), guard=guard,
            )
            selected_path, sha256 = evidence.relative_path, evidence.sha256
        _write_line(f"Selected path: {_safe_text(selected_path, single_line=True)}")
        _write_line(f"Current SHA-256: {sha256}")
        _write_line(
            "Only this file's unstaged Git diff will be inspected. "
            "No model, shell, plugin, MCP or write tool is authorized."
        )
        challenge = f"GIT DIFF {run_id} {sha256}"
        if from_model:
            challenge += f" {proposal.response_sha256}"
        if not _require_tty_confirmation(challenge):
            _write_error("Blocked. Exact local TTY approval is required.")
            return 2
        if from_model and handle.preview_git_diff_proposal(
            run_id=source_values[0],
            session_id=source_values[1],
            request_id=source_values[2],
        ) != proposal:
            raise CoworkerRuntimeUnavailable(
                "Git proposal or owner-granted evidence changed after approval"
            )
        receipt, output = asyncio.run(handle.run_native_git_diff(
            run_id=run_id, path=selected_path,
            expected_sha256=sha256,
        ))
        _write_line(f"Status: {receipt.status}")
        _write_line(f"Native tool session: {receipt.tool_session_id}")
        _write_line(f"Output SHA-256: {receipt.output_sha256}")
        _write_line("Git patch (read-only):")
        _write_line(_safe_text(output) if output else "(no unstaged change)")
        return 0
    except KeyboardInterrupt:
        _write_error(
            "Native tool interrupted; inspect the run status. Do not replay."
        )
        return 130
    except (
        CoworkerDirectoryError, CoworkerNotFound, CoworkerRuntimeUnavailable,
        AshError, OSError, SafetyViolation, SessionStorageError, ScopedIOError,
        ValueError,
    ) as exc:
        _write_error(
            f"Native Git diagnostic blocked or uncertain: {_safe_error(exc)}. "
            "Inspect its run status; never reuse a claimed ID."
        )
        return 2
    except Exception as exc:
        _write_error(
            f"Native Git diagnostic outcome uncertain ({type(exc).__name__}); "
            "inspect the run status before any new request."
        )
        return 2


def _syntax(args: argparse.Namespace) -> int:
    """Expose only the already fixed, non-executing Docker syntax worker."""

    source_ids = (
        args.source_run_id, args.source_session_id, args.source_request_id,
    )
    source_requested = any(value is not None for value in source_ids)
    if source_requested and (
        not all(source_ids) or args.files or args.run_id
    ):
        _write_error(
            "Choose either explicit --file paths or exactly three source IDs; "
            "model-derived checks generate their own one-shot run ID."
        )
        return 2
    if not source_requested and not args.files:
        _write_error("Select at least one --file or a complete model source.")
        return 2
    run_id = args.run_id or str(uuid4())
    try:
        handle = _open_coworker_handle(args)
        if source_requested:
            proposal = handle.preview_syntax_proposal(
                run_id=args.source_run_id,
                session_id=args.source_session_id,
                request_id=args.source_request_id,
            )
            run_id = proposal.task_run_id
            _write_line(f"Model source run: {args.source_run_id}")
            _write_line(f"Model response SHA-256: {proposal.source_response_sha256}")
            for name, sha256 in proposal.file_sha256:
                _write_line(f"Owner-granted file: {_safe_text(name, single_line=True)} ({sha256})")
            _write_line(
                "This is an untrusted model suggestion, not permission to run "
                "arbitrary commands. Only fixed syntax parsing is permitted."
            )
            challenge = (
                f"CHECK SYNTAX {args.source_run_id} "
                f"{proposal.source_response_sha256}"
            )
            if not _require_tty_confirmation(challenge):
                _write_error(
                    "Blocked. Model-selected checks require exact local TTY confirmation."
                )
                return 2
            if handle.preview_syntax_proposal(
                run_id=args.source_run_id,
                session_id=args.source_session_id,
                request_id=args.source_request_id,
            ) != proposal:
                raise CoworkerRuntimeUnavailable(
                    "syntax proposal or evidence changed since owner confirmation"
                )
            _write_line(f"Task run ID: {run_id}")
            report = asyncio.run(handle.check_python_syntax(
                run_id=run_id, permitted_files=proposal.files,
                expected_sha256=dict(proposal.file_sha256),
            ))
        else:
            _write_line(f"Run ID: {_safe_text(run_id, single_line=True)}")
            report = asyncio.run(handle.check_python_syntax(
                run_id=run_id, permitted_files=tuple(args.files),
            ))
    except (
        CoworkerDirectoryError, CoworkerNotFound, CoworkerRuntimeUnavailable,
        AshError, OSError, SafetyViolation, SessionStorageError,
        ScopedIOError, ValueError,
    ) as exc:
        _write_error(
            f"Syntax check refused or outcome unknown: {_safe_error(exc)}. "
            "Do not reuse the run ID after an uncertain dispatch."
        )
        return 2
    except KeyboardInterrupt:
        _write_error(
            "Syntax check interrupted; outcome unknown. Do not reuse the run ID."
        )
        return 130
    except Exception as exc:
        _write_error(
            f"Syntax check failed ({type(exc).__name__}); outcome may be unknown."
        )
        return 2
    _write_line(f"Session ID: {report.session_ref.session_id}")
    for entry in report.files:
        if entry.valid:
            _write_line(f"{_safe_text(entry.name, single_line=True)}: valid syntax")
        else:
            _write_line(
                f"{_safe_text(entry.name, single_line=True)}: invalid syntax "
                f"(line {entry.line}, column {entry.column})"
            )
    _write_line(
        "No Python source was executed and no workspace files were changed."
    )
    return 0 if report.valid else 1


def _investigate(args: argparse.Namespace) -> int:
    """Two persisted no-tool turns around one fixed, owner-scoped literal search."""

    investigation_id = args.investigation_id or str(uuid4())
    _write_line(f"Investigation ID: {_safe_text(investigation_id, single_line=True)}")
    phase = "preflight"
    try:
        # Keep operator-supplied identifiers bounded, printable and stable.
        CoworkerTurnRequest(turn_id=investigation_id, question=args.question)
        handle = _open_coworker_handle(args)
        current = handle.require_local_owner()
        guard = SafetyGuard(current.workspace)
        identity = guard.project_root_identity
        if identity is None:
            raise CoworkerRuntimeUnavailable("investigation workspace is not bound")
        snapshots = read_explicit_evidence(
            current.workspace, tuple(args.files), guard=guard,
        )
        if not snapshots or len({item.relative_path for item in snapshots}) != len(snapshots):
            raise ValueError("investigation needs unique explicitly granted text files")
        descriptors = [
            {"path": item.relative_path, "sha256": item.sha256,
             "size_bytes": item.size_bytes}
            for item in snapshots
        ]
        # Stable, independent run IDs ensure that neither model phase can be
        # replayed after uncertain dispatch, including across process restart.
        plan_id = str(uuid5(UUID(current.coworker_id),
                            f"ash-crew-inspect:{investigation_id}:plan"))
        analysis_id = str(uuid5(UUID(current.coworker_id),
                                f"ash-crew-inspect:{investigation_id}:analysis"))
        phase = "plan"
        _write_line("Planning a bounded literal search (no file content sent yet).")
        plan = asyncio.run(handle.run_readonly_turn(
            CoworkerTurnRequest(
                turn_id=plan_id, question=args.question,
                evidence=json.dumps(descriptors, separators=(",", ":")),
                workspace_identity=identity,
            ), check_model=args.check_model, propose_inspection=True,
        ))
        _write_line(f"Plan run ID: {plan.session_ref.run_id}")
        _write_line(f"Plan session ID: {plan.session_ref.session_id}")
        _write_line(f"Plan request ID: {plan.request_id}")
        phase = "search"
        if plan.session_ref.run_id is None or plan.request_id is None:
            raise CoworkerDirectoryError("inspection plan lacks durable source IDs")
        proposal, matches = handle.execute_readonly_inspection(
            run_id=plan.session_ref.run_id,
            session_id=plan.session_ref.session_id,
            request_id=plan.request_id or "",
        )
        report = json.loads(matches)
        _write_line(f"Selected literal: {_safe_text(proposal.query, single_line=True)}")
        for filename in proposal.files:
            _write_line(f"Selected granted file: {_safe_text(filename, single_line=True)}")
        _write_line(f"Matching lines: {report['total_matching_lines']}")
        _write_line(f"Matches omitted: {report['matches_omitted']}")
        phase = "analysis"
        evidence = json.dumps({
            "tool": "read_only_literal_search_v1",
            "source_run_id": plan.session_ref.run_id,
            "source_response_sha256": proposal.response_sha256,
            "results": report,
        }, ensure_ascii=False, separators=(",", ":"))
        analysis = asyncio.run(handle.run_readonly_turn(
            CoworkerTurnRequest(
                turn_id=analysis_id,
                question=(
                    "Analyze these fixed, read-only, owner-granted literal search "
                    "results for the original question. Report grounded findings "
                    "and uncertainty. No tools or changes. Original question: "
                    + args.question
                ),
                evidence=evidence,
                workspace_identity=identity,
            ),
        ))
        _write_line("Investigation completed, no files changed.")
        _write_line(f"Analysis run ID: {analysis.session_ref.run_id}")
        _write_line(f"Analysis session ID: {analysis.session_ref.session_id}")
        _write_line(f"Analysis request ID: {analysis.request_id}")
        _write_line("Findings:")
        _write_line(_safe_text(analysis.response))
        return 0
    except KeyboardInterrupt:
        _write_error(
            f"Investigation interrupted during {phase}; dispatched model turns "
            "must not be retried with the same investigation ID."
        )
        return 130
    except (
        CoworkerDirectoryError, CoworkerNotFound, CoworkerRuntimeUnavailable,
        AshError, OSError, SafetyViolation, SessionStorageError, ScopedIOError,
        ValueError,
    ) as exc:
        _write_error(
            f"Investigation stopped at {phase}: {_safe_error(exc)}. "
            "Inspect Crew history; do not replay an already dispatched investigation."
        )
        return 2
    except Exception as exc:
        _write_error(
            f"Investigation stopped at {phase} ({type(exc).__name__}); "
            "outcome may be unknown. Inspect Crew history, never replay blindly."
        )
        return 2


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
        if args.crew_action == "ask" and args.propose_git_diff:
            if args.propose_syntax:
                raise ValueError("select only one model proposal kind")
            if not evidence_files:
                raise ValueError("Git diff proposal requires an explicit evidence grant")
            evidence = json.dumps(
                evidence_metadata_manifest(evidence_files),
                ensure_ascii=False, separators=(",", ":"),
            )
        else:
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
    is_chat = args.crew_action == "chat"
    _write_line(
        "Status: continuing bounded read-only conversation."
        if is_chat else "Status: attempting one read-only model turn."
    )
    try:
        result = asyncio.run(
            handle.run_chat_turn(
                request, fresh=args.new, check_model=args.check_model,
            )
            if is_chat
            else handle.run_readonly_turn(
                request, check_model=args.check_model,
                propose_syntax=args.propose_syntax,
                propose_git_diff=args.propose_git_diff,
            )
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
        if action == "revise":
            if args.clear_success_criteria and args.success_criterion is not None:
                raise ValueError(
                    "cannot supply success criteria and clear them together"
                )
            if args.clear_evidence and args.evidence is not None:
                raise ValueError(
                    "cannot supply evidence and clear it together"
                )
            responsibility = handle.revise_responsibility(
                args.responsibility_id,
                expected_revision=args.expected_revision,
                scope=args.scope,
                objective=args.objective,
                success_criteria=(
                    () if args.clear_success_criteria
                    else args.success_criterion
                ),
                evidence_paths=(
                    () if args.clear_evidence else args.evidence
                ),
            )
            _write_line("Responsibility revision saved.")
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
                    draft_correction=args.draft_correction,
                    investigate=args.investigate,
                )
            )
            _render_model_turn(result)
            if args.investigate:
                _write_line("Investigation plan persisted; running bounded search and analysis.")
                analysis = asyncio.run(handle.analyze_responsibility_investigation(result))
                _render_model_turn(analysis)
            return 0
        if action == "timeline":
            entries = handle.responsibility_timeline(
                args.responsibility_id, limit=args.limit,
            )
            if not entries:
                _write_line("No recorded occurrences for this responsibility.")
                return 0
            for entry in entries:
                run = entry.run
                _write_line(
                    f"Occurrence: {_safe_text(run.event_key, single_line=True)} "
                    f"({run.trigger_type})"
                )
                _write_line(f"Plan/run status: {run.status}")
                _write_line(f"Run ID: {run.run_id}")
                if run.session_id:
                    _write_line(f"Run session ID: {run.session_id}")
                _write_line(f"Workflow: {entry.workflow}")
                if run.status == "claimed":
                    _write_line(
                        "Initial outcome unknown; claim consumed. No automatic replay."
                    )
                if run.skip_reason:
                    _write_line(f"Skip reason: {run.skip_reason}")
                if entry.workflow == "investigation":
                    _write_line(f"Analysis status: {entry.analysis_status}")
                    if entry.analysis_run_id:
                        _write_line(f"Analysis run ID: {entry.analysis_run_id}")
                    if entry.analysis_session_id:
                        _write_line(
                            f"Analysis session ID: {entry.analysis_session_id}"
                        )
                    if entry.analysis_status != "completed":
                        _write_line(
                            "Investigation not confirmed complete; inspect session "
                            "history before planning new work."
                        )
                if entry.approval_id is not None:
                    _write_line(f"Correction approval ID: {entry.approval_id}")
                    _write_line(f"Correction approval status: {entry.approval_status}")
                    _write_line(
                        "Approval state is separate from investigation completion."
                    )
                _write_line("")
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
        if action == "run" and args.investigate:
            _write_error(
                "Investigation incomplete or outcome unknown; the first model "
                "turn may already be consumed. Inspect responsibility runs and "
                f"coworker history: {_safe_error(exc)}"
            )
        elif action == "run" and any(
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
                investigate=args.investigate,
            )
        )
        _render_model_turn(result)
        if args.investigate:
            _write_line("Signed-event investigation plan persisted; analyzing search results.")
            analysis = asyncio.run(handle.analyze_responsibility_investigation(result))
            _render_model_turn(analysis)
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
        if args.investigate:
            _write_error(
                "Signed-event investigation incomplete; the signed event may "
                f"already be consumed. Inspect Crew history: {_safe_error(exc)}"
            )
        else:
            _write_error(f"Blocked before model dispatch: {_safe_error(exc)}")
        return 2
    except (
        CoworkerDirectoryError,
        AshError,
        OSError,
        SafetyViolation,
        SessionStorageError,
    ) as exc:
        if args.investigate:
            _write_error(
                "Signed-event investigation incomplete or outcome unknown. "
                "Inspect responsibility runs and coworker history: "
                f"{_safe_error(exc)}"
            )
        elif "evidence" in str(exc).casefold():
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


def _intake(args: argparse.Namespace) -> int:
    """Owner-driven recovery surface; never silently retry an ambiguous event."""

    try:
        handle = _open_coworker_handle(args)
        action = args.crew_intake_action
        if action == "accept":
            receipt = handle.accept_signed_event(
                _read_event_envelope(args.input),
                key_file=args.key_file, responsibility_id=args.responsibility_id,
                investigate=args.investigate,
                draft_correction=args.draft_correction,
            )
            _write_line(f"Accepted event: {_safe_text(receipt.event_key, single_line=True)}")
            _write_line("Durably stored; no model run was dispatched.")
            return 0
        if action == "list":
            receipts = handle.list_accepted_events(limit=args.limit)
            if not receipts:
                _write_line("No accepted event receipts found.")
            for receipt in receipts:
                _write_line(
                    f"Event: {_safe_text(receipt.event_key, single_line=True)} "
                    f"Responsibility: {receipt.responsibility_id} "
                    f"Status: {receipt.status}"
                )
                if receipt.run_id is not None:
                    _write_line(f"Run ID: {receipt.run_id}")
                if receipt.session_id is not None:
                    _write_line(f"Session ID: {receipt.session_id}")
                if receipt.analysis_session_id is not None:
                    _write_line(f"Analysis session ID: {receipt.analysis_session_id}")
            return 0
        if action == "dispatch":
            result = asyncio.run(handle.dispatch_accepted_event(
                responsibility_id=args.responsibility_id,
                event_key=args.event_key, key_file=args.key_file,
            ))
            _write_line("Durably accepted event dispatched once.")
            _render_model_turn(result.plan)
            if result.analysis is not None:
                _write_line("Verified investigation analysis:")
                _render_model_turn(result.analysis)
            return 0
        raise ValueError("unsupported accepted event action")
    except KeyboardInterrupt:
        _write_error(
            "Event intake interrupted; dispatch outcome may be unknown. "
            "Inspect intake list and responsibility timeline before any new action."
        )
        return 130
    except (
        CoworkerDirectoryError, CoworkerNotFound, CoworkerRuntimeUnavailable,
        CrewEventError, AshError, OSError, SafetyViolation,
        SessionStorageError, ValueError,
    ) as exc:
        _write_error(
            "Event intake blocked or incomplete. Inspect durable receipts "
            f"before retrying: {_safe_error(exc)}"
        )
        return 2
    except Exception as exc:
        _write_error(
            f"Event intake outcome unknown ({type(exc).__name__}); inspect receipts."
        )
        return 2


def _listen(args: argparse.Namespace) -> int:
    """Bounded local HTTP transport into the existing signed-event dispatcher."""

    if not 1 <= args.port <= 65535:
        _write_error("Crew ingress port must be between 1 and 65535.")
        return 2
    if args.draft_correction and args.investigate:
        _write_error("Choose investigation or correction drafting, not both.")
        return 2
    try:
        handle = _open_coworker_handle(args)
        current = handle.require_local_owner()
        if args.draft_correction and current.profile_name != "atlas":
            raise CoworkerRuntimeUnavailable("correction drafting requires Atlas")
        responsibility = handle.get_responsibility(args.responsibility_id)
        if responsibility.status != "active":
            raise CoworkerRuntimeUnavailable("event responsibility is not active")
        validate_crew_event_key_file(args.key_file, workspace=current.workspace)

        async def receive(envelope: bytes) -> CrewIngressReceipt:
            if args.store_only:
                try:
                    stored = handle.accept_signed_event(
                        envelope, key_file=args.key_file,
                        responsibility_id=args.responsibility_id,
                        investigate=args.investigate,
                        draft_correction=args.draft_correction,
                    )
                    return CrewIngressReceipt(
                        run_id="", session_id="", request_id=None,
                        accepted_event_key=stored.event_key,
                    )
                except CrewEventError as exc:
                    raise CrewIngressRefusal(
                        403, "invalid_signature_or_event",
                    ) from exc
                except CoworkerRuntimeUnavailable as exc:
                    message = str(exc).casefold()
                    duplicate = "already" in message or "duplicate" in message
                    raise CrewIngressRefusal(
                        409 if duplicate else 422,
                        "duplicate_or_claimed" if duplicate
                        else "responsibility_unavailable",
                    ) from exc
            try:
                result = await handle.run_signed_event(
                    envelope, key_file=args.key_file,
                    responsibility_id=args.responsibility_id,
                    investigate=args.investigate,
                    draft_correction=args.draft_correction,
                )
            except CrewEventError as exc:
                raise CrewIngressRefusal(403, "invalid_signature_or_event") from exc
            except CoworkerModelReadinessError as exc:
                raise CrewIngressRefusal(
                    503 if not exc.provider_request_attempted else 500,
                    "model_unavailable" if not exc.provider_request_attempted
                    else "dispatch_outcome_unknown",
                ) from exc
            except CoworkerRuntimeUnavailable as exc:
                message = str(exc).casefold()
                consumed = "already claimed" in message or "already used" in message
                invalid_scope = any(marker in message for marker in (
                    "not active", "paused", "target does not match",
                ))
                raise CrewIngressRefusal(
                    409 if consumed else 422 if invalid_scope else 500,
                    "duplicate_or_claimed" if consumed
                    else "responsibility_unavailable" if invalid_scope
                    else "dispatch_outcome_unknown",
                ) from exc
            # A completed provider response is not an approved or applied effect.
            analysis_session_id = None
            if args.investigate:
                analysis = await handle.analyze_responsibility_investigation(result)
                analysis_session_id = analysis.session_ref.session_id
            return CrewIngressReceipt(
                run_id=result.session_ref.run_id or "",
                session_id=result.session_ref.session_id,
                request_id=result.request_id,
                analysis_session_id=analysis_session_id,
            )

        def announce_bound(bound_port: int) -> None:
            _write_line(
                f"Signed Crew event ingress: http://127.0.0.1:{bound_port}/crew/event"
            )
            if args.store_only:
                _write_line(
                    "Loopback only. HTTP 202 means signed bytes were committed "
                    "durably, with no model dispatch. Run crew intake dispatch "
                    "explicitly after inspecting the backlog."
                )
            else:
                _write_line(
                    "Loopback only. Signed events are processed serially; HTTP success "
                    "means the model turn finished, not that a correction was approved. "
                    "An interrupted request may have an unknown outcome. Inspect Crew history."
                )
            sys.stdout.flush()

        asyncio.run(serve_crew_ingress(
            receive, port=args.port, ready=announce_bound,
        ))
        return 0
    except KeyboardInterrupt:
        _write_error(
            "Ingress stopped. An in-flight event may have been consumed; "
            "inspect Crew responsibility runs before retrying."
        )
        return 130
    except (
        CoworkerDirectoryError, CoworkerNotFound, CoworkerRuntimeUnavailable,
        CrewEventError, OSError, SafetyViolation, SessionStorageError, ValueError,
    ) as exc:
        _write_error(f"Signed Crew ingress unavailable: {_safe_error(exc)}")
        return 2
    except Exception as exc:
        _write_error(f"Signed Crew ingress unavailable ({type(exc).__name__}).")
        return 2


def _tick(args: argparse.Namespace) -> int:
    try:
        handle = _open_coworker_handle(args)
        result = asyncio.run(
            handle.run_due_model_responsibilities_once(
                check_model=args.check_model,
                only_if_changed=args.only_if_changed,
                investigate=args.investigate,
            )
        )
        if result.status == "idle":
            _write_line("No due scheduled responsibility.")
            return 0
        if result.status == "skipped":
            _write_line(
                "Status: skipped (granted evidence unchanged since last completed run)."
                if result.skip_reason == "evidence_unchanged"
                else "Status: skipped after the configured misfire grace."
            )
            _write_line(f"Responsibility ID: {result.responsibility_id}")
            _write_line(f"Event key: {_safe_text(result.event_key or '', single_line=True)}")
            return 0
        if result.turn is None:
            raise CoworkerDirectoryError("completed model schedule tick has no turn")
        _render_model_turn(result.turn)
        if result.analysis is not None:
            _write_line("Scheduled investigation analysis:")
            _render_model_turn(result.analysis)
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
        if args.investigate:
            _write_error(
                "Scheduled investigation may be partial: the due occurrence "
                "may already have been claimed and advanced. Inspect responsibility "
                f"runs and coworker history: {_safe_error(exc)}"
            )
        elif "evidence" in str(exc).casefold():
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
    if args.investigate and args.only_if_changed:
        _write_error(
            "Investigation and change-only monitoring cannot run together."
        )
        return 2
    try:
        handle = _open_coworker_handle(args)
        if handle.require_owner_binding().lifecycle_status == "archived":
            _write_error("Archived coworkers cannot start a scheduled worker.")
            return 2
        asyncio.run(_run_crew_worker(
            handle, args.poll_seconds, only_if_changed=args.only_if_changed,
            investigate=args.investigate,
        ))
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
    only_if_changed: bool = False,
    investigate: bool = False,
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
                    result = await handle.run_due_model_responsibilities_once(
                        only_if_changed=only_if_changed,
                        investigate=investigate,
                    )
                    if result.status == "completed":
                        if result.turn is None:
                            raise CoworkerDirectoryError(
                                "completed scheduled turn has no persisted result"
                            )
                        if result.analysis is not None:
                            _write_line(
                                "Scheduled investigation analysis saved: "
                                f"{result.analysis.session_ref.session_id}"
                            )
                        _write_line(
                            "Scheduled Crew turn completed; "
                            f"responsibility {result.responsibility_id}, "
                            f"event {_safe_text(result.event_key or '', single_line=True)}. "
                            "Inspect Crew inbox for details."
                        )
                    elif result.status == "skipped":
                        _write_line(
                            (
                                "Scheduled Crew occurrence skipped: evidence unchanged; "
                                if result.skip_reason == "evidence_unchanged"
                                else "Scheduled Crew occurrence skipped after misfire grace; "
                            )
                            + f"responsibility {result.responsibility_id}."
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
    _write_line(f"Revision: {responsibility.revision}")
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
