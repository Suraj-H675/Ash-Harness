"""Safe, progressively loaded instruction skills."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from safety.guard import SafetyGuard
from tools.base import BaseTool, ToolResult, count_output_tokens

MAX_SKILL_BYTES = 512 * 1024


@dataclass(frozen=True)
class InstructionSkill:
    name: str
    description: str
    instructions: str
    path: Path


@dataclass(frozen=True)
class SkillSource:
    paths: tuple[Path, ...]
    namespace: str = ""


class SkillCatalog:
    def __init__(self, roots: tuple[Path | SkillSource, ...]) -> None:
        self.sources = tuple(
            source if isinstance(source, SkillSource) else SkillSource(paths=(source,))
            for source in roots
        )
        self._skills: dict[str, InstructionSkill] = {}
        self.errors: dict[str, str] = {}

    def discover(self) -> list[InstructionSkill]:
        discovered: dict[str, InstructionSkill] = {}
        self.errors.clear()
        for source in self.sources:
            for path in _skill_paths(source.paths):
                try:
                    skill = parse_instruction_skill(path, namespace=source.namespace)
                except (OSError, UnicodeError, ValueError) as exc:
                    self.errors[str(path)] = str(exc)
                    continue
                existing = discovered.get(skill.name)
                if existing is not None:
                    self.errors[str(path)] = (
                        f"duplicate skill name {skill.name!r}; already provided by "
                        f"{existing.path}"
                    )
                    continue
                discovered[skill.name] = skill
        self._skills = discovered
        return list(discovered.values())

    def list(self) -> list[InstructionSkill]:
        if not self._skills:
            self.discover()
        return list(self._skills.values())

    def get(self, name: str) -> InstructionSkill | None:
        if not self._skills:
            self.discover()
        return self._skills.get(name)


def _skill_paths(paths: tuple[Path, ...]) -> list[Path]:
    discovered: set[Path] = set()
    for candidate in paths:
        if candidate.is_file() and candidate.name == "SKILL.md":
            discovered.add(candidate)
        elif candidate.is_dir():
            discovered.update(candidate.rglob("SKILL.md"))
    return sorted(discovered)


def parse_instruction_skill(path: Path, *, namespace: str = "") -> InstructionSkill:
    if path.stat().st_size > MAX_SKILL_BYTES:
        raise ValueError("skill file exceeds 512 KiB")
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
    name = metadata.get("name") or path.parent.name
    if (
        not name
        or name in {".", ".."}
        or any(character.isspace() for character in name)
        or any(character in name for character in ("/", "\\", "\x00"))
    ):
        raise ValueError(
            "skill name must be a non-empty path-safe identifier without whitespace"
        )
    description = metadata.get("description", "")
    if not description:
        description = next(
            (line.lstrip("# ").strip() for line in body.splitlines() if line.strip()),
            "Instruction skill",
        )
    instructions = body.strip()
    if not instructions:
        raise ValueError("skill instructions are empty")
    return InstructionSkill(
        name=f"{namespace}:{name}" if namespace else name,
        description=description,
        instructions=instructions,
        path=path,
    )


class ListSkillsArgs(BaseModel):
    query: str = ""


class ListSkillsTool(BaseTool):
    name = "list_skills"
    description = "List available instruction skills before activating one."
    args_schema = ListSkillsArgs

    def __init__(self, safety_guard: SafetyGuard, catalog: SkillCatalog) -> None:
        super().__init__(safety_guard)
        self.catalog = catalog

    async def run(self, **kwargs: Any) -> ToolResult:
        args = ListSkillsArgs(**kwargs)
        query = args.query.casefold()
        skills = [
            skill
            for skill in self.catalog.list()
            if not query
            or query in skill.name.casefold()
            or query in skill.description.casefold()
        ]
        output = "\n".join(f"{skill.name}: {skill.description}" for skill in skills)
        return ToolResult(
            success=True,
            output=output,
            token_count=count_output_tokens(output),
        )


class ActivateSkillArgs(BaseModel):
    name: str = Field(..., min_length=1)


class ActivateSkillTool(BaseTool):
    name = "activate_skill"
    description = "Load one instruction skill by name when its guidance is relevant."
    args_schema = ActivateSkillArgs

    def __init__(self, safety_guard: SafetyGuard, catalog: SkillCatalog) -> None:
        super().__init__(safety_guard)
        self.catalog = catalog

    async def run(self, **kwargs: Any) -> ToolResult:
        args = ActivateSkillArgs(**kwargs)
        skill = self.catalog.get(args.name)
        if skill is None:
            return ToolResult(
                success=False,
                output="",
                error=f"Unknown skill: {args.name}. Call list_skills first.",
            )
        output = (
            f'<skill name="{skill.name}" source="{skill.path}">\n'
            f"{skill.instructions}\n"
            "</skill>"
        )
        return ToolResult(
            success=True,
            output=output,
            token_count=count_output_tokens(output),
        )
