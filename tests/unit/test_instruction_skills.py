from pathlib import Path

import pytest

from ash.cli import _build_tools
from plugins.skills import (
    MAX_SKILL_BYTES,
    ActivateSkillTool,
    ListSkillsTool,
    ReadSkillResourceTool,
    SkillCatalog,
    SkillSource,
    parse_instruction_skill,
    render_available_skills,
)
from safety.guard import SafetyGuard


def _write_skill(
    root: Path,
    name: str,
    *,
    description: str = "Review code carefully",
    body: str = "# Review\nInspect correctness and tests.",
    extra_frontmatter: str = "",
) -> Path:
    directory = root / name
    directory.mkdir(parents=True)
    path = directory / "SKILL.md"
    path.write_text(
        f"---\nname: {name}\ndescription: {description}\n"
        f"{extra_frontmatter}---\n{body}\n",
        encoding="utf-8",
    )
    return path


@pytest.mark.asyncio
async def test_instruction_skills_discover_list_and_activate(tmp_path) -> None:
    path = _write_skill(tmp_path / "skills", "review")
    catalog = SkillCatalog((tmp_path / "skills",))
    guard = SafetyGuard(tmp_path)

    listed = await ListSkillsTool(guard, catalog).run()
    assert listed.output == "review: Review code carefully"

    activated = await ActivateSkillTool(guard, catalog).run(name="review")
    assert activated.success is True
    assert '<skill name="review"' in activated.output
    assert f'source="{path}"' in activated.output
    assert "Inspect correctness and tests." in activated.output


@pytest.mark.asyncio
async def test_unknown_instruction_skill_fails_cleanly(tmp_path) -> None:
    catalog = SkillCatalog((tmp_path / "skills",))
    result = await ActivateSkillTool(SafetyGuard(tmp_path), catalog).run(name="missing")
    assert result.success is False
    assert "Unknown skill" in (result.error or "")


def test_instruction_skill_discovery_isolates_invalid_files(tmp_path) -> None:
    root = tmp_path / "skills"
    _write_skill(root, "valid", description="Valid skill")
    invalid = root / "invalid"
    invalid.mkdir(parents=True)
    (invalid / "SKILL.md").write_bytes(b"\xff\xfe")

    catalog = SkillCatalog((root,))

    assert [skill.name for skill in catalog.discover()] == ["valid"]
    assert str(invalid / "SKILL.md") in catalog.errors


def test_instruction_skill_discovery_reports_duplicate_names(tmp_path) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    first_path = _write_skill(first, "duplicate")
    second_path = _write_skill(second, "duplicate")

    catalog = SkillCatalog((first, second))
    discovered = catalog.discover()

    assert len(discovered) == 1
    assert discovered[0].path == first_path
    assert "duplicate skill name" in catalog.errors[str(second_path)]


@pytest.mark.parametrize(
    ("name", "contents", "message"),
    [
        ("bad", "# Missing frontmatter\n", "requires YAML frontmatter"),
        (
            "bad",
            "---\nname: bad name\ndescription: Bad\n---\nInstructions\n",
            "1-64 lowercase",
        ),
        (
            "bad",
            "---\nname: other\ndescription: Bad\n---\nInstructions\n",
            "must match parent directory",
        ),
        (
            "bad",
            "---\nname: bad\ndescription: Bad\n---\n",
            "instructions are empty",
        ),
        (
            "bad",
            "---\nname: bad\n---\nInstructions\n",
            "description is required",
        ),
    ],
)
def test_instruction_skill_discovery_rejects_invalid_content(
    tmp_path, name: str, contents: str, message: str
) -> None:
    skill = tmp_path / "skills" / name
    skill.mkdir(parents=True)
    path = skill / "SKILL.md"
    path.write_text(contents, encoding="utf-8")

    catalog = SkillCatalog((tmp_path / "skills",))

    assert catalog.discover() == []
    assert message in catalog.errors[str(path)]


def test_instruction_skill_parses_standard_optional_metadata(tmp_path) -> None:
    path = _write_skill(
        tmp_path,
        "review",
        extra_frontmatter=(
            "license: Apache-2.0\n"
            "compatibility: Requires git\n"
            "allowed-tools: Bash(git:*) Read\n"
            "metadata:\n  author: ash\n  version: '1'\n"
            "user-invocable: true\n"
        ),
    )

    skill = parse_instruction_skill(path)

    assert skill.canonical_name == "review"
    assert skill.license == "Apache-2.0"
    assert skill.compatibility == "Requires git"
    assert skill.allowed_tools == ("Bash(git:*)", "Read")
    assert dict(skill.metadata) == {"author": "ash", "version": "1"}
    assert dict(skill.extra_frontmatter) == {"user-invocable": True}


def test_instruction_skill_discovery_rejects_oversized_file(tmp_path) -> None:
    skill = tmp_path / "skills" / "large"
    skill.mkdir(parents=True)
    path = skill / "SKILL.md"
    path.write_bytes(b"x" * (MAX_SKILL_BYTES + 1))

    catalog = SkillCatalog((tmp_path / "skills",))

    assert catalog.discover() == []
    assert "exceeds 512 KiB" in catalog.errors[str(path)]


def test_instruction_skill_source_namespaces_explicit_paths(tmp_path) -> None:
    declared = _write_skill(tmp_path / "plugin" / "custom", "review")
    _write_skill(tmp_path / "plugin" / "private", "hidden")

    catalog = SkillCatalog((SkillSource(paths=(declared,), namespace="example"),))

    assert [skill.name for skill in catalog.discover()] == ["example:review"]


def test_discovery_stops_at_skill_root(tmp_path) -> None:
    root_skill = _write_skill(tmp_path / "skills", "review")
    nested = root_skill.parent / "references" / "nested"
    nested.mkdir(parents=True)
    (nested / "SKILL.md").write_text(
        "---\nname: nested\ndescription: Hidden nested skill\n---\nDo work.\n",
        encoding="utf-8",
    )

    catalog = SkillCatalog((tmp_path / "skills",))

    assert [skill.name for skill in catalog.discover()] == ["review"]


def test_available_skills_render_only_progressive_metadata(tmp_path) -> None:
    _write_skill(tmp_path, "review", body="SECRET FULL INSTRUCTIONS")
    rendered = render_available_skills(SkillCatalog((tmp_path,)))

    assert "review" in rendered
    assert "Review code carefully" in rendered
    assert "SECRET FULL INSTRUCTIONS" not in rendered


@pytest.mark.asyncio
async def test_skill_resource_reader_is_scoped_to_skill_package(tmp_path) -> None:
    path = _write_skill(tmp_path / "skills", "review")
    reference = path.parent / "references" / "guide.md"
    reference.parent.mkdir()
    reference.write_text("Detailed guidance", encoding="utf-8")
    catalog = SkillCatalog((tmp_path / "skills",))
    tool = ReadSkillResourceTool(SafetyGuard(tmp_path), catalog)

    result = await tool.run(name="review", path="references/guide.md")
    traversal = await tool.run(name="review", path="../outside.md")

    assert result.success is True
    assert result.output == "Detailed guidance"
    assert traversal.success is False
    assert "traversal" in (traversal.error or "")


@pytest.mark.asyncio
async def test_default_tools_exclude_disabled_plugin_skills(
    tmp_path, monkeypatch
) -> None:
    home = tmp_path / "home"
    plugin = home / ".ash" / "plugins" / "example"
    skill = plugin / "skills" / "review"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text(
        "---\nname: review\ndescription: Review code\n---\nReview code.\n",
        encoding="utf-8",
    )
    (plugin / "plugin.json").write_text(
        '{"name": "example", "skills": ["skills/review/SKILL.md"]}',
        encoding="utf-8",
    )
    state = home / ".ash" / "extensions.json"
    state.write_text(
        '{"version": 1, "disabled_plugins": ["example"]}', encoding="utf-8"
    )
    monkeypatch.setenv("HOME", str(home))

    tools = _build_tools(SafetyGuard(tmp_path), tmp_path)
    result = await tools["list_skills"].run()

    assert result.success is True
    assert result.output == ""
