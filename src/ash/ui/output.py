"""Interactive REPL output routing for inline and viewport terminal modes."""

from __future__ import annotations

import builtins
import sys
from typing import Any, TextIO

from ash.ui.terminal import TerminalUI
from ash.ui.safe_text import terminal_safe_text


class ReplPrinter:
    """A ``print``-compatible sink that can commit output to the transcript."""

    def __init__(self, ui: TerminalUI, *, viewport: bool) -> None:
        self.ui = ui
        self.viewport = viewport

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
        if not self.viewport:
            builtins.print(
                *(terminal_safe_text(str(value)) for value in values),
                sep=terminal_safe_text(normalized_sep),
                end=terminal_safe_text(normalized_end),
                file=file,
                flush=flush,
            )
            return

        text = normalized_sep.join(str(value) for value in values) + normalized_end
        text = text.removesuffix("\n").removesuffix("\r")
        if not text:
            return
        is_error = file is not None and file is not sys.stdout
        self.ui.write_status(text, error=is_error)
