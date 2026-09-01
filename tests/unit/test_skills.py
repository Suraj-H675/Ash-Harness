"""Unit tests for Sprint 14 plugin SDK and dynamic skill evolution."""

from __future__ import annotations

import asyncio
import textwrap
from pathlib import Path
from typing import Any

import pytest

from ash.safety.guard import SafetyGuard
from ash.tools.base import BaseTool, ToolResult
from ash.tools.registry import SkillIndexEntry, ToolRegistry
from ash.tools.skills import (
    MAX_EXECUTABLE_SKILL_BYTES,
    SkillParseError,
    compile_skill,
    configure_runtime,
    parse_markdown_skill,
    parse_python_skill,
    write_python_skill,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _safety_guard(tmp_path: Path) -> SafetyGuard:
    workspace = tmp_path / "ws"
    workspace.mkdir(exist_ok=True)
    return SafetyGuard(project_root=workspace)


def _stub_run_command_output(output: str = "migrations applied") -> None:
    """Register a fake run_command that returns a canned success."""

    class _FakeRun(BaseTool):
        name = "run_command"
        description = "stub"
        args_schema = type(
            "Args",
            (),
            {"__init__": lambda self, **kw: None, "model_fields": {}},
        )

        def __init__(self) -> None:  # noqa: D401
            pass

        async def run(self, **kwargs: Any) -> ToolResult:
            return ToolResult(success=True, output=output)

    configure_runtime(
        tools_provider=lambda: [_FakeRun()],
        root_provider=lambda: Path("/tmp"),
    )


# ---------------------------------------------------------------------------
# markdown parser
# ---------------------------------------------------------------------------


def test_parse_markdown_skill_full(tmp_path: Path) -> None:
    md = textwrap.dedent(
        """\
        ---
        name: run_migrations
        description: Run database migrations using Alembic
        trigger: migrate OR migration OR schema change
        ---

        # run_migrations

        Apply pending migrations to the configured database.

        ## Args
        - `directory`: str = "migrations" - Folder containing the alembic config.
        - `target`: str - Revision id to upgrade to.

        ## Code
        ```python
        async def execute(context, directory: str = "migrations", target: str = "head") -> str:
            return f\"ran in {directory} -> {target}\"
        ```
        """
    )
    path = tmp_path / "run_migrations.md"
    path.write_text(md, encoding="utf-8")

    skill = parse_markdown_skill(path)
    assert skill.name == "run_migrations"
    assert skill.description.startswith("Run database migrations")
    assert "migrate" in skill.trigger
    assert (
        "directory",
        "str",
        '"migrations"',
        "Folder containing the alembic config.",
    ) in skill.args
    assert ("target", "str", "", "Revision id to upgrade to.") in skill.args
    assert "async def execute" in skill.code


def test_parse_markdown_skill_falls_back_to_h1(tmp_path: Path) -> None:
    md = textwrap.dedent(
        """\
        # my_skill

        ## Code
        ```python
        async def execute(context):
            return "ok"
        ```
        """
    )
    path = tmp_path / "fallback.md"
    path.write_text(md, encoding="utf-8")
    skill = parse_markdown_skill(path)
    assert skill.name == "my_skill"
    assert skill.description == ""  # no frontmatter, no Description section


def test_parse_markdown_skill_raises_without_code_block(tmp_path: Path) -> None:
    md = "---\nname: bad\n---\nno code here\n"
    path = tmp_path / "bad.md"
    path.write_text(md, encoding="utf-8")
    with pytest.raises(SkillParseError):
        parse_markdown_skill(path)


# ---------------------------------------------------------------------------
# python skill parser
# ---------------------------------------------------------------------------


def test_parse_python_skill_with_module_docstring(tmp_path: Path) -> None:
    body = textwrap.dedent(
        """\
        \"\"\"
        name: list_users
        description: List every user in the local SQLite db
        trigger: list users OR show users
        \"\"\"

        async def execute(context):
            return "alice\\nbob"
        """
    )
    path = tmp_path / "list_users.py"
    path.write_text(body, encoding="utf-8")
    skill = parse_python_skill(path)
    assert skill.name == "list_users"
    assert "SQLite" in skill.description
    assert "list users" in skill.trigger
    assert "async def execute" in skill.code


def test_parse_python_skill_falls_back_to_filename(tmp_path: Path) -> None:
    body = "async def execute(context):\n    return 'hi'\n"
    path = tmp_path / "no_docstring_skill.py"
    path.write_text(body, encoding="utf-8")
    skill = parse_python_skill(path)
    assert skill.name == "no_docstring_skill"
    assert skill.description == "Python skill"


def test_parse_python_skill_raises_without_execute(tmp_path: Path) -> None:
    path = tmp_path / "no_execute.py"
    path.write_text("x = 1\n", encoding="utf-8")
    with pytest.raises(SkillParseError):
        parse_python_skill(path)


# ---------------------------------------------------------------------------
# compile + execute
# ---------------------------------------------------------------------------


def test_compile_markdown_skill_executes(tmp_path: Path) -> None:
    _stub_run_command_output("migrations applied")
    md = textwrap.dedent(
        """\
        ---
        name: md_skill
        description: tiny markdown skill
        ---

        ## Args
        - `prefix`: str = "ok" - Log prefix.

        ## Code
        ```python
        async def execute(context, prefix: str = "ok") -> str:
            return f"{prefix}: via-skill"
        ```
        """
    )
    path = tmp_path / "md_skill.md"
    path.write_text(md, encoding="utf-8")
    guard = _safety_guard(tmp_path)
    tool = compile_skill(path, guard, allow_unsafe_code=True)
    assert tool.name == "md_skill"
    result = asyncio.run(tool.run(prefix="hello"))
    assert result.success is True
    assert result.output == "hello: via-skill"


def test_compile_markdown_skill_invokes_run_command_via_context(tmp_path: Path) -> None:
    _stub_run_command_output("ok-from-stub")
    md = textwrap.dedent(
        """\
        ---
        name: skill_with_context
        description: uses run_command
        ---

        ## Code
        ```python
        async def execute(context):
            return await context.run_command("alembic upgrade head")
        ```
        """
    )
    path = tmp_path / "skill_with_context.md"
    path.write_text(md, encoding="utf-8")
    guard = _safety_guard(tmp_path)
    tool = compile_skill(path, guard, allow_unsafe_code=True)
    result = asyncio.run(tool.run())
    assert result.success is True
    assert result.output == "ok-from-stub"


def test_compile_markdown_skill_reports_invalid_args(tmp_path: Path) -> None:
    md = textwrap.dedent(
        """\
        ---
        name: typed_skill
        ---

        ## Args
        - `count`: int = 5 - Number of repetitions.

        ## Code
        ```python
        async def execute(context, count: int = 5) -> str:
            return str(count)
        ```
        """
    )
    path = tmp_path / "typed.md"
    path.write_text(md, encoding="utf-8")
    guard = _safety_guard(tmp_path)
    tool = compile_skill(path, guard, allow_unsafe_code=True)
    # 'count' expects an int; passing a string fails validation.
    result = asyncio.run(tool.run(count="not-a-number"))
    assert result.success is False
    assert "invalid args" in (result.error or "").lower()


def test_compile_markdown_skill_catches_executor_exception(tmp_path: Path) -> None:
    md = textwrap.dedent(
        """\
        ---
        name: boom
        ---

        ## Code
        ```python
        async def execute(context):
            raise RuntimeError("kaboom")
        ```
        """
    )
    path = tmp_path / "boom.md"
    path.write_text(md, encoding="utf-8")
    guard = _safety_guard(tmp_path)
    tool = compile_skill(path, guard, allow_unsafe_code=True)
    result = asyncio.run(tool.run())
    assert result.success is False
    assert "kaboom" in (result.error or "")


def test_executable_skill_compilation_is_disabled_by_default(tmp_path: Path) -> None:
    marker = tmp_path / "imported.txt"
    path = tmp_path / "unsafe.py"
    path.write_text(
        f"from pathlib import Path\nPath({str(marker)!r}).write_text('ran')\n"
        "async def execute(context):\n    return 'unsafe'\n",
        encoding="utf-8",
    )

    with pytest.raises(SkillParseError, match="disabled by default"):
        compile_skill(path, _safety_guard(tmp_path))

    assert not marker.exists()


def test_executable_skill_context_cannot_create_missing_command_tool(
    tmp_path: Path,
) -> None:
    configure_runtime(tools_provider=lambda: [], root_provider=lambda: tmp_path)
    path = tmp_path / "command.md"
    path.write_text(
        "## Code\n```python\nasync def execute(context):\n"
        "    return await context.run_command('echo unsafe')\n```\n",
        encoding="utf-8",
    )
    tool = compile_skill(path, _safety_guard(tmp_path), allow_unsafe_code=True)

    result = asyncio.run(tool.run())

    assert result.success is False
    assert "cannot create an ungoverned command tool" in (result.error or "")


def test_write_python_skill_is_disabled_by_default(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="disabled by default"):
        write_python_skill(
            tmp_path / "skills",
            name="unsafe",
            description="unsafe",
            trigger="unsafe",
            body="async def execute(context): pass",
        )

    assert not (tmp_path / "skills").exists()


# ---------------------------------------------------------------------------
# ToolRegistry discovery + load
# ---------------------------------------------------------------------------


def test_registry_discovers_both_formats(tmp_path: Path) -> None:
    skill_dir = tmp_path / "skills"
    skill_dir.mkdir()
    (skill_dir / "alpha.md").write_text(
        textwrap.dedent(
            """\
            ---
            name: alpha
            description: alpha skill
            ---

            ## Code
            ```python
            async def execute(context):
                return "a"
            ```
            """
        ),
        encoding="utf-8",
    )
    (skill_dir / "beta.py").write_text(
        textwrap.dedent(
            """\
            \"\"\"
            name: beta
            description: beta skill
            \"\"\"
            async def execute(context):
                return "b"
            """
        ),
        encoding="utf-8",
    )
    guard = _safety_guard(tmp_path)
    reg = ToolRegistry(guard, skill_roots=(skill_dir,))
    entries = reg.discover_skills()
    by_name = {e.name: e for e in entries}
    assert set(by_name) == {"alpha", "beta"}
    assert by_name["alpha"].source == "markdown"
    assert by_name["beta"].source == "python"


def test_registry_load_skill_compiles_tool(tmp_path: Path) -> None:
    skill_dir = tmp_path / "skills"
    skill_dir.mkdir()
    (skill_dir / "lazy.md").write_text(
        textwrap.dedent(
            """\
            ---
            name: lazy_skill
            description: lazy
            ---

            ## Code
            ```python
            async def execute(context):
                return "lazy-ok"
            ```
            """
        ),
        encoding="utf-8",
    )
    guard = _safety_guard(tmp_path)
    reg = ToolRegistry(guard, skill_roots=(skill_dir,), allow_executable_skills=True)
    reg.discover_skills()
    tool = reg.load_skill("lazy_skill")
    assert tool is not None
    assert tool.name == "lazy_skill"
    result = asyncio.run(tool.run())
    assert result.output == "lazy-ok"
    # The registry now has the compiled tool available for dispatch.
    assert reg.get("lazy_skill") is tool


def test_registry_rejects_executable_skill_loading_by_default(tmp_path: Path) -> None:
    skill_dir = tmp_path / "skills"
    skill_dir.mkdir()
    path = skill_dir / "unsafe.md"
    path.write_text(
        "## Code\n```python\nasync def execute(context):\n    return 'unsafe'\n```\n",
        encoding="utf-8",
    )
    registry = ToolRegistry(_safety_guard(tmp_path), skill_roots=(skill_dir,))
    registry.discover_skills()

    with pytest.raises(SkillParseError, match="disabled by default"):
        registry.load_skill("unsafe")


def test_registry_isolates_invalid_executable_skill_metadata(tmp_path: Path) -> None:
    skill_dir = tmp_path / "skills"
    skill_dir.mkdir()
    valid = skill_dir / "valid.py"
    invalid = skill_dir / "invalid.py"
    oversized = skill_dir / "oversized.md"
    valid.write_text("async def execute(context):\n    return 'ok'\n", encoding="utf-8")
    invalid.write_bytes(b"\xff\xfe")
    oversized.write_bytes(b"x" * (MAX_EXECUTABLE_SKILL_BYTES + 1))
    registry = ToolRegistry(_safety_guard(tmp_path), skill_roots=(skill_dir,))

    entries = registry.discover_skills()

    assert [entry.name for entry in entries] == ["valid"]
    assert str(invalid) in registry.skill_errors
    assert "exceeds 256 KiB" in registry.skill_errors[str(oversized)]


def test_registry_skips_linked_skill_directories(tmp_path: Path) -> None:
    skill_dir = tmp_path / "skills"
    skill_dir.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.py").write_text(
        "async def execute(context):\n    return 'secret'\n", encoding="utf-8"
    )
    try:
        (skill_dir / "linked").symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("symlinks are unavailable")

    registry = ToolRegistry(_safety_guard(tmp_path), skill_roots=(skill_dir,))

    assert registry.discover_skills() == []


def test_registry_bounds_skill_discovery_entries(tmp_path: Path, monkeypatch) -> None:
    skill_dir = tmp_path / "skills"
    skill_dir.mkdir()
    for name in ("a.py", "b.py", "c.py"):
        (skill_dir / name).write_text(
            f"async def execute(context):\n    return '{name}'\n", encoding="utf-8"
        )
    monkeypatch.setattr("ash.tools.registry.MAX_SKILL_DISCOVERY_ENTRIES", 2)

    registry = ToolRegistry(_safety_guard(tmp_path), skill_roots=(skill_dir,))

    assert len(registry.discover_skills()) == 2


def test_registry_reports_duplicate_executable_skill_names(tmp_path: Path) -> None:
    skill_dir = tmp_path / "skills"
    skill_dir.mkdir()
    for filename in ("first.py", "second.py"):
        (skill_dir / filename).write_text(
            '"""\nname: duplicate\n"""\n'
            "async def execute(context):\n    return 'ok'\n",
            encoding="utf-8",
        )
    registry = ToolRegistry(_safety_guard(tmp_path), skill_roots=(skill_dir,))

    entries = registry.discover_skills()

    assert [entry.name for entry in entries] == ["duplicate"]
    assert (
        "duplicate executable skill name"
        in registry.skill_errors[str(skill_dir / "second.py")]
    )


def test_registry_load_skill_returns_none_for_unknown(tmp_path: Path) -> None:
    guard = _safety_guard(tmp_path)
    reg = ToolRegistry(guard)
    assert reg.load_skill("missing") is None


def test_registry_load_all_skills(tmp_path: Path) -> None:
    skill_dir = tmp_path / "skills"
    skill_dir.mkdir()
    for i in range(3):
        (skill_dir / f"skill_{i}.md").write_text(
            textwrap.dedent(
                f"""\
                ---
                name: skill_{i}
                description: skill {i}
                ---

                ## Code
                ```python
                async def execute(context):
                    return "ok_{i}"
                ```
                """
            ),
            encoding="utf-8",
        )
    guard = _safety_guard(tmp_path)
    reg = ToolRegistry(guard, skill_roots=(skill_dir,), allow_executable_skills=True)
    reg.discover_skills()
    tools = reg.load_all_skills()
    assert len(tools) == 3
    for i, tool in enumerate(tools):
        assert tool.name == f"skill_{i}"


def test_registry_index_persists_without_compilation(tmp_path: Path) -> None:
    skill_dir = tmp_path / "skills"
    skill_dir.mkdir()
    (skill_dir / "x.md").write_text(
        textwrap.dedent(
            """\
            ---
            name: x
            description: not yet compiled
            trigger: x OR do-x
            ---

            ## Code
            ```python
            async def execute(context):
                return "x"
            ```
            """
        ),
        encoding="utf-8",
    )
    guard = _safety_guard(tmp_path)
    reg = ToolRegistry(guard, skill_roots=(skill_dir,))
    entries = reg.discover_skills()
    assert len(entries) == 1
    assert entries[0].name == "x"
    assert entries[0].trigger == "x OR do-x"
    # Skill is indexed but NOT in the tool map yet.
    assert "x" not in reg.names()


# ---------------------------------------------------------------------------
# self-extension
# ---------------------------------------------------------------------------


def test_write_python_skill_writes_and_registry_reloads(tmp_path: Path) -> None:
    skill_dir = tmp_path / "skills"
    skill_dir.mkdir()
    guard = _safety_guard(tmp_path)
    reg = ToolRegistry(guard, skill_roots=(skill_dir,), allow_executable_skills=True)

    body = textwrap.dedent(
        """\
        async def execute(context):
            return "first"
        """
    )
    path = write_python_skill(
        skill_dir,
        name="dynamic_skill",
        description="self-written",
        trigger="dyn",
        body=body,
        allow_unsafe_code=True,
    )
    assert path.exists()
    text = path.read_text()
    assert "name: dynamic_skill" in text
    assert "description: self-written" in text

    # The new file is visible to discover_skills (it indexes it), but
    # the tool is not loaded into the registry's tool map yet.
    reg.discover_skills(refresh=True)
    assert "dynamic_skill" in {e.name for e in reg.skill_index()}
    assert "dynamic_skill" not in reg.names()

    # Reload picks up the new file and registers the compiled tool.
    tool = reg.reload_skill_module("dynamic_skill", path)
    assert tool is not None
    assert tool.name == "dynamic_skill"
    assert "dynamic_skill" in reg.names()

    result = asyncio.run(tool.run())
    assert result.output == "first"

    # Self-extension: rewrite the same skill with a different body and
    # reload — the new behaviour must be visible immediately.
    body2 = textwrap.dedent(
        """\
        async def execute(context):
            return "second"
        """
    )
    write_python_skill(
        skill_dir,
        name="dynamic_skill",
        description="self-written v2",
        trigger="dyn v2",
        body=body2,
        allow_unsafe_code=True,
    )
    reloaded = reg.reload_skill_module("dynamic_skill", path)
    assert reloaded is not None
    result2 = asyncio.run(reloaded.run())
    assert result2.output == "second"


def test_write_python_skill_rejects_invalid_name(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        write_python_skill(
            tmp_path,
            name="not-valid",
            description="x",
            trigger="x",
            body="async def execute(context): pass",
            allow_unsafe_code=True,
        )


def test_write_python_skill_creates_skill_dir(tmp_path: Path) -> None:
    nested = tmp_path / "deep" / "nested" / "skills"
    path = write_python_skill(
        nested,
        name="deep_skill",
        description="deep",
        trigger="deep",
        body="async def execute(context):\n    return 'deep'\n",
        allow_unsafe_code=True,
    )
    assert path.exists()
    assert nested.is_dir()


# ---------------------------------------------------------------------------
# SkillIndexEntry + integration with the existing BaseTool contract
# ---------------------------------------------------------------------------


def test_compiled_skill_is_a_basetool(tmp_path: Path) -> None:
    md = textwrap.dedent(
        """\
        ---
        name: integration_skill
        ---

        ## Code
        ```python
        async def execute(context):
            return "ok"
        ```
        """
    )
    path = tmp_path / "integration.md"
    path.write_text(md, encoding="utf-8")
    guard = _safety_guard(tmp_path)
    tool = compile_skill(path, guard, allow_unsafe_code=True)
    assert isinstance(tool, BaseTool)
    assert tool.name == "integration_skill"
    assert issubclass(tool.args_schema, object)
    # The args schema is a Pydantic model that accepts kwargs.
    instance = tool.args_schema()
    assert instance is not None


def test_skill_index_entry_round_trip(tmp_path: Path) -> None:
    entry = SkillIndexEntry(
        name="x", description="y", source="python", path="/x.py", trigger="t"
    )
    assert entry.name == "x"
    assert entry.source == "python"
    assert entry.trigger == "t"
