from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from ash.safety.environment import _build_environment_mapping, resolve_host_executable


def test_windows_environment_names_are_case_insensitive() -> None:
    environment = _build_environment_mapping(
        {
            "Path": r"C:\\tools",
            "WINDIR": r"C:\\Windows",
            "ComSpec": r"C:\\Windows\\System32\\cmd.exe",
            "UserProfile": r"C:\\Users\\tester",
            "HomeDrive": "C:",
            "HomePath": r"\\Users\\tester",
            "lc_all": "C",
            "MY_ALLOWED_TOKEN": "allowed",
            "UNRELATED_SECRET": "drop-me",
        },
        {"my_allowed_token"},
        overrides=None,
        platform_name="nt",
    )

    assert environment["Path"] == r"C:\\tools"
    assert environment["WINDIR"] == r"C:\\Windows"
    assert environment["ComSpec"].endswith("cmd.exe")
    assert environment["UserProfile"] == r"C:\\Users\\tester"
    assert environment["HomeDrive"] == "C:"
    assert environment["HomePath"] == r"\\Users\\tester"
    assert environment["lc_all"] == "C"
    assert environment["MY_ALLOWED_TOKEN"] == "allowed"
    assert "UNRELATED_SECRET" not in environment
    assert "PATH" not in environment


def test_windows_overrides_replace_case_variant_without_duplicates() -> None:
    environment = _build_environment_mapping(
        {"Path": r"C:\\old", "SystemRoot": r"C:\\Windows"},
        (),
        overrides={"PATH": r"C:\\new"},
        platform_name="nt",
    )

    assert environment["PATH"] == r"C:\\new"
    assert "Path" not in environment
    assert environment["SystemRoot"] == r"C:\\Windows"


def test_posix_environment_names_remain_case_sensitive() -> None:
    environment = _build_environment_mapping(
        {
            "HOME": "/home/test",
            "Path": "/untrusted/bin",
            "lc_all": "C",
            "MY_ALLOWED_TOKEN": "drop-me",
        },
        {"my_allowed_token"},
        overrides=None,
        platform_name="posix",
    )

    assert environment["HOME"] == "/home/test"
    assert "Path" not in environment
    assert "lc_all" not in environment
    assert "MY_ALLOWED_TOKEN" not in environment


def test_host_executable_lookup_is_scoped_to_each_vetted_path_directory(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    host_bin = tmp_path / "host-bin"
    workspace.mkdir()
    host_bin.mkdir()
    observed: list[tuple[str, str | None]] = []

    def which(command: str, *, path: str | None = None) -> str | None:
        observed.append((command, path))
        if path == str(host_bin.resolve()):
            return str(host_bin / "git.exe")
        return None

    with patch("ash.safety.environment.shutil.which", side_effect=which):
        resolved = resolve_host_executable(
            "git",
            workspace_root=workspace,
            cwd=workspace,
            search_path=str(host_bin),
        )

    assert resolved == str((host_bin / "git.exe").resolve())
    assert observed == [("git", str(host_bin.resolve()))]
