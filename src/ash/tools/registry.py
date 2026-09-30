"""Legacy executable-skill registry compatibility support.

A :class:`ToolRegistry` holds a name -> :class:`BaseTool` map and can discover
legacy executable Python/Markdown skill files on demand. Ash's default runtime
does not instantiate this registry; production skill discovery uses standard
non-executable ``SKILL.md`` packages from :mod:`ash.plugins.skills`.

This module remains available for compatibility callers that explicitly opt in
to in-process executable skills.
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Iterator

from ash.safety.guard import SafetyGuard
from ash.tools.base import BaseTool
from ash.tools.skill_types import SkillIndexEntry

MAX_SKILL_DISCOVERY_ENTRIES = 100_000
MAX_SKILL_DISCOVERY_DEPTH = 32


class ToolRegistry:
    """Name -> :class:`BaseTool` map with optional skill discovery."""

    def __init__(
        self,
        safety_guard: SafetyGuard,
        *,
        skill_roots: tuple[Path, ...] = (),
        allow_executable_skills: bool = False,
    ) -> None:
        self._tools: dict[str, BaseTool] = {}
        self._skill_index: dict[str, SkillIndexEntry] = {}
        self._loaded_skill_modules: dict[str, Path] = {}
        self._safety_guard = safety_guard
        self._skill_roots: list[Path] = list(skill_roots)
        self._allow_executable_skills = allow_executable_skills
        self._lock = threading.Lock()
        self.skill_errors: dict[str, str] = {}

    # --- tool registration ---------------------------------------------

    def register(self, tool: BaseTool) -> None:
        if not isinstance(tool, BaseTool):
            raise TypeError(
                f"register() requires a BaseTool, got {type(tool).__name__}"
            )
        with self._lock:
            self._tools[tool.name] = tool

    def unregister(self, name: str) -> None:
        with self._lock:
            self._tools.pop(name, None)

    def get(self, name: str) -> BaseTool | None:
        return self._tools.get(name)

    def __contains__(self, name: object) -> bool:
        return name in self._tools

    def __iter__(self) -> Iterator[BaseTool]:
        return iter(self._tools.values())

    def __len__(self) -> int:
        return len(self._tools)

    def names(self) -> list[str]:
        return list(self._tools.keys())

    def as_dict(self) -> dict[str, BaseTool]:
        """Return a copy of the name -> tool map for tool dispatch."""

        return dict(self._tools)

    # --- skill discovery ------------------------------------------------

    def add_skill_root(self, root: Path | str) -> None:
        path = Path(root).expanduser()
        path.mkdir(parents=True, exist_ok=True)
        with self._lock:
            if path not in self._skill_roots:
                self._skill_roots.append(path)

    def skill_roots(self) -> list[Path]:
        return list(self._skill_roots)

    def skill_index(self) -> list[SkillIndexEntry]:
        """Return the current index of every discovered skill."""

        return list(self._skill_index.values())

    @property
    def executable_skills_enabled(self) -> bool:
        """Whether this registry may load in-process executable skills."""

        return self._allow_executable_skills

    def index_skill(self, entry: SkillIndexEntry) -> None:
        """Register a skill in the index without compiling it."""

        with self._lock:
            self._skill_index[entry.name] = entry

    def discover_skills(self, refresh: bool = False) -> list[SkillIndexEntry]:
        """Walk the skill roots and build the index.

        Both ``*.py`` and ``*.md`` files are recognized. The function
        never *compiles* skills — it just reads the metadata so the
        index stays cheap. Compilation happens in :meth:`load_skill`.
        """

        if refresh:
            self._skill_index.clear()
        self.skill_errors.clear()

        from ash.tools.skills import (
            parse_markdown_skill_index,
            parse_python_skill_index,
        )

        seen: set[Path] = set()
        for root in self._skill_roots:
            if not root.exists() or root.is_symlink():
                continue
            for path in _iter_skill_files(root):
                if path in seen:
                    continue
                seen.add(path)
                try:
                    if path.suffix == ".py":
                        entry = parse_python_skill_index(path)
                    elif path.suffix == ".md":
                        entry = parse_markdown_skill_index(path)
                    else:
                        continue
                except (OSError, UnicodeError, ValueError) as exc:
                    self.skill_errors[str(path)] = str(exc)
                    continue
                if entry is not None:
                    existing = self._skill_index.get(entry.name)
                    if existing is not None and existing.path != entry.path:
                        self.skill_errors[str(path)] = (
                            f"duplicate executable skill name {entry.name!r}; "
                            f"already provided by {existing.path}"
                        )
                        continue
                    self._skill_index[entry.name] = entry
        return list(self._skill_index.values())

    # --- on-demand skill compilation ------------------------------------

    def load_skill(self, name: str) -> BaseTool | None:
        """Compile ``name`` from disk and register it. Returns the new tool
        or ``None`` if the skill is not in the index."""

        entry = self._skill_index.get(name)
        if entry is None:
            return None
        path = Path(entry.path)
        if not path.exists():
            return None
        from ash.tools.skills import compile_skill

        tool = compile_skill(
            path,
            self._safety_guard,
            allow_unsafe_code=self._allow_executable_skills,
            tools_provider=lambda: list(self.as_dict().values()),
            root_provider=self._safety_guard.ensure_project_root_current,
        )
        self.register(tool)
        return tool

    def load_all_skills(self) -> list[BaseTool]:
        """Compile every skill in the index. Used at startup when the
        caller wants the full tool surface eagerly available."""

        loaded: list[BaseTool] = []
        for entry in list(self._skill_index.values()):
            tool = self.load_skill(entry.name)
            if tool is not None:
                loaded.append(tool)
        return loaded

    # --- dynamic re-import for self-extension ---------------------------

    def reload_skill_module(self, name: str, path: Path) -> BaseTool | None:
        """Re-import a Python skill module on disk and recompile it.

        Used by :func:`ash.tools.skills.write_python_skill` to make
        newly-written skills immediately active. The module is loaded
        under a unique name so the same skill name can be rewritten
        multiple times without leaking stale bytecode into ``sys.modules``.
        """

        if not self._allow_executable_skills:
            from ash.tools.skills import (
                SkillParseError,
                UNSAFE_EXECUTABLE_SKILL_MESSAGE,
            )

            raise SkillParseError(UNSAFE_EXECUTABLE_SKILL_MESSAGE)
        from ash.tools.skills import (
            SkillParseError,
            _execute_python_skill_source,
            _parse_python_skill_source,
            _read_executable_skill_text,
            build_tool_from_python_module,
            validate_executable_skill_name,
        )

        validate_executable_skill_name(name)
        source = _read_executable_skill_text(path)
        parsed = _parse_python_skill_source(path, source)
        if parsed.name != name:
            raise SkillParseError(
                f"reloaded skill declares name {parsed.name!r}, expected {name!r}"
            )
        module_name = f"_ash_skill_{name}_{abs(hash(str(path)))}"
        try:
            module = _execute_python_skill_source(module_name, path, source)
        except Exception:
            raise

        tool = build_tool_from_python_module(
            module,
            self._safety_guard,
            source_path=path,
            parsed_name=parsed.name,
            parsed_description=parsed.description,
            parsed_trigger=parsed.trigger,
            source_path_verified=True,
            allow_unsafe_code=True,
            tools_provider=lambda: list(self.as_dict().values()),
            root_provider=self._safety_guard.ensure_project_root_current,
        )
        self._loaded_skill_modules[name] = path
        self.register(tool)
        # Refresh the index entry so callers see the new path.
        self._skill_index[name] = SkillIndexEntry(
            name=tool.name,
            description=tool.description,
            source="python",
            path=str(path),
            trigger=getattr(module, "__ash_trigger__", "") or "",
        )
        return tool


def _iter_skill_files(root: Path) -> Iterator[Path]:
    """Yield skill files without following links or walking unbounded trees."""

    pending: list[tuple[Path, int]] = [(root, 0)]
    entries_seen = 0
    while pending:
        directory, depth = pending.pop()
        entries: list[Path] = []
        try:
            for entry in directory.iterdir():
                entries.append(entry)
                if len(entries) >= MAX_SKILL_DISCOVERY_ENTRIES:
                    break
        except OSError:
            continue
        entries.sort(key=lambda path: path.name)
        for path in entries:
            entries_seen += 1
            if entries_seen > MAX_SKILL_DISCOVERY_ENTRIES:
                return
            if path.is_symlink() or (
                hasattr(path, "is_junction") and path.is_junction()
            ):
                continue
            try:
                is_directory = path.is_dir()
                is_file = path.is_file()
            except OSError:
                continue
            if is_file and path.suffix in {".py", ".md"}:
                yield path
            elif is_directory and depth < MAX_SKILL_DISCOVERY_DEPTH:
                pending.append((path, depth + 1))
