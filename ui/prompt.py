"""Interactive prompt editor for Ash's terminal session."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Callable, TextIO

from prompt_toolkit import PromptSession
from prompt_toolkit.auto_suggest import AutoSuggestFromHistory
from prompt_toolkit.completion import WordCompleter
from prompt_toolkit.history import FileHistory
from prompt_toolkit.key_binding import KeyBindings

from cli.slash import COMMANDS


def _key_bindings() -> KeyBindings:
    bindings = KeyBindings()

    @bindings.add("escape", "enter")
    def _insert_newline(event) -> None:
        event.current_buffer.insert_text("\n")

    @bindings.add("c-j")
    def _insert_newline_ctrl_j(event) -> None:
        event.current_buffer.insert_text("\n")

    return bindings


class PromptInput:
    """Prompt-toolkit input with a deterministic fallback for redirected stdin."""

    def __init__(
        self,
        *,
        history_path: Path | None = None,
        input_stream: TextIO | None = None,
        status_provider: Callable[[], str] | None = None,
        extra_commands: list[str] | None = None,
    ) -> None:
        self.input_stream = input_stream or sys.stdin
        self.interactive = bool(getattr(self.input_stream, "isatty", lambda: False)())
        self._session: PromptSession[str] | None = None
        if self.interactive:
            path = history_path or (Path.home() / ".ash" / "history")
            path.parent.mkdir(parents=True, exist_ok=True)
            words = sorted(
                {
                    f"/{name}"
                    for command in COMMANDS
                    for name in (command.name, *command.aliases)
                }
            )
            words.extend(f"/{name}" for name in (extra_commands or []))
            words = sorted(set(words))
            self._session = PromptSession(
                history=FileHistory(str(path)),
                auto_suggest=AutoSuggestFromHistory(),
                completer=WordCompleter(words, sentence=True),
                complete_while_typing=True,
                key_bindings=_key_bindings(),
                multiline=False,
                enable_open_in_editor=True,
                bottom_toolbar=status_provider,
            )

    async def read(self, prompt: str = "> ") -> str:
        if self._session is not None:
            return await self._session.prompt_async(prompt)
        line = self.input_stream.readline()
        if line == "":
            raise EOFError
        return line.rstrip("\r\n")
