"""Agent-authored self-improving skill writer (Sprint 14 / V7).

This module bridges the gap between agent execution and skill creation:
:class:`SkillWriter` can write a ``.skill.md`` file to a skill root,
and :func:`on_agent_success` is a post-execution callback that skillifies
successful agent reports that declare ``should_skillify`` in their artifacts.

The agent signals skillification readiness by setting:

``report.artifacts["should_skillify"] = True``

``report.artifacts["skill_body"] = "async def execute(context, **kwargs) -> str: ..."``
"""

from __future__ import annotations

import textwrap
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from agents.subprocess_agent import AgentReport
    from tools.registry import ToolRegistry


@dataclass
class SkillWriteResult:
    """Result of writing a skill file."""

    success: bool
    path: Path | None
    error: str | None


class SkillWriter:
    """Write agent-authored skills to disk in the markdown recipe format.

    Parameters
    ----------
    skill_root
        Directory where skill files are stored. Created if it does not exist.
    registry
        Optional :class:`ToolRegistry` used to reload the skill after writing.
        If ``None``, the caller is responsible for calling
        :meth:`ToolRegistry.reload_skill_module` manually.
    """

    def __init__(
        self,
        skill_root: Path,
        *,
        registry: "ToolRegistry | None" = None,
    ) -> None:
        self.skill_root = skill_root.expanduser()
        self.skill_root.mkdir(parents=True, exist_ok=True)
        self._registry = registry

    def write_skill(
        self,
        *,
        name: str,
        description: str,
        trigger: str = "",
        body: str,
    ) -> SkillWriteResult:
        """Write a ``.skill.md`` file to the skill root directory.

        Returns a :class:`SkillWriteResult` indicating success or failure.
        """
        if not name.isidentifier():
            return SkillWriteResult(
                success=False,
                path=None,
                error=f"skill name must be a valid Python identifier, got {name!r}",
            )
        safe_name = name.replace(" ", "_")
        path = self.skill_root / f"{safe_name}.skill.md"
        contents = textwrap.dedent(body).strip("\n")
        frontmatter = f"""---
name: {name}
description: {description}
trigger: {trigger}
---
"""
        path.write_text(f"{frontmatter}\n{contents}\n", encoding="utf-8")
        return SkillWriteResult(success=True, path=path, error=None)

    def reload(self, name: str, path: Path) -> None:
        """Reload a skill into the registry after writing it to disk.

        Does nothing if no registry was configured at construction time.
        """
        if self._registry is None:
            return
        self._registry.reload_skill_module(name, path)


async def on_agent_success(
    report: "AgentReport",
    skill_root: Path,
    *,
    registry: "ToolRegistry | None" = None,
) -> SkillWriteResult | None:
    """Post-execution callback: skillify a successful agent report.

    Called after an agent completes. If the agent's report has
    ``should_skillify`` set in its artifacts, this function writes a
    ``.skill.md`` file to ``skill_root`` and optionally reloads it
    into ``registry``.

    Returns ``None`` if the report does not declare ``should_skillify``.
    """
    if not report.artifacts.get("should_skillify"):
        return None

    skill_body = report.artifacts.get("skill_body")
    if not skill_body:
        return SkillWriteResult(
            success=False,
            path=None,
            error="should_skillify=True but no skill_body in artifacts",
        )

    writer = SkillWriter(skill_root, registry=registry)
    safe_name = report.task.replace(" ", "_")
    result = writer.write_skill(
        name=safe_name,
        description=f"Automates: {report.task}",
        trigger="",
        body=skill_body,
    )
    if result.success and result.path is not None:
        writer.reload(safe_name, result.path)
    return result
