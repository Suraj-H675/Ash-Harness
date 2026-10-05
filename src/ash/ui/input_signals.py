"""Internal terminal-input control signals."""


class PromptInterrupted(Exception):
    """The user pressed Ctrl-C while Ash owned terminal input."""
