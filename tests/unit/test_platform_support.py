from __future__ import annotations

import pytest

from ash.cli import main
from ash.platform_support import (
    UnsupportedPlatformError,
    native_platform_supported,
    platform_support_error,
    require_supported_native_platform,
)


def test_linux_and_macos_are_supported() -> None:
    assert native_platform_supported("linux") is True
    assert native_platform_supported("darwin") is True
    assert platform_support_error("linux") is None
    assert platform_support_error("darwin") is None


def test_native_windows_redirects_users_to_wsl2() -> None:
    message = platform_support_error("win32")

    assert message is not None
    assert "Native Windows" in message
    assert "WSL2" in message
    assert native_platform_supported("win32") is False


def test_other_native_platforms_fail_with_supported_platforms() -> None:
    message = platform_support_error("freebsd14")

    assert message is not None
    assert "Linux and macOS" in message
    assert native_platform_supported("freebsd14") is False


def test_require_supported_native_platform_raises_actionable_error() -> None:
    with pytest.raises(UnsupportedPlatformError, match="WSL2"):
        require_supported_native_platform("win32")


def test_cli_rejects_unsupported_platform_before_feature_dispatch(
    monkeypatch,
    capsys,
) -> None:
    monkeypatch.setattr(
        "ash.platform_support.platform_support_error",
        lambda: "Native Windows is not currently supported by Ash. Use WSL2.",
    )

    assert main(["providers", "list", "--json"]) == 2
    rendered = capsys.readouterr()
    assert rendered.out == ""
    assert "Native Windows is not currently supported by Ash" in rendered.err
    assert "WSL2" in rendered.err


def test_cli_allows_doctor_to_report_unsupported_platform(
    monkeypatch,
    capsys,
) -> None:
    from ash.commands.doctor import DoctorCheck

    monkeypatch.setattr(
        "ash.platform_support.platform_support_error",
        lambda: "Native Windows is not currently supported by Ash. Use WSL2.",
    )

    async def fake_doctor(*, connect: bool = False):
        assert connect is False
        return [
            DoctorCheck(
                "platform",
                "fail",
                "Windows AMD64",
                "Use Ash inside WSL2.",
            )
        ]

    monkeypatch.setattr("ash.commands.doctor.run_doctor", fake_doctor)

    assert main(["doctor", "--json"]) == 1
    rendered = capsys.readouterr()
    assert rendered.err == ""
    assert '"name": "platform"' in rendered.out
    assert "WSL2" in rendered.out
