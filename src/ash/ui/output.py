"""Sanitized print-compatible output for the interactive REPL."""

from __future__ import annotations

import builtins
import sys
from typing import Any, Callable, TextIO

from ash.ui.safe_text import terminal_safe_text


class ReplPrinter:
    """A ``print``-compatible terminal-safe sink."""

    def __init__(
        self,
        sink: Callable[..., None] | None = None,
    ) -> None:
        self._sink = sink

    def __call__(
        self,
        *values: Any,
        sep: str | None = " ",
        end: str | None = "\n",
        file: TextIO | None = None,
        flush: bool = False,
    ) -> None:
        normalized_sep = " " if sep is None else sep
        normalized_end = "\n" if end is None else end
        safe_values = [terminal_safe_text(str(value)) for value in values]
        safe_sep = terminal_safe_text(normalized_sep)
        safe_end = terminal_safe_text(normalized_end)
        if self._sink is not None and (
            file is None or file is sys.stdout or file is sys.stderr
        ):
            self._sink(
                safe_sep.join(safe_values) + safe_end,
                error=file is sys.stderr,
            )
            return
        builtins.print(*safe_values, sep=safe_sep, end=safe_end, file=file, flush=flush)
