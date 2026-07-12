"""Agent Skills discovery, validation, and progressive loading."""

from __future__ import annotations

import html
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field

from ash.safety.guard import SafetyGuard
from ash.tools.base import BaseTool, ToolResult, count_output_tokens


MAX_SKILL_BYTES = 512 * 1024
MAX_SKILL_RESOURCE_BYTES = 512 * 1024
MAX_LISTED_RESOURCES = 200
SKILL_NAME = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
KNOWN_FRONTMATTER_FIELDS = frozenset(
    {
        "name",
        "description",
        "license",
        "compatibility",
        "metadata",
        "allowed-tools",
    }
)


@dataclass(frozen=True)
class InstructionSkill:
    """One validated Agent Skills instruction package."""

    name: str
    canonical_name: str
    description: str
    instructions: str
    path: Path
    license: str | None = None
    compatibility: str | None = None
    metadata: tuple[tuple[str, str], ...] = ()
    allowed_tools: tuple[str, ...] = ()
    extra_frontmatter: tuple[tuple[str, Any], ...] = ()

    @property
    def root(self) -> Path:
        return self.path.parent


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
        if candidate.name == "SKILL.md" and (
            candidate.is_file() or candidate.is_symlink()
        ):
            discovered.add(candidate)
        elif candidate.is_dir() and not candidate.is_symlink():
            discovered.update(_discover_skill_directory(candidate))
    return sorted(discovered)


def _discover_skill_directory(directory: Path) -> set[Path]:
    """Find skill roots without treating files inside a skill as new skills."""

    manifest = directory / "SKILL.md"
    if manifest.is_file() or manifest.is_symlink():
        return {manifest}
    discovered: set[Path] = set()
    try:
        children = directory.iterdir()
    except OSError:
        return discovered
    for child in children:
        if child.name.startswith(".") or child.name == "node_modules":
            continue
        if child.is_symlink():
            continue
        if child.is_dir():
            discovered.update(_discover_skill_directory(child))
    return discovered


def parse_instruction_skill(path: Path, *, namespace: str = "") -> InstructionSkill:
    """Parse and validate a ``SKILL.md`` using the Agent Skills specification."""

    if path.is_symlink():
        raise ValueError("skill manifest cannot be a symbolic link")
    if path.name != "SKILL.md" or not path.is_file():
        raise ValueError("skill path must point to a SKILL.md file")
    if path.stat().st_size > MAX_SKILL_BYTES:
        raise ValueError("skill file exceeds 512 KiB")
    text = path.read_text(encoding="utf-8").replace("\r\n", "\n").replace("\r", "\n")
    frontmatter_text, instructions = _split_frontmatter(text)
    try:
        raw = yaml.safe_load(frontmatter_text)
    except yaml.YAMLError as exc:
        raise ValueError(f"invalid YAML frontmatter: {exc}") from exc
    if not isinstance(raw, dict) or not all(isinstance(key, str) for key in raw):
        raise ValueError("skill frontmatter must be a YAML mapping with string keys")

    canonical_name = _required_string(raw, "name")
    if len(canonical_name) > 64 or not SKILL_NAME.fullmatch(canonical_name):
        raise ValueError(
            "skill name must be 1-64 lowercase letters, numbers, or single hyphens"
        )
    if canonical_name != path.parent.name:
        raise ValueError(
            f"skill name {canonical_name!r} must match parent directory "
            f"{path.parent.name!r}"
        )

    description = _required_string(raw, "description")
    if len(description) > 1024:
        raise ValueError("skill description exceeds 1024 characters")
    license_name = _optional_string(raw, "license")
    compatibility = _optional_string(raw, "compatibility")
    if compatibility is not None and len(compatibility) > 500:
        raise ValueError("skill compatibility exceeds 500 characters")

    raw_metadata = raw.get("metadata", {})
    if raw_metadata is None:
        raw_metadata = {}
    if not isinstance(raw_metadata, dict) or not all(
        isinstance(key, str) and isinstance(value, str)
        for key, value in raw_metadata.items()
    ):
        raise ValueError("skill metadata must map strings to strings")
    allowed_tools = _optional_string(raw, "allowed-tools")
    body = instructions.strip()
    if not body:
        raise ValueError("skill instructions are empty")

    effective_name = f"{namespace}:{canonical_name}" if namespace else canonical_name
    extras = tuple(
        sorted(
            (
                (key, value)
                for key, value in raw.items()
                if key not in KNOWN_FRONTMATTER_FIELDS
            ),
            key=lambda item: item[0],
        )
    )
    return InstructionSkill(
        name=effective_name,
        canonical_name=canonical_name,
        description=description,
        instructions=body,
        path=path,
        license=license_name,
        compatibility=compatibility,
        metadata=tuple(sorted(raw_metadata.items())),
        allowed_tools=tuple(allowed_tools.split()) if allowed_tools else (),
        extra_frontmatter=extras,
    )


def _split_frontmatter(text: str) -> tuple[str, str]:
    if not text.startswith("---\n"):
        raise ValueError("skill requires YAML frontmatter delimited by ---")
    end = text.find("\n---\n", 4)
    if end < 0:
        raise ValueError("skill YAML frontmatter is missing a closing --- delimiter")
    return text[4:end], text[end + 5 :]


def _required_string(values: dict[str, Any], key: str) -> str:
    value = values.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"skill {key} is required and must be a non-empty string")
    return value.strip()


def _optional_string(values: dict[str, Any], key: str) -> str | None:
    value = values.get(key)
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"skill {key} must be a non-empty string when provided")
    return value.strip()


def render_available_skills(catalog: SkillCatalog) -> str:
    """Render only discovery metadata for progressive prompt disclosure."""

    skills = catalog.list()
    if not skills:
        return ""
    lines = [
        "## Available Skills",
        "Load a skill with activate_skill when its description matches the task.",
        "<available_skills>",
    ]
    for skill in skills:
        lines.extend(
            [
                "  <skill>",
                f"    <name>{html.escape(skill.name)}</name>",
                f"    <description>{html.escape(skill.description)}</description>",
                "  </skill>",
            ]
        )
    lines.append("</available_skills>")
    return "\n".join(lines)


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
        resources = _list_skill_resources(skill)
        resource_note = (
            "\n<resources>\n"
            + "\n".join(f"  {html.escape(item)}" for item in resources)
            + "\n</resources>"
            if resources
            else ""
        )
        output = (
            f'<skill name="{html.escape(skill.name, quote=True)}" '
            f'source="{html.escape(str(skill.path), quote=True)}">\n'
            f"{skill.instructions}{resource_note}\n"
            "</skill>"
        )
        return ToolResult(
            success=True,
            output=output,
            token_count=count_output_tokens(output),
        )


class ReadSkillResourceArgs(BaseModel):
    name: str = Field(..., min_length=1)
    path: str = Field(..., min_length=1)


class ReadSkillResourceTool(BaseTool):
    name = "read_skill_resource"
    description = "Read a text resource inside an activated skill package."
    args_schema = ReadSkillResourceArgs

    def __init__(self, safety_guard: SafetyGuard, catalog: SkillCatalog) -> None:
        super().__init__(safety_guard)
        self.catalog = catalog

    async def run(self, **kwargs: Any) -> ToolResult:
        args = ReadSkillResourceArgs(**kwargs)
        skill = self.catalog.get(args.name)
        if skill is None:
            return ToolResult(
                success=False,
                output="",
                error=f"Unknown skill: {args.name}",
            )
        try:
            resource = _resolve_skill_resource(skill, args.path)
            if resource.stat().st_size > MAX_SKILL_RESOURCE_BYTES:
                raise ValueError("skill resource exceeds 512 KiB")
            output = resource.read_text(encoding="utf-8")
        except (OSError, UnicodeError, ValueError) as exc:
            return ToolResult(success=False, output="", error=str(exc))
        return ToolResult(
            success=True,
            output=output,
            token_count=count_output_tokens(output),
        )


def _list_skill_resources(skill: InstructionSkill) -> list[str]:
    resources: list[str] = []
    for path in sorted(skill.root.rglob("*")):
        if len(resources) >= MAX_LISTED_RESOURCES:
            resources.append("[resource listing truncated]")
            break
        if path == skill.path or path.is_symlink() or not path.is_file():
            continue
        resources.append(path.relative_to(skill.root).as_posix())
    return resources


def _resolve_skill_resource(skill: InstructionSkill, requested: str) -> Path:
    relative = Path(requested)
    if relative.is_absolute() or not relative.parts:
        raise ValueError("skill resource path must be relative")
    if any(part in {"", ".", ".."} for part in relative.parts):
        raise ValueError("skill resource path cannot contain traversal components")
    root = skill.root.resolve()
    candidate = root
    for part in relative.parts:
        candidate = candidate / part
        if candidate.is_symlink():
            raise ValueError("skill resources cannot be symbolic links")
    try:
        resolved = candidate.resolve(strict=True)
        resolved.relative_to(root)
    except (OSError, ValueError) as exc:
        raise ValueError(
            "skill resource is missing or outside the skill package"
        ) from exc
    if not resolved.is_file():
        raise ValueError("skill resource must be a file")
    return resolved
