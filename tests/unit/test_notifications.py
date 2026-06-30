import io

from ui.notifications import (
    NotificationEvent,
    NotificationMethod,
    TerminalNotifier,
    notification_sequence,
    resolve_notification_method,
    sanitize_notification_message,
)


class TtyBuffer(io.StringIO):
    def isatty(self) -> bool:
        return True


class BrokenTty(TtyBuffer):
    def write(self, value: str) -> int:
        raise OSError("terminal closed")


def test_auto_notification_method_uses_conservative_terminal_detection() -> None:
    assert resolve_notification_method("auto", {"TERM_PROGRAM": "iTerm.app"}) == "osc9"
    assert resolve_notification_method("auto", {"TERM": "xterm-kitty"}) == "osc9"
    assert resolve_notification_method("auto", {"WT_SESSION": "123"}) == "bel"
    assert resolve_notification_method("auto", {}) == "bel"


def test_notification_message_is_safe_and_bounded() -> None:
    message = sanitize_notification_message(
        " done\x1b]9;injected\x07\nnext " + "x" * 300
    )

    assert "\x1b" not in message
    assert "\x07" not in message
    assert "\n" not in message
    assert len(message) == 200
    assert message.endswith("...")


def test_osc9_sequence_supports_tmux_passthrough() -> None:
    assert notification_sequence(NotificationMethod.OSC9, "done") == "\x1b]9;done\x07"
    assert (
        notification_sequence(NotificationMethod.OSC9, "done", tmux=True)
        == "\x1bPtmux;\x1b\x1b]9;done\x07\x1b\\"
    )
    assert notification_sequence(NotificationMethod.BEL, "ignored") == "\x07"


def test_terminal_notifier_filters_events_and_requires_a_tty() -> None:
    stream = TtyBuffer()
    notifier = TerminalNotifier(
        "osc9",
        events=["approval_required"],
        stream=stream,
        environment={},
    )

    assert notifier.notify(NotificationEvent.TURN_COMPLETE, "done") is False
    assert notifier.notify(NotificationEvent.APPROVAL_REQUIRED, "approve") is True
    assert stream.getvalue() == "\x1b]9;approve\x07"

    redirected = io.StringIO()
    notifier = TerminalNotifier(
        "bel",
        events=["turn_complete"],
        stream=redirected,
        environment={},
    )
    assert notifier.notify("turn_complete", "done") is False
    assert redirected.getvalue() == ""


def test_terminal_notifier_disables_itself_after_output_failure() -> None:
    notifier = TerminalNotifier(
        "bel",
        events=["turn_complete"],
        stream=BrokenTty(),
        environment={},
    )

    assert notifier.notify("turn_complete", "done") is False
    assert notifier.notify("turn_complete", "done again") is False
