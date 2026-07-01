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


def test_plugin_catalog_reports_duplicate_names(tmp_path) -> None:
    first_root = tmp_path / "first"
    second_root = tmp_path / "second"
    for root in (first_root, second_root):
        plugin = root / "example"
        plugin.mkdir(parents=True)
        (plugin / "plugin.json").write_text(json.dumps({"name": "example"}))

    catalog = PluginCatalog(((first_root, "user"), (second_root, "project")))

    found = catalog.discover()

    assert len(found) == 1
    duplicate = second_root / "example" / "plugin.json"
    assert "duplicate plugin name" in catalog.errors[str(duplicate)]


def test_plugin_skill_paths_use_declared_or_default_locations(tmp_path) -> None:
    root = tmp_path / "plugins"
    plugin = root / "example"
    declared = plugin / "custom" / "review" / "SKILL.md"
    declared.parent.mkdir(parents=True)
    declared.write_text("# Review\n")
    (plugin / "plugin.json").write_text(
        json.dumps({"name": "example", "skills": ["custom/review/SKILL.md"]})
    )

    found = PluginCatalog(((root, "user"),)).discover()

    assert found[0].skill_paths() == (declared,)


def test_plugin_catalog_excludes_disabled_plugins_by_default(tmp_path) -> None:
    root = tmp_path / "plugins"
    plugin = root / "example"
    plugin.mkdir(parents=True)
    (plugin / "plugin.json").write_text(json.dumps({"name": "example"}))
    catalog = PluginCatalog(((root, "user"),), disabled_plugins=frozenset({"example"}))

    assert catalog.discover() == []
    discovered = catalog.discover(include_disabled=True)
    assert len(discovered) == 1
    assert discovered[0].enabled is False
