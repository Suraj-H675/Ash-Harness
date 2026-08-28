from __future__ import annotations

import json
import io
from types import SimpleNamespace

from ash.installer import InstallError, install, main


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
        environ={"PATH": "/isolated/bin:/usr/bin"},
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
            return _completed(stdout="{broken metadata")
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
        environ={"PATH": "/usr/bin"},
    )

    assert ["/usr/bin/pipx", "ensurepath"] in calls
    assert outcome.shell_restart_required is True
