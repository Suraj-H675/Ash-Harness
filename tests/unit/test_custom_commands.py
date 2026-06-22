from cli.custom_commands import CustomCommandCatalog


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
