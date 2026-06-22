import asyncio
import io

import pytest

from ui.prompt import PromptInput


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
