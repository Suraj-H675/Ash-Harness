"""Tool registry and dynamic skill loader (Sprint 14 / V7).

A :class:`ToolRegistry` holds the name -> :class:`BaseTool` map the
loop layer consults when dispatching tool calls. The registry can
also be pointed at a directory of skill files (Python modules with
the V7 docstring convention, or Markdown recipe files compiled by
:mod:`ash.tools.skills`) and will discover / load them on demand.

Lazy loading keeps the system prompt small (per the Pi convention in
ASH_MASTER_PLAN_V2.md): only the skill index is injected into the
model's context, full skill bodies are compiled into active tools
only when a discovery pass runs.
"""

from __future__ import annotations

import importlib.util
import sys
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Iterator

from ash.safety.guard import SafetyGuard
from ash.tools.base import BaseTool


@dataclass(frozen=True)
class SkillIndexEntry:
    """Lightweight skill description used to populate the system prompt."""

    name: str
    description: str
    source: str  # "python" or "markdown"
    path: str
    trigger: str = ""


class ToolRegistry:
    """Name -> :class:`BaseTool` map with optional skill discovery."""

    def __init__(
        self,
        safety_guard: SafetyGuard,
        *,
        skill_roots: tuple[Path, ...] = (),
    ) -> None:
        self._tools: dict[str, BaseTool] = {}
        self._skill_index: dict[str, SkillIndexEntry] = {}
        self._loaded_skill_modules: dict[str, Path] = {}
        self._safety_guard = safety_guard
        self._skill_roots: list[Path] = list(skill_roots)
        self._lock = threading.Lock()

    # --- tool registration ---------------------------------------------

    def register(self, tool: BaseTool) -> None:
        if not isinstance(tool, BaseTool):
            raise TypeError(f"register() requires a BaseTool, got {type(tool).__name__}")
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

        from ash.tools.skills import (
            parse_markdown_skill_index,
            parse_python_skill_index,
        )

        seen: set[Path] = set()
        for root in self._skill_roots:
            if not root.exists():
                continue
            for path in sorted(root.rglob("*")):
                if not path.is_file() or path in seen:
                    continue
                seen.add(path)
                if path.suffix == ".py":
                    entry = parse_python_skill_index(path)
                elif path.suffix == ".md":
                    entry = parse_markdown_skill_index(path)
                else:
                    continue
                if entry is not None:
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

        tool = compile_skill(path, self._safety_guard)
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

        module_name = f"_ash_skill_{name}_{abs(hash(str(path)))}"
        spec = importlib.util.spec_from_file_location(module_name, path)
        if spec is None or spec.loader is None:
            return None
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        try:
            spec.loader.exec_module(module)
        except Exception:
            sys.modules.pop(module_name, None)
            raise
        from ash.tools.skills import (
            build_tool_from_python_module,
            parse_python_skill,
        )

        # Re-parse so the docstring-driven V7 metadata is honoured.
        try:
            parsed = parse_python_skill(path)
            parsed_name = parsed.name
            parsed_description = parsed.description
            parsed_trigger = parsed.trigger
        except SkillParseError:
            parsed_name = parsed_description = parsed_trigger = None

        tool = build_tool_from_python_module(
            module,
            self._safety_guard,
            source_path=path,
            parsed_name=parsed_name,
            parsed_description=parsed_description,
            parsed_trigger=parsed_trigger,
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
