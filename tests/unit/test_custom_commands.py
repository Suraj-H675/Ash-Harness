from ash.commands.custom_commands import CommandSource, CustomCommandCatalog


def test_custom_command_discovery_namespacing_and_arguments(tmp_path) -> None:
    root = tmp_path / "commands"
    path = root / "review" / "security.md"
    path.parent.mkdir(parents=True)
    path.write_text(
        "---\ndescription: Review security\n---\nReview $1. Extra: $ARGUMENTS"
    )
    catalog = CustomCommandCatalog(((root, "user"),))
    commands = catalog.discover()
    assert commands[0].name == "review:security"
    parsed = catalog.parse('/review:security "src app" strict')
    assert parsed is not None
    command, arguments = parsed
    assert command.expand(arguments) == "Review src app. Extra: src app strict"


def test_plugin_command_source_is_namespaced_and_path_scoped(tmp_path) -> None:
    plugin = tmp_path / "plugin"
    declared = plugin / "custom" / "review.md"
    hidden = plugin / "private" / "hidden.md"
    declared.parent.mkdir(parents=True)
    hidden.parent.mkdir(parents=True)
    declared.write_text("Review $ARGUMENTS", encoding="utf-8")
    hidden.write_text("Do not load", encoding="utf-8")
    catalog = CustomCommandCatalog(
        (
            CommandSource(
                paths=(declared,),
                source="plugin:example",
                namespace="example",
            ),
        )
    )

    commands = catalog.discover()

    assert [command.name for command in commands] == ["example:review"]
    assert catalog.parse("/hidden") is None


def test_custom_command_catalog_reports_duplicate_names(tmp_path) -> None:
    root = tmp_path / "commands"
    first = root / "first.md"
    second = root / "second.md"
    root.mkdir()
    contents = "---\nname: duplicate\n---\nRun this prompt.\n"
    first.write_text(contents, encoding="utf-8")
    second.write_text(contents, encoding="utf-8")
    catalog = CustomCommandCatalog(((root, "user"),))

    commands = catalog.discover()

    assert len(commands) == 1
    assert "duplicate command name" in catalog.errors[str(second)]
