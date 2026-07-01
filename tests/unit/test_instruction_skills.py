import pytest

from plugins.skills import (
    MAX_SKILL_BYTES,
    ActivateSkillTool,
    ListSkillsTool,
    SkillCatalog,
    SkillSource,
)
from safety.guard import SafetyGuard


@pytest.mark.asyncio
async def test_instruction_skills_discover_list_and_activate(tmp_path) -> None:
    skill_dir = tmp_path / "skills" / "review"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: review\ndescription: Review code carefully\n---\n"
        "# Review\nInspect correctness and tests.\n"
    )
    catalog = SkillCatalog((tmp_path / "skills",))
    guard = SafetyGuard(tmp_path)

    listed = await ListSkillsTool(guard, catalog).run()
    assert listed.output == "review: Review code carefully"

    activated = await ActivateSkillTool(guard, catalog).run(name="review")
    assert activated.success is True
    assert '<skill name="review"' in activated.output
    assert "Inspect correctness and tests." in activated.output


@pytest.mark.asyncio
async def test_unknown_instruction_skill_fails_cleanly(tmp_path) -> None:
    catalog = SkillCatalog((tmp_path / "skills",))
    result = await ActivateSkillTool(SafetyGuard(tmp_path), catalog).run(name="missing")
    assert result.success is False
    assert "Unknown skill" in (result.error or "")


def test_instruction_skill_discovery_isolates_invalid_files(tmp_path) -> None:
    root = tmp_path / "skills"
    valid = root / "valid"
    invalid = root / "invalid"
    valid.mkdir(parents=True)
    invalid.mkdir(parents=True)
    (valid / "SKILL.md").write_text("# Valid\nDo the work.\n", encoding="utf-8")
    (invalid / "SKILL.md").write_bytes(b"\xff\xfe")

    catalog = SkillCatalog((root,))

    assert [skill.name for skill in catalog.discover()] == ["valid"]
    assert str(invalid / "SKILL.md") in catalog.errors


def test_instruction_skill_discovery_reports_duplicate_names(tmp_path) -> None:
    root = tmp_path / "skills"
    for directory in ("first", "second"):
        skill = root / directory
        skill.mkdir(parents=True)
        (skill / "SKILL.md").write_text(
            "---\nname: duplicate\n---\nUse this skill.\n",
            encoding="utf-8",
        )

    catalog = SkillCatalog((root,))
    discovered = catalog.discover()

    assert len(discovered) == 1
    assert discovered[0].path == root / "first" / "SKILL.md"
    assert "duplicate skill name" in catalog.errors[str(root / "second" / "SKILL.md")]


@pytest.mark.parametrize(
    ("contents", "message"),
    [
        ("---\nname: bad name\n---\nInstructions\n", "path-safe identifier"),
        ("---\nname: empty\n---\n", "instructions are empty"),
    ],
)
def test_instruction_skill_discovery_rejects_invalid_content(
    tmp_path, contents: str, message: str
) -> None:
    skill = tmp_path / "skills" / "bad"
    skill.mkdir(parents=True)
    path = skill / "SKILL.md"
    path.write_text(contents, encoding="utf-8")

    catalog = SkillCatalog((tmp_path / "skills",))

    assert catalog.discover() == []
    assert message in catalog.errors[str(path)]


def test_instruction_skill_discovery_rejects_oversized_file(tmp_path) -> None:
    skill = tmp_path / "skills" / "large"
    skill.mkdir(parents=True)
    path = skill / "SKILL.md"
    path.write_bytes(b"x" * (MAX_SKILL_BYTES + 1))

    catalog = SkillCatalog((tmp_path / "skills",))

    assert catalog.discover() == []
    assert "exceeds 512 KiB" in catalog.errors[str(path)]


def test_instruction_skill_source_namespaces_explicit_paths(tmp_path) -> None:
    declared = tmp_path / "plugin" / "custom" / "review" / "SKILL.md"
    undeclared = tmp_path / "plugin" / "private" / "hidden" / "SKILL.md"
    declared.parent.mkdir(parents=True)
    undeclared.parent.mkdir(parents=True)
    declared.write_text("# Review\nReview code.\n", encoding="utf-8")
    undeclared.write_text("# Hidden\nDo not load.\n", encoding="utf-8")

    catalog = SkillCatalog((SkillSource(paths=(declared,), namespace="example"),))

    assert [skill.name for skill in catalog.discover()] == ["example:review"]
