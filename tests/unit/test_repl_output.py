import io
import sys

from rich.console import Console

from ash.ui.output import ReplPrinter
from ash.ui.terminal import TerminalUI


def test_inline_repl_printer_preserves_print_contract() -> None:
    ui = TerminalUI(console=Console(file=io.StringIO(), force_terminal=False))
    target = io.StringIO()
    printer = ReplPrinter(ui, viewport=False)

    printer("one", "two", sep="-", end="!", file=target, flush=True)

    assert target.getvalue() == "one-two!"
    assert ui.transcript.snapshot() == ()


def test_viewport_repl_printer_routes_output_and_errors() -> None:
    ui = TerminalUI(console=Console(file=io.StringIO(), force_terminal=False))
    printer = ReplPrinter(ui, viewport=True)

    printer("session", "ready")
    printer("bad input", file=sys.stderr, flush=True)
    printer()

    entries = ui.transcript.snapshot()
    assert [(entry.kind, entry.content) for entry in entries] == [
        ("status", "session ready"),
        ("error", "bad input"),
    ]


def test_viewport_repl_printer_preserves_internal_newlines() -> None:
    ui = TerminalUI(console=Console(file=io.StringIO(), force_terminal=False))
    printer = ReplPrinter(ui, viewport=True)

    printer("first\nsecond")

    assert ui.transcript.snapshot()[0].content == "first\nsecond"
