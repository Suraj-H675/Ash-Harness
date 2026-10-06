from __future__ import annotations

import subprocess
from types import SimpleNamespace

import pytest

import ash.ui.clipboard as clipboard_module
from ash.ui.clipboard import ClipboardUnavailable, copy_to_clipboard, read_from_clipboard


def test_copy_to_clipboard_uses_resolved_host_backend(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    resolved: list[str] = []
    calls: list[tuple[list[str], str, dict[str, str]]] = []

    def resolve(command: str, **kwargs) -> str | None:
        del kwargs
        resolved.append(command)
        return "/usr/bin/wl-copy" if command == "wl-copy" else None

    def run(argv, **kwargs):
        calls.append((list(argv), kwargs["input"], dict(kwargs["env"])))
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(clipboard_module, "resolve_host_executable", resolve)
    monkeypatch.setattr(clipboard_module.subprocess, "run", run)
    monkeypatch.setattr(clipboard_module.sys, "platform", "linux")
    monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-1")
    monkeypatch.setenv("OPENAI_API_KEY", "must-not-leak")

    assert copy_to_clipboard("latest answer", workspace_root=tmp_path) == "wl-copy"
    assert resolved == ["wl-copy"]
    assert calls[0][0] == ["/usr/bin/wl-copy"]
    assert calls[0][1] == "latest answer"
    assert calls[0][2]["WAYLAND_DISPLAY"] == "wayland-1"
    assert "OPENAI_API_KEY" not in calls[0][2]


def test_copy_to_clipboard_falls_back_after_backend_failure(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    attempts: list[str] = []

    monkeypatch.setattr(
        clipboard_module,
        "resolve_host_executable",
        lambda command, **kwargs: f"/usr/bin/{command}",
    )

    def run(argv, **kwargs):
        del kwargs
        attempts.append(argv[0])
        if argv[0].endswith("wl-copy"):
            raise subprocess.TimeoutExpired(argv, 2)
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(clipboard_module.subprocess, "run", run)
    monkeypatch.setattr(clipboard_module.sys, "platform", "linux")

    assert copy_to_clipboard("answer", workspace_root=tmp_path) == "xclip"
    assert attempts == ["/usr/bin/wl-copy", "/usr/bin/xclip"]


def test_copy_to_clipboard_rejects_oversized_payload_before_launch(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    launched = False

    def resolve(*args, **kwargs):
        nonlocal launched
        launched = True
        return "/usr/bin/wl-copy"

    monkeypatch.setattr(clipboard_module, "resolve_host_executable", resolve)
    monkeypatch.setattr(clipboard_module, "MAX_CLIPBOARD_BYTES", 4)

    with pytest.raises(ValueError, match="use /export"):
        copy_to_clipboard("12345", workspace_root=tmp_path)
    assert launched is False


def test_copy_to_clipboard_reports_missing_backend(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(clipboard_module.sys, "platform", "linux")
    monkeypatch.setattr(
        clipboard_module,
        "resolve_host_executable",
        lambda command, **kwargs: None,
    )

    with pytest.raises(ClipboardUnavailable, match="wl-clipboard"):
        copy_to_clipboard("answer", workspace_root=tmp_path)


def test_copy_to_clipboard_uses_pbcopy_and_macos_guidance(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[list[str]] = []
    monkeypatch.setattr(clipboard_module.sys, "platform", "darwin")
    monkeypatch.setattr(
        clipboard_module,
        "resolve_host_executable",
        lambda command, **kwargs: "/usr/bin/pbcopy" if command == "pbcopy" else None,
    )

    def run(argv, **kwargs):
        del kwargs
        calls.append(list(argv))
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(clipboard_module.subprocess, "run", run)
    assert copy_to_clipboard("answer", workspace_root=tmp_path) == "pbcopy"
    assert calls == [["/usr/bin/pbcopy"]]

    monkeypatch.setattr(
        clipboard_module,
        "resolve_host_executable",
        lambda command, **kwargs: None,
    )
    with pytest.raises(ClipboardUnavailable, match="pbcopy is unavailable"):
        copy_to_clipboard("answer", workspace_root=tmp_path)


def test_read_from_clipboard_uses_wayland_backend(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[list[str]] = []
    monkeypatch.setattr(clipboard_module.sys, "platform", "linux")
    monkeypatch.setattr(
        clipboard_module,
        "resolve_host_executable",
        lambda command, **kwargs: "/usr/bin/wl-paste" if command == "wl-paste" else None,
    )

    def run(argv, **kwargs):
        del kwargs
        calls.append(list(argv))
        return SimpleNamespace(returncode=0, stdout="hello", stderr="")

    monkeypatch.setattr(clipboard_module.subprocess, "run", run)

    assert read_from_clipboard(workspace_root=tmp_path) == "hello"
    assert calls == [["/usr/bin/wl-paste"]]


def test_read_primary_selection_uses_wayland_primary_backend(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[list[str]] = []
    monkeypatch.setattr(clipboard_module.sys, "platform", "linux")
    monkeypatch.setattr(
        clipboard_module,
        "resolve_host_executable",
        lambda command, **kwargs: "/usr/bin/wl-paste" if command == "wl-paste" else None,
    )

    def run(argv, **kwargs):
        del kwargs
        calls.append(list(argv))
        return SimpleNamespace(returncode=0, stdout="primary", stderr="")

    monkeypatch.setattr(clipboard_module.subprocess, "run", run)

    assert (
        read_from_clipboard(workspace_root=tmp_path, selection="primary")
        == "primary"
    )
    assert calls == [["/usr/bin/wl-paste", "--primary"]]


def test_read_from_clipboard_rejects_oversized_payload(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(clipboard_module.sys, "platform", "linux")
    monkeypatch.setattr(
        clipboard_module,
        "resolve_host_executable",
        lambda command, **kwargs: "/usr/bin/wl-paste" if command == "wl-paste" else None,
    )
    monkeypatch.setattr(clipboard_module, "MAX_CLIPBOARD_BYTES", 4)
    monkeypatch.setattr(
        clipboard_module.subprocess,
        "run",
        lambda argv, **kwargs: SimpleNamespace(
            returncode=0,
            stdout="12345",
            stderr="",
        ),
    )

    with pytest.raises(ValueError, match="clipboard text exceeds"):
        read_from_clipboard(workspace_root=tmp_path)


def test_read_from_clipboard_reports_missing_backend(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(clipboard_module.sys, "platform", "linux")
    monkeypatch.setattr(
        clipboard_module,
        "resolve_host_executable",
        lambda command, **kwargs: None,
    )

    with pytest.raises(ClipboardUnavailable, match="wl-clipboard"):
        read_from_clipboard(workspace_root=tmp_path)
