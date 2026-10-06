"""Sanitized print-compatible output for the interactive REPL."""

from __future__ import annotations

import builtins
from typing import Any, TextIO

from ash.ui.safe_text import terminal_safe_text


class ReplPrinter:
    """A ``print``-compatible terminal-safe sink."""

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
        builtins.print(
            *(terminal_safe_text(str(value)) for value in values),
            sep=terminal_safe_text(normalized_sep),
            end=terminal_safe_text(normalized_end),
            file=file,
            flush=flush,
        )
