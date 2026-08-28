"""Dependency-free public installer for Ash.

The module is intentionally usable both from an installed package and as a
downloaded script.  It owns package-manager detection, repair flags, and final
executable verification so users do not need to understand pipx/uv internals.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence


REPOSITORY_URL = "https://github.com/Suraj-H675/Ash-Harness.git"
SUPPORTED_EXTRAS = ("a2a", "acp", "browser", "server", "vector")
_EXTRAS_PATTERN = re.compile(r"^\s*ash-ai(?:\[([^]]+)\])?\s*@", re.IGNORECASE)
_UV_EXTRAS_PATTERN = re.compile(r"\[extras:\s*([^]]+)\]", re.IGNORECASE)
_UV_ASH_PATH_PATTERN = re.compile(r"^-\s+ash\s+\(([^)]+)\)\s*$", re.MULTILINE)


class InstallError(RuntimeError):
    """A concise, user-actionable installation failure."""


@dataclass(frozen=True)
class InstallResult:
    manager: str
    executable: str
    version: str
    shell_restart_required: bool = False


@dataclass(frozen=True)
class _PipxState:
    installed: bool = False
    extras: tuple[str, ...] = ()
    executable: str | None = None


@dataclass(frozen=True)
class _UvState:
    installed: bool = False
    extras: tuple[str, ...] = ()
    executable: str | None = None


def install(
    *,
    extras: Sequence[str] = (),
    ref: str | None = None,
    runner: Callable[..., Any] = subprocess.run,
    which: Callable[[str], str | None] = shutil.which,
    environ: Mapping[str, str] | None = None,
) -> InstallResult:
    """Install or repair Ash, preserving existing pipx capability extras."""

    environment = dict(os.environ if environ is None else environ)
    pipx = which("pipx")
    uv = which("uv")
    if pipx is None:
        if uv is None:
            raise InstallError("Neither pipx nor uv is installed.")
        previous_uv = _read_uv_state(uv, runner=runner, environment=environment)
        return _install_with_uv(
            uv,
            extras=extras or previous_uv.extras,
            ref=ref,
            runner=runner,
            environment=environment,
            previous=previous_uv,
        )

    try:
        previous = _read_pipx_state(pipx, runner=runner, environment=environment)
    except OSError as exc:
        if uv is None:
            raise InstallError(f"pipx was found but could not start ({exc}).") from exc
        previous_uv = _read_uv_state(uv, runner=runner, environment=environment)
        return _install_with_uv(
            uv,
            extras=extras or previous_uv.extras,
            ref=ref,
            runner=runner,
            environment=environment,
            previous=previous_uv,
        )
    if not previous.installed and uv is not None:
        previous_uv = _read_uv_state(uv, runner=runner, environment=environment)
        if previous_uv.installed:
            return _install_with_uv(
                uv,
                extras=extras or previous_uv.extras,
                ref=ref,
                runner=runner,
                environment=environment,
                previous=previous_uv,
            )
    selected_extras = _normalize_extras(extras or previous.extras)
    package_spec = _package_spec(selected_extras, ref=ref)
    install_environment = dict(environment)
    install_environment["UV_VENV_CLEAR"] = "1"
    completed = runner(
        [pipx, "install", "--force", package_spec],
        env=install_environment,
        check=False,
    )
    if int(getattr(completed, "returncode", 1)) != 0:
        raise InstallError("pipx could not install Ash.")

    current = _read_pipx_state(pipx, runner=runner, environment=environment)
    executable = (
        current.executable
        or previous.executable
        or which("ash")
        or _pipx_executable(pipx, runner=runner, environment=environment)
    )
    if not executable:
        raise InstallError(
            "Ash was installed, but its executable could not be located."
        )
    version = _verify_executable(executable, runner=runner, environment=environment)
    restart_required = _ensure_shell_path(
        pipx,
        manager="pipx",
        executable=executable,
        runner=runner,
        environment=environment,
    )
    return InstallResult(
        manager="pipx",
        executable=executable,
        version=version,
        shell_restart_required=restart_required,
    )


def _pipx_executable(
    pipx: str,
    *,
    runner: Callable[..., Any],
    environment: Mapping[str, str],
) -> str | None:
    configured = environment.get("PIPX_BIN_DIR")
    if configured:
        directory = configured
    else:
        completed = runner(
            [pipx, "environment", "--value", "PIPX_BIN_DIR"],
            env=dict(environment),
            check=False,
            capture_output=True,
            text=True,
        )
        if int(getattr(completed, "returncode", 1)) != 0:
            return None
        directory = str(getattr(completed, "stdout", "")).strip()
    if not directory:
        return None
    executable_name = "ash.exe" if os.name == "nt" else "ash"
    return str(Path(directory) / executable_name)


def _install_with_uv(
    uv: str,
    *,
    extras: Sequence[str],
    ref: str | None,
    runner: Callable[..., Any],
    environment: Mapping[str, str],
    previous: _UvState,
) -> InstallResult:
    package_spec = _package_spec(_normalize_extras(extras), ref=ref)
    completed = runner(
        [uv, "tool", "install", "--force", "--reinstall", package_spec],
        env=dict(environment),
        check=False,
    )
    if int(getattr(completed, "returncode", 1)) != 0:
        raise InstallError("uv could not install Ash.")
    executable = previous.executable
    if not executable:
        directory = runner(
            [uv, "tool", "dir", "--bin"],
            env=dict(environment),
            check=False,
            capture_output=True,
            text=True,
        )
        if int(getattr(directory, "returncode", 1)) != 0:
            raise InstallError(
                "Ash was installed, but uv did not report its executable directory."
            )
        executable_name = "ash.exe" if os.name == "nt" else "ash"
        executable = str(
            Path(str(getattr(directory, "stdout", "")).strip()) / executable_name
        )
    version = _verify_executable(executable, runner=runner, environment=environment)
    restart_required = _ensure_shell_path(
        uv,
        manager="uv",
        executable=executable,
        runner=runner,
        environment=environment,
    )
    return InstallResult(
        manager="uv",
        executable=executable,
        version=version,
        shell_restart_required=restart_required,
    )


def _ensure_shell_path(
    manager_executable: str,
    *,
    manager: str,
    executable: str,
    runner: Callable[..., Any],
    environment: Mapping[str, str],
) -> bool:
    executable_directory = os.path.normcase(
        os.path.abspath(str(Path(executable).parent))
    )
    path_directories = {
        os.path.normcase(os.path.abspath(value))
        for value in environment.get("PATH", "").split(os.pathsep)
        if value
    }
    if executable_directory in path_directories:
        return False
    command = (
        [manager_executable, "ensurepath"]
        if manager == "pipx"
        else [manager_executable, "tool", "update-shell"]
    )
    runner(
        command,
        env=dict(environment),
        check=False,
        capture_output=True,
        text=True,
    )
    return True


def _verify_executable(
    executable: str,
    *,
    runner: Callable[..., Any],
    environment: Mapping[str, str],
) -> str:
    verified = runner(
        [executable, "--version"],
        env=dict(environment),
        check=False,
        capture_output=True,
        text=True,
    )
    if int(getattr(verified, "returncode", 1)) != 0:
        raise InstallError("Ash was installed, but `ash --version` failed.")
    version = str(getattr(verified, "stdout", "")).strip()
    if not version.startswith("ash "):
        raise InstallError("Ash verification returned an unexpected version response.")
    return version


def _read_pipx_state(
    pipx: str,
    *,
    runner: Callable[..., Any],
    environment: Mapping[str, str],
) -> _PipxState:
    completed = runner(
        [pipx, "list", "--json"],
        env=dict(environment),
        check=False,
        capture_output=True,
        text=True,
    )
    if int(getattr(completed, "returncode", 1)) != 0:
        return _PipxState()
    try:
        payload = json.loads(str(getattr(completed, "stdout", "")))
        main = payload["venvs"]["ash-ai"]["metadata"]["main_package"]
    except (json.JSONDecodeError, KeyError, TypeError):
        return _PipxState()
    package_spec = str(main.get("package_or_url", ""))
    match = _EXTRAS_PATTERN.match(package_spec)
    extras = _normalize_extras(
        match.group(1).split(",") if match and match.group(1) else ()
    )
    executable = None
    app_paths = main.get("app_paths", [])
    if isinstance(app_paths, list):
        for entry in app_paths:
            if isinstance(entry, dict) and entry.get("__Path__"):
                executable = str(entry["__Path__"])
                break
    return _PipxState(installed=True, extras=extras, executable=executable)


def _read_uv_state(
    uv: str,
    *,
    runner: Callable[..., Any],
    environment: Mapping[str, str],
) -> _UvState:
    completed = runner(
        [
            uv,
            "tool",
            "list",
            "--show-paths",
            "--show-version-specifiers",
            "--show-extras",
        ],
        env=dict(environment),
        check=False,
        capture_output=True,
        text=True,
    )
    if int(getattr(completed, "returncode", 1)) != 0:
        return _UvState()
    output = str(getattr(completed, "stdout", ""))
    if not re.search(r"^ash-ai\s", output, re.MULTILINE | re.IGNORECASE):
        return _UvState()
    extras_match = _UV_EXTRAS_PATTERN.search(output)
    extras = _normalize_extras(extras_match.group(1).split(",") if extras_match else ())
    path_match = _UV_ASH_PATH_PATTERN.search(output)
    executable = path_match.group(1).strip() if path_match else None
    return _UvState(installed=True, extras=extras, executable=executable)


def _normalize_extras(values: Sequence[str]) -> tuple[str, ...]:
    return tuple(
        sorted({value.strip().casefold() for value in values if value.strip()})
    )


def _package_spec(extras: Sequence[str], *, ref: str | None) -> str:
    suffix = f"[{','.join(extras)}]" if extras else ""
    revision = f"@{ref}" if ref else ""
    return f"ash-ai{suffix} @ git+{REPOSITORY_URL}{revision}"


def main(
    argv: Sequence[str] | None = None,
    *,
    installer: Callable[..., InstallResult] = install,
    stdout: Any = None,
    stderr: Any = None,
    python_version: Sequence[int] = sys.version_info[:2],
) -> int:
    """Run the standalone installer CLI."""

    output = sys.stdout if stdout is None else stdout
    errors = sys.stderr if stderr is None else stderr
    major, minor = int(python_version[0]), int(python_version[1])
    if (major, minor) < (3, 11):
        print(
            f"Ash requires Python 3.11 or newer; this interpreter is Python {major}.{minor}.",
            file=errors,
        )
        return 1
    parser = argparse.ArgumentParser(
        prog="install-ash",
        description="Install, upgrade, or repair Ash.",
    )
    parser.add_argument(
        "--extra",
        action="append",
        default=[],
        choices=SUPPORTED_EXTRAS,
        help="Install an optional capability pack; repeat for multiple packs.",
    )
    parser.add_argument("--ref", help="Install a specific Git branch, tag, or commit.")
    args = parser.parse_args(list(argv) if argv is not None else None)
    try:
        result = installer(extras=args.extra, ref=args.ref)
    except InstallError as exc:
        print(f"Ash installation could not continue: {exc}", file=errors)
        if "Neither pipx nor uv" in str(exc):
            print("Install pipx or uv, then run this installer again.", file=errors)
        return 1
    except OSError as exc:
        print(
            "Ash installation could not continue: could not start the installer "
            f"backend ({exc}).",
            file=errors,
        )
        return 1
    except KeyboardInterrupt:
        print(
            "Ash installation cancelled; no Ash configuration was changed.", file=errors
        )
        return 130
    print(f"Ash is ready ({result.version}) via {result.manager}.", file=output)
    print(f"Executable: {result.executable}", file=output)
    if result.shell_restart_required:
        print(
            "PATH setup was requested; restart your terminal before running `ash`.",
            file=output,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
