"""Interactive prompt editor for Ash's terminal session."""

from __future__ import annotations

import asyncio
import codecs
import os
import sys
from pathlib import Path
from typing import Any, Callable, TextIO

from prompt_toolkit.completion import WordCompleter
from prompt_toolkit.completion.base import Completer, Completion
from prompt_toolkit.document import Document
from prompt_toolkit.formatted_text import FormattedText
from prompt_toolkit.input.base import Input
from prompt_toolkit.output.base import Output

from ash.commands.slash import COMMANDS
from ash.provider_catalog import BUILTIN_PROVIDERS
from ash.safety.anchored_fs import AnchoredDirectory, AnchoredFilesystemError
from ash.ui.history import PrivateFileHistory
from ash.ui.inline_surface import (
    ActivityDockView,
    InlinePromptSurface,
    PromptChoice,
)
from ash.ui.safe_text import terminal_safe_text
from ash.ui.theme import get_theme
from ash.ui.transcript import Transcript


MAX_PATH_COMPLETION_SCAN_ENTRIES = 10_000
MAX_PATH_COMPLETIONS = 200
MAX_LINEAR_INPUT_BYTES = 1_000_000
LINEAR_INPUT_READ_SIZE = 4096


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
        self.effort_provider: Callable[[], tuple[str, ...]] = lambda: ()

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
        if not document.text_before_cursor and not complete_event.completion_requested:
            return
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
        if not bool(getattr(self._repo_map, "ready", True)):
            return
        try:
            matches = self._repo_map.find_definitions(
                prefix,
                case_sensitive=True,
                refresh=False,
            )
            if not matches:
                matches = self._repo_map.find_definitions(
                    prefix,
                    case_sensitive=False,
                    refresh=False,
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
            if canonical == "model":
                candidates = self._model_argument_candidates(prefix)
            elif canonical == "effort":
                levels = self.effort_provider()
                candidates = (
                    (("default", "Use the provider default"),)
                    + tuple((level, "Reasoning effort") for level in levels)
                    if levels
                    else ()
                )
            else:
                candidates = _SLASH_FIRST_ARGUMENTS.get(canonical, ())
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


class PromptInput:
    """Prompt-toolkit input with a deterministic fallback for redirected stdin."""

    def __init__(
        self,
        *,
        history_path: Path | None = None,
        input_stream: TextIO | None = None,
        status_provider: Callable[[], str] | None = None,
        context_provider: Callable[[], tuple[int, int]] | None = None,
        thinking_provider: Callable[[int], tuple[int, Any]] | None = None,
        dock_provider: Callable[[], ActivityDockView] | None = None,
        extra_commands: dict[str, str] | list[str] | None = None,
        model_choices: list[str] | None = None,
        input_mode: str = "emacs",
        keybindings: dict[str, list[str]] | None = None,
        workspace_root: Path | None = None,
        theme: str = "dark",
        no_color: bool = False,
        repo_map: Any | None = None,
        mcp_runtime: Any | None = None,
        screen_reader_mode: bool = False,
        reduced_motion: bool = False,
        input: Input | None = None,
        output: Output | None = None,
        transcript: Transcript | None = None,
    ) -> None:
        if input_mode not in {"emacs", "vi"}:
            raise ValueError("input_mode must be emacs or vi")
        self.input_stream = input_stream or sys.stdin
        self.interactive = bool(getattr(self.input_stream, "isatty", lambda: False)())
        self._surface: InlinePromptSurface | None = None
        self._completer: AshCompleter | None = None
        self._linear_input_buffer = ""
        self._linear_input_parts: list[str] = []
        self._linear_input_size = 0
        self._linear_input_discarding = False
        self._linear_input_eof = False
        self._linear_input_decoder: codecs.IncrementalDecoder | None = None
        self._theme = theme
        self._no_color = no_color
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
            self._surface = InlinePromptSurface(
                history=history,
                completer=completer,
                status_provider=status_provider or (lambda: ""),
                context_provider=context_provider or (lambda: (0, 1)),
                thinking_provider=(
                    thinking_provider
                    or (lambda _width: (0, FormattedText([])))
                ),
                dock_provider=dock_provider,
                input_mode=input_mode,
                keybindings=(
                    keybindings
                    if keybindings is not None
                    else {
                        "newline": ["escape enter", "c-j"],
                        "open_editor": ["c-x c-e"],
                    }
                ),
                theme=get_theme(theme),
                no_color=no_color,
                input=input,
                output=output,
                transcript=transcript,
                reduced_motion=reduced_motion,
                screen_reader_mode=screen_reader_mode,
            )

    async def choose(
        self,
        title: str,
        options: tuple[PromptChoice, ...],
        *,
        default_value: str | None = None,
        context: str = "",
    ) -> str | None:
        if self._surface is not None:
            return await self._surface.choose(
                title,
                options,
                default_value=default_value,
                context=context,
            )
        raise RuntimeError("choice UI requires an interactive cursor terminal")

    @property
    def supports_choice_ui(self) -> bool:
        return self._surface is not None

    @property
    def supports_live_surface(self) -> bool:
        return self._surface is not None

    def set_effort_provider(self, provider: Callable[[], tuple[str, ...]]) -> None:
        if self._completer is not None:
            self._completer.effort_provider = provider

    def write_terminal(self, callback: Callable[[], None]) -> None:
        if self._surface is None:
            callback()
        else:
            self._surface.write_terminal(callback)

    def invalidate(self) -> None:
        if self._surface is not None:
            self._surface.invalidate()

    def clear_visible_screen(self) -> None:
        if self._surface is not None:
            self._surface.clear_visible_screen()

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

    async def read(self, prompt: str = "> ", *, context: str = "") -> str:
        if self.linear_mode:
            return await self._read_linear(prompt)
        if self._surface is not None:
            return await self._surface.read(prompt, context=context)
        line = self.input_stream.readline()
        if line == "":
            raise EOFError
        return line.rstrip("\r\n")

    async def _read_linear(self, prompt: str) -> str:
        """Read one cooked-terminal line without cursor-addressing redraws."""

        safe_prompt = terminal_safe_text(prompt, single_line=True)
        read_size = min(LINEAR_INPUT_READ_SIZE, MAX_LINEAR_INPUT_BYTES + 1)
        # Keep one byte of slack for a trailing CR until CRLF is resolved.
        storage_limit = MAX_LINEAR_INPUT_BYTES + 1

        def warn_oversized() -> None:
            print(
                "Input line exceeded the "
                f"{MAX_LINEAR_INPUT_BYTES:,} UTF-8 byte limit and was discarded.",
                file=sys.stderr,
                flush=True,
            )

        while True:
            sys.stdout.write(safe_prompt)
            sys.stdout.flush()

            newline = self._linear_input_buffer.find("\n")
            if newline >= 0:
                line = self._linear_input_buffer[:newline].rstrip("\r")
                self._linear_input_buffer = self._linear_input_buffer[newline + 1 :]
                if len(line.encode("utf-8")) > MAX_LINEAR_INPUT_BYTES:
                    warn_oversized()
                    continue
                return line

            if self._linear_input_buffer:
                buffered_size = len(self._linear_input_buffer.encode("utf-8"))
                if (
                    self._linear_input_discarding
                    or self._linear_input_size + buffered_size > storage_limit
                ):
                    self._linear_input_parts.clear()
                    self._linear_input_size = 0
                    self._linear_input_discarding = True
                else:
                    self._linear_input_parts.append(self._linear_input_buffer)
                    self._linear_input_size += buffered_size
                self._linear_input_buffer = ""

            if self._linear_input_eof:
                if self._linear_input_discarding:
                    self._linear_input_discarding = False
                    self._linear_input_parts.clear()
                    self._linear_input_size = 0
                    warn_oversized()
                    continue
                if self._linear_input_parts:
                    buffered_line = "".join(self._linear_input_parts).rstrip("\r")
                    self._linear_input_parts.clear()
                    self._linear_input_size = 0
                    if len(buffered_line.encode("utf-8")) <= MAX_LINEAR_INPUT_BYTES:
                        return buffered_line
                    warn_oversized()
                    continue
                raise EOFError

            try:
                fd = self.input_stream.fileno()
            except (AttributeError, OSError, ValueError):
                parts: list[str] = []
                line_size = 0
                discarding = False
                while True:
                    chunk = self.input_stream.readline(read_size)
                    if not chunk:
                        if discarding:
                            fallback_line = None
                            break
                        if parts:
                            fallback_line = "".join(parts).rstrip("\r")
                            if len(fallback_line.encode("utf-8")) <= MAX_LINEAR_INPUT_BYTES:
                                return fallback_line
                            break
                        raise EOFError

                    newline = chunk.find("\n")
                    text = chunk if newline < 0 else chunk[:newline]
                    if not discarding:
                        text_size = len(text.encode("utf-8"))
                        if line_size + text_size > storage_limit:
                            parts.clear()
                            line_size = 0
                            discarding = True
                        elif newline >= 0:
                            fallback_line = ("".join(parts) + text).rstrip("\r")
                            if (
                                len(fallback_line.encode("utf-8"))
                                > MAX_LINEAR_INPUT_BYTES
                            ):
                                fallback_line = None
                        elif text:
                            parts.append(text)
                            line_size += text_size
                    if newline >= 0:
                        if discarding:
                            fallback_line = None
                        break

                if fallback_line is not None:
                    return fallback_line
                warn_oversized()
                continue

            loop = asyncio.get_running_loop()
            completed: asyncio.Future[str | None] = loop.create_future()
            if self._linear_input_decoder is None:
                encoding = getattr(self.input_stream, "encoding", None) or "utf-8"
                decoder_type = codecs.getincrementaldecoder(encoding)
                self._linear_input_decoder = decoder_type(errors="replace")
            decoder = self._linear_input_decoder

            def consume_text(text: str) -> tuple[bool, str | None]:
                if self._linear_input_discarding:
                    newline = text.find("\n")
                    if newline < 0:
                        return False, None
                    self._linear_input_discarding = False
                    self._linear_input_buffer = text[newline + 1 :]
                    return True, None

                newline = text.find("\n")
                content = text if newline < 0 else text[:newline]
                content_size = self._linear_input_size + len(
                    content.encode("utf-8")
                )
                if newline < 0:
                    if content_size > storage_limit:
                        self._linear_input_parts.clear()
                        self._linear_input_size = 0
                        self._linear_input_discarding = True
                    elif content:
                        self._linear_input_parts.append(content)
                        self._linear_input_size = content_size
                    return False, None

                line = None
                if content_size <= storage_limit:
                    candidate = (
                        "".join(self._linear_input_parts) + content
                    ).rstrip("\r")
                    if len(candidate.encode("utf-8")) <= MAX_LINEAR_INPUT_BYTES:
                        line = candidate
                self._linear_input_parts.clear()
                self._linear_input_size = 0
                self._linear_input_buffer = text[newline + 1 :]
                return True, line.rstrip("\r") if line is not None else None

            def on_readable() -> None:
                if completed.done():
                    return
                try:
                    chunk = os.read(fd, read_size)
                except OSError as exc:
                    completed.set_exception(exc)
                    return

                if chunk:
                    done, line = consume_text(decoder.decode(chunk, final=False))
                    if done:
                        completed.set_result(line)
                    return

                self._linear_input_eof = True
                done, line = consume_text(decoder.decode(b"", final=True))
                if done:
                    completed.set_result(line)
                elif self._linear_input_discarding:
                    self._linear_input_discarding = False
                    self._linear_input_parts.clear()
                    self._linear_input_size = 0
                    completed.set_result(None)
                elif self._linear_input_parts:
                    line = "".join(self._linear_input_parts).rstrip("\r")
                    self._linear_input_parts.clear()
                    self._linear_input_size = 0
                    if len(line.encode("utf-8")) <= MAX_LINEAR_INPUT_BYTES:
                        completed.set_result(line)
                    else:
                        completed.set_result(None)
                else:
                    completed.set_exception(EOFError())

            loop.add_reader(fd, on_readable)
            try:
                completed_line = await completed
            finally:
                loop.remove_reader(fd)

            if completed_line is not None:
                return completed_line
            warn_oversized()

    def close(self) -> None:
        if self._surface is not None:
            self._surface.close()

    async def aclose(self) -> None:
        if self._surface is not None:
            await self._surface.aclose()

    async def suspend_for_overlay(self, callback):
        if self._surface is None:
            result = callback()
            if hasattr(result, "__await__"):
                return await result
            return result
        return await self._surface.suspend_for_overlay(callback)
