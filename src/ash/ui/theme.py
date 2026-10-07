"""Selectable terminal color themes shared by Rich and prompt-toolkit."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from prompt_toolkit.styles import BaseStyle, DummyStyle, Style


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
    composer_label: str
    separator: str
    header: str
    header_brand: str
    header_meta: str
    status: str
    empty_title: str
    muted: str
    diff_added: str
    diff_removed: str
    diff_hunk: str
    diff_context: str
    border_primary: str
    border_approval: str
    approval_prompt: str
    success: str
    error: str
    user_message: str = ""
    activity: str = ""


DARK_THEME = Theme(
    name="dark",
    user_prefix="bold #5f87ff",
    assistant_prefix="bold #d0d0d0",
    reasoning_prefix="italic #808080",
    reasoning_body="italic #a8a8a8",
    tool_prefix="bold #ffd75f",
    approval_prefix="bold #ffaf5f",
    status_prefix="bold #808080",
    error_prefix="bold #ff5f5f",
    streaming="italic #808080",
    prompt="bold #5fd7ff",
    composer="bg:#303030 #e6e6e6",
    composer_label="bold #5fd7ff bg:#303030",
    separator="#444444",
    header="bg:#161616 #d0d0d0",
    header_brand="bold #5fd7ff bg:#161616",
    header_meta="bg:#161616 #808080",
    status="bg:#202020 #a8a8a8",
    empty_title="bold #d0d0d0",
    muted="#808080",
    diff_added="#c8c8c8 bg:#14532d",
    diff_removed="#c8c8c8 bg:#6b252e",
    diff_hunk="bold #5f87af",
    diff_context="#a8a8a8",
    border_primary="cyan",
    border_approval="yellow",
    approval_prompt="bold yellow",
    success="green",
    error="red",
    user_message="bold #6f95ff on #303030",
    activity="bold #c8c8c8",
)


LIGHT_THEME = Theme(
    name="light",
    user_prefix="bold #005faf",
    assistant_prefix="bold #222222",
    reasoning_prefix="italic #666666",
    reasoning_body="italic #444444",
    tool_prefix="bold #8a5300",
    approval_prefix="bold #96500a",
    status_prefix="bold #555555",
    error_prefix="bold #b42318",
    streaming="italic #777777",
    prompt="bold #005faf",
    composer="bg:#eaeaea #111111",
    composer_label="bold #005faf bg:#eaeaea",
    separator="#999999",
    header="bg:#f2f2f2 #222222",
    header_brand="bold #005faf bg:#f2f2f2",
    header_meta="bg:#f2f2f2 #666666",
    status="bg:#dddddd #333333",
    empty_title="bold #222222",
    muted="#666666",
    diff_added="#333333 bg:#dff3e4",
    diff_removed="#333333 bg:#f8dddd",
    diff_hunk="bold #005faf",
    diff_context="#555555",
    border_primary="#005faf",
    border_approval="#96500a",
    approval_prompt="bold #96500a",
    success="#007000",
    error="#b42318",
    user_message="bold #005faf on #eaeaea",
    activity="bold #444444",
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


def terminal_styles(theme: Theme) -> dict[str, str]:
    """Return the prompt-toolkit style mapping for Ash's retained surface."""

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
        "composer-label": theme.composer_label,
        "activity": theme.activity,
        "activity-dots": theme.muted,
        "separator": theme.separator,
        "header": theme.header,
        "header-brand": theme.header_brand,
        "header-meta": theme.header_meta,
        "status": theme.status,
        "context-used": theme.user_prefix,
        "context-empty": theme.muted,
        "empty-title": theme.empty_title,
        "muted": theme.muted,
        "diff-added": theme.diff_added,
        "diff-removed": theme.diff_removed,
        "diff-hunk": theme.diff_hunk,
        "diff-context": theme.diff_context,
        "selected": "reverse bold",
        "option": "",
        "meta": theme.muted,
    }


def overlay_styles(theme: Theme) -> dict[str, str]:
    """Return one coherent Ash palette for searchable full-screen overlays."""

    selected = (
        "bold bg:#005f87 #ffffff"
        if theme.name == "dark"
        else "bold bg:#d7eaff #003b70"
    )
    return {
        "title": theme.user_prefix,
        "muted": theme.muted,
        "label": theme.empty_title,
        "selected": selected,
        "option": "bold",
        "session-title": "bold",
        "usage": "bold",
        "meta": theme.muted,
        "current": theme.user_prefix,
        "separator": theme.separator,
        "detail": theme.composer,
        "preview": theme.composer,
        "footer": theme.status,
        "empty": f"italic {theme.muted}",
    }


def prompt_style(styles: dict[str, str], *, no_color: bool = False) -> BaseStyle:
    """Build a prompt-toolkit style while honoring Ash's no-color contract."""

    if no_color:
        return DummyStyle()
    return Style.from_dict(styles)
