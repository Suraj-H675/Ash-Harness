"""Structured logging for Ash.

Uses loguru as the backend. Configured once at import time.
Modules that need a logger import ``get_logger`` rather than creating
their own handlers.
"""

from __future__ import annotations

import sys
from contextlib import contextmanager
from typing import Any

# loguru's logger is the default export.
from loguru import logger as _loguru_logger

# Module-level logger used by ``get_logger()``.
_logger: Any = None


def _configure(*, no_color: bool = False) -> Any:
    """Configure loguru with Ash's preferred defaults."""

    _loguru_logger.remove()

    # Write to stderr, with a format that distinguishes levels and
    # timestamps.  Colour is left to the terminal's own colour support.
    _loguru_logger.add(
        sys.stderr,
        format="<level>{time:YYYY-MM-DD HH:mm:ss}</level> | <level>{level: <8}</level> | <level>{name}</level>:<level>{function}</level> — <level>{message}</level>",
        level="INFO",
        colorize=not no_color,
    )

    return _loguru_logger


def configure_logging(*, no_color: bool) -> Any:
    """Apply the resolved Ash color policy without duplicating handlers."""

    global _logger
    _logger = _configure(no_color=no_color)
    return _logger


def get_logger(name: str) -> Any:
    """Return a logger scoped to ``name`` (typically a module name).

    The first call configures the global logger; subsequent calls return
    child loggers derived from it.
    """

    global _logger
    if _logger is None:
        _logger = _configure()
    return _logger.bind(name=name)


@contextmanager
def temporary_level(level: str) -> Any:
    """Temporarily change the log level within the context block.

    Usage::

        with temporary_level("DEBUG"):
            get_logger("ash.foo").debug("very noisy")
    """

    global _logger
    if _logger is None:
        get_logger("ash")  # force configure

    token = _logger.configure(partial=True, level=level)
    try:
        yield _logger
    finally:
        _logger.configure(partial=True, level=token)
