"""Cached interactive status-line composition."""

from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path
from typing import TYPE_CHECKING

from ash.safety.environment import resolve_host_executable
from ash.safety.guard import SafetyGuard, SafetyViolation
from ash.sandbox.process_utils import (
    ProcessTreeUnavailable,
    prepare_scoped_process_launch,
)
from ash.ui.safe_text import terminal_safe_text

if TYPE_CHECKING:
    from ash.config import AshConfig
    from ash.core.loop import AshLoop
    from ash.sandbox import SandboxManager


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
        cache_read = 0
        cache_write = 0
        estimated_cost = 0.0
        if session is not None:
            try:
                usage = self.loop.session_store.get_session_usage(session.session_id)
                cost = usage.cost_usd
                cache_read = usage.cache_read_tokens
                cache_write = usage.cache_write_tokens
                estimated_cost = usage.estimated_cost_usd
            except KeyError:
                pass
        maximum = max(
            1,
            self.config.max_context_tokens - self.config.max_completion_tokens,
        )
        sandbox_label = self.sandbox.backend_name
        if not self.sandbox.is_fully_isolated():
            sandbox_label += "!"
        display_model = terminal_safe_text(self.config.model, single_line=True)
        display_root = terminal_safe_text(str(self.loop.project_root), single_line=True)
        self._cached = (
            f" {display_model} | {self.loop.permission_policy.mode.value} | "
            f"git:{git_branch(self.loop.project_root)} | "
            f"ctx ~{self.loop._last_context_tokens}/{maximum} | "
            f"cache:{cache_read}r/{cache_write}w | "
            f"{'~' if estimated_cost > 0 else ''}${cost:.4f} | sb:{sandbox_label} | "
            f"s:{session_id} | {display_root} "
        )
        return self._cached


def git_branch(root: Path) -> str:
    """Return branch or detached commit without invoking a shell or pager."""

    try:
        opened = os.stat(root)
        expected_identity = (opened.st_dev, opened.st_ino)
        guard = SafetyGuard(root)
        git = resolve_host_executable("git", workspace_root=root, cwd=root)
        if git is None:
            return "none"
        result = _run_git_probe(
            [git, "symbolic-ref", "--quiet", "--short", "HEAD"],
            root=root,
            guard=guard,
            expected_identity=expected_identity,
        )
        branch = result.stdout.strip()
        if branch:
            return terminal_safe_text(branch, single_line=True)
        detached = _run_git_probe(
            [git, "rev-parse", "--short", "HEAD"],
            root=root,
            guard=guard,
            expected_identity=expected_identity,
        ).stdout.strip()
        return (
            f"@{terminal_safe_text(detached, single_line=True)}"
            if detached
            else "none"
        )
    except (
        OSError,
        ValueError,
        SafetyViolation,
        ProcessTreeUnavailable,
        subprocess.SubprocessError,
    ):
        return "none"


def _run_git_probe(
    command: list[str],
    *,
    root: Path,
    guard: SafetyGuard,
    expected_identity: tuple[int, int],
) -> subprocess.CompletedProcess[str]:
    with prepare_scoped_process_launch(
        command,
        cwd=root,
        guard=guard,
        expected_cwd_identity=expected_identity,
    ) as launch:
        if launch.pass_fds:
            return subprocess.run(
                list(launch.argv),
                cwd=launch.cwd,
                pass_fds=launch.pass_fds,
                check=False,
                capture_output=True,
                text=True,
                timeout=0.25,
            )
        return subprocess.run(
            list(launch.argv),
            cwd=launch.cwd,
            check=False,
            capture_output=True,
            text=True,
            timeout=0.25,
        )
