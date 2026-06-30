"""Central tool permission policy for Ash runtime modes."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any

from safety.grants import PermissionRule, RuleEffect


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
    rule_id: str | None = None


class PermissionPolicy:
    """Resolve a tool call to allow, ask, or deny for the active mode."""

    def __init__(
        self,
        mode: str | PermissionMode = PermissionMode.INTERACTIVE,
        *,
        persistent_tool_grants: set[str] | None = None,
        persistent_rules: list[PermissionRule] | None = None,
        session_rules: list[PermissionRule] | None = None,
    ):
        self.mode = PermissionMode(mode)
        self.persistent_rules = list(persistent_rules or ())
        self.session_rules = list(session_rules or ())
        if persistent_tool_grants:
            existing = {rule.rule_id for rule in self.persistent_rules}
            for tool_name in persistent_tool_grants:
                rule = PermissionRule.create(RuleEffect.ALLOW, tool_name)
                if rule.rule_id not in existing:
                    self.persistent_rules.append(rule)
                    existing.add(rule.rule_id)

    @property
    def persistent_tool_grants(self) -> set[str]:
        """Compatibility view of unscoped persistent allow rules."""

        return {
            rule.tool_name
            for rule in self.persistent_rules
            if rule.effect == RuleEffect.ALLOW and not rule.scoped
        }

    @persistent_tool_grants.setter
    def persistent_tool_grants(self, tool_names: set[str]) -> None:
        retained = [
            rule
            for rule in self.persistent_rules
            if rule.scoped or rule.effect != RuleEffect.ALLOW
        ]
        retained.extend(
            PermissionRule.create(RuleEffect.ALLOW, tool_name)
            for tool_name in sorted(tool_names)
        )
        self.persistent_rules = retained

    def set_persistent_rules(self, rules: list[PermissionRule]) -> None:
        self.persistent_rules = list(rules)

    def add_session_rule(self, rule: PermissionRule) -> None:
        if all(existing.rule_id != rule.rule_id for existing in self.session_rules):
            self.session_rules.append(rule)

    def _matching_rule(
        self,
        effect: RuleEffect,
        tool_name: str,
        arguments: dict[str, Any],
    ) -> PermissionRule | None:
        return next(
            (
                rule
                for rule in (*self.session_rules, *self.persistent_rules)
                if rule.effect == effect and rule.matches(tool_name, arguments)
            ),
            None,
        )

    def evaluate(
        self,
        tool_name: str,
        arguments: dict[str, Any],
    ) -> PolicyDecision:
        read_only = tool_name in READ_ONLY_TOOLS or (
            tool_name == "background_process"
            and arguments.get("action") in {"list", "poll"}
        )
        if self.mode == PermissionMode.DRY_RUN:
            return PolicyDecision(
                PolicyAction.DENY, "dry-run mode forbids side effects"
            )
        deny_rule = self._matching_rule(RuleEffect.DENY, tool_name, arguments)
        if deny_rule is not None:
            return PolicyDecision(
                PolicyAction.DENY,
                "matched deny rule",
                deny_rule.rule_id,
            )
        if self.mode == PermissionMode.PLAN and not read_only:
            return PolicyDecision(PolicyAction.DENY, "plan mode is read-only")
        ask_rule = self._matching_rule(RuleEffect.ASK, tool_name, arguments)
        if ask_rule is not None:
            return PolicyDecision(
                PolicyAction.ASK,
                "matched ask rule",
                ask_rule.rule_id,
            )
        if read_only:
            return PolicyDecision(PolicyAction.ALLOW, "read-only tool")
        allow_rule = self._matching_rule(RuleEffect.ALLOW, tool_name, arguments)
        if allow_rule is not None:
            return PolicyDecision(
                PolicyAction.ALLOW,
                "matched allow rule",
                allow_rule.rule_id,
            )
        if self.mode == PermissionMode.AUTO_APPROVE:
            return PolicyDecision(PolicyAction.ALLOW, "full auto mode")
        if self.mode == PermissionMode.AUTO_EDIT and tool_name in EDIT_TOOLS:
            return PolicyDecision(PolicyAction.ALLOW, "auto-edit mode")
        return PolicyDecision(PolicyAction.ASK, "interactive approval required")
