"""One native Git diff through Ash's mediated tool dispatcher, never the model loop."""

from __future__ import annotations

import io
import unicodedata
from pathlib import Path
from typing import Any, AsyncGenerator, Sequence

from ash.core.loop import AshLoop
from ash.core.redaction import redact_known_secrets
from ash.core.session import SessionStore
from ash.providers.base import ProviderABC, StreamChunk
from ash.providers.messages import MessageInput
from ash.safety.guard import SafetyGuard
from ash.tools.base import ToolResult
from ash.tools.git import GitDiffTool
from ash.ui.headless import HeadlessUI


class _NoModelProvider(ProviderABC):
    @property
    def model_name(self) -> str:
        return "crew-native-tool-only"

    def count_tokens(self, text: str) -> int:
        return 0

    async def stream_chat(
        self,
        messages: Sequence[MessageInput],
        temperature: float = 0.0,
        tools: list[dict[str, Any]] | None = None,
    ) -> AsyncGenerator[StreamChunk, None]:
        raise RuntimeError("native Crew tool execution must never contact a model")
        yield  # pragma: no cover


class _BoundedCrewGitDiffTool(GitDiffTool):
    """Filter tool output *before* Ash persists its tool/audit records."""

    async def run(self, **kwargs: Any) -> ToolResult:
        result = await super().run(**kwargs)
        output = result.output
        if (
            not result.success
            or result.truncated
            or len(output.encode("utf-8")) > 16_384
            or redact_known_secrets(output) != output
            or any(
                unicodedata.category(char) in {"Cc", "Cf", "Cs"}
                and char not in {"\r", "\n", "\t"} for char in output
            )
        ):
            return ToolResult(
                success=False,
                output="",
                error="native Git diagnostic failed bounded output policy",
            )
        return result


async def mediated_git_diff(
    *, workspace: Path, session_database: Path, path: str,
) -> tuple[str, dict[str, Any]]:
    """Use the same intent, permission, audit and recovery path as Ash tools.

    The distinct tool-only session store cannot be opened by a coworker model
    worker; no additional tool, provider, hook or extension is registered.
    """

    guard = SafetyGuard(workspace)
    guard.ensure_project_root_current()
    runtime = AshLoop(
        session_store=SessionStore(session_database),
        provider=_NoModelProvider(),
        safety_guard=guard,
        ui=HeadlessUI(output_format="text", stream=io.StringIO()),
        project_root=workspace,
        tools={"git_diff": _BoundedCrewGitDiffTool(guard)},
        safety_tier="plan",
        max_turn_iterations=1,
        enable_memory_recall=False,
        enable_project_memory=False,
    )
    try:
        session = await runtime.start_session()
        runtime.set_permission_mode("plan")
        result = await runtime.execute_tool(
            "git_diff", {"staged": False, "path": path},
        )
        return session.session_id, result
    finally:
        await runtime.aclose()
