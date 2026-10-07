"""Cheap persistent status data for Ash's retained terminal surface."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

from ash.ui.safe_text import terminal_safe_text

if TYPE_CHECKING:
    from ash.config import AshConfig
    from ash.core.loop import AshLoop


class StatusLine:
    """Expose status values without filesystem, Git, or database probes."""

    def __init__(
        self,
        loop: AshLoop,
        config: AshConfig,
        sandbox: Any | None = None,
    ) -> None:
        self.loop = loop
        self.config = config
        self.sandbox = sandbox
        self._directory = _display_directory(loop.project_root)

    def left(self) -> str:
        """Return model, reasoning capability, and workspace directory."""

        active_model = str(
            getattr(self.loop, "active_model_id", self.config.model)
            or self.config.model
        )
        model_name = active_model.rsplit("/", 1)[-1] or active_model
        capabilities = getattr(getattr(self.loop, "provider", None), "capabilities", None)
        reasoning = (
            "reasoning"
            if bool(getattr(capabilities, "reasoning", False))
            else "no reasoning"
        )
        segments = [
            terminal_safe_text(model_name, single_line=True),
            reasoning,
        ]
        mode = getattr(getattr(self.loop, "permission_policy", None), "mode", None)
        mode_value = getattr(mode, "value", str(mode or ""))
        if mode_value and mode_value != "interactive":
            segments.append(terminal_safe_text(mode_value, single_line=True))
        if self.sandbox is not None and not self.sandbox.is_fully_isolated():
            segments.append("⚠ limited isolation")
        segments.append(terminal_safe_text(self._directory, single_line=True))
        return "  ·  ".join(segments)

    def context_usage(self) -> tuple[int, int]:
        """Return current prompt-context use and the usable context ceiling."""

        fallback_maximum = max(
            1,
            self.config.max_context_tokens - self.config.max_completion_tokens,
        )
        maximum = max(
            1,
            int(getattr(self.loop, "_last_context_maximum", fallback_maximum)),
        )
        used = max(0, int(getattr(self.loop, "_last_context_tokens", 0)))
        return used, maximum


def _display_directory(root: Path) -> str:
    resolved = root.expanduser().resolve()
    home = Path.home().resolve()
    try:
        relative = resolved.relative_to(home)
    except ValueError:
        return str(resolved)
    if not relative.parts:
        return "~"
    return "~/" + relative.as_posix()
