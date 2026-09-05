"""Agent-authored self-improving skill writer (Sprint 14 / V7).

This module bridges the gap between agent execution and legacy executable skill
creation: :class:`SkillWriter` can write a ``.py`` skill file to a skill root,
and :func:`on_agent_success` is a post-execution callback that skillifies
successful agent reports that declare ``should_skillify`` in their artifacts.

The agent signals skillification readiness by setting:

``report.artifacts["should_skillify"] = True``

``report.artifacts["skill_body"] = "async def execute(context, **kwargs) -> str: ..."``
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from ash.tools.skills import (
    UNSAFE_EXECUTABLE_SKILL_MESSAGE,
    _read_executable_skill_text,
    validate_executable_skill_name,
    write_python_skill,
)

if TYPE_CHECKING:
    from ash.agents.subprocess_agent import AgentReport
    from ash.tools.base import BaseTool
    from ash.tools.registry import ToolRegistry


@dataclass
class SkillWriteResult:
    """Result of writing a skill file."""

    success: bool
    path: Path | None
    error: str | None


class SkillWriter:
    """Write agent-authored legacy executable skills to disk.

    Parameters
    ----------
    skill_root
        Directory where skill files are stored. Created only after explicit
        unsafe executable-skill opt-in.
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
        self._registry = registry

    def write_skill(
        self,
        *,
        name: str,
        description: str,
        trigger: str = "",
        body: str,
        allow_unsafe_code: bool = False,
    ) -> SkillWriteResult:
        """Write a legacy executable Python skill to the skill root.

        Returns a :class:`SkillWriteResult` indicating success or failure.
        """
        unsafe_error = self._unsafe_write_error(allow_unsafe_code)
        if unsafe_error is not None:
            return SkillWriteResult(
                success=False,
                path=None,
                error=unsafe_error,
            )
        try:
            path = write_python_skill(
                self.skill_root,
                name=name,
                description=description,
                trigger=trigger,
                body=body,
                allow_unsafe_code=True,
            )
        except OSError as exc:
            return SkillWriteResult(
                success=False,
                path=None,
                error=f"failed to write skill: {exc}",
            )
        except (TypeError, ValueError) as exc:
            return SkillWriteResult(success=False, path=None, error=str(exc))
        return SkillWriteResult(success=True, path=path, error=None)

    def _unsafe_write_error(self, allow_unsafe_code: bool) -> str | None:
        if not allow_unsafe_code:
            return UNSAFE_EXECUTABLE_SKILL_MESSAGE
        if (
            self._registry is not None
            and getattr(self._registry, "executable_skills_enabled", False) is not True
        ):
            return UNSAFE_EXECUTABLE_SKILL_MESSAGE
        return None

    def reload(self, name: str, path: Path) -> "BaseTool | None":
        """Reload a skill into the registry after writing it to disk.

        Does nothing if no registry was configured at construction time.
        """
        if self._registry is None:
            return None
        return self._registry.reload_skill_module(name, path)


async def on_agent_success(
    report: "AgentReport",
    skill_root: Path,
    *,
    registry: "ToolRegistry | None" = None,
    allow_unsafe_code: bool = False,
) -> SkillWriteResult | None:
    """Post-execution callback: skillify a successful agent report.

    Called after an agent completes. If the agent's report has
    ``should_skillify`` set in its artifacts, this function writes a ``.py``
    file to ``skill_root`` and optionally reloads it into ``registry``.

    Returns ``None`` if the report does not declare ``should_skillify``.
    """
    try:
        artifacts = report.artifacts
        success = report.success
    except AttributeError:
        return SkillWriteResult(
            success=False,
            path=None,
            error="report must provide success and artifacts",
        )
    if not isinstance(artifacts, Mapping):
        return SkillWriteResult(
            success=False,
            path=None,
            error="report.artifacts must be a mapping",
        )
    if not isinstance(success, bool):
        return SkillWriteResult(
            success=False,
            path=None,
            error="report.success must be a boolean",
        )
    if "should_skillify" not in artifacts:
        return None
    should_skillify = artifacts["should_skillify"]
    if not isinstance(should_skillify, bool):
        return SkillWriteResult(
            success=False,
            path=None,
            error="report.artifacts.should_skillify must be a boolean",
        )
    if not should_skillify:
        return None
    if not success:
        return SkillWriteResult(
            success=False,
            path=None,
            error="cannot skillify an unsuccessful agent report",
        )

    skill_body = artifacts.get("skill_body")
    if not skill_body:
        return SkillWriteResult(
            success=False,
            path=None,
            error="should_skillify=True but no skill_body in artifacts",
        )
    if not isinstance(skill_body, str):
        return SkillWriteResult(
            success=False,
            path=None,
            error="skill_body must be a string",
        )
    try:
        task = report.task
    except AttributeError:
        return SkillWriteResult(
            success=False,
            path=None,
            error="report.task must be a string",
        )
    if not isinstance(task, str):
        return SkillWriteResult(
            success=False,
            path=None,
            error="report.task must be a string",
        )

    writer = SkillWriter(skill_root, registry=registry)
    unsafe_error = writer._unsafe_write_error(allow_unsafe_code)
    if unsafe_error is not None:
        return SkillWriteResult(success=False, path=None, error=unsafe_error)
    safe_name = task.replace(" ", "_")
    try:
        validate_executable_skill_name(safe_name)
    except ValueError as exc:
        return SkillWriteResult(success=False, path=None, error=str(exc))
    path = writer.skill_root / f"{safe_name}.py"
    try:
        path.lstat()
    except FileNotFoundError:
        previous_exists = False
    except OSError as exc:
        return SkillWriteResult(
            success=False,
            path=None,
            error=f"failed to snapshot existing skill: {exc}",
        )
    else:
        previous_exists = True
    previous_bytes: bytes | None = None
    if previous_exists:
        try:
            previous_bytes = _read_executable_skill_text(path).encode("utf-8")
        except (OSError, UnicodeError, ValueError) as exc:
            return SkillWriteResult(
                success=False,
                path=None,
                error=f"failed to snapshot existing skill: {exc}",
            )
    result = writer.write_skill(
        name=safe_name,
        description=f"Automates: {task}",
        trigger="",
        body=skill_body,
        allow_unsafe_code=allow_unsafe_code,
    )
    if result.success and result.path is not None:
        try:
            reloaded = writer.reload(safe_name, result.path)
            if registry is None or reloaded is not None:
                return result
            reload_error = "failed to reload skill: registry returned no tool"
        except Exception as exc:  # noqa: BLE001
            reload_error = f"failed to reload skill: {exc}"
        try:
            if previous_exists:
                assert previous_bytes is not None
                result.path.write_bytes(previous_bytes)
            else:
                result.path.unlink(missing_ok=True)
        except Exception as rollback_exc:  # noqa: BLE001
            reload_error = f"{reload_error}; rollback failed: {rollback_exc}"
        return SkillWriteResult(
            success=False,
            path=result.path,
            error=reload_error,
        )
    return result
