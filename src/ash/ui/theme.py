"""Selectable terminal color themes shared by Rich and prompt-toolkit."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


ThemeName = Literal["dark", "light"]

_THEME_NAMES: tuple[ThemeName, ...] = ("dark", "light")


@dataclass(frozen=True)
class Theme:
    """Terminal palette and semantic style names for one appearance."""

    name: ThemeName
    user_prefix: str
    assistant_prefix: str
    reasoning_prefix: str
    reasoning_body: str
    tool_prefix: str
    approval_prefix: str
    status_prefix: str
    error_prefix: str
    streaming: str
    prompt: str
    composer: str
    separator: str
    status: str
    border_primary: str
    border_approval: str
    approval_prompt: str
    success: str
    error: str


DARK_THEME = Theme(
    name="dark",
    user_prefix="bold #5fd7ff",
    assistant_prefix="bold #5fff87",
    reasoning_prefix="italic #808080",
    reasoning_body="italic #a8a8a8",
    tool_prefix="bold #ffd75f",
    approval_prefix="bold #ffaf5f",
    status_prefix="bold #808080",
    error_prefix="bold #ff5f5f",
    streaming="italic #808080",
    prompt="bold #5fd7ff",
    composer="bg:#1c1c1c",
    separator="#444444",
    status="bg:#262626 #bcbcbc",
    border_primary="cyan",
    border_approval="yellow",
    approval_prompt="bold yellow",
    success="green",
    error="red",
)


LIGHT_THEME = Theme(
    name="light",
    user_prefix="bold #005faf",
    assistant_prefix="bold #007000",
    reasoning_prefix="italic #666666",
    reasoning_body="italic #444444",
    tool_prefix="bold #8a5300",
    approval_prefix="bold #96500a",
    status_prefix="bold #555555",
    error_prefix="bold #b42318",
    streaming="italic #777777",
    prompt="bold #005faf",
    composer="bg:#eaeaea #111111",
    separator="#999999",
    status="bg:#dddddd #222222",
    border_primary="#005faf",
    border_approval="#96500a",
    approval_prompt="bold #96500a",
    success="#007000",
    error="#b42318",
)

_THEMES: dict[str, Theme] = {
    DARK_THEME.name: DARK_THEME,
    LIGHT_THEME.name: LIGHT_THEME,
}


def normalize_theme_name(value: str) -> ThemeName:
    """Validate and canonicalize a configured or user-supplied theme name."""

    normalized = value.strip().casefold()
    if normalized not in _THEMES:
        allowed = ", ".join(_THEME_NAMES)
        raise ValueError(f"theme must be one of: {allowed}")
    return normalized  # type: ignore[return-value]


def get_theme(name: str | None) -> Theme:
    """Return a theme by name; ``None`` selects the dark default."""

    if name is None:
        return DARK_THEME
    return _THEMES[normalize_theme_name(name)]


def viewport_styles(theme: Theme) -> dict[str, str]:
    """Return the prompt-toolkit style mapping for a viewport theme."""

    return {
        "user-prefix": theme.user_prefix,
        "assistant-prefix": theme.assistant_prefix,
        "reasoning-prefix": theme.reasoning_prefix,
        "reasoning": theme.reasoning_body,
        "tool-prefix": theme.tool_prefix,
        "approval-prefix": theme.approval_prefix,
        "status-prefix": theme.status_prefix,
        "error-prefix": theme.error_prefix,
        "streaming": theme.streaming,
        "prompt": theme.prompt,
        "composer": theme.composer,
        "separator": theme.separator,
        "status": theme.status,
    }
