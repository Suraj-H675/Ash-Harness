import io

from ash.ui.output import ReplPrinter


def test_inline_repl_printer_preserves_print_contract() -> None:
    target = io.StringIO()
    printer = ReplPrinter()

    printer("one", "two", sep="-", end="!", file=target, flush=True)

    assert target.getvalue() == "one-two!"


def test_repl_printer_accepts_none_sep_and_end() -> None:
    target = io.StringIO()

    ReplPrinter()("one", "two", sep=None, end=None, file=target)

    assert target.getvalue() == "one two\n"


def test_inline_repl_printer_neutralizes_terminal_controls() -> None:
    target = io.StringIO()
    printer = ReplPrinter()

    printer("safe\x1b[2J\u202ehidden\u202c", file=target)

    rendered = target.getvalue()
    assert "\x1b[2J" not in rendered
    assert "\u202e" not in rendered
    assert "safe\\x1b[2J\\u202ehidden\\u202c" in rendered
