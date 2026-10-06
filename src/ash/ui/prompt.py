"""Interactive prompt editor for Ash's terminal session."""

from __future__ import annotations

import asyncio
import os
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
from ash.provider_catalog import BUILTIN_PROVIDERS
from ash.safety.anchored_fs import AnchoredDirectory, AnchoredFilesystemError
from ash.ui.history import PrivateFileHistory
from ash.ui.input_signals import PromptInterrupted
from ash.ui.safe_text import terminal_safe_text
from ash.ui.transcript import Transcript
from ash.ui.viewport import TranscriptViewport, ViewportChoice


MAX_PATH_COMPLETION_SCAN_ENTRIES = 10_000
MAX_PATH_COMPLETIONS = 200


def terminal_supports_cursor_ui(
    *,
    input_stream: TextIO | None = None,
    output_stream: TextIO | None = None,
    environment: dict[str, str] | None = None,
) -> bool:
    """Return whether cursor-addressing/full-screen terminal UI is appropriate."""

    stdin = input_stream or sys.stdin
    stdout = output_stream or sys.stdout
    if not bool(getattr(stdin, "isatty", lambda: False)()):
        return False
    if not bool(getattr(stdout, "isatty", lambda: False)()):
        return False
    env = os.environ if environment is None else environment
    term = env.get("TERM", "").strip().casefold()
    return term not in {"dumb", "unknown"}


_SLASH_CANONICAL = {
    name: command.name
    for command in COMMANDS
    for name in (command.name, *command.aliases)
}

_SLASH_FIRST_ARGUMENTS: dict[str, tuple[tuple[str, str], ...]] = {
    "help": tuple((command.name, command.description) for command in COMMANDS),
    "model": tuple((f"{provider.id}/", f"{provider.name} provider") for provider in BUILTIN_PROVIDERS),
    "models": (("--refresh", "Probe the active provider's live model catalog"),),
    "sessions": (("search", "Search prior conversation text"), ("prune", "Delete old sessions")),
    "export": (("jsonl", "Export JSONL"), ("markdown", "Export Markdown")),
    "context": (("--provenance", "Show context provenance details"),),
    "capabilities": (("--refresh", "Refresh negotiated model capabilities"),),
    "plan": (("on", "Enable sprint planning"), ("off", "Disable sprint planning")),
    "goal": (("pause", "Pause the active goal"), ("resume", "Resume the active goal"), ("clear", "Clear the active goal")),
    "plugins": (("install", "Install a plugin"), ("update", "Update a plugin"), ("enable", "Enable a plugin"), ("disable", "Disable a plugin"), ("uninstall", "Uninstall a plugin")),
    "agents": (("--full", "Show full agent details"), ("stop", "Stop an agent"), ("resume", "Resume an agent")),
    "diff": (("--staged", "Show staged changes"), ("--turn", "Show the latest Ash turn diff")),
    "review": (("worktree", "Review working tree changes"), ("staged", "Review staged changes"), ("commit", "Review a commit"), ("branch", "Review against a base branch")),
    "permissions": (
        ("modes", "Compare permission modes"),
        ("interactive", "Interactive permission mode"),
        ("auto_edit", "Auto-edit permission mode"),
        ("plan", "Plan-only permission mode"),
        ("auto_approve", "Auto-approve permission mode"),
        ("dry_run", "Dry-run permission mode"),
        ("allow", "Persistently allow a tool"),
        ("ask", "Persistently ask for a tool"),
        ("deny", "Persistently deny a tool"),
        ("revoke", "Remove allow rules for a tool"),
        ("remove", "Remove a permission rule by ID"),
    ),
    "browser": (("status", "Show browser runtime status"), ("inspect", "Inspect a CDP browser source"), ("connect", "Connect browser tools"), ("disconnect", "Disconnect browser tools"), ("reset-profile", "Clear the Ash browser profile")),
    "mcp": (
        ("status", "Show MCP server status"),
        ("refresh", "Reload MCP capabilities"),
        ("login", "Authorize an OAuth MCP server"),
        ("logout", "Clear MCP OAuth authorization"),
        ("tools", "List MCP tools"),
        ("resources", "List MCP resources"),
        ("prompts", "List MCP prompts"),
        ("watch", "Watch an MCP resource"),
        ("unwatch", "Stop watching an MCP resource"),
        ("watches", "List MCP resource watches"),
        ("tasks", "List MCP tasks"),
        ("cancel", "Cancel an MCP task"),
    ),
    "memory": (("status", "Show memory status"), ("index", "Index one file"), ("index-workspace", "Index workspace files"), ("search", "Search memory"), ("export", "Export memory"), ("clear", "Clear project memory")),
}

_SLASH_SECOND_ARGUMENTS: dict[tuple[str, str], tuple[tuple[str, str], ...]] = {
    ("mcp", "status"): (("--json", "Render MCP status as JSON"),),
    ("plugins", "update"): (("--all", "Update every installed plugin"),),
    ("browser", "connect"): (("--reuse-storage-state", "Reuse bounded user-owned browser storage state"),),
}


def _ordered_slash_words(extra_names: list[str] | tuple[str, ...] = ()) -> list[str]:
    core = [
        f"/{name}"
        for command in COMMANDS
        for name in (command.name, *command.aliases)
    ]
    extras = sorted((f"/{name}" for name in extra_names), key=str.casefold)
    return list(dict.fromkeys((*core, *extras)))


class AshCompleter(Completer):
    """Complete slash commands, ``@`` paths, symbols, and MCP resources."""

    def __init__(
        self,
        commands: list[str],
        workspace_root: Path,
        *,
        command_descriptions: dict[str, str] | None = None,
        model_choices: list[str] | None = None,
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
        self._model_choices = list(dict.fromkeys(model_choices or []))

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
        builtin_order = [
            f"/{name}"
            for command in COMMANDS
            for name in (command.name, *command.aliases)
        ]
        ordered_commands = [item for item in builtin_order if item in commands]
        ordered_commands.extend(
            item for item in commands if item not in set(ordered_commands)
        )
        metadata = {
            f"/{name}": command.description
            for command in COMMANDS
            for name in (command.name, *command.aliases)
        }
        for command, description in (command_descriptions or {}).items():
            metadata.setdefault(command, description)
        for command in commands:
            metadata.setdefault(command, "custom command")
        return WordCompleter(
            ordered_commands,
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

    def set_model_choices(self, model_choices: list[str]) -> None:
        self._model_choices = list(dict.fromkeys(model_choices))

    def get_completions(self, document: Document, complete_event):
        word = document.get_word_before_cursor(WORD=True)
        if not word.startswith("@"):
            if document.text_before_cursor.startswith("/") and any(
                character.isspace() for character in document.text_before_cursor
            ):
                yield from self._slash_argument_completions(document.text_before_cursor)
                return
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

    def _slash_argument_completions(self, text: str):
        command_token, separator, remainder = text.partition(" ")
        if not separator or not command_token.startswith("/"):
            return
        canonical = _SLASH_CANONICAL.get(command_token[1:].casefold())
        if canonical is None:
            return

        trailing_space = bool(remainder) and remainder[-1].isspace()
        parts = remainder.split()
        if trailing_space:
            completed = parts
            prefix = ""
        elif parts:
            completed = parts[:-1]
            prefix = parts[-1]
        else:
            completed = []
            prefix = ""

        if not completed:
            candidates = (
                self._model_argument_candidates(prefix)
                if canonical == "model"
                else _SLASH_FIRST_ARGUMENTS.get(canonical, ())
            )
        elif len(completed) == 1:
            candidates = _SLASH_SECOND_ARGUMENTS.get(
                (canonical, completed[0].casefold()),
                (),
            )
            if canonical == "rewind" and not completed[0].startswith("-"):
                candidates = (("--files", "Restore direct file edits too"),)
        else:
            candidates = AshCompleter._slash_followup_candidates(
                canonical,
                completed,
            )

        normalized_prefix = prefix.casefold()
        for value, description in candidates:
            if normalized_prefix and not value.casefold().startswith(normalized_prefix):
                continue
            yield Completion(
                value,
                start_position=-len(prefix),
                display=value,
                display_meta=description,
            )

    def _model_argument_candidates(self, prefix: str) -> tuple[tuple[str, str], ...]:
        if "/" in prefix:
            normalized = prefix.casefold()
            return tuple(
                (model, "Known model")
                for model in self._model_choices
                if model.casefold().startswith(normalized)
            )

        candidates = list(_SLASH_FIRST_ARGUMENTS["model"])
        known_providers = {value.removesuffix("/").casefold() for value, _ in candidates}
        for model in self._model_choices:
            provider, separator, _model_name = model.partition("/")
            if not separator or provider.casefold() in known_providers:
                continue
            known_providers.add(provider.casefold())
            candidates.append((f"{provider}/", "Configured custom provider"))
        return tuple(candidates)

    @staticmethod
    def _slash_followup_candidates(
        canonical: str,
        completed: list[str],
    ) -> tuple[tuple[str, str], ...]:
        action = completed[0].casefold() if completed else ""
        if canonical == "plugins" and len(completed) >= 2:
            if action == "install" and "--ref" not in completed:
                candidates = [
                    ("--replace", "Replace an existing plugin install"),
                    ("--ref", "Install a specific Git ref"),
                ]
                if "--replace" in completed:
                    candidates = [item for item in candidates if item[0] != "--replace"]
                return tuple(candidates)
            if action == "uninstall" and "--yes" not in completed:
                return (("--yes", "Confirm plugin uninstall"),)
        if (
            canonical == "browser"
            and action == "connect"
            and "--reuse-storage-state" not in completed
        ):
            return (
                (
                    "--reuse-storage-state",
                    "Reuse bounded user-owned browser storage state",
                ),
            )
        return ()

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

    @bindings.add("c-c", eager=True)
    def _interrupt(event) -> None:
        event.app.exit(exception=PromptInterrupted())

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
        model_choices: list[str] | None = None,
        input_mode: str = "emacs",
        keybindings: dict[str, list[str]] | None = None,
        workspace_root: Path | None = None,
        transcript: Transcript | None = None,
        tui_mode: str = "inline",
        theme: str = "dark",
        no_color: bool = False,
        mouse_support: bool = True,
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
        self._linear_input_buffer = ""
        self.screen_reader_mode = screen_reader_mode
        self.supports_full_screen_ui = terminal_supports_cursor_ui(
            input_stream=self.input_stream,
        ) and not screen_reader_mode
        self.linear_mode = self.interactive and not self.supports_full_screen_ui
        if self.interactive and self.supports_full_screen_ui:
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
            extra_names = list(extra_commands or [])
            extra_descriptions: dict[str, str] = {}
            if isinstance(extra_commands, dict):
                extra_names = list(extra_commands)
                extra_descriptions = {
                    f"/{name}": description
                    for name, description in extra_commands.items()
                }
            words = _ordered_slash_words(extra_names)
            completer = AshCompleter(
                words,
                workspace_root or Path.cwd(),
                command_descriptions=extra_descriptions,
                model_choices=model_choices,
                repo_map=repo_map,
                mcp_runtime=mcp_runtime,
            )
            self._completer = completer
            if tui_mode == "viewport":
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
                    mouse_support=mouse_support,
                )
            else:
                self._session = PromptSession(
                    history=history,
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

    async def choose(
        self,
        title: str,
        options: tuple[ViewportChoice, ...],
    ) -> str | None:
        if self._viewport is None:
            raise RuntimeError("choice UI requires viewport mode")
        return await self._viewport.choose(title, options)

    @property
    def uses_viewport(self) -> bool:
        return self._viewport is not None

    def set_extra_commands(self, commands: dict[str, str] | list[str]) -> None:
        if self._completer is None:
            return
        descriptions: dict[str, str] = {}
        if isinstance(commands, dict):
            extra_names = list(commands)
            descriptions = {
                f"/{name}": description for name, description in commands.items()
            }
        else:
            extra_names = list(commands)
        self._completer.set_commands(
            _ordered_slash_words(extra_names),
            command_descriptions=descriptions,
        )

    def set_model_choices(self, model_choices: list[str]) -> None:
        if self._completer is not None:
            self._completer.set_model_choices(model_choices)

    async def read(self, prompt: str = "> ") -> str:
        if self.linear_mode:
            return await self._read_linear(prompt)
        if self._viewport is not None:
            return await self._viewport.read(prompt)
        if self._session is not None:
            return await self._session.prompt_async(prompt)
        line = self.input_stream.readline()
        if line == "":
            raise EOFError
        return line.rstrip("\r\n")

    async def _read_linear(self, prompt: str) -> str:
        """Read one cooked-terminal line without cursor-addressing redraws."""

        safe_prompt = terminal_safe_text(prompt, single_line=True)
        sys.stdout.write(safe_prompt)
        sys.stdout.flush()

        newline = self._linear_input_buffer.find("\n")
        if newline >= 0:
            line = self._linear_input_buffer[:newline]
            self._linear_input_buffer = self._linear_input_buffer[newline + 1 :]
            return line.rstrip("\r")

        try:
            fd = self.input_stream.fileno()
        except (AttributeError, OSError, ValueError):
            line = self.input_stream.readline()
            if line == "":
                raise EOFError
            return line.rstrip("\r\n")

        loop = asyncio.get_running_loop()
        completed: asyncio.Future[str] = loop.create_future()
        encoding = getattr(self.input_stream, "encoding", None) or "utf-8"
        def on_readable() -> None:
            try:
                chunk = os.read(fd, 4096)
            except OSError as exc:
                if not completed.done():
                    completed.set_exception(exc)
                return
            if not chunk:
                if not completed.done():
                    if self._linear_input_buffer:
                        line = self._linear_input_buffer
                        self._linear_input_buffer = ""
                        completed.set_result(line.rstrip("\r"))
                    else:
                        completed.set_exception(EOFError())
                return
            self._linear_input_buffer += chunk.decode(encoding, errors="replace")
            newline = self._linear_input_buffer.find("\n")
            if newline < 0 or completed.done():
                return
            line = self._linear_input_buffer[:newline]
            self._linear_input_buffer = self._linear_input_buffer[newline + 1 :]
            completed.set_result(line.rstrip("\r"))

        loop.add_reader(fd, on_readable)
        try:
            return await completed
        finally:
            loop.remove_reader(fd)

    def close(self) -> None:
        if self._viewport is not None:
            self._viewport.close()
