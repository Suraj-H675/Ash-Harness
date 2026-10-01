"""Model-facing progress updates for the active durable Goal."""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Literal

from pydantic import BaseModel, Field

from ash.core.goals import GoalRecord
from ash.safety.guard import SafetyGuard
from ash.tools.base import BaseTool, ToolResult, count_output_tokens


class UpdateGoalArgs(BaseModel):
    action: Literal["progress", "complete"]
    evidence: str = Field(min_length=1, max_length=16_384)


class UpdateGoalTool(BaseTool):
    name = "update_goal"
    description = (
        "Record concrete evidence for the active session Goal, or mark it complete "
        "only when the objective is actually satisfied. This tool cannot pause, "
        "resume, clear, or create Goals."
    )
    args_schema = UpdateGoalArgs

    def __init__(
        self,
        safety_guard: SafetyGuard,
        update: Callable[[str, str], GoalRecord],
    ) -> None:
        super().__init__(safety_guard)
        self._update = update

    async def run(self, **kwargs) -> ToolResult:
        try:
            args = UpdateGoalArgs(**kwargs)
            goal = self._update(args.action, args.evidence)
        except (KeyError, TypeError, ValueError) as exc:
            return ToolResult(success=False, output="", error=str(exc))
        output = json.dumps(
            {
                "goal_id": goal.goal_id,
                "state": goal.state.value,
                "continuations_used": goal.continuations_used,
                "max_continuations": goal.max_continuations,
                "last_evidence": goal.last_evidence,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
        return ToolResult(
            success=True,
            output=output,
            token_count=count_output_tokens(output),
        )
