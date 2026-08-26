"""Top-level CLI helpers for persisted project permission rules."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ash.safety.grants import (
    ArgumentMatcher,
    MatchOperator,
    PermissionRule,
    RuleEffect,
    add_permission_rule,
    clear_permission_rules,
    load_managed_permission_rules,
    load_permission_rules,
    remove_permission_rule,
    remove_permission_rules_for_tool,
)
from ash.safety.trust import canonical_workspace


def permission_rules_payload(
    workspace: Path,
    rules: list[PermissionRule],
) -> dict[str, Any]:
    managed_rules = load_managed_permission_rules(workspace)
    return {
        "workspace": canonical_workspace(workspace),
        "managed_rules": [rule.as_payload() for rule in managed_rules],
        "rules": [rule.as_payload() for rule in rules],
        "persistent_grants": sorted(
            rule.tool_name
            for rule in (*managed_rules, *rules)
            if rule.effect == RuleEffect.ALLOW and not rule.scoped
        ),
    }


def permission_grants_payload(workspace: Path, grants: set[str]) -> dict[str, Any]:
    """Compatibility payload for callers still rendering bare allow grants."""

    rules = [PermissionRule.create(RuleEffect.ALLOW, tool) for tool in sorted(grants)]
    return permission_rules_payload(workspace, rules)


def _render_matcher(matcher: ArgumentMatcher) -> str:
    value = (
        " ".join(matcher.value)
        if matcher.operator == MatchOperator.COMMAND_PREFIX
        else json.dumps(matcher.value, ensure_ascii=False, sort_keys=True)
    )
    return f"{matcher.argument}:{matcher.operator.value}={value}"


def render_permission_rules(
    workspace: Path,
    rules: list[PermissionRule],
    *,
    json_output: bool = False,
) -> str:
    payload = permission_rules_payload(workspace, rules)
    if json_output:
        return json.dumps(payload, sort_keys=True)
    lines = [f"Workspace: {payload['workspace']}", "Managed policy rules:"]
    if not payload["managed_rules"]:
        lines.append("  (none)")
    for rule in payload["managed_rules"]:
        scope = ""
        if rule["matches"]:
            scope = " " + " AND ".join(
                _render_matcher(ArgumentMatcher.from_payload(matcher))
                for matcher in rule["matches"]
            )
        lines.append(
            f"  {rule['id']} [MANAGED] {rule['effect'].upper()} {rule['tool']}{scope}"
        )
    lines.append("Permission rules:")
    if not rules:
        lines.append("  (none)")
        return "\n".join(lines)
    for rule in rules:
        scope = ""
        if rule.matchers:
            scope = " " + " AND ".join(
                _render_matcher(matcher) for matcher in rule.matchers
            )
        lines.append(
            f"  {rule.rule_id} {rule.effect.value.upper()} {rule.tool_name}{scope}"
        )
    return "\n".join(lines)


def render_permission_grants(
    workspace: Path,
    grants: set[str],
    *,
    json_output: bool = False,
) -> str:
    """Compatibility renderer for the original bare-grant API."""

    return render_permission_rules(
        workspace,
        [PermissionRule.create(RuleEffect.ALLOW, tool) for tool in sorted(grants)],
        json_output=json_output,
    )


def _split_assignment(raw: str, *, option: str) -> tuple[str, str]:
    argument, separator, value = raw.partition("=")
    if not separator or not argument or not value:
        raise ValueError(f"{option} requires ARGUMENT=VALUE")
    return argument, value


def build_argument_matchers(
    *,
    exact: list[str] | None = None,
    contains: list[str] | None = None,
    maximum: list[str] | None = None,
    prefix: list[str] | None = None,
    path_prefix: list[str] | None = None,
    suffix: list[str] | None = None,
    domain: list[str] | None = None,
    command_prefix: list[str] | None = None,
) -> list[ArgumentMatcher]:
    matchers: list[ArgumentMatcher] = []
    for raw in exact or ():
        argument, value = _split_assignment(raw, option="--exact")
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError as exc:
            raise ValueError(
                f"--exact value for {argument!r} must be JSON; "
                'quote string values, for example file_path="README.md"'
            ) from exc
        matchers.append(ArgumentMatcher(argument, MatchOperator.EXACT, parsed))
    for raw in contains or ():
        argument, value = _split_assignment(raw, option="--contains")
        matchers.append(ArgumentMatcher(argument, MatchOperator.CONTAINS, value))
    for raw in maximum or ():
        argument, value = _split_assignment(raw, option="--maximum")
        try:
            parsed = int(value)
        except ValueError as exc:
            raise ValueError(
                f"--maximum value for {argument!r} must be an integer"
            ) from exc
        matchers.append(ArgumentMatcher(argument, MatchOperator.MAX, parsed))
    for raw in prefix or ():
        argument, value = _split_assignment(raw, option="--prefix")
        matchers.append(ArgumentMatcher(argument, MatchOperator.PREFIX, value))
    for raw in path_prefix or ():
        argument, value = _split_assignment(raw, option="--path-prefix")
        matchers.append(ArgumentMatcher(argument, MatchOperator.PATH_PREFIX, value))
    for raw in suffix or ():
        argument, value = _split_assignment(raw, option="--suffix")
        matchers.append(ArgumentMatcher(argument, MatchOperator.SUFFIX, value))
    for raw in domain or ():
        argument, value = _split_assignment(raw, option="--domain")
        matchers.append(ArgumentMatcher(argument, MatchOperator.DOMAIN, value))
    if command_prefix:
        matchers.append(
            ArgumentMatcher(
                "command_line",
                MatchOperator.COMMAND_PREFIX,
                command_prefix,
            )
        )
    return matchers


def add_cli_permission_rule(
    workspace: Path,
    effect: str | RuleEffect,
    tool_name: str,
    *,
    exact: list[str] | None = None,
    contains: list[str] | None = None,
    maximum: list[str] | None = None,
    prefix: list[str] | None = None,
    path_prefix: list[str] | None = None,
    suffix: list[str] | None = None,
    domain: list[str] | None = None,
    command_prefix: list[str] | None = None,
) -> tuple[PermissionRule, list[PermissionRule]]:
    if command_prefix and tool_name != "run_command":
        raise ValueError("--command-prefix is only valid for run_command")
    rule = PermissionRule.create(
        effect,
        tool_name,
        build_argument_matchers(
            exact=exact,
            contains=contains,
            maximum=maximum,
            prefix=prefix,
            path_prefix=path_prefix,
            suffix=suffix,
            domain=domain,
            command_prefix=command_prefix,
        ),
    )
    return rule, add_permission_rule(workspace, rule)


def remove_cli_permission_rule(
    workspace: Path,
    rule_id: str,
) -> list[PermissionRule]:
    return remove_permission_rule(workspace, rule_id)


def allow_permission_grant(workspace: Path, tool_name: str) -> set[str]:
    add_cli_permission_rule(workspace, RuleEffect.ALLOW, tool_name)
    return {
        rule.tool_name
        for rule in load_permission_rules(workspace)
        if rule.effect == RuleEffect.ALLOW and not rule.scoped
    }


def revoke_permission_grant(workspace: Path, tool_name: str) -> set[str]:
    rules = remove_permission_rules_for_tool(
        workspace,
        tool_name,
        effect=RuleEffect.ALLOW,
    )
    return {
        rule.tool_name
        for rule in rules
        if rule.effect == RuleEffect.ALLOW and not rule.scoped
    }


def clear_permission_grants(workspace: Path) -> set[str]:
    clear_permission_rules(workspace)
    return set()
