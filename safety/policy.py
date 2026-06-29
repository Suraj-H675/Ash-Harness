"""Central tool permission policy for Ash runtime modes."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any


class PolicyAction(str, Enum):
    ALLOW = "allow"
    ASK = "ask"
    DENY = "deny"


class PermissionMode(str, Enum):
    INTERACTIVE = "interactive"
    AUTO_EDIT = "auto_edit"
    PLAN = "plan"
    AUTO_APPROVE = "auto_approve"
    DRY_RUN = "dry_run"


READ_ONLY_TOOLS = frozenset(
    {
        "read_file",
        "list_dir",
        "glob_files",
        "search_text",
        "find_symbol",
        "find_references",
        "git_status",
        "git_diff",
        "git_log",
        "list_skills",
        "activate_skill",
        "ask_user",
    }
)
EDIT_TOOLS = frozenset(
    {
        "write_file",
        "replace_file_content",
        "replace_file_edits",
        "whole_edit",
        "apply_patch",
    }
)


@dataclass(frozen=True)
class PolicyDecision:
    action: PolicyAction
    reason: str


class PermissionPolicy:
    """Resolve a tool call to allow, ask, or deny for the active mode."""

    def __init__(
        self,
        mode: str | PermissionMode = PermissionMode.INTERACTIVE,
        *,
        persistent_tool_grants: set[str] | None = None,
    ):
        self.mode = PermissionMode(mode)
        self.persistent_tool_grants = set(persistent_tool_grants or ())

    def evaluate(
        self,
        tool_name: str,
        arguments: dict[str, Any],
    ) -> PolicyDecision:
        if self.mode == PermissionMode.DRY_RUN:
            return PolicyDecision(
                PolicyAction.DENY, "dry-run mode forbids side effects"
            )
        if tool_name in READ_ONLY_TOOLS:
            return PolicyDecision(PolicyAction.ALLOW, "read-only tool")
        if tool_name == "background_process" and arguments.get("action") in {
            "list",
            "poll",
        }:
            return PolicyDecision(PolicyAction.ALLOW, "read-only process observation")
        if self.mode == PermissionMode.PLAN:
            return PolicyDecision(PolicyAction.DENY, "plan mode is read-only")
        if tool_name in self.persistent_tool_grants:
            return PolicyDecision(PolicyAction.ALLOW, "persistent project grant")
        if self.mode == PermissionMode.AUTO_APPROVE:
            return PolicyDecision(PolicyAction.ALLOW, "full auto mode")
        if self.mode == PermissionMode.AUTO_EDIT and tool_name in EDIT_TOOLS:
            return PolicyDecision(PolicyAction.ALLOW, "auto-edit mode")
        return PolicyDecision(PolicyAction.ASK, "interactive approval required")
