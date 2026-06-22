import json

from plugins.registry import PluginCatalog


def test_local_plugin_catalog_discovers_valid_manifest(tmp_path) -> None:
    root = tmp_path / "plugins"
    plugin = root / "example"
    skill = plugin / "skills" / "review"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("# Review")
    (plugin / "plugin.json").write_text(
        json.dumps(
            {
                "name": "example",
                "version": "1.0.0",
                "description": "Example plugin",
                "skills": ["skills/review/SKILL.md"],
            }
        )
    )
    found = PluginCatalog(((root, "user"),)).discover()
    assert len(found) == 1
    assert found[0].manifest.name == "example"


def test_local_plugin_catalog_rejects_escaping_skill_path(tmp_path) -> None:
    root = tmp_path / "plugins"
    plugin = root / "bad"
    plugin.mkdir(parents=True)
    (plugin / "plugin.json").write_text(
        json.dumps({"name": "bad", "skills": ["../../outside"]})
    )
    catalog = PluginCatalog(((root, "user"),))
    assert catalog.discover() == []
    assert catalog.errors
