from __future__ import annotations

import argparse
from pathlib import Path


def configure_parser(parser: argparse.ArgumentParser, version: str) -> None:
    parser.add_argument("--version", action="version", version=f"ash {version}")
    parser.add_argument(
        "--profile",
        help="Use a named user profile for this invocation (overrides the active profile)",
    )
    subparsers = parser.add_subparsers(dest="command")
    setup_parser = subparsers.add_parser(
        "setup",
        help="Configure Ash (model, credentials, web search, browser)",
    )
    setup_parser.add_argument(
        "section",
        nargs="?",
        choices=[
            "model",
            "fallbacks",
            "providers",
            "web",
            "browser",
            "status",
            "all",
        ],
        help=(
            "Section to configure; use fallbacks for the model fallback chain "
            "(providers is a compatibility alias)"
        ),
    )
    setup_parser.add_argument(
        "--quick",
        action="store_true",
        help="Skip optional configuration",
    )
    setup_parser.add_argument(
        "--non-interactive",
        action="store_true",
        help="Run in non-interactive mode (fail if input needed)",
    )
    setup_parser.add_argument(
        "--json",
        action="store_true",
        help="Render setup status as secret-free JSON (status section only)",
    )
    profile_parser = subparsers.add_parser(
        "profile", help="List and select isolated provider/configuration profiles"
    )
    profile_subparsers = profile_parser.add_subparsers(
        dest="profile_action", required=True
    )
    profile_list = profile_subparsers.add_parser("list", help="List available profiles")
    profile_list.add_argument("--json", action="store_true")
    profile_add = profile_subparsers.add_parser("add", help="Create an empty profile")
    profile_add.add_argument("name")
    profile_use = profile_subparsers.add_parser("use", help="Persist the active profile")
    profile_use.add_argument("name")
    profile_show = profile_subparsers.add_parser("show", help="Show one profile without secrets")
    profile_show.add_argument("name")
    profile_show.add_argument("--json", action="store_true")
    profile_remove = profile_subparsers.add_parser(
        "remove", help="Remove a profile and its user-owned state"
    )
    profile_remove.add_argument("name")
    profile_remove.add_argument("--yes", action="store_true")
    crew_parser = subparsers.add_parser(
        "crew",
        help="Manage profile-scoped coworkers, responsibilities, and file approvals",
        description=(
            "Create and ask profile-scoped coworkers. Responsibility turns use one "
            "no-tool model request. Signed events use a local HMAC key file, and "
            "scheduled work runs through ash crew tick or an explicit crew worker."
        ),
    )
    crew_subparsers = crew_parser.add_subparsers(
        dest="crew_action", required=True
    )
    crew_create = crew_subparsers.add_parser(
        "create", help="Bind a coworker to an existing named profile and workspace"
    )
    crew_create.add_argument("--owner", required=True, help="Explicit local owner label")
    crew_create.add_argument("--name", required=True, help="Coworker display name")
    crew_create.add_argument(
        "--profile",
        dest="crew_profile",
        required=True,
        help="Existing named Ash profile for this coworker",
    )
    crew_create.add_argument(
        "--workspace",
        required=True,
        type=Path,
        help="Existing workspace directory bound to this coworker",
    )
    crew_list = crew_subparsers.add_parser(
        "list", help="List coworkers owned by the current local UID"
    )
    crew_list.add_argument("--owner", required=True, help="Explicit local owner label")
    crew_show = crew_subparsers.add_parser(
        "show", help="Inspect one coworker, its responsibilities, and recent activity"
    )
    crew_show.add_argument("--owner", required=True, help="Explicit local owner label")
    crew_show.add_argument("--coworker-id", required=True, help="Immutable coworker ID")
    crew_show.add_argument(
        "--limit", type=int, default=5,
        help="Maximum responsibility summaries and activity entries (1-20)",
    )
    crew_brief = crew_subparsers.add_parser(
        "brief", help="Show, set or clear one coworker's owner-curated context"
    )
    brief_actions = crew_brief.add_subparsers(
        dest="crew_brief_action", required=True
    )
    for action in ("show", "set", "clear", "adopt"):
        item = brief_actions.add_parser(
            action, help=f"{action.title()} the owner-curated coworker brief"
        )
        item.add_argument("--owner", required=True)
        item.add_argument("--coworker-id", required=True)
        if action == "set":
            item.add_argument(
                "--text", required=True,
                help="Owner-authored context; avoid shell-history secrets",
            )
        if action == "adopt":
            for field in ("run-id", "session-id", "request-id"):
                item.add_argument("--" + field, required=True)
            item.add_argument(
                "--excerpt", required=True,
                help="Exact single-line substring of the completed model response",
            )
    crew_inbox = crew_subparsers.add_parser(
        "inbox", help="Show recent local run and approval activity"
    )
    crew_inbox.add_argument("--owner", required=True, help="Explicit local owner label")
    crew_inbox.add_argument(
        "--limit",
        type=int,
        default=20,
        help="Maximum number of newest activity entries to show (1-100)",
    )
    crew_inbox.add_argument(
        "--include-skipped",
        action="store_true",
        help="Include scheduled runs skipped after their misfire grace period",
    )
    for action, help_text in (
        ("pause", "Stop new coworker work while retaining history and decisions"),
        ("resume", "Resume a paused coworker"),
        ("archive", "Permanently stop new work without deleting coworker data"),
    ):
        lifecycle = crew_subparsers.add_parser(action, help=help_text)
        lifecycle.add_argument("--owner", required=True, help="Explicit local owner label")
        lifecycle.add_argument("--coworker-id", required=True, help="Immutable coworker ID")
    crew_ask = crew_subparsers.add_parser(
        "ask",
        help="Ask one read-only question with explicitly listed workspace files",
    )
    crew_ask.add_argument("--owner", required=True, help="Explicit local owner label")
    crew_ask.add_argument("--coworker-id", required=True, help="Immutable coworker ID")
    crew_ask.add_argument("--question", required=True, help="Question for this one-shot turn")
    crew_ask.add_argument(
        "--turn-id",
        help="Stable printable one-shot ID; generated when omitted",
    )
    crew_ask.add_argument(
        "--evidence",
        action="append",
        default=[],
        metavar="WORKSPACE_RELATIVE_PATH",
        help="Explicit read-only evidence file; repeat for each file",
    )
    crew_ask.add_argument(
        "--propose-syntax", action="store_true",
        help="Ask the model to choose one syntax check from explicitly granted root Python files",
    )
    crew_ask.add_argument(
        "--check-model",
        action="store_true",
        help="Probe the selected profile model before claiming this turn",
    )
    crew_chat = crew_subparsers.add_parser(
        "chat", help="Continue a bounded conversation with prior completed chat turns"
    )
    crew_chat.add_argument("--owner", required=True, help="Explicit local owner label")
    crew_chat.add_argument("--coworker-id", required=True, help="Immutable coworker ID")
    crew_chat.add_argument("--message", dest="question", required=True)
    crew_chat.add_argument("--turn-id", help="One-shot turn ID; generated when omitted")
    crew_chat.add_argument(
        "--new", action="store_true",
        help="Start a new conversation without earlier chat context",
    )
    crew_chat.add_argument(
        "--evidence", action="append", default=[],
        metavar="WORKSPACE_RELATIVE_PATH",
        help="Grant one workspace file for this turn only; repeat if needed",
    )
    crew_chat.add_argument(
        "--check-model", action="store_true",
        help="Probe the profile model before claiming this turn",
    )
    crew_syntax = crew_subparsers.add_parser(
        "syntax", help="Run an owner-selected, non-executing Python syntax check in Docker"
    )
    crew_syntax.add_argument("--owner", required=True)
    crew_syntax.add_argument("--coworker-id", required=True)
    crew_syntax.add_argument("--run-id", help="One-shot run ID; generated when omitted")
    crew_syntax.add_argument(
        "--file", dest="files", action="append", default=[],
        metavar="WORKSPACE_RELATIVE_PYTHON_PATH",
        help="Explicit workspace-root Python file to check; repeat as needed",
    )
    for field in ("source-run-id", "source-session-id", "source-request-id"):
        crew_syntax.add_argument(
            "--" + field,
            help="Completed read-only model proposal source; use all three IDs together",
        )
    crew_model_check = crew_subparsers.add_parser(
        "check-model",
        help="Send one synthetic readiness probe without creating a Crew run",
    )
    crew_model_check.add_argument("--owner", required=True, help="Explicit local owner label")
    crew_model_check.add_argument("--coworker-id", required=True, help="Immutable coworker ID")
    crew_history = crew_subparsers.add_parser(
        "history", help="Show recent one-shot turn outcomes after reopening"
    )
    crew_history.add_argument("--owner", required=True, help="Explicit local owner label")
    crew_history.add_argument("--coworker-id", required=True, help="Immutable coworker ID")
    crew_history.add_argument(
        "--limit", type=int, default=10, help="Number of recent outcomes to show (1-100)"
    )
    crew_responsibility = crew_subparsers.add_parser(
        "responsibility",
        help="Create, inspect, and control one coworker responsibility",
    )
    crew_responsibility_subparsers = crew_responsibility.add_subparsers(
        dest="crew_responsibility_action",
        required=True,
    )
    responsibility_create = crew_responsibility_subparsers.add_parser(
        "create", help="Create a bounded owner-controlled responsibility"
    )
    responsibility_create.add_argument("--owner", required=True)
    responsibility_create.add_argument("--coworker-id", required=True)
    responsibility_create.add_argument("--responsibility-id", required=True)
    responsibility_create.add_argument("--scope", required=True)
    responsibility_create.add_argument("--objective", required=True)
    responsibility_create.add_argument(
        "--success-criterion", action="append", required=True
    )
    responsibility_create.add_argument(
        "--evidence",
        action="append",
        default=[],
        metavar="WORKSPACE_RELATIVE_PATH",
        help="Explicit read-only evidence file; repeat for each file",
    )
    responsibility_create.add_argument("--at")
    responsibility_create.add_argument("--every")
    responsibility_create.add_argument("--cron")
    responsibility_create.add_argument("--timezone", default="UTC")
    responsibility_create.add_argument("--misfire-grace-seconds", type=int, default=86_400)
    responsibility_list = crew_responsibility_subparsers.add_parser(
        "list", help="List responsibilities for one coworker"
    )
    responsibility_list.add_argument("--owner", required=True)
    responsibility_list.add_argument("--coworker-id", required=True)
    for action, help_text in (
        ("pause", "Pause one responsibility before a later trigger claim"),
        ("resume", "Resume one paused responsibility"),
        ("complete", "Complete one responsibility permanently"),
    ):
        command = crew_responsibility_subparsers.add_parser(action, help=help_text)
        command.add_argument("--owner", required=True)
        command.add_argument("--coworker-id", required=True)
        command.add_argument("--responsibility-id", required=True)
    responsibility_run = crew_responsibility_subparsers.add_parser(
        "run", help="Run one active responsibility from a local owner trigger"
    )
    responsibility_run.add_argument("--owner", required=True)
    responsibility_run.add_argument("--coworker-id", required=True)
    responsibility_run.add_argument("--responsibility-id", required=True)
    responsibility_run.add_argument("--event-id", required=True)
    responsibility_run.add_argument(
        "--check-model",
        action="store_true",
        help="Probe the selected profile model before claiming this run",
    )
    responsibility_runs = crew_responsibility_subparsers.add_parser(
        "runs", help="Inspect durable trigger claims and persisted model responses"
    )
    responsibility_runs.add_argument("--owner", required=True)
    responsibility_runs.add_argument("--coworker-id", required=True)
    responsibility_runs.add_argument("--responsibility-id", required=True)
    responsibility_runs.add_argument("--limit", type=int, default=10)
    crew_event = crew_subparsers.add_parser(
        "event", help="Admit one signed event from stdin or a local file"
    )
    crew_event.add_argument("--owner", required=True)
    crew_event.add_argument("--coworker-id", required=True)
    crew_event.add_argument("--responsibility-id", required=True)
    crew_event.add_argument(
        "--key-file",
        required=True,
        type=Path,
        help="Owner-provisioned 0600 HMAC key file outside the coworker workspace",
    )
    crew_event.add_argument(
        "--input",
        default="-",
        help="Signed JSON file path, or - to read stdin",
    )
    crew_event.add_argument(
        "--check-model",
        action="store_true",
        help="Probe the selected profile model before claiming this event",
    )
    crew_event.add_argument(
        "--draft-correction",
        action="store_true",
        help="Ask Atlas to return a strict, owner-reviewable file correction JSON",
    )
    crew_worker = crew_subparsers.add_parser(
        "worker", help="Poll one coworker's due schedules until stopped"
    )
    crew_worker.add_argument("--owner", required=True, help="Explicit local owner label")
    crew_worker.add_argument("--coworker-id", required=True, help="Immutable coworker ID")
    crew_worker.add_argument(
        "--poll-seconds", type=int, default=60,
        help="Seconds between due checks (10-3600, default: 60)",
    )
    crew_tick = crew_subparsers.add_parser(
        "tick", help="Run at most one due scheduled responsibility and exit"
    )
    crew_tick.add_argument("--owner", required=True)
    crew_tick.add_argument("--coworker-id", required=True)
    crew_tick.add_argument(
        "--check-model",
        action="store_true",
        help="Probe the selected profile model before claiming a due run",
    )
    crew_propose = crew_subparsers.add_parser(
        "propose",
        help="Stage one exact owner-file or Atlas-drafted correction for review",
    )
    crew_propose.add_argument("--owner", required=True, help="Explicit local owner label")
    crew_propose.add_argument("--coworker-id", required=True, help="Immutable Atlas coworker ID")
    crew_propose.add_argument("--run-id", required=True, help="Completed Atlas run ID")
    crew_propose.add_argument("--session-id", required=True, help="Completed Atlas session ID")
    crew_propose.add_argument("--request-id", required=True, help="Completed Atlas request ID")
    crew_propose.add_argument("--target", required=True, metavar="WORKSPACE_RELATIVE_PATH")
    crew_propose_source = crew_propose.add_mutually_exclusive_group(required=True)
    crew_propose_source.add_argument(
        "--replacement-file",
        metavar="WORKSPACE_RELATIVE_PATH",
        help="Owner-supplied UTF-8 text file under the coworker's workspace",
    )
    crew_propose_source.add_argument(
        "--from-response",
        action="store_true",
        help="Stage a validated exact correction drafted by Atlas from granted evidence",
    )
    crew_approvals = crew_subparsers.add_parser(
        "approvals", help="List durable file approval states"
    )
    crew_approvals.add_argument("--owner", required=True, help="Explicit local owner label")
    crew_approvals.add_argument("--coworker-id", required=True, help="Immutable coworker ID")
    crew_approvals.add_argument("--limit", type=int, default=20)
    crew_approval = crew_subparsers.add_parser(
        "approval", help="Inspect one durable file approval after restart"
    )
    crew_approval_subparsers = crew_approval.add_subparsers(
        dest="crew_approval_action", required=True
    )
    crew_approval_show = crew_approval_subparsers.add_parser(
        "show", help="Show the exact file diff, decision history, and observed outcome"
    )
    crew_approval_show.add_argument("--owner", required=True, help="Explicit local owner label")
    crew_approval_show.add_argument("--coworker-id", required=True, help="Immutable coworker ID")
    crew_approval_show.add_argument("--approval-id", required=True, help="Immutable approval ID")
    for action, help_text in (
        ("approve", "Approve one exact pending file correction at a local TTY"),
        ("deny", "Deny one exact pending file correction at a local TTY"),
        ("revoke", "Revoke an approved correction before apply"),
        ("apply", "Apply one approved correction exactly once at a local TTY"),
    ):
        command = crew_subparsers.add_parser(action, help=help_text)
        command.add_argument("--owner", required=True, help="Explicit local owner label")
        command.add_argument("--coworker-id", required=True, help="Immutable coworker ID")
        command.add_argument("--approval-id", required=True, help="Immutable approval ID")
    providers_parser = subparsers.add_parser(
        "providers",
        help="List provider routes or test the active/provider model route",
    )
    providers_subparsers = providers_parser.add_subparsers(
        dest="providers_action", required=True
    )
    providers_list = providers_subparsers.add_parser(
        "list", help="List built-in provider routes without network access"
    )
    providers_list.add_argument("--json", action="store_true")
    providers_test = providers_subparsers.add_parser(
        "test",
        help="Verify a configured route, discover its models, and run a minimal completion",
    )
    providers_test.add_argument(
        "model",
        nargs="?",
        help="Optional provider/model override; defaults to ASH_MODEL",
    )
    providers_test.add_argument("--json", action="store_true")
    providers_test.add_argument("--timeout", type=float, default=10.0)
    providers_test.add_argument(
        "--tools",
        action="store_true",
        help="Also verify one harmless native tool call on a local vLLM or LM Studio model",
    )
    auth_parser = subparsers.add_parser(
        "auth",
        help="Manage user-owned provider authentication sessions",
    )
    auth_providers = auth_parser.add_subparsers(dest="auth_provider", required=True)
    chatgpt_auth = auth_providers.add_parser(
        "chatgpt",
        help="Manage optional OpenAI ChatGPT-plan authentication",
    )
    chatgpt_actions = chatgpt_auth.add_subparsers(
        dest="chatgpt_auth_action",
        required=True,
    )
    chatgpt_status = chatgpt_actions.add_parser(
        "status", help="Show ChatGPT authentication and active-account status"
    )
    chatgpt_status.add_argument("--json", action="store_true")
    chatgpt_login = chatgpt_actions.add_parser(
        "login",
        help="Continue with ChatGPT using a new or saved registration",
    )
    chatgpt_login.add_argument(
        "client_id",
        nargs="?",
        help="Saved issued client ID to reauthorize; omit to register another account",
    )
    chatgpt_login.add_argument("--json", action="store_true")
    chatgpt_logout = chatgpt_actions.add_parser(
        "logout", help="Remove the active ChatGPT authentication session"
    )
    chatgpt_logout.add_argument("--json", action="store_true")
    chatgpt_accounts = chatgpt_actions.add_parser(
        "accounts", help="List saved ChatGPT account registrations"
    )
    chatgpt_accounts.add_argument("--json", action="store_true")
    chatgpt_use = chatgpt_actions.add_parser(
        "use", help="Select which saved ChatGPT account Ash should use"
    )
    chatgpt_use.add_argument("client_id")
    chatgpt_use.add_argument("--json", action="store_true")
    chatgpt_models = chatgpt_actions.add_parser(
        "models", help="List models available through ChatGPT authentication"
    )
    chatgpt_models.add_argument("--json", action="store_true")
    doctor_parser = subparsers.add_parser(
        "doctor", help="Diagnose local setup and runtime dependencies"
    )
    doctor_parser.add_argument("--json", action="store_true", dest="json_output")
    doctor_parser.add_argument(
        "--connect",
        action="store_true",
        help="Also probe the configured local or API endpoint",
    )
    sandbox_parser = subparsers.add_parser(
        "sandbox", help="Inspect sandbox enforcement or build the baseline image"
    )
    sandbox_subparsers = sandbox_parser.add_subparsers(
        dest="sandbox_action", required=True
    )
    sandbox_status_parser = sandbox_subparsers.add_parser(
        "status", help="Show the selected sandbox backend and enforcement readiness"
    )
    sandbox_status_parser.add_argument("--json", action="store_true")
    sandbox_build_parser = sandbox_subparsers.add_parser(
        "build", help="Build the local Docker sandbox image"
    )
    sandbox_build_parser.add_argument("--image", default=None)
    config_parser = subparsers.add_parser(
        "config", help="Inspect Ash configuration sources"
    )
    config_subparsers = config_parser.add_subparsers(
        dest="config_action", required=True
    )
    config_explain = config_subparsers.add_parser(
        "explain", help="Show effective config values and their sources"
    )
    config_explain.add_argument("--json", action="store_true")
    marketplace_parser = subparsers.add_parser(
        "marketplace",
        help="Manage user-owned signed plugin marketplace catalogs",
    )
    marketplace_subparsers = marketplace_parser.add_subparsers(
        dest="marketplace_action", required=True
    )
    marketplace_list = marketplace_subparsers.add_parser(
        "list", help="List registered publisher catalogs without network access"
    )
    marketplace_list.add_argument("--json", action="store_true")
    marketplace_add = marketplace_subparsers.add_parser(
        "add", help="Verify and register one signed catalog v2 source"
    )
    marketplace_add.add_argument("source")
    marketplace_add.add_argument("--replace", action="store_true")
    marketplace_add.add_argument("--json", action="store_true")
    marketplace_remove = marketplace_subparsers.add_parser(
        "remove", help="Remove one registered publisher catalog"
    )
    marketplace_remove.add_argument("publisher")
    marketplace_remove.add_argument("--json", action="store_true")
    diff_mode_parser = subparsers.add_parser(
        "diff-mode",
        help="Select approval diff preview layout",
    )
    diff_mode_parser.add_argument("mode", choices=["unified", "side-by-side"])
    ollama_parser = subparsers.add_parser(
        "ollama",
        help="Manage local Ollama models",
    )
    ollama_subparsers = ollama_parser.add_subparsers(
        dest="ollama_action", required=True
    )
    ollama_pull = ollama_subparsers.add_parser(
        "pull", help="Download one model through the local Ollama service"
    )
    ollama_pull.add_argument("model")
    ollama_pull.add_argument(
        "--timeout",
        type=int,
        default=1800,
        help="Pull timeout in seconds",
    )
    trust_parser = subparsers.add_parser(
        "trust", help="Inspect or change project extension trust"
    )
    trust_parser.add_argument(
        "action",
        choices=["status", "add", "remove"],
        help="status inspects trust; add trusts the path; remove revokes trust",
    )
    trust_parser.add_argument("path", nargs="?", type=Path, default=Path.cwd())
    reset_parser = subparsers.add_parser(
        "reset", help="Selectively remove Ash local configuration or data"
    )
    reset_parser.add_argument(
        "--config",
        action="store_true",
        help="Remove default-profile config, trust decisions, and permission grants",
    )
    reset_parser.add_argument(
        "--sessions",
        action="store_true",
        help="Remove the default-profile session/database directory",
    )
    reset_parser.add_argument(
        "--cache",
        action="store_true",
        help="Remove default-profile caches, history, and local memory indexes",
    )
    reset_parser.add_argument(
        "--all",
        action="store_true",
        help=(
            "Select config, sessions, and cache together; named profiles and "
            "installed extensions are retained"
        ),
    )
    reset_parser.add_argument("--yes", action="store_true")
    update_parser = subparsers.add_parser(
        "update", help="Check GitHub for a newer Ash release"
    )
    update_parser.add_argument("--json", action="store_true")
    update_parser.add_argument(
        "--apply",
        action="store_true",
        help="Install the latest published Ash release through the managed installer",
    )
    repair_parser = subparsers.add_parser(
        "repair",
        help="Reinstall the exact current immutable Ash release",
    )
    repair_parser.add_argument("--json", action="store_true")
    storage_parser = subparsers.add_parser(
        "storage", help="Check, back up, restore, or maintain the session database"
    )
    storage_subparsers = storage_parser.add_subparsers(
        dest="storage_action", required=True
    )
    storage_check = storage_subparsers.add_parser(
        "check", help="Validate session database integrity and report storage state"
    )
    storage_check.add_argument("--json", action="store_true")
    storage_backup = storage_subparsers.add_parser(
        "backup", help="Create a consistent backup of the session database"
    )
    storage_backup.add_argument("destination", nargs="?", type=Path)
    storage_restore = storage_subparsers.add_parser(
        "restore", help="Replace the session database from a validated backup"
    )
    storage_restore.add_argument("backup", type=Path)
    storage_restore.add_argument("--yes", action="store_true")
    storage_redact = storage_subparsers.add_parser(
        "redact-turn-inputs",
        help="Replace a bounded batch of legacy prompt text in one session's journal",
    )
    storage_redact.add_argument(
        "--session",
        required=True,
        dest="session_id",
        help="Session whose turn-journal input values will be replaced",
    )
    storage_redact.add_argument("--yes", action="store_true")
    storage_debug = storage_subparsers.add_parser(
        "debug-bundle", help="Create a bounded, redacted diagnostics bundle"
    )
    storage_debug.add_argument("destination", nargs="?", type=Path)
    metrics_parser = subparsers.add_parser(
        "metrics", help="Show aggregate local-only model usage metrics"
    )
    metrics_parser.add_argument("--json", action="store_true")
    audit_parser = subparsers.add_parser(
        "audit", help="Inspect or export tamper-evident session audit logs"
    )
    audit_subparsers = audit_parser.add_subparsers(dest="audit_action", required=True)
    audit_list = audit_subparsers.add_parser(
        "list", help="List tamper-evident audit records for one session"
    )
    audit_list.add_argument("--session", required=True, dest="audit_session")
    audit_list.add_argument("--json", action="store_true")
    audit_verify = audit_subparsers.add_parser(
        "verify", help="Verify the hash chain for one session audit log"
    )
    audit_verify.add_argument("--session", required=True, dest="audit_session")
    audit_verify.add_argument("--json", action="store_true")
    audit_export = audit_subparsers.add_parser(
        "export", help="Export one session audit log to a file"
    )
    audit_export.add_argument("--session", required=True, dest="audit_session")
    audit_export.add_argument("--output", required=True, type=Path)
    sessions_parser = subparsers.add_parser("sessions", help="List saved Ash sessions")
    sessions_parser.add_argument(
        "sessions_action",
        nargs="?",
        choices=["list", "tree", "recovery", "search"],
        default="list",
        help=(
            "list sessions; tree shows branch lineage; recovery shows interrupted-turn "
            "reports; search finds transcript text"
        ),
    )
    sessions_parser.add_argument(
        "--session",
        help="Session ID or exact title for tree/recovery inspection",
    )
    sessions_parser.add_argument("--query", default="")
    sessions_parser.add_argument("--limit", type=int, default=20)
    sessions_parser.add_argument("--all-projects", action="store_true")
    sessions_parser.add_argument("--json", action="store_true")
    plans_parser = subparsers.add_parser(
        "plans", help="Inspect or update persisted sprint plans"
    )
    plans_subparsers = plans_parser.add_subparsers(dest="plans_action", required=True)
    plans_list = plans_subparsers.add_parser(
        "list", help="List persisted sprint plans"
    )
    plans_list.add_argument("--limit", type=int, default=20)
    plans_list.add_argument("--all-projects", action="store_true")
    plans_list.add_argument("--json", action="store_true")
    plans_show = plans_subparsers.add_parser(
        "show", help="Show one sprint plan and its items"
    )
    plans_show.add_argument("sprint_id")
    plans_show.add_argument("--json", action="store_true")
    plans_update = plans_subparsers.add_parser(
        "update", help="Update one item in a persisted sprint plan"
    )
    plans_update.add_argument("sprint_id")
    plans_update.add_argument("item_idx", type=int)
    plans_update.add_argument(
        "status",
        choices=["pending", "in_progress", "done", "skipped", "failed"],
    )
    plans_update.add_argument("--notes", default="")
    plans_update.add_argument("--json", action="store_true")
    cron_parser = subparsers.add_parser(
        "cron", help="Create and operate durable unattended Ash schedules"
    )
    cron_subparsers = cron_parser.add_subparsers(dest="cron_action", required=True)
    cron_status = cron_subparsers.add_parser(
        "status",
        help=(
            "Show scheduler, run, and recent worker heartbeat status "
            "for this workspace"
        ),
    )
    cron_status.add_argument("--json", action="store_true")
    cron_list = cron_subparsers.add_parser(
        "list", help="List enabled schedules for this workspace"
    )
    cron_list.add_argument("--all", action="store_true", dest="include_disabled")
    cron_list.add_argument("--json", action="store_true")
    cron_add = cron_subparsers.add_parser(
        "add", help="Create one validated one-shot, interval, or cron schedule"
    )
    cron_add.add_argument("name")
    cron_add.add_argument("--prompt", required=True, help="Prompt text, or - for stdin")
    cron_schedule = cron_add.add_mutually_exclusive_group(required=True)
    cron_schedule.add_argument("--at", help="Future ISO 8601 timestamp with UTC offset")
    cron_schedule.add_argument("--every", help="Interval such as 30m, 2h, or 1d")
    cron_schedule.add_argument(
        "--cron", dest="cron_expression", help="Five-field cron expression"
    )
    cron_add.add_argument(
        "--timezone", default="UTC", help="IANA timezone for --cron (default: UTC)"
    )
    cron_add.add_argument(
        "--misfire-grace",
        type=int,
        default=86_400,
        help="Maximum lateness in seconds, 0..2592000 (default: 86400)",
    )
    cron_add.add_argument(
        "--timeout",
        type=float,
        default=1800.0,
        help="Whole-turn wall timeout in seconds, 1..86400 (default: 1800)",
    )
    cron_add.add_argument(
        "--token-budget",
        type=int,
        default=100_000,
        help="Aggregate prompt and completion budget, 1..10000000",
    )
    cron_add.add_argument(
        "--webhook-url",
        help="HTTPS webhook that receives terminal automation run results",
    )
    cron_add.add_argument(
        "--webhook-secret-env",
        help="Environment variable containing the optional webhook HMAC secret",
    )
    cron_add.add_argument("--json", action="store_true")
    cron_show = cron_subparsers.add_parser(
        "show", help="Show a schedule and its stored prompt"
    )
    cron_show.add_argument("job")
    cron_show.add_argument("--json", action="store_true")
    for cron_action, cron_help in (
        ("pause", "Prevent future claims without cancelling an active run"),
        ("resume", "Resume future claims for a paused schedule"),
    ):
        cron_change = cron_subparsers.add_parser(cron_action, help=cron_help)
        cron_change.add_argument("job")
        cron_change.add_argument("--json", action="store_true")
    cron_remove = cron_subparsers.add_parser(
        "remove", help="Soft-delete a schedule while retaining run history"
    )
    cron_remove.add_argument("job")
    cron_remove.add_argument("--yes", action="store_true")
    cron_remove.add_argument("--json", action="store_true")
    cron_run = cron_subparsers.add_parser(
        "run", help="Run one schedule immediately through the isolated worker"
    )
    cron_run.add_argument("job")
    cron_run.add_argument("--json", action="store_true")
    cron_history = cron_subparsers.add_parser(
        "history", help="Show newest-first terminal and active run history"
    )
    cron_history.add_argument("job", nargs="?")
    cron_history.add_argument("--limit", type=int, default=100)
    cron_history.add_argument("--json", action="store_true")
    cron_deliveries = cron_subparsers.add_parser(
        "deliveries", help="Show durable webhook delivery status"
    )
    cron_deliveries.add_argument("job", nargs="?")
    cron_deliveries.add_argument("--limit", type=int, default=100)
    cron_deliveries.add_argument("--json", action="store_true")
    cron_cancel = cron_subparsers.add_parser(
        "cancel", help="Request cancellation of one active run"
    )
    cron_cancel.add_argument("run_id")
    cron_cancel.add_argument("--json", action="store_true")
    cron_worker = cron_subparsers.add_parser(
        "worker", help="Claim and execute due schedules for this workspace"
    )
    cron_worker.add_argument(
        "--once",
        action="store_true",
        help="Drain one due batch; exit 0 on success, 1 on failure, or 130 on stop",
    )
    cron_worker.add_argument("--json", action="store_true")
    permissions_parser = subparsers.add_parser(
        "permissions", help="Inspect or change persistent project tool grants"
    )
    permissions_parser.add_argument("--json", action="store_true")
    permissions_subparsers = permissions_parser.add_subparsers(
        dest="permissions_action"
    )
    permissions_status = permissions_subparsers.add_parser(
        "status", help="Show persistent and managed permission rules"
    )
    permissions_status.add_argument("--json", action="store_true")
    permissions_modes = permissions_subparsers.add_parser(
        "modes",
        help="Explain permission modes without changing policy",
    )
    permissions_modes.add_argument("--json", action="store_true")
    for effect in ("allow", "ask", "deny"):
        permissions_rule = permissions_subparsers.add_parser(
            effect,
            help=f"Add a persistent {effect} rule for one tool",
        )
        permissions_rule.add_argument("tool_name")
        permissions_rule.add_argument(
            "--exact",
            action="append",
            default=[],
            metavar="ARGUMENT=JSON",
        )
        permissions_rule.add_argument(
            "--contains",
            action="append",
            default=[],
            metavar="ARGUMENT=TEXT",
        )
        permissions_rule.add_argument(
            "--maximum",
            action="append",
            default=[],
            metavar="ARGUMENT=INTEGER",
        )
        permissions_rule.add_argument(
            "--in",
            dest="in",
            action="append",
            default=[],
            metavar="ARGUMENT=[VALUE,...]",
        )
        permissions_rule.add_argument(
            "--prefix",
            action="append",
            default=[],
            metavar="ARGUMENT=TEXT",
        )
        permissions_rule.add_argument(
            "--path-prefix",
            action="append",
            default=[],
            metavar="PATH_ARGUMENT=RELATIVE_PATH",
        )
        permissions_rule.add_argument(
            "--path-glob",
            action="append",
            default=[],
            metavar="PATH_ARGUMENT=RELATIVE_GLOB",
        )
        permissions_rule.add_argument(
            "--suffix",
            action="append",
            default=[],
            metavar="PATH_ARGUMENT=.EXT",
        )
        permissions_rule.add_argument(
            "--domain",
            action="append",
            default=[],
            metavar="URL_OR_DOMAIN_ARGUMENT=HOSTNAME",
        )
        permissions_rule.add_argument(
            "--command-prefix",
            nargs="+",
            default=[],
            metavar="TOKEN",
        )
        permissions_rule.add_argument("--json", action="store_true")
    permissions_revoke = permissions_subparsers.add_parser(
        "revoke", help="Remove persistent rules for one tool"
    )
    permissions_revoke.add_argument("tool_name")
    permissions_revoke.add_argument("--json", action="store_true")
    permissions_remove = permissions_subparsers.add_parser(
        "remove", help="Remove one persistent permission rule by ID"
    )
    permissions_remove.add_argument("rule_id")
    permissions_remove.add_argument("--json", action="store_true")
    permissions_clear = permissions_subparsers.add_parser(
        "clear", help="Remove all persistent project permission rules"
    )
    permissions_clear.add_argument("--yes", action="store_true")
    permissions_clear.add_argument("--json", action="store_true")
    extensions_parser = subparsers.add_parser(
        "extensions", help="Inspect and manage Ash extension components"
    )
    extensions_parser.add_argument(
        "extensions_action",
        nargs="?",
        choices=[
            "all",
            "skills",
            "commands",
            "agents",
            "plugins",
            "hooks",
            "inspect",
            "validate",
            "search",
            "install",
            "update",
            "enable",
            "disable",
            "uninstall",
        ],
        default="all",
        help=(
            "inspect extension catalogs or search, install, update, enable, disable, "
            "validate, and uninstall extension components"
        ),
    )
    extensions_parser.add_argument("extensions_target", nargs="?")
    extensions_parser.add_argument("--ref")
    extensions_parser.add_argument(
        "--catalog",
        action="append",
        help=(
            "Signed catalog path or HTTPS URL; repeat for publisher-qualified "
            "catalog v2 selection"
        ),
    )
    extensions_parser.add_argument("--replace", action="store_true")
    extensions_parser.add_argument("--yes", action="store_true")
    extensions_parser.add_argument(
        "--all",
        dest="extensions_all",
        action="store_true",
        help="Update every tracked plugin when used with `extensions update`",
    )
    extensions_parser.add_argument("--json", action="store_true")
    agents_parser = subparsers.add_parser(
        "agents", help="Inspect persisted subagent status and reports"
    )
    agents_subparsers = agents_parser.add_subparsers(
        dest="agents_action", required=True
    )
    agents_list = agents_subparsers.add_parser(
        "list", help="List persisted subagent summaries"
    )
    agents_list.add_argument("--json", action="store_true")
    agents_reports = agents_subparsers.add_parser(
        "reports", help="Show recent durable subagent completion reports"
    )
    agents_reports.add_argument("--limit", type=int, default=20)
    agents_reports.add_argument("--json", action="store_true")
    agents_tasks = agents_subparsers.add_parser(
        "tasks", help="Inspect durable delegated task state"
    )
    agents_tasks.add_argument(
        "--state",
        choices=("queued", "leased", "running", "succeeded", "failed", "cancelled"),
    )
    agents_tasks.add_argument("--owner")
    agents_tasks.add_argument("--graph", dest="graph_id")
    agents_tasks.add_argument("--limit", type=int, default=100)
    agents_tasks.add_argument("--json", action="store_true")
    agents_events = agents_subparsers.add_parser(
        "events", help="Inspect durable subagent task events"
    )
    agents_events.add_argument("--task", dest="task_id")
    agents_events.add_argument("--type", dest="event_type")
    agents_events.add_argument("--after", type=int, default=0, dest="after_sequence")
    agents_events.add_argument("--limit", type=int, default=100)
    agents_events.add_argument("--json", action="store_true")
    agents_cancel = agents_subparsers.add_parser(
        "cancel", help="Cancel a delegated task graph"
    )
    agents_cancel.add_argument("graph_id")
    agents_cancel.add_argument("--reason", default="cancelled by operator")
    agents_cancel.add_argument("--yes", action="store_true")
    agents_cancel.add_argument("--json", action="store_true")
    agents_messages = agents_subparsers.add_parser(
        "messages", help="List durable messages exchanged with subagents"
    )
    agents_messages.add_argument("--recipient", default="lead")
    agents_messages.add_argument("--all", action="store_true", dest="all_messages")
    agents_messages.add_argument("--limit", type=int, default=50)
    agents_messages.add_argument("--json", action="store_true")
    agents_approvals = agents_subparsers.add_parser(
        "approvals", help="List durable subagent approval requests"
    )
    agents_approvals.add_argument("--all", action="store_true", dest="all_approvals")
    agents_approvals.add_argument("--limit", type=int, default=50)
    agents_approvals.add_argument("--json", action="store_true")
    agents_approve = agents_subparsers.add_parser(
        "approve", help="Approve one pending subagent request"
    )
    agents_approve.add_argument("request_id", type=int)
    agents_approve.add_argument("--json", action="store_true")
    agents_deny = agents_subparsers.add_parser(
        "deny", help="Deny one pending subagent request"
    )
    agents_deny.add_argument("request_id", type=int)
    agents_deny.add_argument("--feedback", default="")
    agents_deny.add_argument("--json", action="store_true")
    agents_send = agents_subparsers.add_parser(
        "send", help="Send a durable message to a subagent"
    )
    agents_send.add_argument("recipient")
    agents_send.add_argument("content")
    agents_send.add_argument("--sender", default="lead")
    agents_send.add_argument("--type", default="steer", dest="message_type")
    agents_send.add_argument("--json-content", action="store_true")
    agents_send.add_argument("--force", action="store_true")
    agents_send.add_argument("--json", action="store_true")
    agents_branches = agents_subparsers.add_parser(
        "branches", help="List isolated subagent worktree branches"
    )
    agents_branches.add_argument("--json", action="store_true")
    agents_apply = agents_subparsers.add_parser(
        "apply", help="Apply a completed subagent branch into the current worktree"
    )
    agents_apply.add_argument("branch")
    agents_discard = agents_subparsers.add_parser(
        "discard", help="Discard an isolated subagent branch and worktree"
    )
    agents_discard.add_argument("branch")
    agents_discard.add_argument("--yes", action="store_true")
    serve_parser = subparsers.add_parser(
        "serve",
        help="Run the authenticated Ash HTTP API (loopback by default; remote requires opt-in and TLS)",
    )
    serve_parser.add_argument("--host", default="127.0.0.1")
    serve_parser.add_argument("--port", type=int, default=8765)
    serve_parser.add_argument("--token-env", default="ASH_SERVER_TOKEN")
    serve_parser.add_argument("--rate-limit", type=int, default=60)
    serve_parser.add_argument("--approval-timeout", type=float, default=300.0)
    serve_parser.add_argument("--allow-remote", action="store_true")
    serve_parser.add_argument("--ssl-certfile")
    serve_parser.add_argument("--ssl-keyfile")
    serve_parser.add_argument(
        "--log-level",
        choices=["critical", "error", "warning", "info", "debug"],
        default="info",
    )
    remote_parser = subparsers.add_parser(
        "remote",
        help="Control an authenticated Ash HTTP server from another terminal or machine",
    )
    remote_subparsers = remote_parser.add_subparsers(
        dest="remote_action",
        required=True,
    )
    remote_common = argparse.ArgumentParser(add_help=False)
    remote_common.add_argument("url", help="Ash server origin, for example https://host:8765")
    remote_common.add_argument("--token-env", default="ASH_SERVER_TOKEN")
    remote_common.add_argument("--timeout", type=float, default=300.0)

    remote_status = remote_subparsers.add_parser(
        "status",
        parents=[remote_common],
        help="Show remote runtime/session status",
    )
    remote_status.add_argument("--json", action="store_true")
    remote_sessions = remote_subparsers.add_parser(
        "sessions",
        parents=[remote_common],
        help="List durable sessions on the remote Ash host",
    )
    remote_sessions.add_argument("--query", default="")
    remote_sessions.add_argument("--limit", type=int, default=20)
    remote_sessions.add_argument("--json", action="store_true")
    remote_new = remote_subparsers.add_parser(
        "new",
        parents=[remote_common],
        help="Create and select a new remote session",
    )
    remote_new.add_argument("--json", action="store_true")
    remote_resume = remote_subparsers.add_parser(
        "resume",
        parents=[remote_common],
        help="Select an existing durable remote session",
    )
    remote_resume.add_argument("session_id")
    remote_resume.add_argument("--json", action="store_true")
    remote_prompt = remote_subparsers.add_parser(
        "prompt",
        parents=[remote_common],
        help="Run one streamed remote turn",
    )
    remote_prompt.add_argument("prompt")
    remote_chat = remote_subparsers.add_parser(
        "chat",
        parents=[remote_common],
        help="Attach an interactive terminal to the remote Ash runtime",
    )
    remote_chat.add_argument("--session-id")
    remote_chat.set_defaults(json=False)
    remote_steer = remote_subparsers.add_parser(
        "steer",
        parents=[remote_common],
        help="Queue guidance for the currently running remote turn",
    )
    remote_steer.add_argument("text")
    remote_steer.add_argument("--json", action="store_true")
    remote_approvals = remote_subparsers.add_parser(
        "approvals",
        parents=[remote_common],
        help="List pending remote tool approvals",
    )
    remote_approvals.add_argument("--json", action="store_true")
    remote_approve = remote_subparsers.add_parser(
        "approve",
        parents=[remote_common],
        help="Approve one pending remote tool call",
    )
    remote_approve.add_argument("request_id")
    remote_approve.add_argument("--json", action="store_true")
    remote_deny = remote_subparsers.add_parser(
        "deny",
        parents=[remote_common],
        help="Deny one pending remote tool call",
    )
    remote_deny.add_argument("request_id")
    remote_deny.add_argument("--json", action="store_true")
    acp_parser = subparsers.add_parser(
        "acp", help="Run Ash as an Agent Client Protocol v1 stdio agent"
    )
    acp_parser.add_argument(
        "--check",
        action="store_true",
        help="Validate the optional ACP runtime without starting stdio transport",
    )
    a2a_parser = subparsers.add_parser(
        "a2a", help="Expose or call agents through Agent2Agent Protocol 1.0"
    )
    a2a_subparsers = a2a_parser.add_subparsers(dest="a2a_action", required=True)
    a2a_subparsers.add_parser("check", help="Validate the optional A2A runtime")
    a2a_serve = a2a_subparsers.add_parser(
        "serve", help="Run the authenticated A2A 1.0 HTTP server"
    )
    a2a_serve.add_argument("--host", default="127.0.0.1")
    a2a_serve.add_argument("--port", type=int, default=8770)
    a2a_serve.add_argument("--public-url")
    a2a_serve.add_argument("--token-env", default="ASH_A2A_TOKEN")
    a2a_serve.add_argument("--rate-limit", type=int, default=60)
    a2a_serve.add_argument("--allow-remote", action="store_true")
    a2a_serve.add_argument("--ssl-certfile")
    a2a_serve.add_argument("--ssl-keyfile")
    a2a_serve.add_argument(
        "--log-level",
        choices=["critical", "error", "warning", "info", "debug"],
        default="info",
    )
    a2a_inspect = a2a_subparsers.add_parser(
        "inspect", help="Resolve and print a remote Agent Card"
    )
    a2a_inspect.add_argument("url")
    a2a_inspect.add_argument("--token-env", default="ASH_A2A_TOKEN")
    a2a_inspect.add_argument("--timeout", type=float, default=30.0)
    a2a_send = a2a_subparsers.add_parser(
        "send", help="Send a text task to a remote A2A agent"
    )
    a2a_send.add_argument("url")
    a2a_send.add_argument("prompt", help="Prompt text, or - to read standard input")
    a2a_send.add_argument("--context-id")
    a2a_send.add_argument("--token-env", default="ASH_A2A_TOKEN")
    a2a_send.add_argument("--timeout", type=float, default=300.0)
    a2a_send.add_argument("--json", action="store_true")
    lsp_parser = subparsers.add_parser(
        "lsp", help="Inspect and query managed language servers"
    )
    lsp_subparsers = lsp_parser.add_subparsers(dest="lsp_action", required=True)
    lsp_status = lsp_subparsers.add_parser(
        "status", help="List detected and configured language servers"
    )
    lsp_status.add_argument("--json", action="store_true")
    lsp_diagnostics = lsp_subparsers.add_parser(
        "diagnostics", help="Get diagnostics for a workspace file"
    )
    lsp_diagnostics.add_argument("file_path")
    lsp_diagnostics.add_argument("--json", action="store_true")
    lsp_query = lsp_subparsers.add_parser(
        "query", help="Run a semantic language-server query"
    )
    lsp_query.add_argument(
        "operation",
        choices=[
            "hover",
            "definition",
            "references",
            "implementation",
            "documentSymbol",
            "workspaceSymbol",
            "prepareRename",
            "rename",
            "codeAction",
            "formatting",
            "prepareCallHierarchy",
            "incomingCalls",
            "outgoingCalls",
        ],
    )
    lsp_query.add_argument("file_path", nargs="?", default="")
    lsp_query.add_argument("--line", type=int, default=1)
    lsp_query.add_argument("--character", type=int, default=1)
    lsp_query.add_argument("--query", default="")
    lsp_query.add_argument("--new-name", default="")
    lsp_query.add_argument("--end-line", type=int)
    lsp_query.add_argument("--end-character", type=int)
    lsp_query.add_argument("--code-action-kind", default="")
    lsp_query.add_argument("--server", default="")
    lsp_query.add_argument("--tab-size", type=int)
    lsp_query.add_argument(
        "--insert-spaces",
        action=argparse.BooleanOptionalAction,
        default=None,
    )
    lsp_query.add_argument("--json", action="store_true")
    mcp_subparser = subparsers.add_parser(
        "mcp", help="Configure, authenticate, inspect, and probe MCP servers"
    )
    mcp_action_subparsers = mcp_subparser.add_subparsers(dest="action", required=True)
    mcp_list = mcp_action_subparsers.add_parser(
        "list", help="List configured MCP servers without connecting"
    )
    mcp_list.add_argument("--json", action="store_true")
    mcp_status = mcp_action_subparsers.add_parser(
        "status", help="Show configured MCP servers and authentication state"
    )
    mcp_status.add_argument("--json", action="store_true")
    mcp_probe = mcp_action_subparsers.add_parser(
        "probe", help="Connect to one MCP server and inspect its capabilities"
    )
    mcp_probe.add_argument("server_name")
    mcp_probe.add_argument("--timeout", type=float, default=30.0)
    mcp_probe.add_argument("--json", action="store_true")
    mcp_add = mcp_action_subparsers.add_parser(
        "add", help="Add or replace one MCP server configuration"
    )
    mcp_add.add_argument("server_name")
    mcp_add.add_argument(
        "--transport", choices=["stdio", "http", "sse"], default="stdio"
    )
    mcp_add.add_argument("--url", default="")
    mcp_add.add_argument(
        "--env",
        action="append",
        default=[],
        help=(
            "Server environment KEY=VALUE; credential-bearing keys must use "
            "$VAR or ${VAR} rather than a literal secret"
        ),
    )
    mcp_add.add_argument(
        "--header",
        action="append",
        default=[],
        help=(
            "HTTP header KEY=VALUE; Authorization/API-key headers must use an "
            "environment-variable reference"
        ),
    )
    mcp_add.add_argument("--auth", choices=["none", "oauth"], default="none")
    mcp_add.add_argument("--oauth-client-id", default="")
    mcp_add.add_argument("--oauth-client-metadata-url", default="")
    mcp_add.add_argument("--oauth-issuer", default="")
    mcp_add.add_argument("--oauth-client-secret-env", default="")
    mcp_add.add_argument("--oauth-scope", default="")
    mcp_add.add_argument("--oauth-redirect-port", type=int, default=0)
    mcp_add.add_argument("--json", action="store_true")
    mcp_remove = mcp_action_subparsers.add_parser(
        "remove", help="Remove one MCP server configuration"
    )
    mcp_remove.add_argument("server_name")
    mcp_login = mcp_action_subparsers.add_parser(
        "login", help="Authorize one OAuth-enabled MCP server"
    )
    mcp_login.add_argument("server_name")
    mcp_login.add_argument("--no-browser", action="store_true")
    mcp_login.add_argument("--timeout", type=float, default=300.0)
    mcp_login.add_argument("--scope", default="")
    mcp_logout = mcp_action_subparsers.add_parser(
        "logout", help="Remove saved OAuth credentials for one MCP server"
    )
    mcp_logout.add_argument("server_name")
    parser.add_argument(
        "--db-directory",
        type=Path,
        default=None,
        help="Override ASH_DB_DIRECTORY for this run.",
    )
    session_group = parser.add_mutually_exclusive_group()
    session_group.add_argument(
        "--session",
        default=None,
        help="Restore an existing session by id instead of creating a new one.",
    )
    session_group.add_argument(
        "-c",
        "--continue",
        dest="continue_session",
        action="store_true",
        help="Continue the most recently updated session in this project.",
    )
    session_group.add_argument(
        "-r",
        "--resume",
        nargs="?",
        const="",
        default=None,
        metavar="SESSION",
        help="Resume by id/name, or open the interactive session picker.",
    )
    parser.add_argument(
        "--fork-session",
        action="store_true",
        help="Fork the resumed session instead of appending to it.",
    )
    parser.add_argument(
        "-p",
        "--prompt",
        default=None,
        help="Run one non-interactive prompt and exit.",
    )
    parser.add_argument(
        "--output-format",
        choices=["text", "json", "stream-json"],
        default="text",
        help="Output format for --prompt mode.",
    )
    parser.add_argument(
        "--mode",
        choices=["interactive", "auto_edit", "plan", "auto_approve", "dry_run"],
        default=None,
        help="Override the configured permission mode for this run.",
    )
    parser.add_argument(
        "--json-schema",
        type=Path,
        default=None,
        help="Require one-shot output to validate against a JSON Schema file.",
    )
    parser.add_argument(
        "--ci",
        action="store_true",
        help="Run without interactive prompts or ANSI; defaults one-shot output to stream-json.",
    )
