"""Bounded global and trusted-project instruction discovery."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import shlex


MAX_INSTRUCTION_FILE_BYTES = 128 * 1024
MAX_INSTRUCTION_IMPORT_DEPTH = 5
_AFFIRMATIVE_DIRECTIVES = ("always ", "must ", "use ", "prefer ", "do ")
_NEGATIVE_DIRECTIVES = ("never ", "do not ", "don't ", "dont ", "avoid ", "no ")


@dataclass(frozen=True)
class InstructionDiagnostic:
    path: Path
    message: str
    severity: str = "warn"


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
    diagnostics: list[InstructionDiagnostic] | None = None,
) -> list[InstructionFile]:
    files: list[InstructionFile] = []
    global_path = Path.home() / ".ash" / "ASH.md"
    files.extend(
        _read_with_imports(
            global_path,
            "user",
            root=global_path.parent,
            diagnostics=diagnostics,
            seen=set(),
        )
    )

    if not include_project:
        lint_instruction_conflicts(files, diagnostics)
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
        files.extend(
            _read_with_imports(
                path,
                "project",
                root=root,
                diagnostics=diagnostics,
                seen=set(),
            )
        )
    lint_instruction_conflicts(files, diagnostics)
    return files


def lint_instruction_conflicts(
    files: list[InstructionFile],
    diagnostics: list[InstructionDiagnostic] | None = None,
) -> None:
    """Append diagnostics for direct contradictory instruction directives."""

    seen: dict[str, tuple[str, InstructionFile, int, str]] = {}
    reported: set[tuple[Path, int, Path, int, str]] = set()
    for item in files:
        for line_number, line in enumerate(item.content.splitlines(), start=1):
            directive = _parse_directive(line)
            if directive is None:
                continue
            polarity, key, display = directive
            previous = seen.get(key)
            if previous is None:
                seen[key] = (polarity, item, line_number, display)
                continue
            previous_polarity, previous_item, previous_line, previous_display = previous
            if previous_polarity == polarity:
                continue
            report_key = (
                previous_item.path,
                previous_line,
                item.path,
                line_number,
                key,
            )
            if report_key in reported:
                continue
            reported.add(report_key)
            _diagnose(
                diagnostics,
                item.path,
                "instruction conflict: "
                f"line {line_number} ({display!r}) conflicts with "
                f"{previous_item.path}:{previous_line} ({previous_display!r})",
            )


def render_instructions(
    files: list[InstructionFile],
    diagnostics: list[InstructionDiagnostic] | None = None,
) -> str:
    if not files and not diagnostics:
        return ""
    sections = [
        f"### {item.scope.title()} instructions: {item.path}\n{item.content}"
        for item in files
    ]
    if diagnostics:
        rendered_diagnostics = "\n".join(
            f"- {item.severity}: {item.path}: {item.message}" for item in diagnostics
        )
        sections.append(f"### Instruction diagnostics\n{rendered_diagnostics}")
    return "## Persistent Instructions\n\n" + "\n\n".join(sections)


def _read_with_imports(
    path: Path,
    scope: str,
    *,
    root: Path,
    diagnostics: list[InstructionDiagnostic] | None,
    seen: set[Path],
    depth: int = 0,
) -> list[InstructionFile]:
    resolved = path.expanduser().resolve()
    root = root.expanduser().resolve()
    if resolved in seen:
        _diagnose(diagnostics, path, "instruction import skipped because it is cyclic")
        return []
    if depth > MAX_INSTRUCTION_IMPORT_DEPTH:
        _diagnose(
            diagnostics,
            path,
            f"instruction import depth exceeds {MAX_INSTRUCTION_IMPORT_DEPTH}",
        )
        return []
    try:
        resolved.relative_to(root)
    except ValueError:
        _diagnose(diagnostics, path, f"instruction import escapes trusted root: {root}")
        return []

    seen.add(resolved)
    instruction = _read(resolved, scope)
    if instruction is None:
        if depth > 0:
            _diagnose(diagnostics, path, "instruction import file does not exist")
        return []

    content, imports = _extract_imports(instruction.content, instruction.path)
    files = [
        InstructionFile(
            path=instruction.path,
            content=content,
            scope=instruction.scope,
        )
    ]
    for imported in imports:
        files.extend(
            _read_with_imports(
                imported,
                scope,
                root=root,
                diagnostics=diagnostics,
                seen=seen,
                depth=depth + 1,
            )
        )
    return files


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


def _extract_imports(content: str, source: Path) -> tuple[str, list[Path]]:
    lines: list[str] = []
    imports: list[Path] = []
    for line in content.splitlines():
        stripped = line.strip()
        if not stripped.startswith("@import"):
            lines.append(line)
            continue
        try:
            parts = shlex.split(stripped)
        except ValueError:
            lines.append(line)
            continue
        if len(parts) != 2 or parts[0] != "@import":
            lines.append(line)
            continue
        imported = Path(parts[1]).expanduser()
        if not imported.is_absolute():
            imported = source.parent / imported
        imports.append(imported)
    return "\n".join(lines).strip(), imports


def _parse_directive(line: str) -> tuple[str, str, str] | None:
    normalized = _normalize_directive_text(line)
    if not normalized:
        return None
    for prefix in _NEGATIVE_DIRECTIVES:
        if normalized.startswith(prefix):
            action = normalized.removeprefix(prefix).strip()
            return _directive("negative", action, normalized)
    for prefix in _AFFIRMATIVE_DIRECTIVES:
        if normalized.startswith(prefix):
            action = normalized.removeprefix(prefix).strip()
            return _directive("affirmative", action, normalized)
    return None


def _directive(
    polarity: str,
    action: str,
    display: str,
) -> tuple[str, str, str] | None:
    key = " ".join(action.rstrip(".:;").split())
    if len(key) < 3:
        return None
    return polarity, key, display


def _normalize_directive_text(line: str) -> str:
    text = line.strip().casefold()
    while text.startswith(("-", "*", "+", ">", "#")):
        text = text[1:].strip()
    if text.startswith("[ ]") or text.startswith("[x]"):
        text = text[3:].strip()
    return " ".join(text.split())


def _diagnose(
    diagnostics: list[InstructionDiagnostic] | None,
    path: Path,
    message: str,
) -> None:
    if diagnostics is not None:
        diagnostics.append(InstructionDiagnostic(path=path, message=message))
