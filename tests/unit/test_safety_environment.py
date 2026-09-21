from __future__ import annotations

from ash.safety.environment import _build_environment_mapping


def test_windows_environment_names_are_case_insensitive() -> None:
    environment = _build_environment_mapping(
        {
            "Path": r"C:\\tools",
            "WINDIR": r"C:\\Windows",
            "ComSpec": r"C:\\Windows\\System32\\cmd.exe",
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
