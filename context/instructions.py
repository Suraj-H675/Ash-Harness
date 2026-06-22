"""Bounded global and trusted-project instruction discovery."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


MAX_INSTRUCTION_FILE_BYTES = 128 * 1024


@dataclass(frozen=True)
class InstructionFile:
    path: Path
    content: str
    scope: str


def discover_instructions(
    workspace: Path,
    *,
    include_project: bool,
    current_directory: Path | None = None,
) -> list[InstructionFile]:
    files: list[InstructionFile] = []
    global_path = Path.home() / ".ash" / "ASH.md"
    global_instruction = _read(global_path, "user")
    if global_instruction is not None:
        files.append(global_instruction)

    if not include_project:
        return files
    root = workspace.expanduser().resolve()
    current = (current_directory or Path.cwd()).expanduser().resolve()
    try:
        relative = current.relative_to(root)
    except ValueError:
        relative = Path()
    candidates = [root / "ASH.md", root / ".ash" / "ASH.md"]
    cursor = root
    for part in relative.parts:
        cursor /= part
        candidates.append(cursor / "ASH.md")
    seen: set[Path] = set()
    for path in candidates:
        if path in seen:
            continue
        seen.add(path)
        instruction = _read(path, "project")
        if instruction is not None:
            files.append(instruction)
    return files


def render_instructions(files: list[InstructionFile]) -> str:
    if not files:
        return ""
    sections = [
        f"### {item.scope.title()} instructions: {item.path}\n{item.content}"
        for item in files
    ]
    return "## Persistent Instructions\n\n" + "\n\n".join(sections)


def _read(path: Path, scope: str) -> InstructionFile | None:
    if not path.is_file():
        return None
    size = path.stat().st_size
    if size > MAX_INSTRUCTION_FILE_BYTES:
        raise ValueError(f"Instruction file is too large ({size} bytes): {path}")
    return InstructionFile(
        path=path,
        content=path.read_text(encoding="utf-8").strip(),
        scope=scope,
    )
