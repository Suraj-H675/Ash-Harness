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


class CustomCommandCatalog:
    def __init__(self, roots: tuple[tuple[Path, str], ...]) -> None:
        self.roots = roots
        self.errors: dict[str, str] = {}
        self._commands: dict[str, CustomCommand] = {}

    def discover(self) -> list[CustomCommand]:
        commands: dict[str, CustomCommand] = {}
        self.errors.clear()
        for root, source in self.roots:
            if not root.is_dir():
                continue
            for path in sorted(root.rglob("*.md")):
                try:
                    command = _parse(path, root, source)
                except (OSError, UnicodeError, ValueError) as exc:
                    self.errors[str(path)] = str(exc)
                    continue
                commands.setdefault(command.name, command)
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


def _parse(path: Path, root: Path, source: str) -> CustomCommand:
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
                    metadata[key.strip().casefold()] = value.strip().strip('"\'')
            body = text[end + 5 :]
    relative = path.relative_to(root).with_suffix("")
    default_name = ":".join(relative.parts)
    name = metadata.get("name", default_name)
    if not name or any(character.isspace() for character in name):
        raise ValueError("command name must be non-empty and contain no whitespace")
    description = metadata.get("description", "Custom prompt command")
    if not body.strip():
        raise ValueError("command template is empty")
    return CustomCommand(name, description, body.strip(), path, source)
