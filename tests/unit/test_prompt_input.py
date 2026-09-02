import asyncio
import io

import pytest
from prompt_toolkit.completion import CompleteEvent
from prompt_toolkit.document import Document

import ash.ui.prompt as prompt_module
from ash.ui.prompt import AshCompleter, PromptInput


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


def test_prompt_completion_updates_after_plugin_reload(
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
        extra_commands=["old:command"],
    )

    prompt.set_extra_commands(["example:review"])
    completer = captured["completer"]
    completions = list(
        completer.get_completions(
            Document("/example:r"), CompleteEvent(completion_requested=True)
        )
    )

    assert [completion.text for completion in completions] == ["/example:review"]


def test_path_completion_scans_a_bounded_number_of_entries(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path.resolve()
    original_iterdir = prompt_module.Path.iterdir

    def fake_iterdir(path):
        if path == root:
            return (root / f"file-{index:05d}.txt" for index in range(20_000))
        return original_iterdir(path)

    monkeypatch.setattr(prompt_module.Path, "iterdir", fake_iterdir)
    completer = AshCompleter([], root)

    completions = list(
        completer.get_completions(
            Document("@file-"), CompleteEvent(completion_requested=True)
        )
    )

    assert len(completions) == prompt_module.MAX_PATH_COMPLETIONS
