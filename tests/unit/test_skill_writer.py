from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from ash.agents.subprocess_agent import AgentReport
from ash.safety.guard import SafetyGuard
from ash.tools.registry import ToolRegistry
from ash.tools.skill_writer import on_agent_success
from ash.tools.skill_writer import SkillWriter
from ash.tools.skills import MAX_EXECUTABLE_SKILL_BYTES


def test_skill_writer_rejects_executable_persistence_by_default(
    tmp_path: Path,
) -> None:
    root = tmp_path / "skills"

    result = SkillWriter(root).write_skill(
        name="unsafe_skill",
        description="unsafe",
        trigger="unsafe",
        body="async def execute(context):\n    return 'unsafe'",
    )

    assert result.success is False
    assert result.path is None
    assert "disabled by default" in (result.error or "")
    assert not root.exists()


def test_on_agent_success_rejects_executable_persistence_by_default(
    tmp_path: Path,
) -> None:
    root = tmp_path / "skills"
    report = AgentReport(
        agent_id="agent-1",
        role="general",
        task="unsafe callback",
        success=True,
        summary="done",
        artifacts={
            "should_skillify": True,
            "skill_body": "async def execute(context):\n    return 'unsafe'",
        },
    )

    result = asyncio.run(on_agent_success(report, root))

    assert result is not None
    assert result.success is False
    assert result.path is None
    assert "disabled by default" in (result.error or "")
    assert not root.exists()


def test_on_agent_success_opt_in_writes_and_reloads_python_skill(
    tmp_path: Path,
) -> None:
    root = tmp_path / "skills"
    registry = ToolRegistry(
        SafetyGuard(tmp_path), skill_roots=(root,), allow_executable_skills=True
    )
    report = AgentReport(
        agent_id="agent-1",
        role="general",
        task="dynamic skill",
        success=True,
        summary="done",
        artifacts={
            "should_skillify": True,
            "skill_body": (
                "async def execute(context):\n"
                "    return 'reloaded'\n"
            ),
        },
    )

    result = asyncio.run(
        on_agent_success(
            report,
            root,
            registry=registry,
            allow_unsafe_code=True,
        )
    )

    assert result is not None
    assert result.success is True
    assert result.path is not None
    assert result.path.suffix == ".py"
    tool = registry.get("dynamic_skill")
    assert tool is not None
    assert asyncio.run(tool.run()).output == "reloaded"


def test_on_agent_success_removes_new_skill_when_reload_fails(
    tmp_path: Path,
) -> None:
    root = tmp_path / "skills"
    registry = ToolRegistry(
        SafetyGuard(tmp_path), skill_roots=(root,), allow_executable_skills=True
    )
    report = AgentReport(
        agent_id="agent-1",
        role="general",
        task="broken new skill",
        success=True,
        summary="done",
        artifacts={
            "should_skillify": True,
            "skill_body": (
                "raise RuntimeError('import failed')\n\n"
                "async def execute(context):\n"
                "    return 'unreachable'\n"
            ),
        },
    )

    result = asyncio.run(
        on_agent_success(
            report,
            root,
            registry=registry,
            allow_unsafe_code=True,
        )
    )

    path = root / "broken_new_skill.py"
    assert result is not None
    assert result.success is False
    assert result.path == path
    assert result.error == "failed to reload skill: import failed"
    assert not path.exists()


def test_on_agent_success_rejects_linked_existing_target_without_following_it(
    tmp_path: Path,
) -> None:
    root = tmp_path / "skills"
    root.mkdir()
    external = tmp_path / "external.py"
    external_bytes = b"external skill bytes\n"
    external.write_bytes(external_bytes)
    path = root / "linked_skill.py"
    try:
        path.symlink_to(external)
    except OSError:
        pytest.skip("symlinks are unavailable")

    registry = ToolRegistry(
        SafetyGuard(tmp_path), skill_roots=(root,), allow_executable_skills=True
    )
    report = AgentReport(
        agent_id="agent-1",
        role="general",
        task="linked skill",
        success=True,
        summary="done",
        artifacts={
            "should_skillify": True,
            "skill_body": "async def execute(context):\n    return 'new'",
        },
    )

    result = asyncio.run(
        on_agent_success(
            report,
            root,
            registry=registry,
            allow_unsafe_code=True,
        )
    )

    assert result is not None
    assert result.success is False
    assert result.path is None
    assert result.error == (
        f"failed to snapshot existing skill: executable skill cannot be a link: {path}"
    )
    assert path.is_symlink()
    assert external.read_bytes() == external_bytes
    assert registry.get("linked_skill") is None
    assert registry.skill_index() == []


def test_on_agent_success_rejects_oversized_existing_target_before_reload(
    tmp_path: Path,
) -> None:
    root = tmp_path / "skills"
    root.mkdir()
    path = root / "oversized_skill.py"
    original_bytes = b"x" * (MAX_EXECUTABLE_SKILL_BYTES + 1)
    path.write_bytes(original_bytes)
    registry = ToolRegistry(
        SafetyGuard(tmp_path), skill_roots=(root,), allow_executable_skills=True
    )
    report = AgentReport(
        agent_id="agent-1",
        role="general",
        task="oversized skill",
        success=True,
        summary="done",
        artifacts={
            "should_skillify": True,
            "skill_body": "async def execute(context):\n    return 'new'",
        },
    )

    result = asyncio.run(
        on_agent_success(
            report,
            root,
            registry=registry,
            allow_unsafe_code=True,
        )
    )

    assert result is not None
    assert result.success is False
    assert result.path is None
    assert result.error == (
        "failed to snapshot existing skill: executable skill exceeds 256 KiB"
    )
    assert path.read_bytes() == original_bytes
    assert registry.get("oversized_skill") is None
    assert registry.skill_index() == []


def test_on_agent_success_rejects_invalid_task_name_before_snapshot(
    tmp_path: Path,
) -> None:
    root = tmp_path / "skills"
    root.mkdir()
    external = tmp_path / "outside.py"
    sentinel = b"x" * (MAX_EXECUTABLE_SKILL_BYTES + 1)
    external.write_bytes(sentinel)
    registry = ToolRegistry(
        SafetyGuard(tmp_path), skill_roots=(root,), allow_executable_skills=True
    )
    report = AgentReport(
        agent_id="agent-1",
        role="general",
        task="../outside",
        success=True,
        summary="done",
        artifacts={
            "should_skillify": True,
            "skill_body": "async def execute(context):\n    return 'new'",
        },
    )

    result = asyncio.run(
        on_agent_success(
            report,
            root,
            registry=registry,
            allow_unsafe_code=True,
        )
    )

    assert result is not None
    assert result.success is False
    assert result.path is None
    assert result.error == (
        "skill name must be a valid Python identifier, got '../outside'"
    )
    assert external.read_bytes() == sentinel
    assert registry.get("../outside") is None
    assert registry.skill_index() == []


def test_on_agent_success_restores_replaced_skill_when_reload_fails(
    tmp_path: Path,
) -> None:
    root = tmp_path / "skills"
    registry = ToolRegistry(
        SafetyGuard(tmp_path), skill_roots=(root,), allow_executable_skills=True
    )
    first_report = AgentReport(
        agent_id="agent-1",
        role="general",
        task="stable replacement",
        success=True,
        summary="done",
        artifacts={
            "should_skillify": True,
            "skill_body": (
                "async def execute(context):\n"
                "    return 'old behavior'\n"
            ),
        },
    )

    first_result = asyncio.run(
        on_agent_success(
            first_report,
            root,
            registry=registry,
            allow_unsafe_code=True,
        )
    )
    path = root / "stable_replacement.py"
    old_bytes = path.read_bytes()
    old_tool = registry.get("stable_replacement")

    assert first_result is not None
    assert first_result.success is True
    assert old_tool is not None
    assert asyncio.run(old_tool.run()).output == "old behavior"

    replacement_report = AgentReport(
        agent_id="agent-1",
        role="general",
        task="stable replacement",
        success=True,
        summary="done",
        artifacts={
            "should_skillify": True,
            "skill_body": (
                "raise RuntimeError('replacement import failed')\n\n"
                "async def execute(context):\n"
                "    return 'new behavior'\n"
            ),
        },
    )

    result = asyncio.run(
        on_agent_success(
            replacement_report,
            root,
            registry=registry,
            allow_unsafe_code=True,
        )
    )

    assert result is not None
    assert result.success is False
    assert result.path == path
    assert result.error == "failed to reload skill: replacement import failed"
    assert path.read_bytes() == old_bytes
    assert registry.get("stable_replacement") is old_tool
    assert asyncio.run(old_tool.run()).output == "old behavior"


def test_skill_writer_rejects_oversized_serialized_python_before_writing(
    tmp_path: Path,
) -> None:
    root = tmp_path / "skills"
    body = "async def execute(context):\n    return 'ok'\n" + (
        "#" * MAX_EXECUTABLE_SKILL_BYTES
    )

    result = SkillWriter(root).write_skill(
        name="oversized_skill",
        description="oversized",
        trigger="",
        body=body,
        allow_unsafe_code=True,
    )

    assert result.success is False
    assert result.path is None
    assert "256 KiB" in (result.error or "")
    assert not root.exists()


def test_on_agent_success_rejects_failed_report_without_writing(
    tmp_path: Path,
) -> None:
    root = tmp_path / "skills"
    report = AgentReport(
        agent_id="agent-1",
        role="general",
        task="failed skill",
        success=False,
        summary="failed",
        artifacts={
            "should_skillify": True,
            "skill_body": "async def execute(context):\n    return 'bad'",
        },
    )

    result = asyncio.run(
        on_agent_success(report, root, allow_unsafe_code=True)
    )

    assert result is not None
    assert result.success is False
    assert result.path is None
    assert "successful" in (result.error or "")
    assert not root.exists()


def test_on_agent_success_ignores_failed_report_without_skillification_request(
    tmp_path: Path,
) -> None:
    report = AgentReport(
        agent_id="agent-1",
        role="general",
        task="failed non-skill",
        success=False,
        summary="failed",
    )

    assert asyncio.run(on_agent_success(report, tmp_path / "skills")) is None


def test_on_agent_success_rejects_non_string_body_without_writing(
    tmp_path: Path,
) -> None:
    root = tmp_path / "skills"
    report = AgentReport(
        agent_id="agent-1",
        role="general",
        task="malformed skill",
        success=True,
        summary="done",
        artifacts={"should_skillify": True, "skill_body": {"code": "bad"}},
    )

    result = asyncio.run(
        on_agent_success(report, root, allow_unsafe_code=True)
    )

    assert result is not None
    assert result.success is False
    assert result.path is None
    assert result.error == "skill_body must be a string"
    assert not root.exists()


def test_on_agent_success_rejects_malformed_artifacts_without_writing(
    tmp_path: Path,
) -> None:
    root = tmp_path / "skills"
    report = AgentReport(
        agent_id="agent-1",
        role="general",
        task="malformed report",
        success=True,
        summary="done",
        artifacts=None,  # type: ignore[arg-type]
    )

    result = asyncio.run(on_agent_success(report, root, allow_unsafe_code=True))

    assert result is not None
    assert result.success is False
    assert result.path is None
    assert result.error == "report.artifacts must be a mapping"
    assert not root.exists()


def test_on_agent_success_rejects_non_string_task_without_writing(
    tmp_path: Path,
) -> None:
    root = tmp_path / "skills"
    report = AgentReport(
        agent_id="agent-1",
        role="general",
        task=None,  # type: ignore[arg-type]
        success=True,
        summary="done",
        artifacts={
            "should_skillify": True,
            "skill_body": "async def execute(context):\n    return 'bad'",
        },
    )

    result = asyncio.run(
        on_agent_success(report, root, allow_unsafe_code=True)
    )

    assert result is not None
    assert result.success is False
    assert result.path is None
    assert result.error == "report.task must be a string"
    assert not root.exists()


def test_on_agent_success_rejects_invalid_python_body_without_writing(
    tmp_path: Path,
) -> None:
    root = tmp_path / "skills"
    report = AgentReport(
        agent_id="agent-1",
        role="general",
        task="malformed skill",
        success=True,
        summary="done",
        artifacts={
            "should_skillify": True,
            "skill_body": "this is not valid Python syntax:",
        },
    )

    result = asyncio.run(
        on_agent_success(report, root, allow_unsafe_code=True)
    )

    assert result is not None
    assert result.success is False
    assert result.path is None
    assert "invalid Python skill" in (result.error or "")
    assert not root.exists()


def test_skill_writer_converts_filesystem_errors_to_failed_result(
    tmp_path: Path,
) -> None:
    root = tmp_path / "not-a-directory"
    root.write_text("occupied", encoding="utf-8")

    result = SkillWriter(root).write_skill(
        name="path_error",
        description="path error",
        trigger="",
        body="async def execute(context):\n    return 'ok'",
        allow_unsafe_code=True,
    )

    assert result.success is False
    assert result.path is None
    assert (result.error or "").startswith("failed to write skill:")


def test_skill_writer_does_not_clobber_existing_file_on_validation_failure(
    tmp_path: Path,
) -> None:
    root = tmp_path / "skills"
    root.mkdir()
    path = root / "stable_skill.py"
    original = (
        '"""\nname: stable_skill\ndescription: stable\ntrigger: \n"""\n\n'
        "async def execute(context):\n    return 'original'\n"
    )
    path.write_text(original, encoding="utf-8")

    result = SkillWriter(root).write_skill(
        name="stable_skill",
        description="stable",
        body="not valid Python:",
        allow_unsafe_code=True,
    )

    assert result.success is False
    assert path.read_text(encoding="utf-8") == original


def test_on_agent_success_rejects_registry_without_unsafe_opt_in(
    tmp_path: Path,
) -> None:
    root = tmp_path / "skills"
    registry = ToolRegistry(SafetyGuard(tmp_path), skill_roots=(root,))
    report = AgentReport(
        agent_id="agent-1",
        role="general",
        task="reload error",
        success=True,
        summary="done",
        artifacts={
            "should_skillify": True,
            "skill_body": "async def execute(context):\n    return 'ok'",
        },
    )

    result = asyncio.run(
        on_agent_success(
            report,
            root,
            registry=registry,
            allow_unsafe_code=True,
        )
    )

    assert result is not None
    assert result.success is False
    assert result.path is None
    assert "disabled by default" in (result.error or "")
    assert not root.exists()


def test_on_agent_success_converts_reload_errors_to_failed_result(
    tmp_path: Path, monkeypatch,
) -> None:
    root = tmp_path / "skills"
    registry = ToolRegistry(
        SafetyGuard(tmp_path), skill_roots=(root,), allow_executable_skills=True
    )
    calls: list[tuple[str, Path]] = []

    def fail_reload(name: str, path: Path) -> None:
        calls.append((name, path))
        raise RuntimeError("reload failed")

    monkeypatch.setattr(registry, "reload_skill_module", fail_reload)
    report = AgentReport(
        agent_id="agent-1",
        role="general",
        task="reload error",
        success=True,
        summary="done",
        artifacts={
            "should_skillify": True,
            "skill_body": "async def execute(context):\n    return 'ok'",
        },
    )

    result = asyncio.run(
        on_agent_success(
            report,
            root,
            registry=registry,
            allow_unsafe_code=True,
        )
    )

    assert result is not None
    assert result.success is False
    assert result.path == root / "reload_error.py"
    assert result.error == "failed to reload skill: reload failed"
    assert calls == [("reload_error", root / "reload_error.py")]


def test_on_agent_success_rejects_reload_without_loaded_tool(
    tmp_path: Path, monkeypatch,
) -> None:
    root = tmp_path / "skills"
    registry = ToolRegistry(
        SafetyGuard(tmp_path), skill_roots=(root,), allow_executable_skills=True
    )
    monkeypatch.setattr(registry, "reload_skill_module", lambda name, path: None)
    report = AgentReport(
        agent_id="agent-1",
        role="general",
        task="missing reload",
        success=True,
        summary="done",
        artifacts={
            "should_skillify": True,
            "skill_body": "async def execute(context):\n    return 'ok'",
        },
    )

    result = asyncio.run(
        on_agent_success(
            report,
            root,
            registry=registry,
            allow_unsafe_code=True,
        )
    )

    assert result is not None
    assert result.success is False
    assert result.path == root / "missing_reload.py"
    assert result.error == "failed to reload skill: registry returned no tool"


def test_skill_writer_supports_sync_execute_when_opted_in(tmp_path: Path) -> None:
    root = tmp_path / "skills"
    registry = ToolRegistry(
        SafetyGuard(tmp_path), skill_roots=(root,), allow_executable_skills=True
    )

    writer = SkillWriter(root, registry=registry)
    result = writer.write_skill(
        name="sync_skill",
        description="synchronous compatibility",
        trigger="",
        body="def execute(context):\n    return 'sync result'",
        allow_unsafe_code=True,
    )
    assert result.success is True
    assert result.path is not None

    writer.reload("sync_skill", result.path)
    tool = registry.get("sync_skill")
    assert tool is not None
    tool_result = asyncio.run(tool.run())
    assert tool_result.success is True
    assert tool_result.output == "sync result"
