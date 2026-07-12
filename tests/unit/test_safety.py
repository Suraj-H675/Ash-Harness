from pathlib import Path

import pytest

from ash.safety.guard import SafetyGuard, SafetyViolation


def test_validate_path_allows_paths_inside_project_root(tmp_path: Path) -> None:
    project_root = tmp_path / "project"
    project_root.mkdir()
    target = project_root / "src" / "app.py"
    target.parent.mkdir()
    target.write_text("print('ok')", encoding="utf-8")

    guard = SafetyGuard(project_root)

    assert guard.validate_path("src/app.py") == target.resolve()
    assert guard.validate_path(target) == target.resolve()


def test_validate_path_blocks_traversal_escape(tmp_path: Path) -> None:
    project_root = tmp_path / "project"
    project_root.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("secret", encoding="utf-8")

    guard = SafetyGuard(project_root)

    with pytest.raises(SafetyViolation, match="outside project scope"):
        guard.validate_path("../outside.txt")

    with pytest.raises(SafetyViolation, match="outside project scope"):
        guard.validate_path(outside)


def test_validate_path_blocks_symlink_escape(tmp_path: Path) -> None:
    project_root = tmp_path / "project"
    project_root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    outside_file = outside / "secret.txt"
    outside_file.write_text("secret", encoding="utf-8")
    link = project_root / "linked-outside"

    try:
        link.symlink_to(outside, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"Symlink creation is unavailable: {exc}")

    guard = SafetyGuard(project_root)

    with pytest.raises(SafetyViolation, match="outside project scope"):
        guard.validate_path("linked-outside/secret.txt")


def test_validate_mutation_path_rejects_in_scope_symlink(tmp_path: Path) -> None:
    project_root = tmp_path / "project"
    project_root.mkdir()
    target = project_root / "target"
    target.mkdir()
    link = project_root / "linked"
    try:
        link.symlink_to(target, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"Symlink creation is unavailable: {exc}")

    guard = SafetyGuard(project_root)

    assert guard.validate_path("linked/file.txt") == target / "file.txt"
    with pytest.raises(SafetyViolation, match="symlink or junction"):
        guard.validate_mutation_path("linked/file.txt")


def test_validate_mutation_path_allows_new_nested_path(tmp_path: Path) -> None:
    project_root = tmp_path / "project"
    project_root.mkdir()
    guard = SafetyGuard(project_root)

    assert guard.validate_mutation_path("new/nested/file.txt") == (
        project_root / "new" / "nested" / "file.txt"
    )


def test_validate_path_enforces_allowed_directories(tmp_path: Path) -> None:
    project_root = tmp_path / "project"
    allowed = project_root / "src"
    denied = project_root / "tests"
    allowed.mkdir(parents=True)
    denied.mkdir()

    allowed_file = allowed / "app.py"
    denied_file = denied / "test_app.py"
    allowed_file.write_text("print('ok')", encoding="utf-8")
    denied_file.write_text("def test_app(): pass", encoding="utf-8")

    guard = SafetyGuard(project_root, allowed_directories=[Path("src")])

    assert guard.validate_path("src/app.py") == allowed_file.resolve()
    with pytest.raises(SafetyViolation, match="outside project scope"):
        guard.validate_path("tests/test_app.py")


def test_allowed_directories_cannot_expand_beyond_project_root(tmp_path: Path) -> None:
    project_root = tmp_path / "project"
    project_root.mkdir()

    with pytest.raises(
        SafetyViolation, match="Allowed directory is outside project root"
    ):
        SafetyGuard(project_root, allowed_directories=[tmp_path])


@pytest.mark.parametrize(
    "command",
    [
        "sudo rm -rf /",
        "mkfs.ext4 /dev/sda1",
        "dd if=/dev/zero of=/dev/sda",
        "chmod -R 777 /",
        "chown root:root /etc/passwd",
        "shutdown now",
        "reboot",
        "passwd root",
        "Format-Volume -DriveLetter C",
        "Remove-Item C:\\ -Recurse -Force",
        "del /s /q c:\\*",
        "diskpart",
        "bootrec /fixmbr",
        "net user attacker password /add",
        "reg delete HKLM\\Software\\Example",
    ],
)
def test_validate_command_blocks_linux_and_windows_dangerous_patterns(
    tmp_path: Path,
    command: str,
) -> None:
    guard = SafetyGuard(tmp_path)

    with pytest.raises(SafetyViolation, match="Blocked command pattern"):
        guard.validate_command(command)


def test_validate_command_allows_non_blocklisted_commands(tmp_path: Path) -> None:
    guard = SafetyGuard(tmp_path)

    assert guard.validate_command("python -m pytest tests/unit") == (True, "")
