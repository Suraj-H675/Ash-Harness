"""Interactive prompt editor for Ash's terminal session."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Any, Callable, TextIO

from prompt_toolkit import PromptSession
from prompt_toolkit.auto_suggest import AutoSuggestFromHistory
from prompt_toolkit.completion import WordCompleter
from prompt_toolkit.completion.base import Completer, Completion
from prompt_toolkit.document import Document
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.enums import EditingMode

from ash.commands.slash import COMMANDS
from ash.safety.anchored_fs import AnchoredDirectory, AnchoredFilesystemError
from ash.ui.history import PrivateFileHistory
from ash.ui.transcript import Transcript
from ash.ui.viewport import TranscriptViewport


MAX_PATH_COMPLETION_SCAN_ENTRIES = 10_000
MAX_PATH_COMPLETIONS = 200


class AshCompleter(Completer):
    """Complete slash commands, ``@`` paths, symbols, and MCP resources."""

    def __init__(
        self,
        commands: list[str],
        workspace_root: Path,
        *,
        command_descriptions: dict[str, str] | None = None,
        repo_map: Any | None = None,
        mcp_runtime: Any | None = None,
    ) -> None:
        self._commands = self._build_command_completer(
            commands,
            command_descriptions=command_descriptions,
        )
        self._root = workspace_root.resolve()
        self._repo_map = repo_map
        self._mcp_runtime = mcp_runtime

    def set_commands(
        self,
        commands: list[str],
        *,
        command_descriptions: dict[str, str] | None = None,
    ) -> None:
        self._commands = self._build_command_completer(
            commands,
            command_descriptions=command_descriptions,
        )

    @staticmethod
    def _build_command_completer(
        commands: list[str],
        *,
        command_descriptions: dict[str, str] | None = None,
    ) -> WordCompleter:
        metadata = {
            f"/{name}": command.description
            for command in COMMANDS
            for name in (command.name, *command.aliases)
        }
        metadata.update(command_descriptions or {})
        for command in commands:
            metadata.setdefault(command, "custom command")
        return WordCompleter(
            commands,
            sentence=True,
            ignore_case=True,
            meta_dict=metadata,
        )

    def set_providers(
        self,
        *,
        repo_map: Any | None = None,
        mcp_runtime: Any | None = None,
    ) -> None:
        if repo_map is not None:
            self._repo_map = repo_map
        if mcp_runtime is not None:
            self._mcp_runtime = mcp_runtime

    def get_completions(self, document: Document, complete_event):
        word = document.get_word_before_cursor(WORD=True)
        if not word.startswith("@"):
            yield from self._commands.get_completions(document, complete_event)
            return
        typed = word[1:].strip("\"'")
        if typed.startswith("symbol:") or typed.startswith("mcp:"):
            yield from self._extended_completions(typed, word)
            return
        relative = Path(typed)
        parent = (self._root / relative.parent).resolve()
        try:
            parent.relative_to(self._root)
        except ValueError:
            return
        if not parent.is_dir():
            return
        prefix = relative.name.casefold()
        children = []
        try:
            for index, child in enumerate(parent.iterdir(), 1):
                if index > MAX_PATH_COMPLETION_SCAN_ENTRIES:
                    break
                children.append(child)
        except OSError:
            return
        for child in sorted(
            children,
            key=lambda item: item.name.casefold(),
        )[:MAX_PATH_COMPLETIONS]:
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

    def _symbol_completions(self, prefix: str, word: str):
        if self._repo_map is None or not prefix:
            return
        try:
            matches = self._repo_map.find_definitions(prefix, case_sensitive=True)
            if not matches:
                matches = self._repo_map.find_definitions(
                    prefix,
                    case_sensitive=False,
                )
            if not matches:
                matches = [
                    symbol
                    for file_node in self._repo_map.files
                    for symbol in file_node.symbols
                    if prefix.casefold() in symbol.name.casefold()
                ]
        except Exception:
            return
        try:
            matches = matches[:50]
        except Exception:
            return
        for symbol in sorted(matches, key=lambda item: item.name.casefold()):
            path = Path(symbol.file_path)
            try:
                relative = path.resolve().relative_to(self._root).as_posix()
            except ValueError:
                relative = path.as_posix()
            replacement = f"@symbol:{symbol.name}"
            display_meta = f"{symbol.kind} {relative}:{symbol.start_line}"
            yield Completion(
                replacement,
                start_position=-len(word),
                display=replacement,
                display_meta=display_meta[:120],
            )

    def _mcp_completions(self, prefix: str, word: str):
        if self._mcp_runtime is None:
            return
        runtime = self._mcp_runtime

        async def collect() -> list[dict[str, Any]]:
            return await runtime.list_resources()

        try:
            try:
                asyncio.get_running_loop()
            except RuntimeError:
                resources = asyncio.run(collect())
            else:
                return
        except Exception:
            return
        yield from self._render_mcp_completions(resources, prefix=prefix, word=word)

    @staticmethod
    def _render_mcp_completions(
        resources: list[dict[str, Any]], *, prefix: str, word: str
    ):
        candidates: list[tuple[str, str]] = []
        for resource in resources:
            server = str(resource.get("server", ""))
            uri = str(resource.get("uri", ""))
            name = str(resource.get("name") or uri)
            if uri and (not prefix or prefix.casefold() in uri.casefold()):
                candidates.append((f"@mcp:{server}/{uri}", f"MCP {server} {name}"))
        for replacement, meta in sorted(candidates, key=lambda item: item[0])[:50]:
            yield Completion(
                replacement,
                start_position=-len(word),
                display=replacement,
                display_meta=meta[:120],
            )

    async def get_completions_async(self, document: Document, complete_event):
        word = document.get_word_before_cursor(WORD=True)
        typed = word[1:].strip("\"'") if word.startswith("@") else ""
        scheme, separator, prefix = typed.partition(":")
        if separator == ":" and scheme == "mcp":
            if self._mcp_runtime is None:
                return
            try:
                resources = await asyncio.wait_for(
                    self._mcp_runtime.list_resources(),
                    timeout=0.25,
                )
            except Exception:
                return
            for completion in self._render_mcp_completions(
                resources,
                prefix=prefix,
                word=word,
            ):
                yield completion
            return
        for completion in self.get_completions(document, complete_event):
            yield completion

    def _extended_completions(self, typed: str, word: str):
        scheme, separator, prefix = typed.partition(":")
        if separator != ":":
            return
        if scheme == "symbol":
            yield from self._symbol_completions(prefix, word)
        elif scheme == "mcp":
            yield from self._mcp_completions(prefix, word)


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
        header_provider: Callable[[], str] | None = None,
        viewport_status_provider: Callable[[], str] | None = None,
        extra_commands: dict[str, str] | list[str] | None = None,
        input_mode: str = "emacs",
        keybindings: dict[str, list[str]] | None = None,
        workspace_root: Path | None = None,
        transcript: Transcript | None = None,
        tui_mode: str = "inline",
        theme: str = "dark",
        no_color: bool = False,
        repo_map: Any | None = None,
        mcp_runtime: Any | None = None,
        screen_reader_mode: bool = False,
    ) -> None:
        if input_mode not in {"emacs", "vi"}:
            raise ValueError("input_mode must be emacs or vi")
        if tui_mode not in {"viewport", "inline"}:
            raise ValueError("tui_mode must be viewport or inline")
        self.input_stream = input_stream or sys.stdin
        self.interactive = bool(getattr(self.input_stream, "isatty", lambda: False)())
        self._session: PromptSession[str] | None = None
        self._viewport: TranscriptViewport | None = None
        self._completer: AshCompleter | None = None
        self.screen_reader_mode = screen_reader_mode
        if self.interactive:
            path = history_path or (Path.home() / ".ash" / "history")
            if history_path is None:
                try:
                    with AnchoredDirectory.open(
                        path.parent,
                        create=True,
                        private=True,
                        pin_path=True,
                    ) as history_directory:
                        history_directory.validation_path()
                except AnchoredFilesystemError as exc:
                    raise ValueError(
                        f"refusing to use redirected prompt history path: {path}"
                    ) from exc
            history = PrivateFileHistory(path)
            words = sorted(
                {
                    f"/{name}"
                    for command in COMMANDS
                    for name in (command.name, *command.aliases)
                }
            )
            extra_descriptions: dict[str, str] = {}
            if isinstance(extra_commands, dict):
                words.extend(f"/{name}" for name in extra_commands)
                extra_descriptions = {
                    f"/{name}": description
                    for name, description in extra_commands.items()
                }
            else:
                words.extend(f"/{name}" for name in (extra_commands or []))
            words = sorted(set(words))
            completer = AshCompleter(
                words,
                workspace_root or Path.cwd(),
                command_descriptions=extra_descriptions,
                repo_map=repo_map,
                mcp_runtime=mcp_runtime,
            )
            self._completer = completer
            if tui_mode == "viewport" and not screen_reader_mode:
                self._viewport = TranscriptViewport(
                    transcript or Transcript(),
                    history_path=path,
                    history=history,
                    completer=completer,
                    status_provider=viewport_status_provider or status_provider,
                    header_provider=header_provider,
                    input_mode=input_mode,
                    keybindings=keybindings,
                    theme=theme,
                    no_color=no_color,
                )
            else:
                self._session = PromptSession(
                    history=history,
                    auto_suggest=(
                        None if screen_reader_mode else AutoSuggestFromHistory()
                    ),
                    completer=None if screen_reader_mode else completer,
                    complete_while_typing=not screen_reader_mode,
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
                    bottom_toolbar=None if screen_reader_mode else status_provider,
                )

    @property
    def uses_viewport(self) -> bool:
        return self._viewport is not None

    def set_extra_commands(self, commands: dict[str, str] | list[str]) -> None:
        if self._completer is None:
            return
        words = {
            f"/{name}"
            for command in COMMANDS
            for name in (command.name, *command.aliases)
        }
        descriptions: dict[str, str] = {}
        if isinstance(commands, dict):
            words.update(f"/{name}" for name in commands)
            descriptions = {
                f"/{name}": description for name, description in commands.items()
            }
        else:
            words.update(f"/{name}" for name in commands)
        self._completer.set_commands(
            sorted(words),
            command_descriptions=descriptions,
        )

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
