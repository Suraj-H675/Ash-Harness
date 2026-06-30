import asyncio
import io

import pytest

import ui.prompt as prompt_module
from ui.prompt import PromptInput


class TtyStringIO(io.StringIO):
    def isatty(self) -> bool:
        return True


def test_redirected_input_uses_line_fallback() -> None:
    stream = io.StringIO("hello\n")
    prompt = PromptInput(input_stream=stream)
    assert prompt.interactive is False
    assert asyncio.run(prompt.read()) == "hello"


def test_redirected_eof_is_reported() -> None:
    prompt = PromptInput(input_stream=io.StringIO(""))
    with pytest.raises(EOFError):
        asyncio.run(prompt.read())


def test_invalid_input_mode_is_rejected() -> None:
    with pytest.raises(ValueError, match="input_mode"):
        PromptInput(input_stream=io.StringIO(""), input_mode="modal")
    with pytest.raises(ValueError, match="tui_mode"):
        PromptInput(input_stream=io.StringIO(""), tui_mode="floating")


def test_screen_reader_mode_uses_reduced_dynamic_prompt(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured = {}

    class FakePromptSession:
        def __init__(self, **kwargs) -> None:
            captured.update(kwargs)

    monkeypatch.setattr(prompt_module, "PromptSession", FakePromptSession)

    prompt = PromptInput(
        input_stream=TtyStringIO(),
        history_path=tmp_path / "history",
        tui_mode="viewport",
        screen_reader_mode=True,
    )

    assert prompt.uses_viewport is False
    assert prompt.screen_reader_mode is True
    assert captured["auto_suggest"] is None
    assert captured["completer"] is None
    assert captured["complete_while_typing"] is False
    assert captured["bottom_toolbar"] is None
