from __future__ import annotations

import json
import io
import os
from types import SimpleNamespace

import pytest

from ash.installer import (
    InstallError,
    _ensure_shell_path,
    _quarantine_pipx_metadata,
    install,
    main,
)


def _completed(returncode: int = 0, *, stdout: str = "", stderr: str = ""):
    return SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)


def test_existing_pipx_install_is_rebuilt_without_exposing_uv_edge_cases() -> None:
    calls: list[tuple[list[str], dict[str, object]]] = []
    metadata = json.dumps(
        {
            "venvs": {
                "ash-ai": {
                    "metadata": {
                        "main_package": {
                            "package_or_url": (
                                "ash-ai[browser,server] @ "
                                "git+https://github.com/Suraj-H675/Ash-Harness.git"
                            ),
                            "app_paths": [
                                {
                                    "__Path__": "/isolated/bin/ash",
                                    "__type__": "Path",
                                }
                            ],
                        }
                    }
                }
            }
        }
    )

    def runner(command, **kwargs):
        calls.append((list(command), kwargs))
        if command[1:] == ["list", "--json"]:
            return _completed(stdout=metadata)
        if command[1:3] == ["install", "--force"]:
            return _completed()
        if command == ["/isolated/bin/ash", "--version"]:
            return _completed(stdout="ash 0.1.0\n")
        raise AssertionError(f"unexpected command: {command}")

    outcome = install(
        runner=runner,
        which=lambda name: "/usr/bin/pipx" if name == "pipx" else None,
        environ={
            "PATH": "/isolated/bin:/usr/bin",
            "PIPX_BIN_DIR": "/isolated/bin",
        },
    )

    install_command, install_kwargs = calls[1]
    assert install_command == [
        "/usr/bin/pipx",
        "install",
        "--force",
        "ash-ai[browser,server] @ git+https://github.com/Suraj-H675/Ash-Harness.git",
    ]
    assert install_kwargs["env"]["UV_VENV_CLEAR"] == "1"
    assert outcome.manager == "pipx"
    assert outcome.executable == "/isolated/bin/ash"
    assert outcome.version == "ash 0.1.0"


def test_existing_pypi_style_pipx_spec_preserves_capability_extras() -> None:
    calls: list[list[str]] = []
    metadata = json.dumps(
        {
            "venvs": {
                "ash-ai": {
                    "metadata": {
                        "main_package": {
                            "package_or_url": "ash-ai[browser,server]",
                            "app_paths": [
                                {
                                    "__Path__": "/isolated/bin/ash",
                                    "__type__": "Path",
                                }
                            ],
                        }
                    }
                }
            }
        }
    )

    def runner(command, **kwargs):
        calls.append(list(command))
        if command[1:] == ["list", "--json"]:
            return _completed(stdout=metadata)
        if command[1:3] == ["install", "--force"]:
            return _completed()
        if command == ["/isolated/bin/ash", "--version"]:
            return _completed(stdout="ash 0.1.0\n")
        raise AssertionError(f"unexpected command: {command}")

    install(
        runner=runner,
        which=lambda name: "/usr/bin/pipx" if name == "pipx" else None,
        environ={
            "PATH": "/isolated/bin:/usr/bin",
            "PIPX_BIN_DIR": "/isolated/bin",
        },
    )

    assert [
        "/usr/bin/pipx",
        "install",
        "--force",
        "ash-ai[browser,server] @ git+https://github.com/Suraj-H675/Ash-Harness.git",
    ] in calls


def test_uv_is_a_supported_fallback_when_pipx_is_unavailable() -> None:
    calls: list[list[str]] = []

    def runner(command, **kwargs):
        calls.append(list(command))
        if command[1:3] == ["tool", "list"]:
            return _completed(stdout="")
        if command[1:3] == ["tool", "install"]:
            return _completed()
        if command[1:] == ["tool", "dir", "--bin"]:
            return _completed(stdout="/isolated/bin\n")
        if command == ["/isolated/bin/ash", "--version"]:
            return _completed(stdout="ash 0.1.0\n")
        raise AssertionError(f"unexpected command: {command}")

    outcome = install(
        runner=runner,
        which=lambda name: "/usr/bin/uv" if name == "uv" else None,
        environ={"PATH": "/isolated/bin:/usr/bin"},
    )

    assert calls[1] == [
        "/usr/bin/uv",
        "tool",
        "install",
        "--force",
        "--reinstall",
        "ash-ai @ git+https://github.com/Suraj-H675/Ash-Harness.git",
    ]
    assert outcome.manager == "uv"
    assert outcome.executable == "/isolated/bin/ash"


def test_public_installer_turns_manager_failures_into_one_clear_message() -> None:
    stdout = io.StringIO()
    stderr = io.StringIO()

    def fail_install(**kwargs):
        raise InstallError("Neither pipx nor uv is installed.")

    assert main([], installer=fail_install, stdout=stdout, stderr=stderr) == 1
    assert stdout.getvalue() == ""
    assert stderr.getvalue() == (
        "Ash installation could not continue: Neither pipx nor uv is installed.\n"
        "Install pipx or uv, then run this installer again.\n"
    )


def test_broken_pipx_metadata_recovers_executable_from_pipx_bin_directory() -> None:
    def runner(command, **kwargs):
        if command[1:] == ["list", "--json"]:
            return _completed(
                stdout=json.dumps(
                    {
                        "venvs": {
                            "ash-ai": {
                                "metadata": {
                                    "main_package": {
                                        "package_or_url": (
                                            "ash-ai @ git+https://example.invalid/ash"
                                        ),
                                        "app_paths": [],
                                    }
                                }
                            }
                        }
                    }
                )
            )
        if command[1:3] == ["install", "--force"]:
            return _completed()
        if command[1:] == ["environment", "--value", "PIPX_BIN_DIR"]:
            return _completed(stdout="/isolated/bin\n")
        if command == ["/isolated/bin/ash", "--version"]:
            return _completed(stdout="ash 0.1.0\n")
        raise AssertionError(f"unexpected command: {command}")

    outcome = install(
        runner=runner,
        which=lambda name: "/usr/bin/pipx" if name == "pipx" else None,
        environ={"PATH": "/isolated/bin:/usr/bin"},
    )

    assert outcome.executable == "/isolated/bin/ash"


def test_public_installer_rejects_unsupported_python_before_running_tools() -> None:
    stderr = io.StringIO()

    def unexpected_install(**kwargs):
        raise AssertionError("installer must not run")

    assert (
        main(
            [],
            installer=unexpected_install,
            stdout=io.StringIO(),
            stderr=stderr,
            python_version=(3, 10),
        )
        == 1
    )
    assert stderr.getvalue() == (
        "Ash requires Python 3.11 or newer; this interpreter is Python 3.10.\n"
    )


def test_existing_uv_install_keeps_manager_and_capability_extras() -> None:
    calls: list[list[str]] = []
    uv_listing = (
        "ash-ai v0.1.0 [required: git+https://example.invalid/ash] "
        "[extras: acp, browser] (/isolated/tools/ash-ai)\n"
        "- ash (/isolated/bin/ash)\n"
    )

    def runner(command, **kwargs):
        calls.append(list(command))
        if command == ["/usr/bin/pipx", "list", "--json"]:
            return _completed(stdout='{"venvs": {}}')
        if command[1:] == [
            "tool",
            "list",
            "--show-paths",
            "--show-version-specifiers",
            "--show-extras",
        ]:
            return _completed(stdout=uv_listing)
        if command[1:3] == ["tool", "install"]:
            return _completed()
        if command == ["/isolated/bin/ash", "--version"]:
            return _completed(stdout="ash 0.1.0\n")
        raise AssertionError(f"unexpected command: {command}")

    outcome = install(
        runner=runner,
        which=lambda name: {
            "pipx": "/usr/bin/pipx",
            "uv": "/usr/bin/uv",
        }.get(name),
        environ={"PATH": "/isolated/bin:/usr/bin"},
    )

    assert [
        "/usr/bin/uv",
        "tool",
        "install",
        "--force",
        "--reinstall",
        "ash-ai[acp,browser] @ git+https://github.com/Suraj-H675/Ash-Harness.git",
    ] in calls
    assert outcome.manager == "uv"


def test_public_installer_contains_process_start_errors() -> None:
    stderr = io.StringIO()

    def fail_install(**kwargs):
        raise OSError("permission denied")

    assert main([], installer=fail_install, stdout=io.StringIO(), stderr=stderr) == 1
    assert stderr.getvalue() == (
        "Ash installation could not continue: could not start the installer "
        "backend (permission denied).\n"
    )


def test_unusable_pipx_binary_falls_back_to_uv() -> None:
    def runner(command, **kwargs):
        if command == ["/broken/pipx", "list", "--json"]:
            raise OSError("bad interpreter")
        if command[1:3] == ["tool", "list"]:
            return _completed(stdout="")
        if command[1:3] == ["tool", "install"]:
            return _completed()
        if command[1:] == ["tool", "dir", "--bin"]:
            return _completed(stdout="/isolated/bin\n")
        if command == ["/isolated/bin/ash", "--version"]:
            return _completed(stdout="ash 0.1.0\n")
        raise AssertionError(f"unexpected command: {command}")

    outcome = install(
        runner=runner,
        which=lambda name: {
            "pipx": "/broken/pipx",
            "uv": "/usr/bin/uv",
        }.get(name),
        environ={"PATH": "/isolated/bin:/usr/bin"},
    )

    assert outcome.manager == "uv"


def test_installer_updates_shell_path_when_executable_directory_is_missing() -> None:
    metadata = json.dumps(
        {
            "venvs": {
                "ash-ai": {
                    "metadata": {
                        "main_package": {
                            "package_or_url": "ash-ai @ git+https://example.invalid/ash",
                            "app_paths": [{"__Path__": "/isolated/bin/ash"}],
                        }
                    }
                }
            }
        }
    )
    calls: list[list[str]] = []

    def runner(command, **kwargs):
        calls.append(list(command))
        if command[1:] == ["list", "--json"]:
            return _completed(stdout=metadata)
        if command[1:3] == ["install", "--force"]:
            return _completed()
        if command == ["/isolated/bin/ash", "--version"]:
            return _completed(stdout="ash 0.1.0\n")
        if command[1:] == ["ensurepath"]:
            return _completed()
        raise AssertionError(f"unexpected command: {command}")

    outcome = install(
        runner=runner,
        which=lambda name: "/usr/bin/pipx" if name == "pipx" else None,
        environ={"PATH": "/usr/bin", "PIPX_BIN_DIR": "/isolated/bin"},
    )

    assert ["/usr/bin/pipx", "ensurepath"] in calls
    assert outcome.shell_restart_required is True


def test_installer_uses_exposed_pipx_launcher_directory_for_path_check() -> None:
    metadata = json.dumps(
        {
            "venvs": {
                "ash-ai": {
                    "metadata": {
                        "main_package": {
                            "package_or_url": "ash-ai @ git+https://example.invalid/ash",
                            "app_paths": [
                                {
                                    "__Path__": "/tmp/ash-pipx-home/venvs/ash-ai/bin/ash",
                                }
                            ],
                        }
                    }
                }
            }
        }
    )
    calls: list[list[str]] = []

    def runner(command, **kwargs):
        calls.append(list(command))
        if command[1:] == ["list", "--json"]:
            return _completed(stdout=metadata)
        if command[1:3] == ["install", "--force"]:
            return _completed()
        if command == ["/tmp/ash-pipx-home/venvs/ash-ai/bin/ash", "--version"]:
            return _completed(stdout="ash 0.1.0\n")
        if command[1:] == ["ensurepath"]:
            return _completed()
        raise AssertionError(f"unexpected command: {command}")

    outcome = install(
        runner=runner,
        which=lambda name: "/usr/bin/pipx" if name == "pipx" else None,
        environ={
            "PATH": "/tmp/ash-user-bin:/usr/bin:/bin",
            "PIPX_BIN_DIR": "/tmp/ash-user-bin",
        },
    )

    assert outcome.shell_restart_required is False
    assert ["/usr/bin/pipx", "ensurepath"] not in calls


def test_installer_prefers_its_exposed_launcher_over_global_ash() -> None:
    metadata = json.dumps(
        {
            "venvs": {
                "ash-ai": {
                    "metadata": {
                        "main_package": {
                            "package_or_url": "ash-ai @ git+https://example.invalid/ash",
                        }
                    }
                }
            }
        }
    )
    calls: list[list[str]] = []

    def runner(command, **kwargs):
        calls.append(list(command))
        if command[1:] == ["list", "--json"]:
            return _completed(stdout=metadata)
        if command[1:3] == ["install", "--force"]:
            return _completed()
        if command == ["/tmp/ash-user-bin/ash", "--version"]:
            return _completed(stdout="ash 0.1.0\n")
        raise AssertionError(f"unexpected command: {command}")

    outcome = install(
        runner=runner,
        which=lambda name: {
            "pipx": "/usr/bin/pipx",
            "ash": "/tmp/older-ash/bin/ash",
        }.get(name),
        environ={
            "PATH": "/tmp/ash-user-bin:/usr/bin:/bin",
            "PIPX_BIN_DIR": "/tmp/ash-user-bin",
        },
    )

    assert outcome.executable == "/tmp/ash-user-bin/ash"
    assert ["/tmp/ash-user-bin/ash", "--version"] in calls
    assert ["/tmp/older-ash/bin/ash", "--version"] not in calls


def test_ensure_shell_path_normalizes_symlink_equivalent_directories(tmp_path) -> None:
    actual = tmp_path / "actual-bin"
    actual.mkdir()
    exposed = tmp_path / "exposed-bin"
    try:
        exposed.symlink_to(actual, target_is_directory=True)
    except OSError:
        pytest.skip("symlinks are unavailable")

    calls: list[list[str]] = []

    def runner(command, **kwargs):
        calls.append(list(command))
        return _completed()

    restart_required = _ensure_shell_path(
        "/usr/bin/pipx",
        manager="pipx",
        launcher_directory=str(exposed),
        runner=runner,
        environment={"PATH": str(actual)},
    )

    assert restart_required is False
    assert calls == []


def test_installer_repairs_corrupt_pipx_metadata(tmp_path) -> None:
    pipx_home = tmp_path / "pipx-home"
    metadata_directory = pipx_home / "venvs" / "ash-ai"
    metadata_directory.mkdir(parents=True)
    metadata_path = metadata_directory / "pipx_metadata.json"
    metadata_path.write_text("{", encoding="utf-8")
    launcher_directory = tmp_path / "bin"
    launcher_directory.mkdir()
    managed_executable = str(metadata_directory / "bin" / "ash")
    repaired_metadata = {
        "venvs": {
            "ash-ai": {
                "metadata": {
                    "main_package": {
                        "package_or_url": "ash-ai @ git+https://example.invalid/ash",
                        "app_paths": [{"__Path__": managed_executable}],
                    }
                }
            }
        }
    }
    calls: list[list[str]] = []
    installed = False

    def runner(command, **kwargs):
        nonlocal installed
        calls.append(list(command))
        if command == ["/usr/bin/pipx", "list", "--json"]:
            if not installed:
                return _completed(returncode=1, stderr="JSONDecodeError")
            return _completed(stdout=json.dumps(repaired_metadata))
        if command[1:3] == ["install", "--force"]:
            assert not metadata_path.exists()
            quarantined = list(
                metadata_directory.glob("pipx_metadata.json.corrupt-*")
            )
            assert len(quarantined) == 1
            metadata_path.write_text(json.dumps(repaired_metadata), encoding="utf-8")
            installed = True
            return _completed()
        if command == [managed_executable, "--version"]:
            return _completed(stdout="ash 0.1.0\n")
        raise AssertionError(f"unexpected command: {command}")

    outcome = install(
        runner=runner,
        which=lambda name: "/usr/bin/pipx" if name == "pipx" else None,
        environ={
            "PATH": f"{launcher_directory}{os.pathsep}/usr/bin",
            "PIPX_HOME": str(pipx_home),
            "PIPX_BIN_DIR": str(launcher_directory),
        },
    )

    assert outcome.executable == managed_executable
    assert outcome.version == "ash 0.1.0"
    assert metadata_path.exists()
    assert list(metadata_directory.glob("pipx_metadata.json.corrupt-*")) == []
    install_calls = [
        call for call in calls if call[:3] == ["/usr/bin/pipx", "install", "--force"]
    ]
    assert len(install_calls) == 1
    assert install_calls[0][3].startswith("ash-ai @ git+")


def test_corrupt_pipx_metadata_repair_does_not_fall_back_to_uv(tmp_path) -> None:
    pipx_home = tmp_path / "pipx-home"
    metadata_directory = pipx_home / "venvs" / "ash-ai"
    metadata_directory.mkdir(parents=True)
    metadata_path = metadata_directory / "pipx_metadata.json"
    metadata_path.write_text("{", encoding="utf-8")
    launcher_directory = tmp_path / "bin"
    launcher_directory.mkdir()
    managed_executable = str(metadata_directory / "bin" / "ash")
    repaired_metadata = {
        "venvs": {
            "ash-ai": {
                "metadata": {
                    "main_package": {
                        "package_or_url": "ash-ai @ git+https://example.invalid/ash",
                        "app_paths": [{"__Path__": managed_executable}],
                    }
                }
            }
        }
    }
    calls: list[list[str]] = []
    installed = False

    def runner(command, **kwargs):
        nonlocal installed
        calls.append(list(command))
        if command == ["/usr/bin/pipx", "list", "--json"]:
            if not installed:
                return _completed(returncode=1, stderr="JSONDecodeError")
            return _completed(stdout=json.dumps(repaired_metadata))
        if command[1:3] == ["install", "--force"]:
            assert not metadata_path.exists()
            metadata_path.write_text(json.dumps(repaired_metadata), encoding="utf-8")
            installed = True
            return _completed()
        if command[0] == "/usr/bin/uv" and command[1:] == [
            "tool",
            "list",
            "--show-paths",
            "--show-version-specifiers",
            "--show-extras",
        ]:
            return _completed(stdout="ash-ai v0.1.0 /tmp/old-ash\n")
        if command[0] == "/usr/bin/uv":
            raise AssertionError(f"UV must not bypass pipx repair: {command}")
        if command == [managed_executable, "--version"]:
            return _completed(stdout="ash 0.1.0\n")
        raise AssertionError(f"unexpected command: {command}")

    outcome = install(
        which=lambda name: {
            "pipx": "/usr/bin/pipx",
            "uv": "/usr/bin/uv",
        }.get(name),
        runner=runner,
        environ={
            "PATH": f"{launcher_directory}{os.pathsep}/usr/bin",
            "PIPX_HOME": str(pipx_home),
            "PIPX_BIN_DIR": str(launcher_directory),
        },
    )

    assert outcome.manager == "pipx"
    assert outcome.executable == managed_executable
    assert not any(call[0] == "/usr/bin/uv" for call in calls)
    assert list(metadata_directory.glob("pipx_metadata.json.corrupt-*")) == []


def test_installer_rejects_successful_pipx_install_without_resulting_ash(
    tmp_path,
) -> None:
    calls: list[list[str]] = []

    def runner(command, **kwargs):
        calls.append(list(command))
        if command == ["/usr/bin/pipx", "list", "--json"]:
            return _completed(stdout=json.dumps({"venvs": {}}))
        if command[1:3] == ["install", "--force"]:
            return _completed()
        if command == ["/tmp/older-ash/bin/ash", "--version"]:
            raise AssertionError("global Ash must not verify a missing pipx install")
        raise AssertionError(f"unexpected command: {command}")

    with pytest.raises(InstallError, match="Ash is absent"):
        install(
            runner=runner,
            which=lambda name: {
                "pipx": "/usr/bin/pipx",
                "ash": "/tmp/older-ash/bin/ash",
            }.get(name),
            environ={"PATH": "/usr/bin"},
        )

    assert ["/tmp/older-ash/bin/ash", "--version"] not in calls


def test_quarantine_pipx_metadata_does_not_overwrite_existing_destination(
    tmp_path,
    monkeypatch,
) -> None:
    metadata_path = tmp_path / "pipx_metadata.json"
    metadata_path.write_text("corrupt", encoding="utf-8")
    first = tmp_path / "pipx_metadata.json.corrupt-first"
    first.write_text("sentinel", encoding="utf-8")
    second = tmp_path / "pipx_metadata.json.corrupt-second"
    values = iter(["first", "second"])
    monkeypatch.setattr(
        "ash.installer.uuid.uuid4",
        lambda: SimpleNamespace(hex=next(values)),
    )

    result = _quarantine_pipx_metadata(metadata_path)

    assert result == second
    assert not metadata_path.exists()
    assert first.read_text(encoding="utf-8") == "sentinel"
    assert second.read_text(encoding="utf-8") == "corrupt"


def test_installer_does_not_quarantine_metadata_for_unrelated_pipx_failure(
    tmp_path,
) -> None:
    pipx_home = tmp_path / "pipx-home"
    metadata_directory = pipx_home / "venvs" / "ash-ai"
    metadata_directory.mkdir(parents=True)
    metadata_path = metadata_directory / "pipx_metadata.json"
    metadata_path.write_text(json.dumps({"valid": True}), encoding="utf-8")
    original_metadata = metadata_path.read_bytes()

    def runner(command, **kwargs):
        if command == ["/usr/bin/pipx", "list", "--json"]:
            return _completed(returncode=1, stderr="permission denied")
        if command[1:3] == ["install", "--force"]:
            raise AssertionError("pipx install must not run after unrelated failure")
        raise AssertionError(f"unexpected command: {command}")

    with pytest.raises(InstallError):
        install(
            runner=runner,
            which=lambda name: "/usr/bin/pipx" if name == "pipx" else None,
            environ={
                "PATH": "/usr/bin",
                "PIPX_HOME": str(pipx_home),
                "PIPX_BIN_DIR": str(tmp_path / "bin"),
            },
        )

    assert metadata_path.read_bytes() == original_metadata
    assert list(metadata_directory.glob("pipx_metadata.json.corrupt-*")) == []
