"""Interactive prompt editor for Ash's terminal session."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Callable, TextIO

from prompt_toolkit import PromptSession
from prompt_toolkit.auto_suggest import AutoSuggestFromHistory
from prompt_toolkit.completion import WordCompleter
from prompt_toolkit.completion.base import Completer, Completion
from prompt_toolkit.document import Document
from prompt_toolkit.history import FileHistory
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.enums import EditingMode

from cli.slash import COMMANDS
from ui.transcript import Transcript
from ui.viewport import TranscriptViewport


class AshCompleter(Completer):
    """Complete slash commands and workspace-relative ``@`` paths."""

    def __init__(self, commands: list[str], workspace_root: Path) -> None:
        self._commands = WordCompleter(commands, sentence=True)
        self._root = workspace_root.resolve()

    def get_completions(self, document: Document, complete_event):
        word = document.get_word_before_cursor(WORD=True)
        if not word.startswith("@"):
            yield from self._commands.get_completions(document, complete_event)
            return
        typed = word[1:].strip("\"'")
        relative = Path(typed)
        parent = (self._root / relative.parent).resolve()
        try:
            parent.relative_to(self._root)
        except ValueError:
            return
        if not parent.is_dir():
            return
        prefix = relative.name.casefold()
        try:
            children = sorted(parent.iterdir(), key=lambda item: item.name.casefold())
        except OSError:
            return
        for child in children[:200]:
            if not child.name.casefold().startswith(prefix):
                continue
            resolved = child.resolve()
            try:
                rel = resolved.relative_to(self._root).as_posix()
            except ValueError:
                continue
            if child.is_dir():
                rel += "/"
            replacement = f'@"{rel}"' if " " in rel else f"@{rel}"
            yield Completion(
                replacement,
                start_position=-len(word),
                display=replacement,
                display_meta="directory" if child.is_dir() else "file",
            )


def _key_bindings(bindings_by_action: dict[str, list[str]]) -> KeyBindings:
    bindings = KeyBindings()

    def _insert_newline(event) -> None:
        event.current_buffer.insert_text("\n")

    def _open_editor(event) -> None:
        event.current_buffer.open_in_editor()

    handlers = {
        "newline": _insert_newline,
        "open_editor": _open_editor,
    }
    for action, sequences in bindings_by_action.items():
        handler = handlers[action]
        for sequence in sequences:
            bindings.add(*sequence.split())(handler)

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
        input_mode: str = "emacs",
        keybindings: dict[str, list[str]] | None = None,
        workspace_root: Path | None = None,
        transcript: Transcript | None = None,
        tui_mode: str = "inline",
    ) -> None:
        if input_mode not in {"emacs", "vi"}:
            raise ValueError("input_mode must be emacs or vi")
        if tui_mode not in {"viewport", "inline"}:
            raise ValueError("tui_mode must be viewport or inline")
        self.input_stream = input_stream or sys.stdin
        self.interactive = bool(getattr(self.input_stream, "isatty", lambda: False)())
        self._session: PromptSession[str] | None = None
        self._viewport: TranscriptViewport | None = None
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
            completer = AshCompleter(words, workspace_root or Path.cwd())
            if tui_mode == "viewport":
                self._viewport = TranscriptViewport(
                    transcript or Transcript(),
                    history_path=path,
                    completer=completer,
                    status_provider=status_provider,
                    input_mode=input_mode,
                    keybindings=keybindings,
                )
            else:
                self._session = PromptSession(
                    history=FileHistory(str(path)),
                    auto_suggest=AutoSuggestFromHistory(),
                    completer=completer,
                    complete_while_typing=True,
                    key_bindings=_key_bindings(
                        keybindings
                        if keybindings is not None
                        else {
                            "newline": ["escape enter", "c-j"],
                            "open_editor": ["c-x c-e"],
                        }
                    ),
                    editing_mode=(
                        EditingMode.VI if input_mode == "vi" else EditingMode.EMACS
                    ),
                    multiline=False,
                    enable_open_in_editor=True,
                    bottom_toolbar=status_provider,
                )

    @property
    def uses_viewport(self) -> bool:
        return self._viewport is not None

    async def read(self, prompt: str = "> ") -> str:
        if self._viewport is not None:
            return await self._viewport.read(prompt)
        if self._session is not None:
            return await self._session.prompt_async(prompt)
        line = self.input_stream.readline()
        if line == "":
            raise EOFError
        return line.rstrip("\r\n")

    def close(self) -> None:
        if self._viewport is not None:
            self._viewport.close()
