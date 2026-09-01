"""Safe Markdown custom-command discovery and expansion."""

from __future__ import annotations

import shlex
from dataclasses import dataclass
from pathlib import Path


MAX_COMMAND_BYTES = 128 * 1024
MAX_COMMAND_DISCOVERY_ENTRIES = 100_000
MAX_COMMAND_DISCOVERY_DEPTH = 32


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
    entries_seen = 0
    for candidate in paths:
        if candidate.is_file() and candidate.suffix.casefold() == ".md":
            discovered.add((candidate, candidate.parent))
            continue
        if not candidate.is_dir() or candidate.is_symlink():
            continue
        pending: list[tuple[Path, int]] = [(candidate, 0)]
        while pending:
            directory, depth = pending.pop()
            children: list[Path] = []
            try:
                for child in directory.iterdir():
                    children.append(child)
                    if len(children) >= MAX_COMMAND_DISCOVERY_ENTRIES:
                        break
            except OSError:
                continue
            children.sort(key=lambda path: path.name)
            for path in children:
                entries_seen += 1
                if entries_seen > MAX_COMMAND_DISCOVERY_ENTRIES:
                    return sorted(discovered)
                if path.is_symlink() or (
                    hasattr(path, "is_junction") and path.is_junction()
                ):
                    continue
                try:
                    is_directory = path.is_dir()
                    is_file = path.is_file()
                except OSError:
                    continue
                if is_file and path.suffix.casefold() == ".md":
                    discovered.add((path, candidate))
                elif is_directory and depth < MAX_COMMAND_DISCOVERY_DEPTH:
                    pending.append((path, depth + 1))
    return sorted(discovered)


def _parse(
    path: Path,
    root: Path,
    source: str,
    *,
    namespace: str = "",
) -> CustomCommand:
    if path.is_symlink() or (hasattr(path, "is_junction") and path.is_junction()):
        raise ValueError("command file cannot be a link")
    with path.open("rb") as handle:
        raw = handle.read(MAX_COMMAND_BYTES + 1)
    if len(raw) > MAX_COMMAND_BYTES:
        raise ValueError("command file exceeds 128 KiB")
    text = raw.decode("utf-8")
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
