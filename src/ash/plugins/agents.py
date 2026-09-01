"""Declarative custom subagent definitions."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from ash.agents.subprocess_agent import AGENT_ROLES

MAX_AGENT_BYTES = 256 * 1024
MAX_AGENT_DISCOVERY_ENTRIES = 100_000
MAX_AGENT_DISCOVERY_DEPTH = 32


@dataclass(frozen=True)
class AgentDefinition:
    name: str
    description: str
    instructions: str
    path: Path
    base_role: str = "general"
    allowed_tools: tuple[str, ...] = ()


@dataclass(frozen=True)
class AgentSource:
    paths: tuple[Path, ...]
    namespace: str = ""


class AgentCatalog:
    def __init__(self, sources: tuple[Path | AgentSource, ...]) -> None:
        self.sources = tuple(
            source if isinstance(source, AgentSource) else AgentSource(paths=(source,))
            for source in sources
        )
        self.errors: dict[str, str] = {}

    def discover(self) -> list[AgentDefinition]:
        definitions: dict[str, AgentDefinition] = {}
        self.errors.clear()
        for source in self.sources:
            for path in _agent_paths(source.paths):
                try:
                    definition = parse_agent_definition(
                        path, namespace=source.namespace
                    )
                except (OSError, UnicodeError, ValueError) as exc:
                    self.errors[str(path)] = str(exc)
                    continue
                existing = definitions.get(definition.name)
                if existing is not None:
                    self.errors[str(path)] = (
                        f"duplicate agent name {definition.name!r}; already provided "
                        f"by {existing.path}"
                    )
                    continue
                definitions[definition.name] = definition
        return list(definitions.values())


def parse_agent_definition(path: Path, *, namespace: str = "") -> AgentDefinition:
    if path.is_symlink() or (hasattr(path, "is_junction") and path.is_junction()):
        raise ValueError("agent definition cannot be a link")
    with path.open("rb") as handle:
        raw = handle.read(MAX_AGENT_BYTES + 1)
    if len(raw) > MAX_AGENT_BYTES:
        raise ValueError("agent definition exceeds 256 KiB")
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
    name = metadata.get("name") or path.stem
    if (
        not name
        or name in {".", ".."}
        or any(character.isspace() for character in name)
        or any(character in name for character in ("/", "\\", "\x00"))
    ):
        raise ValueError("agent name must be a path-safe identifier without whitespace")
    base_role = metadata.get("base-role", metadata.get("role", "general"))
    if base_role not in AGENT_ROLES:
        raise ValueError(f"agent base-role must be one of {AGENT_ROLES}")
    instructions = body.strip()
    if not instructions:
        raise ValueError("agent instructions are empty")
    description = metadata.get("description", "Custom subagent")
    tools = tuple(
        item.strip() for item in metadata.get("tools", "").split(",") if item.strip()
    )
    if any(
        any(character.isspace() for character in tool)
        or any(character in tool for character in ("/", "\\", "\x00"))
        for tool in tools
    ):
        raise ValueError("agent tool names must be path-safe identifiers")
    return AgentDefinition(
        name=f"{namespace}:{name}" if namespace else name,
        description=description,
        instructions=instructions,
        path=path,
        base_role=base_role,
        allowed_tools=tools,
    )


def _agent_paths(paths: tuple[Path, ...]) -> list[Path]:
    discovered: set[Path] = set()
    entries_seen = 0
    for candidate in paths:
        if candidate.is_file() and candidate.suffix.casefold() == ".md":
            discovered.add(candidate)
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
                    if len(children) >= MAX_AGENT_DISCOVERY_ENTRIES:
                        break
            except OSError:
                continue
            children.sort(key=lambda path: path.name)
            for path in children:
                entries_seen += 1
                if entries_seen > MAX_AGENT_DISCOVERY_ENTRIES:
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
                    discovered.add(path)
                elif is_directory and depth < MAX_AGENT_DISCOVERY_DEPTH:
                    pending.append((path, depth + 1))
    return sorted(discovered)
