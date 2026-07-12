"""Safe terminal-native desktop notifications for interactive Ash sessions."""

from __future__ import annotations

import os
import sys
import unicodedata
from collections.abc import Mapping
from enum import StrEnum
from typing import Protocol, TextIO


class NotificationMethod(StrEnum):
    """Terminal sequence used to request a notification."""

    OFF = "off"
    AUTO = "auto"
    OSC9 = "osc9"
    BEL = "bel"


class NotificationEvent(StrEnum):
    """Interactive events that can request user attention."""

    TURN_COMPLETE = "turn_complete"
    APPROVAL_REQUIRED = "approval_required"


class NotificationSink(Protocol):
    """Structural interface consumed by the interactive turn controller."""

    def notify(self, event: str | NotificationEvent, message: str) -> bool: ...


_OSC9_TERM_PROGRAMS = {
    "ghostty",
    "iterm.app",
    "iterm2",
    "warpstaging",
    "warpterminal",
    "warpterminal.app",
    "wezterm",
}
_MAX_MESSAGE_LENGTH = 200


def resolve_notification_method(
    method: str | NotificationMethod,
    environment: Mapping[str, str] | None = None,
) -> NotificationMethod:
    """Resolve ``auto`` using conservative terminal capability detection."""

    selected = NotificationMethod(method)
    if selected != NotificationMethod.AUTO:
        return selected
    env = os.environ if environment is None else environment
    term_program = env.get("TERM_PROGRAM", "").strip().casefold()
    supports_osc9 = (
        term_program in _OSC9_TERM_PROGRAMS
        or env.get("TERM", "").strip().casefold() == "xterm-kitty"
        or bool(env.get("KITTY_WINDOW_ID"))
    )
    return NotificationMethod.OSC9 if supports_osc9 else NotificationMethod.BEL


def sanitize_notification_message(message: str) -> str:
    """Remove control characters and bound content embedded in OSC sequences."""

    without_controls = "".join(
        character
        for character in message
        if not unicodedata.category(character).startswith("C")
    )
    normalized = " ".join(without_controls.split()) or "Ash needs attention"
    if len(normalized) <= _MAX_MESSAGE_LENGTH:
        return normalized
    return normalized[: _MAX_MESSAGE_LENGTH - 3].rstrip() + "..."


def notification_sequence(
    method: NotificationMethod,
    message: str,
    *,
    tmux: bool = False,
) -> str:
    """Build a BEL or OSC 9 sequence, including tmux DCS passthrough."""

    if method == NotificationMethod.BEL:
        return "\x07"
    if method != NotificationMethod.OSC9:
        return ""
    sequence = f"\x1b]9;{sanitize_notification_message(message)}\x07"
    if not tmux:
        return sequence
    escaped = sequence.replace("\x1b", "\x1b\x1b")
    return f"\x1bPtmux;{escaped}\x1b\\"


class TerminalNotifier:
    """Emit configured notification events without affecting turn execution."""

    def __init__(
        self,
        method: str | NotificationMethod = NotificationMethod.OFF,
        *,
        events: list[str] | tuple[str, ...] | set[str] | frozenset[str] = (),
        stream: TextIO | None = None,
        environment: Mapping[str, str] | None = None,
    ) -> None:
        self._environment = os.environ if environment is None else environment
        self.method = resolve_notification_method(method, self._environment)
        self.events = frozenset(NotificationEvent(event) for event in events)
        self.stream = sys.stdout if stream is None else stream
        self._tmux = bool(self._environment.get("TMUX"))
        self._available = self.method != NotificationMethod.OFF and bool(
            getattr(self.stream, "isatty", lambda: False)()
        )

    def notify(self, event: str | NotificationEvent, message: str) -> bool:
        """Emit one event and return whether its sequence was written."""

        if not self._available or NotificationEvent(event) not in self.events:
            return False
        sequence = notification_sequence(self.method, message, tmux=self._tmux)
        if not sequence:
            return False
        try:
            self.stream.write(sequence)
            self.stream.flush()
        except (OSError, ValueError):
            self._available = False
            return False
        return True
