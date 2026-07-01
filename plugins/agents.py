"""Declarative custom subagent definitions."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from agents.subprocess_agent import AGENT_ROLES

MAX_AGENT_BYTES = 256 * 1024


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
    if path.stat().st_size > MAX_AGENT_BYTES:
        raise ValueError("agent definition exceeds 256 KiB")
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
    for candidate in paths:
        if candidate.is_file() and candidate.suffix.casefold() == ".md":
            discovered.add(candidate)
        elif candidate.is_dir():
            discovered.update(candidate.rglob("*.md"))
    return sorted(discovered)
