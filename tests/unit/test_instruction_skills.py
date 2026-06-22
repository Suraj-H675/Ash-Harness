import pytest

from plugins.skills import ActivateSkillTool, ListSkillsTool, SkillCatalog
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
    result = await ActivateSkillTool(SafetyGuard(tmp_path), catalog).run(
        name="missing"
    )
    assert result.success is False
    assert "Unknown skill" in (result.error or "")
