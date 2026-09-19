from pathlib import Path

import pytest

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


def test_custom_command_catalog_rejects_duplicate_metadata_keys(tmp_path) -> None:
    root = tmp_path / "commands"
    path = root / "ambiguous.md"
    root.mkdir()
    path.write_text(
        "---\nname: safe\nname: override\n---\nRun this prompt.\n",
        encoding="utf-8",
    )
    catalog = CustomCommandCatalog(((root, "user"),))

    assert catalog.discover() == []
    assert "duplicate command metadata key: 'name'" in catalog.errors[str(path)]


def test_custom_command_catalog_rejects_direct_linked_command(tmp_path: Path) -> None:
    target = tmp_path / "target.md"
    target.write_text("Run this prompt.", encoding="utf-8")
    linked = tmp_path / "linked.md"
    try:
        linked.symlink_to(target)
    except OSError:
        pytest.skip("symlinks are unavailable")

    catalog = CustomCommandCatalog(
        (CommandSource(paths=(linked,), source="user"),)
    )

    assert catalog.discover() == []
    assert "cannot be a link" in catalog.errors[str(linked)]


def test_custom_command_swap_cannot_escape_source_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "commands"
    root.mkdir()
    command_path = root / "safe.md"
    saved = root / "safe-saved.md"
    outside = tmp_path / "outside.md"
    command_path.write_text("Safe prompt", encoding="utf-8")
    outside.write_text("Outside prompt", encoding="utf-8")
    real_is_symlink = Path.is_symlink
    checks = 0
    swapped = False

    def is_symlink_then_swap(path: Path) -> bool:
        nonlocal checks, swapped
        result = real_is_symlink(path)
        if path == command_path:
            checks += 1
        if path == command_path and checks == 2 and not result and not swapped:
            swapped = True
            command_path.rename(saved)
            try:
                command_path.symlink_to(outside)
            except OSError as exc:
                pytest.skip(f"symlink creation is unavailable: {exc}")
        return result

    monkeypatch.setattr(Path, "is_symlink", is_symlink_then_swap)

    catalog = CustomCommandCatalog(((root, "user"),))
    commands = catalog.discover()

    assert swapped is True
    assert commands == []
    assert str(command_path) in catalog.errors


def test_custom_command_parent_swap_cannot_escape_source_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "commands"
    nested = root / "review"
    nested.mkdir(parents=True)
    command_path = nested / "security.md"
    saved = root / "review-saved"
    outside = tmp_path / "outside"
    outside.mkdir()
    command_path.write_text("Safe prompt", encoding="utf-8")
    (outside / "security.md").write_text("Outside prompt", encoding="utf-8")
    real_is_symlink = Path.is_symlink
    checks = 0
    swapped = False

    def is_symlink_then_swap(path: Path) -> bool:
        nonlocal checks, swapped
        result = real_is_symlink(path)
        if path == command_path:
            checks += 1
        if path == command_path and checks == 2 and not result and not swapped:
            swapped = True
            nested.rename(saved)
            try:
                nested.symlink_to(outside, target_is_directory=True)
            except OSError as exc:
                pytest.skip(f"symlink creation is unavailable: {exc}")
        return result

    monkeypatch.setattr(Path, "is_symlink", is_symlink_then_swap)

    catalog = CustomCommandCatalog(((root, "user"),))
    commands = catalog.discover()

    assert swapped is True
    assert commands == []
    assert str(command_path) in catalog.errors


def test_custom_command_catalog_bounds_recursive_discovery(
    tmp_path: Path, monkeypatch
) -> None:
    root = tmp_path / "commands"
    root.mkdir()
    for name in ("a.md", "b.md", "c.md"):
        (root / name).write_text(f"Prompt for {name}", encoding="utf-8")
    monkeypatch.setattr(
        "ash.commands.custom_commands.MAX_COMMAND_DISCOVERY_ENTRIES", 2
    )

    commands = CustomCommandCatalog(((root, "user"),)).discover()

    assert len(commands) <= 2
