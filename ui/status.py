"""Cached interactive status-line composition."""

from __future__ import annotations

import subprocess
import time
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from config import AshConfig
    from core.loop import AshLoop
    from sandbox import SandboxManager


class StatusLine:
    """Build a concise toolbar without probing Git/SQLite on every redraw."""

    def __init__(
        self,
        loop: AshLoop,
        config: AshConfig,
        sandbox: SandboxManager,
        *,
        refresh_seconds: float = 1.0,
    ) -> None:
        self.loop = loop
        self.config = config
        self.sandbox = sandbox
        self.refresh_seconds = refresh_seconds
        self._last_refresh = 0.0
        self._cached = ""

    def __call__(self) -> str:
        now = time.monotonic()
        if self._cached and now - self._last_refresh < self.refresh_seconds:
            return self._cached
        self._last_refresh = now
        session = self.loop.current_session
        session_id = session.session_id[:8] if session else "none"
        cost = 0.0
        if session is not None:
            try:
                cost = self.loop.session_store.get_session_usage(
                    session.session_id
                ).cost_usd
            except KeyError:
                pass
        maximum = max(
            1,
            self.config.max_context_tokens - self.config.max_completion_tokens,
        )
        self._cached = (
            f" {self.config.model} | {self.loop.permission_policy.mode.value} | "
            f"git:{git_branch(self.loop.project_root)} | "
            f"ctx ~{self.loop._last_context_tokens}/{maximum} | "
            f"${cost:.4f} | sb:{self.sandbox.backend_name} | "
            f"s:{session_id} | {self.loop.project_root} "
        )
        return self._cached


def git_branch(root: Path) -> str:
    """Return branch or detached commit without invoking a shell or pager."""

    try:
        result = subprocess.run(
            ["git", "symbolic-ref", "--quiet", "--short", "HEAD"],
            cwd=root,
            check=False,
            capture_output=True,
            text=True,
            timeout=0.25,
        )
        branch = result.stdout.strip()
        if branch:
            return branch
        detached = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=root,
            check=False,
            capture_output=True,
            text=True,
            timeout=0.25,
        ).stdout.strip()
        return f"@{detached}" if detached else "none"
    except (OSError, subprocess.SubprocessError):
        return "none"
