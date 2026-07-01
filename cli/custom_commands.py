"""Safe Markdown custom-command discovery and expansion."""

from __future__ import annotations

import shlex
from dataclasses import dataclass
from pathlib import Path


MAX_COMMAND_BYTES = 128 * 1024


@dataclass(frozen=True)
class CustomCommand:
    name: str
    description: str
    template: str
    path: Path
    source: str

    def expand(self, arguments: list[str]) -> str:
        output = self.template.replace("$ARGUMENTS", " ".join(arguments))
        for index, argument in reversed(list(enumerate(arguments, 1))):
            output = output.replace(f"${index}", argument)
        return output


@dataclass(frozen=True)
class CommandSource:
    paths: tuple[Path, ...]
    source: str
    namespace: str = ""


class CustomCommandCatalog:
    def __init__(self, roots: tuple[tuple[Path, str] | CommandSource, ...]) -> None:
        self.sources = tuple(
            source
            if isinstance(source, CommandSource)
            else CommandSource(paths=(source[0],), source=source[1])
            for source in roots
        )
        self.errors: dict[str, str] = {}
        self._commands: dict[str, CustomCommand] = {}

    def discover(self) -> list[CustomCommand]:
        commands: dict[str, CustomCommand] = {}
        self.errors.clear()
        for source in self.sources:
            for path, root in _command_paths(source.paths):
                try:
                    command = _parse(
                        path,
                        root,
                        source.source,
                        namespace=source.namespace,
                    )
                except (OSError, UnicodeError, ValueError) as exc:
                    self.errors[str(path)] = str(exc)
                    continue
                existing = commands.get(command.name)
                if existing is not None:
                    self.errors[str(path)] = (
                        f"duplicate command name {command.name!r}; already provided by "
                        f"{existing.path}"
                    )
                    continue
                commands[command.name] = command
        self._commands = commands
        return list(commands.values())

    def parse(self, text: str) -> tuple[CustomCommand, list[str]] | None:
        if not self._commands:
            self.discover()
        if not text.startswith("/"):
            return None
        try:
            parts = shlex.split(text[1:])
        except ValueError as exc:
            raise ValueError(f"Invalid custom command syntax: {exc}") from exc
        if not parts or parts[0] not in self._commands:
            return None
        return self._commands[parts[0]], parts[1:]


def _command_paths(paths: tuple[Path, ...]) -> list[tuple[Path, Path]]:
    discovered: set[tuple[Path, Path]] = set()
    for candidate in paths:
        if candidate.is_file() and candidate.suffix.casefold() == ".md":
            discovered.add((candidate, candidate.parent))
        elif candidate.is_dir():
            discovered.update((path, candidate) for path in candidate.rglob("*.md"))
    return sorted(discovered)


def _parse(
    path: Path,
    root: Path,
    source: str,
    *,
    namespace: str = "",
) -> CustomCommand:
    if path.stat().st_size > MAX_COMMAND_BYTES:
        raise ValueError("command file exceeds 128 KiB")
    text = path.read_text(encoding="utf-8")
    metadata: dict[str, str] = {}
    body = text
    if text.startswith("---\n"):
        end = text.find("\n---\n", 4)
        if end != -1:
            for line in text[4:end].splitlines():
                key, separator, value = line.partition(":")
                if separator:
                    metadata[key.strip().casefold()] = value.strip().strip("\"'")
            body = text[end + 5 :]
    relative = path.relative_to(root).with_suffix("")
    default_name = ":".join(relative.parts)
    name = metadata.get("name", default_name)
    if not name or any(character.isspace() for character in name):
        raise ValueError("command name must be non-empty and contain no whitespace")
    description = metadata.get("description", "Custom prompt command")
    if not body.strip():
        raise ValueError("command template is empty")
    if namespace:
        name = f"{namespace}:{name}"
    return CustomCommand(name, description, body.strip(), path, source)
