"""Interactive REPL output routing for inline and viewport terminal modes."""

from __future__ import annotations

import builtins
import sys
from typing import Any, TextIO

from ui.terminal import TerminalUI


class ReplPrinter:
    """A ``print``-compatible sink that can commit output to the transcript."""

    def __init__(self, ui: TerminalUI, *, viewport: bool) -> None:
        self.ui = ui
        self.viewport = viewport

    def __call__(
        self,
        *values: Any,
        sep: str = " ",
        end: str = "\n",
        file: TextIO | None = None,
        flush: bool = False,
    ) -> None:
        if not self.viewport:
            builtins.print(*values, sep=sep, end=end, file=file, flush=flush)
            return

        text = sep.join(str(value) for value in values) + end
        text = text.removesuffix("\n").removesuffix("\r")
        if not text:
            return
        is_error = file is not None and file is not sys.stdout
        self.ui.write_status(text, error=is_error)
