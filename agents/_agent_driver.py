"""Command-line driver for spawned Ash subagents."""

from __future__ import annotations

import argparse
import asyncio
from pathlib import Path

from agents.shared_state import SharedState
from agents.subprocess_agent import SubprocessAgent, make_simple_text_task


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="ash-agent-driver")
    parser.add_argument("--agent-id", required=True)
    parser.add_argument("--db-path", required=True)
    parser.add_argument("--role", default="general")
    parser.add_argument("--task", default="")
    args = parser.parse_args(argv)

    shared_state = SharedState(Path(args.db_path))
    try:
        agent = SubprocessAgent(
            agent_id=args.agent_id,
            role=args.role,
            task=args.task,
            shared_state=shared_state,
            runner=make_simple_text_task(f"completed: {args.task}"),
        )
        report = asyncio.run(agent.run_in_process())
        return 0 if report.success else 1
    finally:
        shared_state.close()


if __name__ == "__main__":
    raise SystemExit(main())
