"""Safe, progressively loaded instruction skills."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from safety.guard import SafetyGuard
from tools.base import BaseTool, ToolResult, count_output_tokens


@dataclass(frozen=True)
class InstructionSkill:
    name: str
    description: str
    instructions: str
    path: Path


class SkillCatalog:
    def __init__(self, roots: tuple[Path, ...]) -> None:
        self.roots = roots
        self._skills: dict[str, InstructionSkill] = {}

    def discover(self) -> list[InstructionSkill]:
        discovered: dict[str, InstructionSkill] = {}
        for root in self.roots:
            if not root.exists():
                continue
            for path in sorted(root.rglob("SKILL.md")):
                skill = parse_instruction_skill(path)
                if skill.name not in discovered:
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


def parse_instruction_skill(path: Path) -> InstructionSkill:
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
    description = metadata.get("description", "")
    if not description:
        description = next(
            (line.lstrip("# ").strip() for line in body.splitlines() if line.strip()),
            "Instruction skill",
        )
    return InstructionSkill(
        name=name,
        description=description,
        instructions=body.strip(),
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
