"""Command-line driver for spawned Ash subagents."""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path
from typing import Any

from ash.safe_io import strict_json_loads
from ash.agents.approval_channel import (
    ApprovalChannelError,
    ApprovalEndpoint,
    request_foreground_approval,
)
from ash.agents.shared_state import SharedState
from ash.agents.subprocess_agent import (
    MAX_SUBPROCESS_SPEC_BYTES,
    SubprocessAgent,
    make_simple_text_task,
)


def _required_text(spec: dict[str, Any], key: str) -> str:
    value = spec.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError(f"subagent specification field {key!r} must be non-empty text")
    return value


def _optional_positive_int(spec: dict[str, Any], key: str, default: int) -> int:
    value = spec.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"subagent specification field {key!r} must be positive")
    return value


def _load_spec() -> dict[str, Any]:
    raw = sys.stdin.buffer.read(MAX_SUBPROCESS_SPEC_BYTES + 1)
    if len(raw) > MAX_SUBPROCESS_SPEC_BYTES:
        raise ValueError("subagent subprocess specification is too large")
    value = strict_json_loads(raw)
    if not isinstance(value, dict) or value.get("version") != 1:
        raise ValueError("unsupported subagent subprocess specification")
    return value


def _permission_policy_from_spec(raw: Any):
    from ash.safety.grants import PermissionRule
    from ash.safety.policy import PermissionPolicy

    if not isinstance(raw, dict):
        raise ValueError("subagent permission policy must be an object")
    mode = raw.get("mode", "interactive")
    if not isinstance(mode, str):
        raise ValueError("subagent permission policy mode must be text")

    def rules(name: str) -> list[PermissionRule]:
        value = raw.get(name, [])
        if not isinstance(value, list):
            raise ValueError(f"subagent permission policy {name} must be a list")
        return [PermissionRule.from_payload(item) for item in value]

    return PermissionPolicy(
        mode,
        managed_rules=rules("managed_rules"),
        persistent_rules=rules("persistent_rules"),
        session_rules=rules("session_rules"),
    )


def _custom_agents_from_spec(raw: Any) -> dict[str, Any]:
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ValueError("subagent custom agent definition must be an object")
    from ash.plugins.agents import AgentDefinition

    name = _required_text(raw, "name")
    description = raw.get("description", "Custom subagent")
    instructions = _required_text(raw, "instructions")
    path = _required_text(raw, "path")
    base_role = _required_text(raw, "base_role")
    allowed_tools = raw.get("allowed_tools", [])
    if not isinstance(description, str):
        raise ValueError("subagent custom agent description must be text")
    if not isinstance(allowed_tools, list) or not all(
        isinstance(item, str) and item for item in allowed_tools
    ):
        raise ValueError("subagent custom agent allowed_tools is invalid")
    definition = AgentDefinition(
        name=name,
        description=description,
        instructions=instructions,
        path=Path(path),
        base_role=base_role,
        allowed_tools=tuple(allowed_tools),
    )
    return {name: definition}


async def _run_durable_task(spec: dict[str, Any]) -> int:
    from ash.config import AshConfig
    from ash.providers.registry import get_provider_registry
    from ash.safety.guard import SafetyGuard
    from ash.tools.agent import SpawnAgentTool

    db_path = _required_text(spec, "db_path")
    task_id = _required_text(spec, "task_id")
    workspace_value = _required_text(spec, "workspace_root")
    config_payload = spec.get("config")
    if not isinstance(config_payload, dict):
        raise ValueError("subagent durable config must be an object")
    provider_env = spec.get("provider_env", {})
    if not isinstance(provider_env, dict) or not all(
        isinstance(key, str)
        and key
        and isinstance(value, str)
        for key, value in provider_env.items()
    ):
        raise ValueError("subagent provider environment is invalid")
    os.environ.update(provider_env)

    workspace = Path(workspace_value).expanduser().resolve()
    config = AshConfig.model_validate(config_payload).model_copy(
        update={
            "workspace_root": workspace,
            "agent_execution_mode": "in_process",
        }
    )
    policy = _permission_policy_from_spec(spec.get("permission_policy", {}))
    custom_agents = _custom_agents_from_spec(spec.get("custom_agent"))
    max_return_chars = _optional_positive_int(spec, "max_return_chars", 20_000)
    max_turn_iterations = _optional_positive_int(spec, "max_turn_iterations", 12)
    require_dispatchable = spec.get("require_dispatchable", False)
    if type(require_dispatchable) is not bool:
        raise ValueError("subagent require_dispatchable must be boolean")
    raw_approval_endpoint = spec.get("approval_channel")
    approval_endpoint = (
        ApprovalEndpoint.from_payload(raw_approval_endpoint)
        if raw_approval_endpoint is not None
        else None
    )

    shared_state = SharedState(Path(db_path))
    if approval_endpoint is not None:
        durable_task = shared_state.tasks.get_task(task_id)
        if durable_task is None:
            raise ValueError("subagent approval endpoint task does not exist")
        if (
            approval_endpoint.task_id != task_id
            or durable_task.metadata.get("agent_id") != approval_endpoint.agent_id
            or approval_endpoint.attempt != durable_task.attempt + 1
        ):
            raise ValueError("subagent approval endpoint does not match durable task")

    tool = SpawnAgentTool(
        SafetyGuard(workspace),
        shared_state,
        lambda: get_provider_registry().build(config),
        max_return_chars=max_return_chars,
        config=config,
        max_turn_iterations=max_turn_iterations,
        custom_agents=custom_agents,
        provider_config_backed=False,
    )
    tool.set_permission_policy_provider(lambda: policy)

    if approval_endpoint is not None:
        async def approve_foreground_tool(
            agent_id: str,
            tool_name: str,
            arguments: dict[str, Any],
        ) -> bool | str:
            if agent_id != approval_endpoint.agent_id:
                return "Foreground approval identity mismatch."
            try:
                decision = await request_foreground_approval(
                    approval_endpoint,
                    tool_name=tool_name,
                    arguments=arguments,
                )
            except ApprovalChannelError as exc:
                return f"Foreground approval failed closed: {exc}"
            for delta in decision.rules:
                rules = getattr(policy, delta.category)
                if all(existing.rule_id != delta.rule.rule_id for existing in rules):
                    rules.append(delta.rule)
            if decision.approved:
                return True
            return decision.feedback or False

        tool.set_foreground_approval_broker(approve_foreground_tool)
    try:
        result = await tool.run_queued_task(
            task_id,
            require_dispatchable=require_dispatchable,
            wait=True,
            approval_mode="live" if approval_endpoint is not None else "durable",
        )
        return 0 if result.success else 1
    finally:
        await tool.aclose()


def _run_simple_spec(spec: dict[str, Any]) -> int:
    agent_id = _required_text(spec, "agent_id")
    db_path = _required_text(spec, "db_path")
    role = _required_text(spec, "role")
    task = _required_text(spec, "task")
    raw_allowlist = spec.get("tool_allowlist", [])
    if not isinstance(raw_allowlist, list) or not all(
        isinstance(item, str) and item for item in raw_allowlist
    ):
        raise ValueError("subagent specification tool_allowlist is invalid")
    metadata = spec.get("metadata", {})
    if not isinstance(metadata, dict):
        raise ValueError("subagent specification metadata must be an object")
    if not all(isinstance(key, str) for key in metadata):
        raise ValueError("subagent specification metadata keys must be strings")
    sandbox_tier = spec.get("sandbox_tier", 1)
    if isinstance(sandbox_tier, bool) or not isinstance(sandbox_tier, int):
        raise ValueError("subagent specification sandbox_tier must be an integer")
    workspace_value = spec.get("workspace_root", "")
    if not isinstance(workspace_value, str):
        raise ValueError("subagent specification workspace_root must be text")
    allow_custom_role = spec.get("allow_custom_role", False)
    if type(allow_custom_role) is not bool:
        raise ValueError("subagent specification allow_custom_role must be boolean")
    token_budget = _optional_positive_int(spec, "token_budget", 4000)
    return_budget = _optional_positive_int(spec, "return_budget", 2000)

    shared_state = SharedState(Path(db_path))
    try:
        agent = SubprocessAgent(
            agent_id=agent_id,
            role=role,
            task=task,
            shared_state=shared_state,
            runner=make_simple_text_task(f"completed: {task}"),
            tool_allowlist=tuple(raw_allowlist),
            token_budget=token_budget,
            return_budget=return_budget,
            metadata=dict(metadata),
            sandbox_tier=sandbox_tier,
            workspace_root=Path(workspace_value) if workspace_value else None,
            allow_custom_role=allow_custom_role,
        )
        report = asyncio.run(agent.run_in_process())
        return 0 if report.success else 1
    finally:
        shared_state.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="ash-agent-driver")
    parser.add_argument("--spec-stdin", action="store_true")
    parser.add_argument("--agent-id")
    parser.add_argument("--db-path")
    parser.add_argument("--role", default="general")
    parser.add_argument("--task", default="")
    args = parser.parse_args(argv)

    if args.spec_stdin:
        spec = _load_spec()
        if spec.get("kind", "simple") == "durable_task":
            return asyncio.run(_run_durable_task(spec))
        if spec.get("kind", "simple") != "simple":
            raise ValueError("unsupported subagent subprocess kind")
        return _run_simple_spec(spec)
    else:
        if not args.agent_id or not args.db_path:
            parser.error("--agent-id and --db-path are required without --spec-stdin")
        agent_id = args.agent_id
        db_path = args.db_path
        role = args.role
        task = args.task
        raw_allowlist: list[str] = []
        metadata: dict[str, Any] = {}
        sandbox_tier = 1
        workspace_value = ""
        allow_custom_role = False
        token_budget = 4000
        return_budget = 2000

    return _run_simple_spec(
        {
            "agent_id": agent_id,
            "db_path": db_path,
            "role": role,
            "task": task,
            "tool_allowlist": raw_allowlist,
            "metadata": metadata,
            "sandbox_tier": sandbox_tier,
            "workspace_root": workspace_value,
            "allow_custom_role": allow_custom_role,
            "token_budget": token_budget,
            "return_budget": return_budget,
        }
    )


if __name__ == "__main__":
    raise SystemExit(main())
