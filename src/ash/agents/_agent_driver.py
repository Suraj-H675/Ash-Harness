"""Command-line driver for spawned Ash subagents."""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path
from typing import Any

from ash.safe_io import strict_json_loads
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
    else:
        if not args.agent_id or not args.db_path:
            parser.error("--agent-id and --db-path are required without --spec-stdin")
        agent_id = args.agent_id
        db_path = args.db_path
        role = args.role
        task = args.task
        raw_allowlist = []
        metadata = {}
        sandbox_tier = 1
        workspace_value = ""
        allow_custom_role = False
        token_budget = 4000
        return_budget = 2000

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


if __name__ == "__main__":
    raise SystemExit(main())
