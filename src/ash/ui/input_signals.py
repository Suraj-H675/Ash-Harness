"""Internal terminal-input control signals."""

import asyncio


class PromptInterrupted(Exception):
    """The user pressed Ctrl-C while Ash owned terminal input."""


class UserRequestedTurnCancellation(asyncio.CancelledError):
    """A live-turn input boundary received an explicit user interrupt."""
