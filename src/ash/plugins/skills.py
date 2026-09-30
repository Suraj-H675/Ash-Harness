"""Agent Skills discovery, validation, and progressive loading."""

from __future__ import annotations

import html
import os
import re
import stat
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field

from ash.safety.anchored_fs import AnchoredDirectory, AnchoredFilesystemError
from ash.safety.guard import SafetyGuard, SafetyViolation
from ash.tools.base import BaseTool, ToolResult, count_output_tokens


MAX_SKILL_BYTES = 512 * 1024
MAX_SKILL_FRONTMATTER_BYTES = 64 * 1024
MAX_SKILL_YAML_DEPTH = 32
MAX_SKILL_YAML_NODES = 4_096
MAX_SKILL_RESOURCE_BYTES = 512 * 1024
MAX_LISTED_RESOURCES = 200
MAX_SKILL_DISCOVERY_ENTRIES = 100_000
MAX_SKILL_DISCOVERY_DEPTH = 32
MAX_SKILL_RESOURCE_ENTRIES = 100_000
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


class _UniqueKeySafeLoader(yaml.SafeLoader):
    """Safe YAML loader with explicit structural resource limits."""

    def __init__(self, stream: Any) -> None:
        super().__init__(stream)
        self._ash_yaml_depth = 0
        self._ash_yaml_nodes = 0

    def compose_node(self, parent: Any, index: Any) -> Any:
        if self.check_event(yaml.AliasEvent):
            event = self.get_event()
            raise yaml.composer.ComposerError(
                None,
                None,
                "YAML aliases are not allowed in skill frontmatter",
                event.start_mark,
            )
        self._ash_yaml_depth += 1
        self._ash_yaml_nodes += 1
        try:
            event = self.peek_event()
            if self._ash_yaml_depth > MAX_SKILL_YAML_DEPTH:
                raise yaml.composer.ComposerError(
                    None,
                    None,
                    (
                        "skill YAML frontmatter exceeds maximum nesting depth "
                        f"{MAX_SKILL_YAML_DEPTH}"
                    ),
                    event.start_mark,
                )
            if self._ash_yaml_nodes > MAX_SKILL_YAML_NODES:
                raise yaml.composer.ComposerError(
                    None,
                    None,
                    (
                        "skill YAML frontmatter exceeds maximum node count "
                        f"{MAX_SKILL_YAML_NODES}"
                    ),
                    event.start_mark,
                )
            return super().compose_node(parent, index)
        finally:
            self._ash_yaml_depth -= 1

    def construct_mapping(self, node: Any, deep: bool = False) -> dict[Any, Any]:
        self.flatten_mapping(node)
        mapping: dict[Any, Any] = {}
        for key_node, value_node in node.value:
            key = self.construct_object(key_node, deep=deep)
            try:
                duplicate = key in mapping
            except TypeError as exc:
                raise yaml.constructor.ConstructorError(
                    "while constructing a mapping",
                    node.start_mark,
                    "found an unhashable mapping key",
                    key_node.start_mark,
                ) from exc
            if duplicate:
                raise yaml.constructor.ConstructorError(
                    "while constructing a mapping",
                    node.start_mark,
                    f"found duplicate mapping key {key!r}",
                    key_node.start_mark,
                )
            mapping[key] = self.construct_object(value_node, deep=deep)
        return mapping


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
    package_identity: tuple[int, int] | None = None

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
        for skill in self._skills.values():
            _ensure_skill_package_current(skill)
        return list(self._skills.values())

    def get(self, name: str) -> InstructionSkill | None:
        if not self._skills:
            self.discover()
        skill = self._skills.get(name)
        if skill is not None:
            _ensure_skill_package_current(skill)
        return skill


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

    discovered: set[Path] = set()
    pending: list[tuple[Path, int]] = [(directory, 0)]
    entries_seen = 0
    while pending:
        current, depth = pending.pop()
        manifest = current / "SKILL.md"
        if manifest.is_file() or manifest.is_symlink():
            discovered.add(manifest)
            continue
        try:
            children = current.iterdir()
            for child in children:
                entries_seen += 1
                if entries_seen > MAX_SKILL_DISCOVERY_ENTRIES:
                    return discovered
                if child.name.startswith(".") or child.name == "node_modules":
                    continue
                if child.is_symlink() or (
                    hasattr(child, "is_junction") and child.is_junction()
                ):
                    continue
                if child.is_dir() and depth < MAX_SKILL_DISCOVERY_DEPTH:
                    pending.append((child, depth + 1))
        except OSError:
            continue
    return discovered


def parse_instruction_skill(path: Path, *, namespace: str = "") -> InstructionSkill:
    """Parse and validate a ``SKILL.md`` using the Agent Skills specification."""

    if path.is_symlink():
        raise ValueError("skill manifest cannot be a symbolic link")
    if path.name != "SKILL.md":
        raise ValueError("skill path must point to a SKILL.md file")
    try:
        with AnchoredDirectory.open(
            path.parent,
            create=False,
            private=False,
        ) as directory:
            manifest = directory.stat(path.name)
            if manifest is None or not stat.S_ISREG(manifest.st_mode):
                if manifest is not None and stat.S_ISLNK(manifest.st_mode):
                    raise ValueError("skill manifest cannot be a symbolic link")
                raise ValueError("skill path must point to a SKILL.md file")
            raw = directory.read_file(path.name, max_bytes=MAX_SKILL_BYTES)
            if raw is None:
                raise ValueError("skill path must point to a SKILL.md file")
            root_metadata = os.fstat(directory.descriptor)
            package_identity = _directory_identity(root_metadata)
            directory.validation_path()
    except (AnchoredFilesystemError, ValueError) as exc:
        message = str(exc).casefold()
        if "symlink" in message or "junction" in message:
            raise ValueError("skill manifest cannot be a symbolic link") from exc
        if "exceeds" in message and str(MAX_SKILL_BYTES) in message:
            raise ValueError("skill file exceeds 512 KiB") from exc
        raise
    return parse_instruction_skill_bytes(
        raw,
        path,
        namespace=namespace,
        package_identity=package_identity,
    )


def parse_instruction_skill_bytes(
    raw: bytes,
    path: Path,
    *,
    namespace: str = "",
    package_identity: tuple[int, int] | None = None,
) -> InstructionSkill:
    """Parse one bounded skill from immutable bytes."""

    if len(raw) > MAX_SKILL_BYTES:
        raise ValueError("skill file exceeds 512 KiB")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("skill file is not valid UTF-8") from exc
    return _parse_instruction_skill_text(
        text,
        path,
        namespace=namespace,
        package_identity=package_identity,
    )


def _parse_instruction_skill_text(
    text: str,
    path: Path,
    *,
    namespace: str = "",
    package_identity: tuple[int, int] | None = None,
) -> InstructionSkill:
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    frontmatter_text, instructions = _split_frontmatter(text)
    if len(frontmatter_text.encode("utf-8")) > MAX_SKILL_FRONTMATTER_BYTES:
        raise ValueError("skill YAML frontmatter exceeds 64 KiB")
    try:
        raw = yaml.load(frontmatter_text, Loader=_UniqueKeySafeLoader)
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
        package_identity=package_identity,
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
        try:
            self.safety_guard.ensure_project_root_current()
        except SafetyViolation as exc:
            return ToolResult(success=False, output="", error=str(exc))
        query = args.query.casefold()
        try:
            skills = [
                skill
                for skill in self.catalog.list()
                if not query
                or query in skill.name.casefold()
                or query in skill.description.casefold()
            ]
        except (AnchoredFilesystemError, OSError, ValueError) as exc:
            return ToolResult(success=False, output="", error=str(exc))
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
        try:
            self.safety_guard.ensure_project_root_current()
        except SafetyViolation as exc:
            return ToolResult(success=False, output="", error=str(exc))
        try:
            skill = self.catalog.get(args.name)
        except (AnchoredFilesystemError, OSError, ValueError) as exc:
            return ToolResult(success=False, output="", error=str(exc))
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
        try:
            self.safety_guard.ensure_project_root_current()
        except SafetyViolation as exc:
            return ToolResult(success=False, output="", error=str(exc))
        try:
            skill = self.catalog.get(args.name)
        except (AnchoredFilesystemError, OSError, ValueError) as exc:
            return ToolResult(success=False, output="", error=str(exc))
        if skill is None:
            return ToolResult(
                success=False,
                output="",
                error=f"Unknown skill: {args.name}",
            )
        try:
            output = _read_skill_resource_text(
                skill,
                args.path,
                max_bytes=MAX_SKILL_RESOURCE_BYTES,
            )
        except (AnchoredFilesystemError, OSError, UnicodeError, ValueError) as exc:
            return ToolResult(success=False, output="", error=str(exc))
        return ToolResult(
            success=True,
            output=output,
            token_count=count_output_tokens(output),
        )


def _list_skill_resources(skill: InstructionSkill) -> list[str]:
    resources: list[str] = []
    for path in _iter_skill_resource_paths(skill):
        if len(resources) >= MAX_LISTED_RESOURCES:
            resources.append("[resource listing truncated]")
            break
        if path == skill.path:
            continue
        resources.append(path.relative_to(skill.root).as_posix())
    return resources


def _directory_identity(metadata: os.stat_result) -> tuple[int, int] | None:
    if metadata.st_ino == 0:
        return None
    return int(metadata.st_dev), int(metadata.st_ino)


def _ensure_skill_package_current(skill: InstructionSkill) -> None:
    try:
        with _open_skill_root(skill):
            pass
    except AnchoredFilesystemError as exc:
        raise ValueError(
            f"skill package identity changed after discovery: {skill.root}"
        ) from exc


@contextmanager
def _open_skill_root(skill: InstructionSkill) -> Iterator[AnchoredDirectory]:
    try:
        directory = AnchoredDirectory.open(
            skill.root,
            create=False,
            private=False,
        )
    except (AnchoredFilesystemError, OSError) as exc:
        raise ValueError(f"skill package is unavailable: {skill.root}") from exc
    try:
        observed = _directory_identity(os.fstat(directory.descriptor))
        if skill.package_identity is not None and observed != skill.package_identity:
            raise ValueError(
                f"skill package identity changed after discovery: {skill.root}"
            )
        directory.validation_path()
        yield directory
    finally:
        directory.close()


def _iter_skill_resource_paths(skill: InstructionSkill) -> Iterator[Path]:
    entries_seen = 0

    def walk(
        directory: AnchoredDirectory,
        relative: tuple[str, ...],
        depth: int,
    ) -> Iterator[Path]:
        nonlocal entries_seen
        names = sorted(directory.list_names())[:MAX_SKILL_RESOURCE_ENTRIES]
        for name in names:
            entries_seen += 1
            if entries_seen > MAX_SKILL_RESOURCE_ENTRIES:
                return
            metadata = directory.stat(name)
            if metadata is None or stat.S_ISLNK(metadata.st_mode):
                continue
            child_relative = (*relative, name)
            if stat.S_ISREG(metadata.st_mode):
                yield skill.root.joinpath(*child_relative)
                continue
            if not stat.S_ISDIR(metadata.st_mode) or depth >= MAX_SKILL_DISCOVERY_DEPTH:
                continue
            child = directory.child(name, expected=metadata)
            try:
                yield from walk(child, child_relative, depth + 1)
            finally:
                child.close()

    with _open_skill_root(skill) as root_directory:
        yield from walk(root_directory, (), 0)


def _skill_resource_parts(requested: str) -> tuple[str, ...]:
    relative = Path(requested)
    if relative.is_absolute() or not relative.parts:
        raise ValueError("skill resource path must be relative")
    if any(part in {"", ".", ".."} for part in relative.parts):
        raise ValueError("skill resource path cannot contain traversal components")
    return tuple(relative.parts)


def _read_skill_resource_text(
    skill: InstructionSkill,
    requested: str,
    *,
    max_bytes: int,
) -> str:
    parts = _skill_resource_parts(requested)
    with _open_skill_root(skill) as root_directory:
        directory = root_directory
        owned: list[AnchoredDirectory] = []
        try:
            for part in parts[:-1]:
                metadata = directory.stat(part)
                if metadata is None:
                    raise ValueError("skill resource is missing or outside the skill package")
                if stat.S_ISLNK(metadata.st_mode):
                    raise ValueError("skill resources cannot be symbolic links")
                if not stat.S_ISDIR(metadata.st_mode):
                    raise ValueError("skill resource parent must be a directory")
                child = directory.child(part, expected=metadata)
                owned.append(child)
                directory = child
            metadata = directory.stat(parts[-1])
            if metadata is None:
                raise ValueError("skill resource is missing or outside the skill package")
            if stat.S_ISLNK(metadata.st_mode):
                raise ValueError("skill resources cannot be symbolic links")
            if not stat.S_ISREG(metadata.st_mode):
                raise ValueError("skill resource must be a file")
            try:
                raw = directory.read_file(parts[-1], max_bytes=max_bytes)
            except AnchoredFilesystemError as exc:
                if "exceeds" in str(exc).casefold():
                    raise ValueError("skill resource exceeds 512 KiB") from exc
                raise
            if raw is None:
                raise ValueError("skill resource is missing or outside the skill package")
            return raw.decode("utf-8")
        finally:
            for child in reversed(owned):
                child.close()
